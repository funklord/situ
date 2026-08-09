"""Matching a reply to its request in C: rung 5 of the layer ladder (26.97).

Rung 4 holds bytes between calls; this holds *messages*. What it holds is a
pending table: the key of every request recorded, and the caller's handle for
it, so that a reply can be matched back.

**The schema states the pairing and the generated code holds the set.** That
is the distinction 0030's amendment turned on -- a schema describes and does
not hold -- and it is what makes the acknowledgement case expressible here
after 0030 wrote it off. "An ack names a sequence number that was actually
sent" needs the set of what was sent; the set lives in this table, sized by
the caller, and the schema never grew a construct that quantifies over it.

THE KEY IS PACKED, AND REFUSED WHERE IT DOES NOT FIT. The conversation key is
whatever fields the relation's equality constraints name, read as *values*
through the generated getters and packed into one `uint64_t`. Where the
widths sum past 64 bits there is no packing that does not lose something, and
two different exchanges that collided would be matched to each other -- a
wrong answer with no symptom. So it is refused, and the refusal says the sum.

Nothing here allocates. The slots are the caller's array and the capacity is
the caller's number; a full table answers SITU_ERR_BOUNDS rather than
evicting something the caller still wanted.
"""

from __future__ import annotations

from situc import ast
from situc.codegen.c.names import ident, macro
from situc.relation import KEY_BITS, Read, Refused, Side, key_sides
from situc.resolve import ResolvedSchema

__all__ = ["generate", "refusals"]

def _key(sides: Side, view: str, prefix: str) -> list[str]:
	"""Pack one side's fields into a `uint64_t`, widest shift last."""
	lines = ["\tuint64_t key = 0u;"]
	for chain, width in sides:
		source = view
		for step in chain:
			target = f"situ_k{step.target}"
			if isinstance(step, Read):
				getter = ident(prefix, step.struct, step.member, "get")
				lines.append(f"\tconst uint64_t {target} = (uint64_t)"
				             f"{getter}({source});\t/* {step.path} */")
				lines.append(f"\tkey = (key << {width}u) | {target};")
			else:
				accessor = ident(prefix, step.struct, step.member, "view")
				lines += [
					f"\tsitu_view_t {target};",
					f"\tif ({accessor}({source}, &{target}) != SITU_OK) {{",
					"\t\treturn SITU_ERR_BOUNDS;",
					"\t}",
				]
				source = target
	return lines


def refusals(schema: ast.Schema,
		resolved: ResolvedSchema) -> list[tuple[str, str]]:
	"""Every relation that gets no table, and why."""
	found = []
	for relation in schema.relations():
		try:
			key_sides(relation, resolved)
		except Refused as why:
			found.append((relation.name, str(why)))
	return found


def _table(relation: ast.Relation, resolved: ResolvedSchema,
		prefix: str) -> list[str]:
	request, response = key_sides(relation, resolved)
	name  = ident(prefix, "conv", relation.name)
	first, second = relation.params

	return [
		f"/* Pending requests for `{relation.name}`.",
		" *",
		" * The slots are yours and so is their number. A full table answers",
		" * SITU_ERR_BOUNDS rather than evicting an exchange you still wanted.",
		" */",
		"typedef struct {",
		"\tuint64_t key;",
		"\tuint32_t id;\t/* your handle for the request */",
		"\tuint8_t  live;",
		f"}} {name}_slot_t;",
		"",
		"typedef struct {",
		f"\t{name}_slot_t *slots;",
		"\tuint32_t cap;",
		f"}} {name}_t;",
		"",
		f"static inline void {name}_init({name}_t *table,",
		f"                               {name}_slot_t *slots, uint32_t cap)",
		"{",
		"\ttable->slots = slots;",
		"\ttable->cap   = cap;",
		"\tfor (uint32_t i = 0u; i < cap; i++) {",
		"\t\tslots[i].live = 0u;",
		"\t}",
		"}",
		"",
		f"/* Remember `{first.name}` under its key, against your handle for it. */",
		f"static inline situ_err_t {name}_record({name}_t *table,",
		f"                                       situ_view_t {first.name},",
		"                                       uint32_t id)",
		"{",
		*_key(request, first.name, prefix),
		"",
		"\tfor (uint32_t i = 0u; i < table->cap; i++) {",
		"\t\tif (table->slots[i].live == 0u) {",
		"\t\t\ttable->slots[i].key  = key;",
		"\t\t\ttable->slots[i].id   = id;",
		"\t\t\ttable->slots[i].live = 1u;",
		"\t\t\treturn SITU_OK;",
		"\t\t}",
		"\t}",
		"\treturn SITU_ERR_BOUNDS;",
		"}",
		"",
		f"/* Match `{second.name}` to a request, and forget it.",
		" *",
		" * Forgetting is what makes a second reply to one request answer",
		" * SITU_ERR_CONSTRAINT: a late or duplicated response names an",
		" * exchange that is over, which is not the same as one that never",
		" * happened and is reported the same way because a caller can do",
		" * nothing different about either. */",
		f"static inline situ_err_t {name}_match({name}_t *table,",
		f"                                      situ_view_t {second.name},",
		"                                      uint32_t *id)",
		"{",
		*_key(response, second.name, prefix),
		"",
		"\tfor (uint32_t i = 0u; i < table->cap; i++) {",
		"\t\tif (table->slots[i].live != 0u && table->slots[i].key == key) {",
		"\t\t\t*id = table->slots[i].id;",
		"\t\t\ttable->slots[i].live = 0u;",
		"\t\t\treturn SITU_OK;",
		"\t\t}",
		"\t}",
		"\treturn SITU_ERR_CONSTRAINT;",
		"}",
		"",
	]


def generate(schema: ast.Schema, resolved: ResolvedSchema, basename: str,
		prefix: str = "situ") -> dict[str, str]:
	"""The conversation header, or nothing where no relation carries a key."""
	ready = []
	for relation in schema.relations():
		try:
			ready.append(_table(relation, resolved, prefix))
		except Refused:
			continue

	if not ready:
		return {}

	guard = macro(prefix, basename, "CONVERSE_H")
	lines = [
		f"/* Generated by situc from {basename}.situ -- do not edit.",
		" *",
		" * Conversations: a pending table per relation, matching a reply to",
		" * the request it answers. Rung 5 of the layer ladder, whose new",
		" * permission is holding messages between calls.",
		" *",
		" * The schema states the pairing and this holds the set. Nothing",
		" * allocates: the slots are the caller's.",
		" *",
		" * A separate header on purpose -- a rung adds files rather than",
		" * changing them (decision 0032).",
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

	for block in ready:
		lines.extend(block)

	lines += [
		"#ifdef __cplusplus",
		"}",
		"#endif",
		"",
		f"#endif /* {guard} */",
	]

	return {f"{basename}_converse.h": "\n".join(lines) + "\n"}
