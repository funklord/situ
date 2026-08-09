"""Owned decode against caller-supplied backing: rung 2 (26.99, 0031 C/D).

`--owned` (26.69) emits a fixed-size C struct and refuses a variable-length
member, for a reason worth quoting because this is what answers it: "a
pointer reintroduces exactly the lifetime the caller was escaping, and an
array of the worst case is a decision about memory nobody asked for". Both
halves are true, and both stop being true once the caller supplies the
backing.

So this is the two-pass shape 0031 describes for cases C and D. `backing`
says how many bytes the variable members need, from the bytes themselves;
`decode` copies them into storage the caller owns and points the struct at
that. The buffer may go; the backing outlives it, which is the whole trade.

**Nothing here allocates**, which is what keeps rung 2 honest for cases C and
D. Case E -- a codec that expands without a bound -- is refused before
codegen by the layer check, because no measure pass exists for it at all.

WHAT IT READS THROUGH. The generated `_len` and `_ptr` accessors, never
offsets recomputed here. A delimited member's span and a length-prefixed
run's extent are questions the ordinary header already answers, and a second
answer would eventually disagree with the first.
"""

from __future__ import annotations

from situc import ast
from situc.codegen.c.names import ident, macro
from situc.codegen.c.owned import owned_structs
from situc.layout import Placement
from situc.resolve import ResolvedSchema, ResolvedStruct
from situc.traverse import is_own_member, local_name

__all__ = ["editable", "generate", "refusals"]


def _runs(struct: ResolvedStruct) -> list[Placement]:
	"""Members whose length the data decides and whose bytes are copyable.

	A length-prefixed run or a delimited one. Both have a `_ptr` and a `_len`
	in the ordinary header, which is exactly what makes them copyable without
	this file learning where they start.
	"""
	found = []
	for entry in struct.entries:
		placement = entry.placement
		if not is_own_member(struct, placement):
			continue
		if placement.scalar is None or placement.scalar.bits != 8:
			continue
		if placement.sized_by is None and placement.delimiter is None:
			continue
		found.append(placement)
	return found


def editable(resolved: ResolvedSchema) -> list[ResolvedStruct]:
	"""Structs `--owned` refuses only because the data decides a length.

	Deliberately narrow. A variant, a region or a TLV run is refused here too
	-- those are shapes rather than lengths, and giving them a backing buffer
	would not make an honest owned form of them.
	"""
	ownable = {struct.name for struct in owned_structs(resolved)}
	found   = []
	for struct in resolved.structs.values():
		if struct.name in ownable or struct.layout.register is not None:
			continue
		runs = _runs(struct)
		if not runs:
			continue
		if any(entry.placement.kind in ("variant", "region", "tlv", "opaque",
		                                "coded", "sealed", "authenticated")
		       for entry in struct.entries
		       if is_own_member(struct, entry.placement)):
			continue
		found.append(struct)
	return found


def refusals(resolved: ResolvedSchema) -> list[tuple[str, str]]:
	"""Every struct that gets neither an owned nor an edit form, and why."""
	ownable  = {struct.name for struct in owned_structs(resolved)}
	editable_ = {struct.name for struct in editable(resolved)}
	found = []
	for name, struct in resolved.structs.items():
		if name in ownable or name in editable_ or struct.layout.register:
			continue
		blocked = [entry.placement for entry in struct.entries
		           if is_own_member(struct, entry.placement)
		           and entry.placement.kind in ("variant", "region", "tlv",
		                                        "opaque", "coded", "sealed",
		                                        "authenticated")]
		if blocked:
			found.append((name, f"`{blocked[0].name}` is a {blocked[0].kind}, "
			                    f"which is a shape the data decides rather "
			                    f"than a length; backing does not make an "
			                    f"honest owned form of it"))
	return found


def _fields(struct: ResolvedStruct, runs: list[Placement],
		prefix: str) -> list[str]:
	lines = []
	for entry in struct.entries:
		placement = entry.placement
		if not is_own_member(struct, placement):
			continue
		local = local_name(struct, placement)
		if placement in runs:
			lines += [
				f"\tconst uint8_t *{local};\t/* into your backing */",
				f"\tuint32_t {local}_len;",
			]
		elif placement.scalar is not None and placement.array_count is None:
			width = max(8, placement.scalar.bits)
			width = 8 if width <= 8 else 16 if width <= 16 else \
				32 if width <= 32 else 64
			sign  = "int" if placement.scalar.signed else "uint"
			lines.append(f"\t{sign}{width}_t {local};")
	return lines


