"""One message from many datagrams, in C: `<basename>_reassemble.h` (26.1123).

Beside rung 4 rather than on it: a datagram protocol frames nothing, it
holds the bytes of one message across many receives. The table is the
caller's -- one struct, sized by the schema's `messages` and `bytes` --
and nothing allocates.

THE RULES ARE fuzznet's, from its review (inbox 20261121T1000Z), and each
is there because the other choice was exploited or nearly:

- No total is carried; a STRIDE is. Every piece but the last is exactly
  the stride, the last no longer. The stride is set by the first piece
  that is not the last, never by the last: under loss piece 0 is often the
  one lost, and a stride taken from the final piece would let whoever
  sends it choose the buffer's layout. A last piece arriving first is held
  at the slot's end until a stride-setting piece places it.
- The bound is checked by division, `stride <= capacity / count`: the
  product once wrapped to zero and left a live slot.
- Every piece is keyed by the relation's equalities, which is how a later
  piece is held to the first: one claiming another count or sender is
  another message, and never touches this one's buffer.
- `missing` answers ranges, `(first, count)` each capped by the caller, so
  a re-request costs what is missing rather than one entry per piece.

Each of the `messages` slots owns `bytes / messages` of buffer and one
mark bit per piece it can hold; a message larger than that is refused
rather than spilled into a neighbour's.
"""

from __future__ import annotations

from situc import __version__, ast
from situc.codegen.c.converse import _key, _key_bytes
from situc.codegen.c.names import c_name, header_guard, ident, macro
from situc.codegen.c.relate import _binding, _local
from situc.relation import (KeyLayout, Refused, key_layout, key_part,
                             path_bindings, paths_in)
from situc.resolve import ResolvedSchema

__all__ = ["generate", "refusals"]


def _attr(held: ast.Reassemble, name: str) -> ast.Attr | None:
	return next((attr for attr in held.attrs if attr.name == name), None)


def _number(held: ast.Reassemble, name: str) -> int:
	found = _attr(held, name)
	value = getattr(found.value, "value", None) if found is not None else None
	assert isinstance(value, int), (held.name, name)
	return value


def _path(held: ast.Reassemble, name: str) -> str:
	found = _attr(held, name)
	assert found is not None and found.value is not None, (held.name, name)
	return paths_in(found.value)[0]


def _relation(schema: ast.Schema, held: ast.Reassemble) -> ast.Relation:
	return next(one for one in schema.relations() if one.name == held.relation)


def _piece(held: ast.Reassemble, resolved: ResolvedSchema,
		prefix: str) -> tuple[list[str], str, str, str | None]:
	"""How the piece's bytes are reached: the statements, the pointer and
	length expressions, and the sealed region it sits behind, if any.

	Behind a sealed region the piece is read through that region's gate
	-- the interior is only reachable once its tag has verified (14.3) --
	so `add` takes the gate, as 26.1122's matching does."""
	parts = _path(held, "piece").split(".")[1:]
	struct = resolved.structs.get(held.param.type_name)
	if struct is None:
		raise Refused(f"`{held.param.type_name}` has no resolved layout")
	first = next((entry.placement for entry in struct.entries
	              if entry.placement.path == f"{struct.name}.{parts[0]}"), None)
	if first is not None and first.kind == "sealed":
		if len(parts) != 2:
			raise Refused(f"`{_path(held, 'piece')}` reaches past a member "
			              f"of `{parts[0]}`")
		member = next((entry.placement for entry in struct.entries
		               if entry.placement.path
		               == f"{struct.name}.{parts[0]}.{parts[1]}"), None)
		if member is None:
			raise Refused(f"`{_path(held, 'piece')}` names no member")
		_bytes_run(member, _path(held, "piece"))
		gate = f"{held.param.name}_gate"
		return ([], f"{ident(prefix, struct.name, parts[0], parts[1], 'ptr')}"
		            f"({gate})",
		        f"{ident(prefix, struct.name, parts[0], parts[1], 'len')}"
		        f"({gate})", parts[0])

	lines: list[str] = []
	source = held.param.name
	for at, part in enumerate(parts):
		member = next((entry.placement for entry in struct.entries
		               if entry.placement.path == f"{struct.name}.{part}"),
		              None)
		if member is None:
			raise Refused(f"`{_path(held, 'piece')}` names no member")
		if at == len(parts) - 1:
			_bytes_run(member, _path(held, "piece"))
			return (lines,
			        f"{ident(prefix, struct.name, part, 'ptr')}({source})",
			        f"{ident(prefix, struct.name, part, 'len')}({source})",
			        None)
		inner = resolved.structs.get(member.type_name or "")
		if inner is None:
			raise Refused(f"`{_path(held, 'piece')}` reaches through "
			              f"`{part}`, which is not a struct")
		local = f"situ_piece{at}"
		lines += [
			f"\tsitu_view_t {local};",
			f"\tif ({ident(prefix, struct.name, part, 'view')}({source}, "
			f"&{local}) != SITU_OK) {{",
			"\t\treturn SITU_ERR_BOUNDS;",
			"\t}",
		]
		source, struct = local, inner
	raise Refused(f"`{_path(held, 'piece')}` names no member")


