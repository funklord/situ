"""Append-only building in Rust: the write half of rung 2 (26.858, 26.860).

`plain_build` says which structs and C's plan what each part is. The
sequence is C's: bounds-check what is left, store at the cursor, advance,
and take a view over exactly what was written and `validate()` it, because
constraints are the view's to state and two checks of one schema is how
they come to disagree.

**A member is taken as its integer, not its enum.** A Rust enum cannot hold
a value nothing names, and `default = pass` says a message may carry one.

**A fixed run is `&[u8; N]`**, so the compiler holds the length C's header
can only ask for; a measured run is a slice, whose `len()` is its size.

**The backing is `&mut [u8]`**, so the compiler already says what C's
header asks for: no view of the buffer exists while it is being filled.
"""

from __future__ import annotations

from situc import __version__, ast, traverse
from situc.codegen import plain_build
from situc.codegen.c.build import (LITERAL, RUN, SCALAR, SIZE, SPAN, ZERO, Part,
                                   Shape)
from situc.codegen.plain_build import plain
from situc.codegen.rust.edit import _scalar
from situc.codegen.rust.emit import _ident, _pascal, _writer
from situc.layout import BITS_PER_BYTE
from situc.resolve import ResolvedSchema

__all__ = ["generate", "plain", "declined", "Shape"]


def declined(resolved: ResolvedSchema,
		shape: Shape) -> list[tuple[str, str]]:
	"""What this builder does not build, and why, for stderr."""
	return plain_build.declined(resolved, shape, "Rust")


def _room(size: str) -> list[str]:
	return [f"\tif situ_out.len() - situ_at < {size} {{",
	        "\t\treturn Err(Error::Bounds);", "\t}"]


def _store(part: Part, value: str) -> list[str]:
	"""One scalar's bounds check, store and advance -- C's three steps."""
	assert part.placement.scalar is not None
	width = part.placement.scalar.bits // BITS_PER_BYTE
	return [
		*_room(str(width)),
		f"\tsitu_rt::{_writer(part.placement.endian)}(situ_out, situ_at,"
		f" {width}, {value} as u64);",
		f"\tsitu_at += {width};",
	]


def _size(part: Part, run: str) -> list[str]:
	"""A size field, from the length of the run it measures."""
	assert part.placement.scalar is not None
	limit = (1 << part.placement.scalar.bits) - 1
	if part.solved is None:
		return [
			*([f"\tif {run}.len() as u64 > {limit} {{",
			   "\t\treturn Err(Error::Constraint);", "\t}"]
			  if part.placement.scalar.bits < 64 else []),
			*_store(part, f"{run}.len()"),
		]
	scale, offset = part.solved.scale, part.solved.offset
	shift = f" + {-offset}" if offset < 0 else f" - {offset}" if offset else ""
	return [
		"\t{",
		f"\t\tlet situ_solved = {run}.len() as i64{shift};",
		"\t\tif situ_solved < 0"
		+ (f" || situ_solved % {scale} != 0" if scale != 1 else "") + " {",
		"\t\t\treturn Err(Error::Constraint);",
		"\t\t}",
		*([f"\t\tlet situ_solved = situ_solved / {scale};"]
		  if scale != 1 else []),
		*([f"\t\tif situ_solved > {limit} {{",
		   "\t\t\treturn Err(Error::Constraint);",
		   "\t\t}"] if limit < (1 << 63) - 1 else []),
		*(f"\t{line}" for line in _store(part, "situ_solved")),
		"\t}",
	]


