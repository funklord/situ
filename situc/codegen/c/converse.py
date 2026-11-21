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
from situc.codegen import argued_refusal
from situc.codegen.c.names import c_name, header_guard, ident, macro
from situc.relation import (KeyLayout, KeyStep, Read, ReadBytes, Refused,
                            Side, after_region, key_layout)
from situc.resolve import ResolvedSchema
from situc import __version__

__all__ = ["generate", "refusals"]

def _key(sides: Side, view: str, prefix: str, gated: bool = False
		) -> list[str]:
	"""Pack one side's fields into a `uint64_t`, widest shift last.

	`gated`: `view` is a sealed region's gate (26.1122). A member behind it
	is read through the gate's own accessor, and a clear one through the
	view the gate was opened over -- one value handed in, both read."""
	lines = ["\tuint64_t key = 0u;"]
	for chain, width in sides:
		source = f"{view}.view" if gated else view
		for step in chain:
			target = f"situ_k{step.target}"
			if isinstance(step, Read) and step.region is not None:
				getter = ident(prefix, step.struct, step.region, step.member,
				               "get")
				lines.append(f"\tconst uint64_t {target} = (uint64_t)"
				             f"{getter}({view});\t/* {step.path} */")
				lines.append(f"\tkey = (key << {width}u) | {target};")
			elif isinstance(step, Read):
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


def _key_bytes(sides: Side, view: str, prefix: str,
		total: int, gated: bool = False) -> list[str]:
	"""The exact-bytes key (0042): parts in declaration order, a scalar part
	as big-endian ceil(width/8) bytes, a bytes part verbatim. The layout is
	the language's; this is only C's spelling of it. `gated` as `_key`."""
	lines = [f"\tuint8_t key[{total}u];", "\tuint32_t at = 0u;"]
	for chain, width in sides:
		source = f"{view}.view" if gated else view
		for step in chain:
			target = f"situ_k{step.target}"
			behind = getattr(step, "region", None)
			if behind is not None:
				# Through the gate's accessor, which takes the gate.
				source = view
			if isinstance(step, ReadBytes):
				pointer = ident(prefix, step.struct, *([behind] if behind
				                                       else []),
				                step.member, "ptr")
				lines += [
					f"\tmemcpy(&key[at], {pointer}({source}), "
					f"{width // 8}u);\t/* {step.path} */",
					f"\tat += {width // 8}u;",
				]
			elif isinstance(step, Read):
				count = (width + 7) // 8
				getter = ident(prefix, step.struct, *([behind] if behind
				                                      else []),
				               step.member, "get")
				lines += [
					f"\tconst uint64_t {target} = (uint64_t)"
					f"{getter}({source});\t/* {step.path} */",
					f"\tfor (uint32_t b = 0u; b < {count}u; b++) {{",
					f"\t\tkey[at + b] = (uint8_t)({target} >> "
					f"(8u * ({count}u - 1u - b)));",
					"\t}",
					f"\tat += {count}u;",
				]
			else:
				accessor = ident(prefix, step.struct, step.member, "view")
				lines += [
					f"\tsitu_view_t {target};",
					f"\tif ({accessor}({source}, &{target}) != SITU_OK) {{",
					"\t\treturn SITU_ERR_BOUNDS;",
					"\t}",
				]
				source = target
	lines.append("\t(void)at;")
	return lines


def refusals(schema: ast.Schema,
		resolved: ResolvedSchema) -> list[tuple[str, str]]:
	"""Every relation that gets no table, and why."""
	found = []
	for relation in schema.relations():
		try:
			key_layout(relation, resolved)
		except Refused as why:
			found.append((relation.name, str(why)))
	return found


def _key_parameter(chain: list[KeyStep]) -> str:
	"""A request key part's parameter name: its path below the request,
	flattened -- `head.msg` is `head_msg` -- so two parts named alike in
	different structs stay two parameters."""
	return c_name(chain[-1].path.split(".", 1)[1])