def _bytes_run(placement: object, path: str) -> None:
	scalar = getattr(placement, "scalar", None)
	if scalar is None or scalar.bits != 8 \
			or (getattr(placement, "array_count", None) is None
			    and getattr(placement, "sized_by", None) is None
			    and getattr(placement, "size_expr", None) is None):
		raise Refused(f"`{path}` is not a run of bytes, which is what a "
		              f"piece carries")


#: Why a table refused, in this order everywhere (fuzznet, 2026-11-22):
#: the C macros, Python's and Rust's constants, C++'s, and the stream
#: test's numbers.
WHY = (
	("NONE", "taken or a resend, or a piece `_add` could not read"),
	("FULL", "no slot free"),
	("PER", "its `per` value holds `per_max` already"),
	("TOO_LARGE", "the count or bytes past a slot's capacity"),
	("EXPIRED", "past its own deadline"),
	("INDEX", "no count, or an index past it"),
	("COUNT", "not its message's count"),
	("EMPTY", "a piece not the last with no bytes"),
	("STRIDE", "a piece not the last not the stride long"),
	("LAST_LONGER", "the last piece longer than the stride"),
	("CONFLICT", "a piece held already, with other bytes"),
)


def per_cap(schema: ast.Schema, held: ast.Reassemble,
		resolved: ResolvedSchema) -> tuple[int, int, int] | None:
	"""`per = f.field, per_max = N`: where the part of the key `field` is
	sits -- `relation.key_part`'s answer -- and N; None without a `per`.

	The field must BE a part of the key: the table holds keys, so a cap
	counted by anything else would have to keep a second value per slot,
	and a field outside the key does not say which messages are one
	sender's. Refused by name where it is not (fuzznet, inbox
	20261122T1804Z)."""
	if _attr(held, "per") is None:
		return None
	relation = _relation(schema, held)
	path = _path(held, "per")
	first = relation.params[0].name
	part = key_part(key_layout(relation, resolved),
	                first + path[len(held.param.name):])
	if part is None:
		raise Refused(f"`per` names `{path}`, which is no part of "
		              f"`{held.relation}`'s key; a cap counts messages by a "
		              f"part of the key the table holds")
	return part[0], part[1], _number(held, "per_max")


def refusals(schema: ast.Schema,
		resolved: ResolvedSchema) -> list[tuple[str, str]]:
	"""Every reassembly this backend cannot emit, and why."""
	found: list[tuple[str, str]] = []
	for held in schema.reassemblies():
		try:
			key_layout(_relation(schema, held), resolved)
			for key in ("index", "count", "expires"):
				if _attr(held, key) is not None:
					path_bindings(_path(held, key), held.param, resolved)
			_piece(held, resolved, "situ")
			per_cap(schema, held, resolved)
		except Refused as why:
			found.append((held.name, str(why)))
	return found