def _one(struct: ResolvedStruct, prefix: str) -> list[str]:
	runs  = _runs(struct)
	name  = ident(prefix, struct.name, "edit")
	view  = ident(prefix, struct.name, "view")

	lines = [
		f"/* `{struct.name}`, decoded into storage you own.",
		" *",
		" * Call `backing` for how many bytes the variable members need, then",
		" * `decode` with that much storage. Both take the length you have,",
		" * because this struct's extent is decided by its own bytes. The",
		" * message may go afterwards;",
		" * the backing is yours and outlives it, which is the whole trade.",
		" */",
		"typedef struct {",
		*_fields(struct, runs, prefix),
		f"}} {name}_t;",
		"",
		"/* How much backing a whole one needs, from the bytes. */",
		f"static inline situ_err_t {name}_backing(const situ_msg_t *msg,",
		"                                        uint32_t len, uint32_t *need)",
		"{",
		"\tsitu_view_t view;",
		f"\tconst situ_err_t err = {view}(msg, 0u, len, &view);",
		"\tif (err != SITU_OK) {",
		"\t\treturn err;",
		"\t}",
		"",
		"\t*need = 0u;",
	]
	for placement in runs:
		local = local_name(struct, placement)
		lines.append(f"\t*need += {ident(prefix, struct.name, local, 'len')}"
		             f"(view);")
	lines += [
		"\treturn SITU_OK;",
		"}",
		"",
		"/* Copy into your backing. SITU_ERR_BOUNDS where it does not fit,",
		" * which `backing` above answers before you have to guess. */",
		f"static inline situ_err_t {name}_decode(const situ_msg_t *msg,",
		"                                       uint32_t len,",
		"                                       uint8_t *backing, uint32_t cap,",
		f"                                       {name}_t *out)",
		"{",
		"\tsitu_view_t view;",
		f"\tsitu_err_t err = {view}(msg, 0u, len, &view);",
		"\tif (err != SITU_OK) {",
		"\t\treturn err;",
		"\t}",
		"",
		"\tuint32_t need = 0u;",
		f"\terr = {name}_backing(msg, len, &need);",
		"\tif (err != SITU_OK) {",
		"\t\treturn err;",
		"\t}",
		"\tif (need > cap) {",
		"\t\treturn SITU_ERR_BOUNDS;",
		"\t}",
		"",
		"\tuint32_t at = 0u;",
	]

	for entry in struct.entries:
		placement = entry.placement
		if not is_own_member(struct, placement):
			continue
		local = local_name(struct, placement)
		if placement in runs:
			length = f"{ident(prefix, struct.name, local, 'len')}(view)"
			source = f"{ident(prefix, struct.name, local, 'ptr')}(view)"
			lines += [
				f"\tout->{local}_len = {length};",
				f"\tout->{local}     = backing + at;",
				f"\tfor (uint32_t i = 0u; i < out->{local}_len; i++) {{",
				f"\t\tbacking[at + i] = {source}[i];",
				"\t}",
				f"\tat += out->{local}_len;",
			]
		elif placement.scalar is not None and placement.array_count is None:
			lines.append(f"\tout->{local} = "
			             f"{ident(prefix, struct.name, local, 'get')}(view);")

	lines += ["\t(void)at;", "\treturn SITU_OK;", "}", ""]
	return lines


def generate(schema: ast.Schema, resolved: ResolvedSchema, basename: str,
		prefix: str = "situ") -> dict[str, str]:
	"""The edit header, or nothing where no struct needs backing."""
	structs = editable(resolved)
	if not structs:
		return {}

	guard = macro(prefix, basename, "EDIT_H")
	lines = [
		f"/* Generated by situc from {basename}.situ -- do not edit.",
		" *",
		" * Owned decode against backing you supply: rung 2 of the layer",
		" * ladder. `--owned` refuses a variable-length member because a",
		" * pointer would reintroduce the lifetime the caller was escaping",
		" * and a worst-case array is a decision about memory nobody asked",
		" * for. Both stop being true once the backing is yours.",
		" *",
		" * Nothing here allocates. A separate header -- a rung adds files",
		" * rather than changing them (decision 0032).",
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

	for struct in structs:
		lines.extend(_one(struct, prefix))

	lines += ["#ifdef __cplusplus", "}", "#endif", "",
	          f"#endif /* {guard} */"]

	return {f"{basename}_edit.h": "\n".join(lines) + "\n"}