def _key_builder(name: str, request: Side, layout: KeyLayout) -> list[str]:
	"""`<table>_key(...)`: the key a request would have, from its parts'
	values, packed exactly as the table packs a request it reads. A value
	wider than its part is cut to the part, as the field would hold it."""
	if layout.packed:
		params = [f"uint64_t {_key_parameter(chain)}"
		          for chain, _ in request]
		lines = ["\tuint64_t key = 0u;"]
		for chain, width in request:
			value = _key_parameter(chain)
			cut = (value if width >= 64
			       else f"({value} & UINT64_C({hex((1 << width) - 1)}))")
			lines.append(f"\tkey = (key << {width}u) | {cut};")
		lines.append("\treturn key;")
		head = f"static inline uint64_t {name}_key("
	else:
		params = []
		lines = ["\tuint32_t at = 0u;"]
		for chain, width in request:
			value = _key_parameter(chain)
			if isinstance(chain[-1], ReadBytes):
				params.append(f"const uint8_t *{value}")
				lines += [f"\tmemcpy(&key[at], {value}, {width // 8}u);",
				          f"\tat += {width // 8}u;"]
			else:
				count = (width + 7) // 8
				params.append(f"uint64_t {value}")
				lines += [
					f"\tfor (uint32_t b = 0u; b < {count}u; b++) {{",
					f"\t\tkey[at + b] = (uint8_t)({value} >> "
					f"(8u * ({count}u - 1u - b)));",
					"\t}",
					f"\tat += {count}u;",
				]
		lines.append("\t(void)at;")
		params.append(f"uint8_t key[{layout.total_bytes}]")
		head = f"static inline void {name}_key("
	pad = " " * len(head)
	signature = [head + params[0] + ("," if len(params) > 1 else ")")]
	signature += [pad + one + ("," if at < len(params) - 2 else ")")
	              for at, one in enumerate(params[1:])]
	return [
		f"/* The key a request with these values would have, packed as the",
		" * table packs one it reads. */",
		*signature,
		"{",
		*lines,
		"}",
		"",
	]