def _per_lines(schema: ast.Schema, held: ast.Reassemble,
		resolved: ResolvedSchema, layout: KeyLayout, upper: str) -> list[str]:
	"""A new message refused where one value of the `per` field already
	holds `per_max` messages: one sender cannot take every slot."""
	cap = per_cap(schema, held, resolved)
	if cap is None:
		return []
	start, width, _ = cap
	if layout.packed:
		mask = (1 << width) - 1
		same = (f"((t->slot[i].key >> {start}u) & UINT64_C({mask:#x})) == "
		        f"((key >> {start}u) & UINT64_C({mask:#x}))")
	else:
		same = (f"memcmp(&t->slot[i].key[{start}u], &key[{start}u], "
		        f"{width}u) == 0")
	return [
		f"\t\t/* `per {_path(held, 'per')}`: at most `per_max` messages a"
		" value (fuzznet,",
		"\t\t * inbox 20261122T1804Z). */",
		"\t\tuint32_t same_part = 0u;",
		"\t\tfor (uint32_t i = 0u; i < t->messages; i++) {",
		f"\t\t\tif (t->slot[i].live != 0u && {same}) {{",
		"\t\t\t\tsame_part++;",
		"\t\t\t}",
		"\t\t}",
		"\t\tif (same_part >= t->per_max) {",
		f"\t\t\tt->why = {upper}_WHY_PER;",
		"\t\t\treturn SITU_ERR_BOUNDS;",
		"\t\t}",
	]