def _write(part: Part, parts: list[Part]) -> list[str]:
	"""One part, as `c/build.py` writes it."""
	if part.role == SCALAR:
		return _store(part, _ident(part.local))
	if part.role == SIZE:
		run = next(one.local for one in parts
		           if one.placement is part.sizes)
		return _size(part, _ident(run))
	if part.role == ZERO:
		ones = traverse.reserved_policy(part.placement.attrs) == "must_be_one"
		return [*_room(str(part.bytes)),
		        f"\tsitu_out[situ_at..situ_at + {part.bytes}]"
		        f".fill({'0xFF' if ones else '0'});",
		        f"\tsitu_at += {part.bytes};"]
	if part.role == LITERAL:
		spelt = ", ".join(f"0x{byte:02X}" for byte in part.literal)
		return [*_room(str(part.bytes)),
		        f"\tsitu_out[situ_at..situ_at + {part.bytes}]"
		        f".copy_from_slice(&[{spelt}]);",
		        f"\tsitu_at += {part.bytes};"]
	local = _ident(part.local)
	if part.role == RUN:
		return [*_room(str(part.bytes)),
		        f"\tsitu_out[situ_at..situ_at + {part.bytes}]"
		        f".copy_from_slice({local});",
		        f"\tsitu_at += {part.bytes};"]
	assert part.role == SPAN, part.role
	return [*_room(f"{local}.len()"),
	        f"\tsitu_out[situ_at..situ_at + {local}.len()]"
	        f".copy_from_slice({local});",
	        f"\tsitu_at += {local}.len();"]


def _parameters(parts: list[Part]) -> list[str]:
	found: list[str] = []
	for part in parts:
		name = _ident(part.local)
		if part.role == SCALAR:
			found.append(f"{name}: {_scalar(part.placement)}")
		elif part.role == RUN:
			found.append(f"{name}: &[u8; {part.bytes}]")
		elif part.role == SPAN:
			found.append(f"{name}: &[u8]")
	return found


def generate(schema: ast.Schema, resolved: ResolvedSchema,
		basename: str) -> dict[str, str]:
	"""`<basename>_build.rs`, or nothing where no struct qualifies.

	A rung adds files rather than changing them (0032).
	"""
	shape = Shape("", "situ", resolved.structs,
	              {decl.name: decl for decl in schema.varints()})
	structs = plain(resolved, shape)
	if not structs:
		return {}

	lines = [
		f"// Generated by situc {__version__} from {basename}.situ"
		" -- do not edit.",
		"//",
		"// Append-only building: the write half of rung 2. Each member goes",
		"// down in schema order into backing you supply, nothing moves once",
		"// written, and the `&mut` is the compiler saying no view of the",
		"// buffer exists while it is being filled.",
		"//",
		"// A size field is computed from the run it measures rather than",
		"// supplied, so a message cannot disagree with its own schema. A",
		"// layout this cannot write forward has no builder here, and",
		"// `situc build` names it. Nothing allocates; `situ_rt` is no_std.",
		"",
		f"use crate::{basename}::*;",
		"use crate::situ_rt::{self, Error, Result};",
	]

	for struct in structs:
		held  = _pascal(struct.name)
		parts = plain_build.parts(struct, shape)
		assert parts is not None
		lines.extend([
			"",
			f"/// Build a {struct.name} into `situ_out` and validate the result.",
			"///",
			"/// Returns its length. `Err(Bounds)` where the slice is short of",
			"/// what the members need, `Err(Constraint)` where a run is longer",
			"/// than its size field can say, and whatever `validate` answers",
			"/// for a result the schema refuses.",
			f"pub fn build_{_ident(struct.name).removeprefix('r#')}(",
			"\tsitu_out: &mut [u8],",
			*(f"\t{one}," for one in _parameters(parts)),
			") -> Result<usize> {",
			"\tlet mut situ_at = 0usize;",
			"",
		])
		for part in parts:
			lines.extend([*_write(part, parts), ""])
		lines.extend([
			"\t// Over exactly what was written, not over the backing: a longer",
			"\t// slice would hide a struct that came out short.",
			f"\t{held}::new(&situ_out[..situ_at])?.validate()?;",
			"\tOk(situ_at)",
			"}",
		])

	return {f"{basename}_build.rs": "\n".join(lines) + "\n"}