def _table(relation: ast.Relation, resolved: ResolvedSchema,
		prefix: str) -> list[str]:
	layout = key_layout(relation, resolved)
	request, response = layout.request, layout.response
	name  = ident(prefix, "conv", relation.name)
	first, second = relation.params
	region = after_region(relation)
	# What `match` and `peek` take: a plain view, or with `[after]` the
	# region's gate, which only a verified open produces -- so a reply
	# nobody verified cannot end an exchange (26.1122).
	reply = (f"{ident(prefix, second.type_name, region, 't')} {second.name}"
	         if region else f"situ_view_t {second.name}")
	asked = (_key(request, first.name, prefix) if layout.packed
	         else _key_bytes(request, first.name, prefix, layout.total_bytes))
	heard = (_key(response, second.name, prefix, bool(region))
	         if layout.packed
	         else _key_bytes(response, second.name, prefix,
	                         layout.total_bytes, bool(region)))
	same = ("table->slots[i].key == key" if layout.packed else
	        f"memcmp(table->slots[i].key, key, {layout.total_bytes}u) == 0")

	return [
		f"/* Pending requests for `{relation.name}`.",
		" *",
		" * The slots are yours and so is their number. A full table answers",
		" * SITU_ERR_BOUNDS rather than evicting an exchange you still wanted.",
		" *",
		" * `peer` is yours too: a number for whatever the reply must come",
		" * back through to count -- a session, the context that verifies it",
		" * -- where the key alone would let two of them meet (26.1122).",
		" * Reuse one only once nothing can verify under the old. The calls",
		" * without `_from` hold peer 0. */",
		"typedef struct {",
		("\tuint64_t key;" if layout.packed
		 else f"\tuint8_t  key[{layout.total_bytes}u];"),
		"\tuint32_t id;\t/* your handle for the request */",
		"\tuint32_t peer;",
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
		*_key_builder(name, request, layout),
		f"/* Remember a request by its key alone, under `peer`, against your",
		" * handle for it: one this side awaits without holding its bytes --",
		" * sealed and sent by somebody else, or forwarded -- built with",
		f" * `{name}_key` (fuzznet, inbox 20261121T0600Z). */",
		f"static inline situ_err_t {name}_record_key_from({name}_t *table,",
		("                                                uint64_t key,"
		 if layout.packed else
		 f"                                                const uint8_t "
		 f"key[{layout.total_bytes}],"),
		"                                                uint32_t peer,",
		"                                                uint32_t id)",
		"{",
		"\tfor (uint32_t i = 0u; i < table->cap; i++) {",
		"\t\tif (table->slots[i].live == 0u) {",
		("\t\t\ttable->slots[i].key  = key;" if layout.packed else
		 f"\t\t\tmemcpy(table->slots[i].key, key, {layout.total_bytes}u);"),
		"\t\t\ttable->slots[i].id   = id;",
		"\t\t\ttable->slots[i].peer = peer;",
		"\t\t\ttable->slots[i].live = 1u;",
		"\t\t\treturn SITU_OK;",
		"\t\t}",
		"\t}",
		"\treturn SITU_ERR_BOUNDS;",
		"}",
		"",
		f"/* Remember `{first.name}` under its key and `peer`, against your",
		" * handle for it. */",
		f"static inline situ_err_t {name}_record_from({name}_t *table,",
		f"                                            situ_view_t {first.name},",
		"                                            uint32_t peer,",
		"                                            uint32_t id)",
		"{",
		*asked,
		"",
		f"\treturn {name}_record_key_from(table, key, peer, id);",
		"}",
		"",
		f"/* Remember `{first.name}` under its key, against your handle for it. */",
		f"static inline situ_err_t {name}_record({name}_t *table,",
		f"                                       situ_view_t {first.name},",
		"                                       uint32_t id)",
		"{",
		f"\treturn {name}_record_from(table, {first.name}, 0u, id);",
		"}",
		"",
		f"/* Match `{second.name}`, from `peer`, to a request, and forget it --",
		f" * or, `_match_key_from`, a key from `{name}_key`: for a caller that",
		" * verifies and reassembles a reply before matching it, and holds its",
		" * key's values by then rather than the frame (fuzznet, inbox",
		" * 20261121T1530Z).",
		*([f" * Handed the gate of `{region}`: the reply's tag has verified",
		   " * before anything it says can end an exchange (26.1122)."]
		  if region else []),
		" *",
		" * Forgetting is what makes a second reply to one request answer",
		" * SITU_ERR_CONSTRAINT: a late or duplicated response names an",
		" * exchange that is over, which is not the same as one that never",
		" * happened and is reported the same way because a caller can do",
		" * nothing different about either. */",
		f"static inline situ_err_t {name}_match_key_from({name}_t *table,",
		("                                               uint64_t key,"
		 if layout.packed else
		 f"                                               const uint8_t "
		 f"key[{layout.total_bytes}],"),
		"                                               uint32_t peer,",
		"                                               uint32_t *id)",
		"{",
		"\tfor (uint32_t i = 0u; i < table->cap; i++) {",
		"\t\tif (table->slots[i].live != 0u && table->slots[i].peer == peer",
		f"\t\t    && {same}) {{",
		"\t\t\t*id = table->slots[i].id;",
		"\t\t\ttable->slots[i].live = 0u;",
		"\t\t\treturn SITU_OK;",
		"\t\t}",
		"\t}",
		"\treturn SITU_ERR_CONSTRAINT;",
		"}",
		"",
		f"static inline situ_err_t {name}_match_from({name}_t *table,",
		f"                                           {reply},",
		"                                           uint32_t peer,",
		"                                           uint32_t *id)",
		"{",
		*heard,
		"",
		f"\treturn {name}_match_key_from(table, key, peer, id);",
		"}",
		"",
		f"static inline situ_err_t {name}_match({name}_t *table,",
		f"                                      {reply},",
		"                                      uint32_t *id)",
		"{",
		f"\treturn {name}_match_from(table, {second.name}, 0u, id);",
		"}",
		"",
		f"/* Forget the request held under `id`, if one is. An exchange that",
		" * ended without its reply -- expired, or refused a driver slot --",
		" * leaves no key a late reply could match and no slot taken: the",
		" * driver dropped its own and kept this one, so after `cap`",
		" * expiries every send was SITU_ERR_BOUNDS for good (26.1054).",
		" * Every key held for `id`: a relayed exchange is awaited under",
		" * two (26.1126). */",
		f"static inline void {name}_forget({name}_t *table, uint32_t id)",
		"{",
		"\tfor (uint32_t i = 0u; i < table->cap; i++) {",
		"\t\tif (table->slots[i].live != 0u && table->slots[i].id == id) {",
		"\t\t\ttable->slots[i].live = 0u;",
		"\t\t}",
		"\t}",
		"}",
		"",
		f"/* Whether `{second.name}` answers a request still pending, and which,",
		" * without taking it: a reply in many pieces is asked once a piece,",
		" * and only the last should end the exchange (26.1009). */",
		f"static inline situ_err_t {name}_peek_from(const {name}_t *table,",
		f"                                          {reply},",
		"                                          uint32_t peer,",
		"                                          uint32_t *id)",
		"{",
		*heard,
		"",
		"\tfor (uint32_t i = 0u; i < table->cap; i++) {",
		"\t\tif (table->slots[i].live != 0u && table->slots[i].peer == peer",
		f"\t\t    && {same}) {{",
		"\t\t\t*id = table->slots[i].id;",
		"\t\t\treturn SITU_OK;",
		"\t\t}",
		"\t}",
		"\treturn SITU_ERR_CONSTRAINT;",
		"}",
		"",
		f"static inline situ_err_t {name}_peek(const {name}_t *table,",
		f"                                     {reply},",
		"                                     uint32_t *id)",
		"{",
		f"\treturn {name}_peek_from(table, {second.name}, 0u, id);",
		"}",
		"",
	]