def _table(schema: ast.Schema, held: ast.Reassemble,
		resolved: ResolvedSchema, prefix: str) -> list[str]:
	name     = ident(prefix, held.name)
	upper    = macro(prefix, held.name)
	param    = held.param.name
	layout   = key_layout(_relation(schema, held), resolved)
	messages = _number(held, "messages")
	total    = _number(held, "bytes")
	timeout  = _number(held, "timeout")
	unit     = getattr(getattr(_attr(held, "unit"), "value", None), "name",
	                   None) or "ms"
	capacity = total // messages
	marks    = (capacity + 7) // 8

	reads: list[str] = []
	local: dict[str, str] = {}
	first = 0
	# `expires` (fuzznet, inbox 20261122T1804Z): the piece's own deadline,
	# its low 32 bits on the table's wrapping clock.
	for key in ("index", "count", "expires"):
		if key == "expires" and _attr(held, key) is None:
			continue
		bindings, _ = path_bindings(_path(held, key), held.param, resolved,
		                            first)
		first += len(bindings)
		for bind in bindings:
			reads += _binding(bind, "uint32_t", prefix)
		local[key] = _local(bindings[-1].target)
	index, count = local["index"], local["count"]
	expires = local.get("expires")
	walk, piece, piece_len, region = _piece(held, resolved, prefix)
	keyed = (_key(layout.request, param, prefix) if layout.packed
	         else _key_bytes(layout.request, param, prefix,
	                         layout.total_bytes))
	key_type = ("uint64_t key" if layout.packed
	            else f"uint8_t  key[{layout.total_bytes}u]")
	same = ("t->slot[i].key == key" if layout.packed else
	        f"memcmp(t->slot[i].key, key, {layout.total_bytes}u) == 0")
	keep = ("t->slot[s].key = key;" if layout.packed else
	        f"memcpy(t->slot[s].key, key, {layout.total_bytes}u);")
	struct = c_name(held.param.type_name)
	gate_type = (ident(prefix, held.param.type_name, region, "t")
	             if region else None)
	arg = (f"{gate_type} {param}_gate" if region
	       else f"situ_view_t {param}")
	opened = ([f"\tsitu_view_t {param} = {param}_gate.view;"]
	          if region else [])
	del struct
	# `_add_parts`' own names for what `_add` reads (fuzznet's item 3).
	p_index, p_count, p_expires = "index", "count", "deadline"
	key_param = ("uint64_t key" if layout.packed else "const uint8_t *key")
	name_pad  = " " * len(name)
	# `per_max` given at init too, the schema's its maximum (fuzznet: one
	# schema, tables of 8 a sender and of 1).
	per = per_cap(schema, held, resolved)
	per_most = per[2] if per is not None else None

	return [
		f"/* `{held.name}`: one message from up to `{upper}_MESSAGES` in "
		"flight, each",
		f" * of at most `{upper}_CAPACITY` bytes, pieces keyed by "
		f"`{held.relation}` and",
		f" * placed by `{_path(held, 'index')}` of `{_path(held, 'count')}`."
		f" The clock is yours, in {unit}:",
		f" * a message whose pieces stop is dropped `{upper}_TIMEOUT` after "
		"its first.",
		" * The three are maxima: `_init_in` takes smaller ones and the"
		" caller's",
		" * memory (fuzznet, inbox 20261122T1804Z), `_init` the schema's and"
		" a",
		f" * `{name}_store_t`, more than {_number(held, 'bytes')} bytes:"
		" static storage or the",
		" * heap rather than a stack frame. */",
		f"#define {upper}_MESSAGES {messages}u",
		f"#define {upper}_CAPACITY {capacity}u",
		f"#define {upper}_TIMEOUT  {timeout}u",
		*([f"#define {upper}_PER_MAX  {per_most}u"]
		  if per_most is not None else []),
		"/* Why `_add` refused, in `t->why` after any answer but SITU_OK and",
		" * SITU_ERR_TRUNCATED, which leave it NONE (fuzznet, 2026-11-22). */",
		*[f"#define {upper}_WHY_{why:<11} {number}u\t/* {says} */"
		  for number, (why, says) in enumerate(WHY)],
		"/* The memory `_init_in` wants for `messages` slots of `capacity`"
		" bytes. */",
		f"#define {upper}_BYTES_FOR(messages, capacity) "
		"((size_t)(messages) * (capacity))",
		f"#define {upper}_MARKS_FOR(messages, capacity) "
		"((size_t)(messages) * (((capacity) + 7u) / 8u))",
		"typedef struct {",
		f"\t{key_type};",
		"\tuint32_t count;\t\t/* pieces in the message */",
		"\tuint32_t stride;\t/* every piece's length but the last's */",
		"\tuint32_t last_len;\t/* the last piece's, once held */",
		"\tuint32_t held;\t\t/* pieces held so far */",
		"\tuint32_t deadline;",
		"\tuint8_t  live;",
		"\tuint8_t  stride_known;",
		"\tuint8_t  last_parked;\t/* held at the end, its place unknown */",
		f"}} {name}_slot_t;",
		"",
		"typedef struct {",
		"\tuint32_t messages;\t/* the bounds this table was given */",
		"\tuint32_t capacity;",
		"\tuint32_t timeout;",
		"\tuint32_t marks_per;\t/* a slot's bytes of marks */",
		f"\tuint32_t why;\t\t/* the last refusal's `{upper}_WHY_` */",
		*(["\tuint32_t per_max;\t/* messages one `per` value holds */"]
		  if per_most is not None else []),
		f"\t{name}_slot_t *slot;",
		"\tuint8_t *bytes;",
		"\tuint8_t *marks;",
		f"}} {name}_t;",
		"",
		"typedef struct {",
		f"\t{name}_slot_t slot[{upper}_MESSAGES];",
		f"\tuint8_t bytes[{upper}_MESSAGES * {upper}_CAPACITY];",
		f"\tuint8_t marks[{upper}_MESSAGES * {marks}u];",
		f"}} {name}_store_t;",
		"",
		"/** A table of smaller bounds than the schema's, in the caller's"
		" memory:",
		" *  `slot` holds `messages` slots, `bytes` `_BYTES_FOR` and `marks`",
		" *  `_MARKS_FOR` bytes"
		+ (", `per_max` messages a `per` value" if per_most is not None
		   else "")
		+ ". SITU_ERR_BOUNDS for a zero bound or one above",
		" *  the schema's, which the generated code was written to. */",
		f"static inline situ_err_t {name}_init_in({name}_t *t,"
		" uint32_t messages,",
		"\t\tuint32_t capacity, uint32_t timeout,"
		f" {name}_slot_t *slot,",
		"\t\tuint8_t *bytes, uint8_t *marks"
		+ (", uint32_t per_max)" if per_most is not None else ")"),
		"{",
		f"\tif (messages == 0u || messages > {upper}_MESSAGES",
		f"\t    || capacity == 0u || capacity > {upper}_CAPACITY",
		*([f"\t    || per_max == 0u || per_max > {upper}_PER_MAX"]
		  if per_most is not None else []),
		f"\t    || timeout == 0u || timeout > {upper}_TIMEOUT) {{",
		"\t\treturn SITU_ERR_BOUNDS;",
		"\t}",
		"\tt->messages = messages;",
		"\tt->capacity = capacity;",
		"\tt->timeout  = timeout;",
		"\tt->marks_per = (capacity + 7u) / 8u;",
		"\tt->why       = 0u;",
		*(["\tt->per_max = per_max;"] if per_most is not None else []),
		"\tt->slot  = slot;",
		"\tt->bytes = bytes;",
		"\tt->marks = marks;",
		"\tfor (uint32_t i = 0u; i < messages; i++) {",
		"\t\tslot[i].live = 0u;",
		"\t}",
		"\treturn SITU_OK;",
		"}",
		"",
		"/** The schema's bounds, in `store`. */",
		f"static inline void {name}_init({name}_t *t, {name}_store_t *store)",
		"{",
		f"\t(void){name}_init_in(t, {upper}_MESSAGES, {upper}_CAPACITY,",
		f"\t               {name_pad}{upper}_TIMEOUT, store->slot,"
		" store->bytes, store->marks"
		+ (f",\n\t               {name_pad}{upper}_PER_MAX);"
		   if per_most is not None else ");"),
		"}",
		"",
		f"static inline int {name}_marked(const {name}_t *t, uint32_t s,"
		" uint32_t i)",
		"{",
		"\treturn (int)(((uint32_t)t->marks[(size_t)s * t->marks_per + i / 8u] >> (i % 8u)) & 1u);",
		"}",
		"",
		f"/** `_add` from parts the caller has already read and verified: the",
		" *  key as the conversation table's `_key(...)` builds it, the piece's",
		" *  index and count, its bytes"
		+ (", and its own deadline" if expires else "")
		+ ".",
		" *  For a caller that opens a piece itself before the table sees it",
		" *  (fuzznet, inbox 20261122T1804Z); `_add` reads these and calls it. */",
		f"static inline situ_err_t {name}_add_parts({name}_t *t,",
		f"\t\t{key_param}, uint32_t index, uint32_t count,",
		"\t\tconst uint8_t *data, uint32_t len,"
		+ (" uint32_t deadline," if expires else ""),
		"\t\tuint32_t now, const uint8_t **message, uint32_t *length)",
		"{",
		f"\tt->why = {upper}_WHY_NONE;",
		f"\tif ({p_count} == 0u || {p_index} >= {p_count}) {{",
		f"\t\tt->why = {upper}_WHY_INDEX;",
		"\t\treturn SITU_ERR_CONSTRAINT;",
		"\t}",
		f"\tif ({p_count} > t->capacity) {{",
		f"\t\tt->why = {upper}_WHY_TOO_LARGE;",
		"\t\treturn SITU_ERR_BOUNDS;\t/* a byte a piece at least */",
		"\t}",
		*([f"\tif ((int32_t)(now - {p_expires}) >= 0) {{",
		   f"\t\tt->why = {upper}_WHY_EXPIRED;",
		   "\t\treturn SITU_ERR_BOUNDS;\t/* past its own deadline */",
		   "\t}"] if expires else []),
		"",
		f"\tuint32_t s = t->messages;",
		f"\tfor (uint32_t i = 0u; i < t->messages; i++) {{",
		f"\t\tif (t->slot[i].live != 0u && {same}) {{",
		"\t\t\ts = i;",
		"\t\t\tbreak;",
		"\t\t}",
		"\t}",
		f"\tif (s == t->messages) {{",
		*_per_lines(schema, held, resolved, layout, upper),
		f"\t\tfor (uint32_t i = 0u; i < t->messages; i++) {{",
		"\t\t\tif (t->slot[i].live == 0u) {",
		"\t\t\t\ts = i;",
		"\t\t\t\tbreak;",
		"\t\t\t}",
		"\t\t}",
		f"\t\tif (s == t->messages) {{",
		f"\t\t\tt->why = {upper}_WHY_FULL;",
		"\t\t\treturn SITU_ERR_BOUNDS;",
		"\t\t}",
		f"\t\t{keep}",
		f"\t\tt->slot[s].count        = {p_count};",
		"\t\tt->slot[s].stride       = 0u;",
		"\t\tt->slot[s].last_len     = 0u;",
		"\t\tt->slot[s].held         = 0u;",
		f"\t\tt->slot[s].deadline     = now + t->timeout;",
		# A piece's own deadline bounds the slot it opens, and only that:
		# fuzznet fixes a message's deadline from its first piece, and a
		# later one never moves it (fuzznet, 2026-11-23).
		*([f"\t\tif ((int32_t)({p_expires} - t->slot[s].deadline) < 0) {{",
		   f"\t\t\tt->slot[s].deadline = {p_expires};",
		   "\t\t}"] if expires else []),
		"\t\tt->slot[s].stride_known = 0u;",
		"\t\tt->slot[s].last_parked  = 0u;",
		"\t\tt->slot[s].live         = 1u;",
		f"\t\tfor (uint32_t b = 0u; b < t->marks_per; b++) {{",
		"\t\t\tt->marks[(size_t)s * t->marks_per + b] = 0u;",
		"\t\t}",
		"\t}",
		f"\t{name}_slot_t *m = &t->slot[s];",
		"\tuint8_t *body = &t->bytes[(size_t)s * t->capacity];",
		# Every piece of one message says the same count. A relation whose
		# key leaves the count out lets a later piece say a larger one, and
		# its index, checked only against its own count, then placed it at
		# `index * stride`, past the slot: a write half a megabyte beyond
		# the table, found under ASan.
		f"\tif ({p_count} != m->count) {{",
		f"\t\tt->why = {upper}_WHY_COUNT;",
		"\t\treturn SITU_ERR_CONSTRAINT;\t/* not its message's count */",
		"\t}",
		f"\tconst int last = {p_index} == m->count - 1u;",
		f"\tif ({name}_marked(t, s, {p_index})) {{",
		"\t\t/* Held already. The same bytes are a resend; others are a",
		"\t\t * rewrite of what was taken, refused so the caller sees it",
		"\t\t * (fuzznet, 2026-11-22). The first bytes are kept either way.",
		"\t\t * A piece not the last is held only once the stride is known;",
		"\t\t * the last is at its place, or parked at the slot's end. */",
		"\t\tconst uint32_t had = last ? m->last_len : m->stride;",
		"\t\tconst uint8_t *was = (last && m->last_parked)",
		"\t\t                     ? &body[t->capacity - had]",
		f"\t\t                     : &body[{p_index} * m->stride];",
		"\t\tif (len != had || memcmp(was, data, len) != 0) {",
		f"\t\t\tt->why = {upper}_WHY_CONFLICT;",
		"\t\t\treturn SITU_ERR_CONSTRAINT;",
		"\t\t}",
		"\t\treturn SITU_ERR_TRUNCATED;",
		"\t}",
		"\tif (!last) {",
		"\t\tif (!m->stride_known) {",
		"\t\t\t/* The first piece that is not the last sets the stride,",
		"\t\t\t * bounded by division so no product can wrap. */",
		f"\t\t\tif (len == 0u || len > t->capacity / m->count) {{",
		"\t\t\t\tm->live = m->held != 0u;",
		f"\t\t\t\tt->why = len == 0u ? {upper}_WHY_EMPTY : {upper}_WHY_TOO_LARGE;",
		"\t\t\t\treturn SITU_ERR_BOUNDS;",
		"\t\t\t}",
		"\t\t\tif (m->last_parked && m->last_len > len) {",
		"\t\t\t\tm->live = m->held != 0u;",
		f"\t\t\t\tt->why = {upper}_WHY_LAST_LONGER;",
		"\t\t\t\treturn SITU_ERR_CONSTRAINT;",
		"\t\t\t}",
		"\t\t\tm->stride = len;",
		"\t\t\tm->stride_known = 1u;",
		"\t\t\tif (m->last_parked) {",
		"\t\t\t\t/* Placed now: from the end of the slot to its own. */",
		f"\t\t\t\tmemmove(&body[(m->count - 1u) * m->stride],",
		f"\t\t\t\t        &body[t->capacity - m->last_len],",
		"\t\t\t\t        m->last_len);",
		"\t\t\t\tm->last_parked = 0u;",
		"\t\t\t}",
		"\t\t} else if (len != m->stride) {",
		f"\t\t\tt->why = {upper}_WHY_STRIDE;",
		"\t\t\treturn SITU_ERR_CONSTRAINT;",
		"\t\t}",
		f"\t\tmemcpy(&body[{p_index} * m->stride], data, len);",
		"\t} else {",
		"\t\tif (m->stride_known && len > m->stride) {",
		f"\t\t\tt->why = {upper}_WHY_LAST_LONGER;",
		"\t\t\treturn SITU_ERR_CONSTRAINT;",
		"\t\t}",
		f"\t\tif (len > t->capacity) {{",
		"\t\t\tm->live = m->held != 0u;",
		f"\t\t\tt->why = {upper}_WHY_TOO_LARGE;",
		"\t\t\treturn SITU_ERR_BOUNDS;",
		"\t\t}",
		"\t\tm->last_len = len;",
		"\t\tif (m->stride_known) {",
		f"\t\t\tmemcpy(&body[{p_index} * m->stride], data, len);",
		"\t\t} else {",
		"\t\t\t/* Its place depends on a stride not yet known: held at",
		"\t\t\t * the end, and never the thing that decides the stride. */",
		f"\t\t\tmemcpy(&body[t->capacity - len], data, len);",
		"\t\t\tm->last_parked = 1u;",
		"\t\t}",
		"\t}",
		f"\tt->marks[(size_t)s * t->marks_per + {p_index} / 8u] |= (uint8_t)(1u << ({p_index} % 8u));",
		"\tm->held++;",
		"\tif (m->held < m->count) {",
		"\t\treturn SITU_ERR_TRUNCATED;",
		"\t}",
		"\tm->live = 0u;",
		"\tif (m->count == 1u) {",
		"\t\t/* One piece, so no stride: it is where it was held. */",
		f"\t\t*message = &body[t->capacity - m->last_len];",
		"\t\t*length  = m->last_len;",
		"\t} else {",
		"\t\t*message = body;",
		"\t\t*length  = (m->count - 1u) * m->stride + m->last_len;",
		"\t}",
		"\treturn SITU_OK;",
		"}",
		"",
		f"/** Take one piece. SITU_OK where it completes its message:",
		" *  `*message` and `*length` are the whole of it, valid until the next",
		" *  `_add`, and the slot is free again. SITU_ERR_TRUNCATED where more",
		" *  is wanted (a piece already held with the same bytes is that, too).",
		" *  SITU_ERR_CONSTRAINT for a piece that cannot be: an index past its",
		" *  count, no count, a piece not the last whose length is not the",
		" *  stride, a last one longer, a piece held already with other bytes.",
		" *  SITU_ERR_BOUNDS where it does not fit: no slot free, or a",
		f" *  message past `{upper}_CAPACITY`, which drops it. `t->why` says which."
		+ (f" Handed `{region}`'s" if region else ""),
		*([" *  gate: a piece is taken only once its tag has verified."]
		  if region else []),
		" */",
		f"static inline situ_err_t {name}_add({name}_t *t, {arg},",
		"\t\tuint32_t now, const uint8_t **message, uint32_t *length)",
		"{",
		"\tsitu_err_t err = SITU_OK;",
		f"\tt->why = {upper}_WHY_NONE;\t/* and so where the piece cannot be read */",
		*opened,
		*keyed,
		*reads,
		*walk,
		"\t(void)err;",
		f"\tconst uint8_t *data = {piece};",
		f"\tconst uint32_t len = {piece_len};",
		f"\treturn {name}_add_parts(t, key, {index}, {count}, data, len,"
		+ (f" {expires}," if expires else "") + " now, message, length);",
		"}",
		"",
		f"/** The pieces a message still wants, as up to `cap` ranges each of",
		" *  at most `most` pieces: `first[k]` and `count[k]`. Answers how",
		" *  many ranges; 0 where no such message is held. Build the",
		" *  re-request yourself: it carries your expiry and your seal. */",
		f"static inline uint32_t {name}_missing(const {name}_t *t,",
		("\t\tuint64_t key," if layout.packed else
		 "\t\tconst uint8_t *key,"),
		"\t\tuint32_t *first, uint32_t *count, uint32_t cap, uint32_t most)",
		"{",
		f"\tfor (uint32_t s = 0u; s < t->messages; s++) {{",
		f"\t\tconst {name}_slot_t *m = &t->slot[s];",
		("\t\tif (m->live == 0u || m->key != key) {" if layout.packed else
		 f"\t\tif (m->live == 0u || memcmp(m->key, key, "
		 f"{layout.total_bytes}u) != 0) {{"),
		"\t\t\tcontinue;",
		"\t\t}",
		"\t\tuint32_t n = 0u;",
		"\t\tfor (uint32_t i = 0u; i < m->count && n < cap; i++) {",
		f"\t\t\tif ({name}_marked(t, s, i)) {{",
		"\t\t\t\tcontinue;",
		"\t\t\t}",
		"\t\t\tif (n > 0u && first[n - 1u] + count[n - 1u] == i",
		"\t\t\t    && count[n - 1u] < most) {",
		"\t\t\t\tcount[n - 1u]++;",
		"\t\t\t} else {",
		"\t\t\t\tfirst[n] = i;",
		"\t\t\t\tcount[n] = 1u;",
		"\t\t\t\tn++;",
		"\t\t\t}",
		"\t\t}",
		"\t\treturn n;",
		"\t}",
		"\treturn 0u;",
		"}",
		"",
		f"/** Drop the message held under `key`, as a caller does for an",
		" *  exchange it has abandoned, rather than let it hold its slot until",
		" *  it expires (fuzznet, inbox 20261122T1804Z, item 2). Answers 1",
		" *  where one was held, 0 where none was. */",
		f"static inline uint32_t {name}_forget({name}_t *t,",
		("\t\tuint64_t key)" if layout.packed else
		 "\t\tconst uint8_t *key)"),
		"{",
		f"\tfor (uint32_t s = 0u; s < t->messages; s++) {{",
		("\t\tif (t->slot[s].live != 0u && t->slot[s].key == key) {"
		 if layout.packed else
		 f"\t\tif (t->slot[s].live != 0u && memcmp(t->slot[s].key, key, "
		 f"{layout.total_bytes}u) == 0) {{"),
		"\t\t\tt->slot[s].live = 0u;",
		"\t\t\treturn 1u;",
		"\t\t}",
		"\t}",
		"\treturn 0u;",
		"}",
		"",
		f"/** Drop every message whose first piece came `{upper}_TIMEOUT` ago",
		" *  or more, by the wrapping clock. Answers how many went. */",
		f"static inline uint32_t {name}_expire({name}_t *t, uint32_t now)",
		"{",
		"\tuint32_t gone = 0u;",
		f"\tfor (uint32_t s = 0u; s < t->messages; s++) {{",
		"\t\tif (t->slot[s].live != 0u",
		"\t\t    && (int32_t)(now - t->slot[s].deadline) >= 0) {",
		"\t\t\tt->slot[s].live = 0u;",
		"\t\t\tgone++;",
		"\t\t}",
		"\t}",
		"\treturn gone;",
		"}",
		"",
	]


def generate(schema: ast.Schema, resolved: ResolvedSchema, basename: str,
		prefix: str = "situ") -> dict[str, str]:
	refused = {name for name, _ in refusals(schema, resolved)}
	ready = [held for held in schema.reassemblies() if held.name not in refused]
	if not ready:
		return {}
	guard = header_guard(prefix, basename, "REASSEMBLE_H")
	lines = [
		f"/* Generated by situc {__version__} from {basename}.situ -- do not "
		"edit.",
		" *",
		" * Reassembly (26.1123): one message from many datagrams. The table",
		" * is yours and nothing allocates.",
		" */",
		"",
		f"#ifndef {guard}",
		f"#define {guard}",
		"",
		"#include <string.h>",
		"",
		'#include "situ.h"',
		f'#include "{basename}.h"',
		"",
		"#ifdef __cplusplus",
		'extern "C" {',
		"#endif",
		"",
	]
	for held in ready:
		lines += _table(schema, held, resolved, prefix)
	lines += ["#ifdef __cplusplus", "}", "#endif", "",
	          f"#endif /* {guard} */"]
	return {f"{basename}_reassemble.h": "\n".join(lines) + "\n"}
