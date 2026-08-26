"""Cross-message relations in C: rung 3 of the layer ladder (26.95).

The walk lives in `situc.relation`, which decides what a relation *means* and
which ones are expressible at all; this file decides only how C spells one.
That split is what keeps four backends from disagreeing about whether a given
schema compiles.

WHAT THIS EMITS, AND WHY IT IS A SEPARATE FILE. A `<name>_relate.h` and
`<name>_relate.c` beside the ordinary output, for the reason `owned.py` gives
for the same choice and one sharper: 0032 requires a rung to add files and
change none, so folding the predicate into the ordinary header would break
additivity while still appearing to hold.

HOW IT COMPARES. Through the generated getters, never against the bytes. That
is what makes the comparison one of *values*: the getter already byte-swaps,
scales a fixed-point field and decodes a BCD one, so a big-endian `u16`
against a little-endian `u32` compares correctly with nobody thinking about
it.

WHY EVERY OPERAND IS WIDENED. Generated C is compiled with `-Wconversion
-Wsign-conversion -Werror`, so an `i8` field compared against a `u16` one is
a build failure rather than a warning. Casting both to one 64-bit type per
constraint makes every comparison same-typed by construction. Which 64-bit
type, and when there is no correct one, is `situc.relation`'s call.
"""

from __future__ import annotations

from situc import ast
from situc.codegen.c.names import ident
from situc.relation import (Constraint, Read, ReadBytes, SubView, plans,
                            refusals, render)
from situc.resolve import ResolvedSchema
from situc import __version__

__all__ = ["generate", "refusals", "signature"]


def signature(relation: ast.Relation, prefix: str) -> str:
	params = ", ".join(f"situ_view_t {param.name}" for param in relation.params)
	return f"situ_err_t {ident(prefix, 'rel', relation.name)}({params})"


def _local(name: str) -> str:
	"""A binding's local, prefixed so it cannot collide with a parameter.

	The plan names bindings `_0`, `_1` and so on; a schema identifier cannot
	begin with the project prefix followed by a digit, so `situ_v_0` is safe
	against any parameter name an author can write.
	"""
	return f"situ_v{name}"


def _source(name: str) -> str:
	"""A binding reads either a parameter, named by the author, or an earlier
	binding, named `_0` by the plan. Only the second gets the prefix."""
	return _local(name) if name.startswith("_") else name


def _binding(bind: SubView | Read | ReadBytes, cast: str,
		prefix: str) -> list[str]:
	source = _source(bind.source)
	target = _local(bind.target)

	if isinstance(bind, SubView):
		accessor = ident(prefix, bind.struct, bind.member, "view")
		return [
			f"\tsitu_view_t {target};\t/* {bind.path} */",
			f"\terr = {accessor}({source}, &{target});",
			"\tif (err != SITU_OK) {",
			"\t\treturn err;",
			"\t}",
		]

	if isinstance(bind, ReadBytes):
		# `_ptr` rather than `_get`: an array has no single value, and the
		# span is what the comparison is over.
		pointer = ident(prefix, bind.struct, bind.member, "ptr")
		return [f"\tconst uint8_t *{target} = {pointer}({source});"
		        f"\t/* {bind.path} */"]

	getter = ident(prefix, bind.struct, bind.member, "get")
	return [f"\t{cast} {target} = ({cast}){getter}({source});"
	        f"\t/* {bind.path} */"]


def _body(constraints: list[Constraint], prefix: str) -> list[str]:
	lines: list[str] = []

	for constraint in constraints:
		cast  = "int64_t" if constraint.signed else "uint64_t"
		block: list[str] = []

		for bind in constraint.bindings:
			block += _binding(bind, cast, prefix)

		if constraint.bytes_equal is not None:
			same  = constraint.bytes_equal
			test  = "!=" if not same.negated else "=="
			block += [
				f"\tif (memcmp({_local(same.left)}, {_local(same.right)}, "
				f"{same.length}u) {test} 0) {{",
				"\t\treturn SITU_ERR_CONSTRAINT;",
				"\t}",
			]
			lines += ["\t{", *[f"\t{line}" for line in block], "\t}", ""]
			continue

		assert constraint.expr is not None
		locals_for = {path: _local(name)
		              for path, name in constraint.locals_for.items()}
		block += [
			f"\tif (!{render(constraint.expr, locals_for)}) {{",
			"\t\treturn SITU_ERR_CONSTRAINT;",
			"\t}",
		]

		lines += ["\t{", *[f"\t{line}" for line in block], "\t}", ""]

	return lines


def _comment(relation: ast.Relation) -> list[str]:
	first, second = relation.params
	return [
		f"/** Whether `{second.name}` is a well-formed counterpart to "
		f"`{first.name}`.",
		" *",
		" * Pure: reads two views, holds nothing, allocates nothing, and does",
		" * not know which messages exist. The caller owns the pairing.",
		" *",
		f" * Parameter order is temporal -- `{first.name}` is the message seen",
		" * first, which is what lets a dissector say \"response to frame N\".",
		" *",
		" * SITU_OK              every constraint holds",
		" * SITU_ERR_CONSTRAINT  one did not",
		" * SITU_ERR_BOUNDS      a sub-view did not fit the view it was in",
		" */",
	]


def generate(schema: ast.Schema, resolved: ResolvedSchema, basename: str,
		prefix: str = "situ") -> dict[str, str]:
	"""The relation header and source, or nothing if none is expressible."""
	ready = plans(schema, resolved)
	if not ready:
		return {}

	guard  = ident(prefix, basename, "RELATE_H").upper()
	header = [
		f"/* Generated by situc {__version__} from {basename}.situ -- do not edit.",
		" *",
		" * Cross-message relations: a pure predicate per relation, over two",
		" * views the caller already holds. No state, no allocation, and no",
		" * knowledge of which messages exist.",
		" *",
		" * A separate file on purpose. This is rung 3 of the layer ladder and",
		" * a rung adds files rather than changing them, so the ordinary",
		" * header is byte-identical whether or not this one was asked for.",
		" */",
		"",
		f"#ifndef {guard}",
		f"#define {guard}",
		"",
		"#include \"situ.h\"",
		f"#include \"{basename}.h\"",
		"",
		"#ifdef __cplusplus",
		"extern \"C\" {",
		"#endif",
		"",
	]

	for relation, _ in ready:
		header += [*_comment(relation), f"{signature(relation, prefix)};", ""]

	header += [
		"#ifdef __cplusplus",
		"}",
		"#endif",
		"",
		f"#endif /* {guard} */",
	]

	source = [
		f"/* Generated by situc {__version__} from {basename}.situ -- do not edit. */",
		"",
		f"#include \"{basename}_relate.h\"",
		"",
	]

	# Only where something needs it. An unconditional include is a header a
	# freestanding target may not have, for a function it does not call.
	if any(one.bytes_equal is not None
	       for _, constraints in ready for one in constraints):
		source[3:3] = ["#include <string.h>", ""]

	for relation, constraints in ready:
		source += [
			f"{signature(relation, prefix)}",
			"{",
			"\tsitu_err_t err = SITU_OK;",
			"\t(void)err;",
			"",
			*_body(constraints, prefix),
			"\treturn SITU_OK;",
			"}",
			"",
		]

	return {
		f"{basename}_relate.h": "\n".join(header) + "\n",
		f"{basename}_relate.c": "\n".join(source).rstrip("\n") + "\n",
	}