def generate(schema: ast.Schema, resolved: ResolvedSchema, basename: str,
		prefix: str = "situ") -> dict[str, str]:
	"""The conversation header, or nothing where no relation carries a key."""
	ready = []
	declined = []
	needs_string_h = False
	for relation in schema.relations():
		# A side that takes a `parameter` (0050). This table reads the
		# conversation key off two bare `situ_view_t`s through the generated
		# getters, and a getter behind an argument takes it as a trailing
		# parameter -- which none of these functions has a signature to take
		# or a caller to be given it by. Rung 3 declines for the same reason
		# and in the same words; the other three backends are handed views
		# that carry the argument, so their tables are emitted.
		#
		# NOT added to `refusals()`, and that is deliberate rather than an
		# oversight: `situc build` prints THIS module's list whatever the
		# target, so an entry here would tell a C++ caller there is no
		# conversation table for a relation whose table was just emitted.
		# The note below carries the reason instead, which is the copy the
		# C caller actually reads.
		if argued_refusal(relation, resolved):
			declined.append(relation)
			continue
		try:
			layout = key_layout(relation, resolved)
			ready.append(_table(relation, resolved, prefix))
			needs_string_h = needs_string_h or not layout.packed
		except Refused:
			continue

	# Emitted for the declined ones too: with every relation declined this
	# header would otherwise not exist at all, and a caller who asked for
	# rung 5 would have nothing to read.
	if not ready and not declined:
		return {}

	guard = header_guard(prefix, basename, "CONVERSE_H")
	lines = [
		f"/* Generated by situc {__version__} from {basename}.situ -- do not edit.",
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
		*(["#include <string.h>", ""] if needs_string_h else [""]),
		"#ifdef __cplusplus",
		"extern \"C\" {",
		"#endif",
		"",
	]

	for block in ready:
		lines.extend(block)

	for relation in declined:
		lines += [
			f"/* No conversation table for `{relation.name}`: a side takes a",
			" * `parameter`.",
			" *",
			" * A parameter is an argument the caller supplies. This table",
			" * reads the conversation key off two views it is handed rather",
			" * than off bytes it acquires, so there is no call of its own",
			" * the argument could arrive on -- and the getters behind one",
			" * take it as a trailing parameter (decision 0050). The other",
			" * three backends take views that carry the argument, so their",
			" * tables are emitted.",
			" */",
			"",
		]

	lines += [
		"#ifdef __cplusplus",
		"}",
		"#endif",
		"",
		f"#endif /* {guard} */",
	]

	return {f"{basename}_converse.h": "\n".join(lines) + "\n"}
