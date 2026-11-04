"""Append-only building in C++: the write half of rung 2 (26.858, 26.860).

`plain_build` says which structs and C's plan what each part is. The
sequence is C's: bounds-check what is left, store at the cursor, advance,
and take a view over exactly what was written and `validate()` it, because
constraints are the view's to state and two checks of one schema is how
they come to disagree.

**A member is taken as its integer, not its enum.** An `enum class` value
has to be one the type can name, and `default = pass` says a message may
carry one that nothing names; Python's builder takes an int for the same
reason.

**In `situ_build`, not `situ`**, so a schema's struct cannot collide with
its own builder: a struct called `build_x` beside one called `x` would.
"""

from __future__ import annotations

from situc import __version__, ast, traverse
from situc.codegen import plain_build
from situc.codegen.c.build import (BITS, LITERAL, RUN, SCALAR, SIZE, SPAN, ZERO,
                                   Part, Shape)
from situc.codegen.c.names import c_name
from situc.codegen.cpp.edit import _scalar
from situc.codegen.cpp.emit import _order_suffix
from situc.codegen.cpp.names import class_name
from situc.codegen.plain_build import plain
from situc.layout import BITS_PER_BYTE
from situc.resolve import ResolvedSchema, ResolvedStruct

__all__ = ["generate", "plain", "declined", "Shape"]

BOUNDS     = "\t\treturn ::situ::rt::err::bounds;"
CONSTRAINT = "\t\treturn ::situ::rt::err::constraint;"


def declined(resolved: ResolvedSchema,
		shape: Shape) -> list[tuple[str, str]]:
	"""What this builder does not build, and why, for stderr."""
	return plain_build.declined(resolved, shape, "C++")


def _room(size: str) -> list[str]:
	return [f"\tif (situ_cap - situ_at < {size}) {{", BOUNDS, "\t}"]


def _bit_store(part: Part, value: str) -> list[str]:
	"""Set a field inside a group `_write` zeroed; the cursor stays put."""
	held = part.placement
	assert held.scalar is not None
	order = traverse.bit_extractor(held.scalar, held)
	return [f"\tsitu_bits_set_{order}(situ_out + situ_at, {part.bit_at}u,"
	        f" {held.size_bits}u, static_cast<std::uint64_t>({value}));"]


def _store(part: Part, value: str) -> list[str]:
	"""One scalar's bounds check, store and advance -- C's three steps."""
	assert part.placement.scalar is not None
	if part.role == BITS:
		return _bit_store(part, value)
	bits  = part.placement.scalar.bits
	width = bits // BITS_PER_BYTE
	return [
		*_room(f"{width}u"),
		(f"\tsitu_out[situ_at] = static_cast<std::uint8_t>({value});"
		 if bits == BITS_PER_BYTE else
		 f"\tsitu_put_{_order_suffix(part.placement.endian)}{bits}("
		 f"situ_out + situ_at, static_cast<std::uint{bits}_t>({value}));"),
		f"\tsitu_at += {width}u;",
	]


def _size(part: Part, run: str) -> list[str]:
	"""A size field, from the length of the run it measures."""
	assert part.placement.scalar is not None
	width = part.placement.size_bits or 0
	limit = (1 << width) - 1
	if part.solved is None:
		return [
			*([f"\tif ({run}_len > {limit}u) {{", CONSTRAINT, "\t}"]
			  if width < 32 else []),
			*_store(part, f"{run}_len"),
		]
	scale, offset = part.solved.scale, part.solved.offset
	shift = f" + {-offset}" if offset < 0 else f" - {offset}" if offset else ""
	return [
		"\t{",
		f"\t\tstd::int64_t situ_solved = static_cast<std::int64_t>({run}_len)"
		f"{shift};",
		"",
		"\t\tif (situ_solved < 0"
		+ (f" || situ_solved % {scale} != 0" if scale != 1 else "") + ") {",
		"\t\t\treturn ::situ::rt::err::constraint;",
		"\t\t}",
		*([f"\t\tsitu_solved /= {scale};"] if scale != 1 else []),
		*([f"\t\tif (situ_solved > {limit}) {{",
		   "\t\t\treturn ::situ::rt::err::constraint;",
		   "\t\t}"] if limit < (1 << 63) - 1 else []),
		*(f"\t{line}" for line in _store(part, "situ_solved")),
		"\t}",
	]


def _bits(part: Part, parts: list[Part]) -> list[str]:
	"""One field of a byte several share.

	The group is zeroed when it opens, which is what writes `must_be_zero`,
	and the bytes are spent when it closes. A value wider than its field is
	refused rather than masked: `situ_bits_set_*` would write a valid
	message saying something else.
	"""
	held  = part.placement
	limit = (1 << (held.size_bits or 0)) - 1
	lines = [*_room(f"{part.bytes}u"),
	         f"\tstd::memset(situ_out + situ_at, 0, {part.bytes}u);"] \
		if part.opens else []
	if part.sizes is not None:
		run = next(one.local for one in parts
		           if one.placement is part.sizes)
		lines += _size(part, run)
	elif plain_build.takes_value(part):
		lines += [f"\tif (static_cast<std::uint64_t>({part.local}) > {limit}u) {{",
		          CONSTRAINT, "\t}", *_bit_store(part, part.local)]
	elif plain_build.stated_ones(part):
		lines += _bit_store(part, f"{limit}u")
	if part.closes:
		lines.append(f"\tsitu_at += {part.bytes}u;")
	return lines


def _write(struct: ResolvedStruct, part: Part, parts: list[Part]) -> list[str]:
	"""One part, as `c/build.py` writes it."""
	if part.role == BITS:
		return _bits(part, parts)
	if part.role == SCALAR:
		return _store(part, part.local)
	if part.role == SIZE:
		run = next(one.local for one in parts
		           if one.placement is part.sizes)
		return _size(part, run)
	if part.role == ZERO:
		ones = traverse.reserved_policy(part.placement.attrs) == "must_be_one"
		return [*_room(f"{part.bytes}u"),
		        f"\tstd::memset(situ_out + situ_at, {'0xFF' if ones else '0'},"
		        f" {part.bytes}u);",
		        f"\tsitu_at += {part.bytes}u;"]
	if part.role == LITERAL:
		spelt = ", ".join(f"0x{byte:02X}u" for byte in part.literal)
		return [*_room(f"{part.bytes}u"),
		        "\t{",
		        f"\t\tstatic constexpr std::uint8_t situ_fixed[{part.bytes}] ="
		        f" {{ {spelt} }};",
		        "",
		        f"\t\tstd::memcpy(situ_out + situ_at, situ_fixed, {part.bytes}u);",
		        "\t}",
		        f"\tsitu_at += {part.bytes}u;"]
	if part.role == RUN:
		return [f"\tif ({part.local} == nullptr) {{", BOUNDS, "\t}",
		        *_room(f"{part.bytes}u"),
		        f"\tstd::memcpy(situ_out + situ_at, {part.local}, {part.bytes}u);",
		        f"\tsitu_at += {part.bytes}u;"]
	assert part.role == SPAN, part.role
	return [*_room(f"{part.local}_len"),
	        f"\tif ({part.local}_len != 0u) {{",
	        f"\t\tif ({part.local} == nullptr) {{",
	        "\t\t\treturn ::situ::rt::err::bounds;",
	        "\t\t}",
	        f"\t\tstd::memcpy(situ_out + situ_at, {part.local}, {part.local}_len);",
	        "\t}",
	        f"\tsitu_at += {part.local}_len;"]


def _parameters(parts: list[Part]) -> list[str]:
	found: list[str] = []
	for part in parts:
		if plain_build.takes_value(part):
			found.append(f"{_scalar(part.placement)} {part.local}")
		elif part.role == RUN:
			found.append(f"const std::uint8_t *{part.local}")
		elif part.role == SPAN:
			found.extend([f"const std::uint8_t *{part.local}",
			              f"std::uint32_t {part.local}_len"])
	return found


def generate(schema: ast.Schema, resolved: ResolvedSchema,
		basename: str) -> dict[str, str]:
	"""`<basename>_build.hpp`, or nothing where no struct qualifies.

	A rung adds files rather than changing them (0032).
	"""
	shape = Shape("", "situ", resolved.structs,
	              {decl.name: decl for decl in schema.varints()})
	structs = plain(resolved, shape)
	if not structs:
		return {}

	guard = f"SITU_{c_name(basename).upper()}_BUILD_HPP"
	lines = [
		f"/* Generated by situc {__version__} from {basename}.situ"
		" -- do not edit.",
		" *",
		" * Append-only building: the write half of rung 2. Each member goes",
		" * down in schema order into backing you supply, nothing moves once",
		" * written, and no view into the buffer exists while it is being",
		" * filled -- which is why this needs rung 2's permission and nothing",
		" * above it.",
		" *",
		" * A size field is computed from the run it measures rather than",
		" * supplied, so a message cannot disagree with its own schema. A",
		" * layout this cannot write forward has no builder here, and",
		" * `situc build` names it. Nothing here allocates.",
		" */",
		"",
		f"#ifndef {guard}",
		f"#define {guard}",
		"",
		"#include <cstdint>",
		"#include <cstring>",
		"",
		f"#include \"{basename}.hpp\"",
		"",
		"namespace situ_build {",
	]

	for struct in structs:
		held  = class_name(struct)
		parts = plain_build.parts(struct, shape)
		assert parts is not None
		length = "" if struct.layout.is_fixed_size else " situ_at,"
		lines.extend([
			"",
			f"/** Build a {struct.name} into `situ_out` and validate the result.",
			" *",
			" * `situ_wrote` is set only on success. `bounds` where `situ_cap`",
			" * is short of what the members need, `constraint` where a run",
			" * is longer than its size field can say, and whatever `validate`",
			" * answers for a result the schema refuses.",
			" */",
			f"[[nodiscard]] inline ::situ::rt::err {held}(",
			"\t\tstd::uint8_t *situ_out,",
			"\t\tstd::uint32_t situ_cap,",
			*(f"\t\t{one}," for one in _parameters(parts)),
			"\t\tstd::uint32_t &situ_wrote) noexcept",
			"{",
			"\tstd::uint32_t situ_at = 0;",
			"",
			"\tif (situ_out == nullptr) {",
			BOUNDS,
			"\t}",
			"",
		])
		for part in parts:
			lines.extend([*_write(struct, part, parts), ""])
		lines.extend([
			"\t/* Over exactly what was written, not over the backing: a longer",
			"\t * buffer would hide a struct that came out short. */",
			"\t::situ::rt::message situ_msg(situ_out, situ_at);",
			f"\t::situ::{held} situ_view;",
			f"\t::situ::rt::err situ_e = ::situ::{held}::at(situ_msg, 0,{length}"
			" situ_view);",
			"",
			"\tif (situ_e == ::situ::rt::err::ok) {",
			"\t\tsitu_e = situ_view.validate();",
			"\t}",
			"\tif (situ_e == ::situ::rt::err::ok) {",
			"\t\tsitu_wrote = situ_at;",
			"\t}",
			"\treturn situ_e;",
			"}",
		])

	lines.extend(["", "}  /* namespace situ_build */", "",
	              f"#endif /* {guard} */"])
	return {f"{basename}_build.hpp": "\n".join(lines) + "\n"}
