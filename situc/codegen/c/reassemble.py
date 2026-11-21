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
from situc.relation import Refused, key_layout, path_bindings, paths_in
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


def refusals(schema: ast.Schema,
		resolved: ResolvedSchema) -> list[tuple[str, str]]:
	"""Every reassembly this backend cannot emit, and why."""
	found: list[tuple[str, str]] = []
	for held in schema.reassemblies():
		try:
			key_layout(_relation(schema, held), resolved)
			for key in ("index", "count"):
				path_bindings(_path(held, key), held.param, resolved)
			_piece(held, resolved, "situ")
		except Refused as why:
			found.append((held.name, str(why)))
	return found


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
	for key in ("index", "count"):
		bindings, _ = path_bindings(_path(held, key), held.param, resolved,
		                            first)
		first += len(bindings)
		for bind in bindings:
			reads += _binding(bind, "uint32_t", prefix)
		local[key] = _local(bindings[-1].target)
	index, count = local["index"], local["count"]
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

	return [
		f"/* `{held.name}`: one message from up to `{upper}_MESSAGES` in "
		"flight, each",
		f" * of at most `{upper}_CAPACITY` bytes, pieces keyed by "
		f"`{held.relation}` and",
		f" * placed by `{_path(held, 'index')}` of `{_path(held, 'count')}`."
		f" The clock is yours, in {unit}:",
		f" * a message whose pieces stop is dropped `{upper}_TIMEOUT` after "
		"its first.",
		" * It holds every slot's buffer itself, more than"
		f" {_number(held, 'bytes')} bytes: give it static",
		" * storage or the heap rather than a stack frame. Zero-initialise"
		" it, or",
		" * call `_init`. */",
		f"#define {upper}_MESSAGES {messages}u",
		f"#define {upper}_CAPACITY {capacity}u",
		f"#define {upper}_TIMEOUT  {timeout}u",
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
		f"\t{name}_slot_t slot[{upper}_MESSAGES];",
		f"\tuint8_t bytes[{upper}_MESSAGES][{upper}_CAPACITY];",
		f"\tuint8_t marks[{upper}_MESSAGES][{marks}u];",
		f"}} {name}_t;",
		"",
		f"static inline void {name}_init({name}_t *t)",
		"{",
		f"\tfor (uint32_t i = 0u; i < {upper}_MESSAGES; i++) {{",
		"\t\tt->slot[i].live = 0u;",
		"\t}",
		"}",
		"",
		f"static inline int {name}_marked(const {name}_t *t, uint32_t s,"
		" uint32_t i)",
		"{",
		"\treturn (int)(((uint32_t)t->marks[s][i / 8u] >> (i % 8u)) & 1u);",
		"}",
		"",
		f"/** Take one piece. SITU_OK where it completes its message:",
		" *  `*message` and `*length` are the whole of it, valid until the next",
		" *  `_add`, and the slot is free again. SITU_ERR_TRUNCATED where more",
		" *  is wanted (a piece already held is that, too). SITU_ERR_CONSTRAINT",
		" *  for a piece that cannot be: an index past its count, no count, a",
		" *  piece not the last whose length is not the stride, a last one",
		" *  longer. SITU_ERR_BOUNDS where it does not fit: no slot free, or a",
		f" *  message past `{upper}_CAPACITY`, which drops it."
		+ (f" Handed `{region}`'s" if region else ""),
		*([" *  gate: a piece is taken only once its tag has verified."]
		  if region else []),
		" */",
		f"static inline situ_err_t {name}_add({name}_t *t, {arg},",
		"\t\tuint32_t now, const uint8_t **message, uint32_t *length)",
		"{",
		"\tsitu_err_t err = SITU_OK;",
		*opened,
		*keyed,
		*reads,
		*walk,
		"\t(void)err;",
		f"\tconst uint8_t *data = {piece};",
		f"\tconst uint32_t len = {piece_len};",
		f"\tif ({count} == 0u || {index} >= {count}) {{",
		"\t\treturn SITU_ERR_CONSTRAINT;",
		"\t}",
		f"\tif ({count} > {upper}_CAPACITY) {{",
		"\t\treturn SITU_ERR_BOUNDS;\t/* a byte a piece at least */",
		"\t}",
		"",
		f"\tuint32_t s = {upper}_MESSAGES;",
		f"\tfor (uint32_t i = 0u; i < {upper}_MESSAGES; i++) {{",
		f"\t\tif (t->slot[i].live != 0u && {same}) {{",
		"\t\t\ts = i;",
		"\t\t\tbreak;",
		"\t\t}",
		"\t}",
		f"\tif (s == {upper}_MESSAGES) {{",
		f"\t\tfor (uint32_t i = 0u; i < {upper}_MESSAGES; i++) {{",
		"\t\t\tif (t->slot[i].live == 0u) {",
		"\t\t\t\ts = i;",
		"\t\t\t\tbreak;",
		"\t\t\t}",
		"\t\t}",
		f"\t\tif (s == {upper}_MESSAGES) {{",
		"\t\t\treturn SITU_ERR_BOUNDS;",
		"\t\t}",
		f"\t\t{keep}",
		f"\t\tt->slot[s].count        = {count};",
		"\t\tt->slot[s].stride       = 0u;",
		"\t\tt->slot[s].last_len     = 0u;",
		"\t\tt->slot[s].held         = 0u;",
		f"\t\tt->slot[s].deadline     = now + {upper}_TIMEOUT;",
		"\t\tt->slot[s].stride_known = 0u;",
		"\t\tt->slot[s].last_parked  = 0u;",
		"\t\tt->slot[s].live         = 1u;",
		f"\t\tfor (uint32_t b = 0u; b < {marks}u; b++) {{",
		"\t\t\tt->marks[s][b] = 0u;",
		"\t\t}",
		"\t}",
		f"\t{name}_slot_t *m = &t->slot[s];",
		# Every piece of one message says the same count. A relation whose
		# key leaves the count out lets a later piece say a larger one, and
		# its index, checked only against its own count, then placed it at
		# `index * stride`, past the slot: a write half a megabyte beyond
		# the table, found under ASan.
		f"\tif ({count} != m->count) {{",
		"\t\treturn SITU_ERR_CONSTRAINT;\t/* not its message's count */",
		"\t}",
		f"\tif ({name}_marked(t, s, {index})) {{",
		"\t\treturn SITU_ERR_TRUNCATED;\t/* held already: a resend */",
		"\t}",
		f"\tconst int last = {index} == m->count - 1u;",
		"\tif (!last) {",
		"\t\tif (!m->stride_known) {",
		"\t\t\t/* The first piece that is not the last sets the stride,",
		"\t\t\t * bounded by division so no product can wrap. */",
		f"\t\t\tif (len == 0u || len > {upper}_CAPACITY / m->count) {{",
		"\t\t\t\tm->live = 0u;",
		"\t\t\t\treturn SITU_ERR_BOUNDS;",
		"\t\t\t}",
		"\t\t\tif (m->last_parked && m->last_len > len) {",
		"\t\t\t\tm->live = 0u;",
		"\t\t\t\treturn SITU_ERR_CONSTRAINT;",
		"\t\t\t}",
		"\t\t\tm->stride = len;",
		"\t\t\tm->stride_known = 1u;",
		"\t\t\tif (m->last_parked) {",
		"\t\t\t\t/* Placed now: from the end of the slot to its own. */",
		f"\t\t\t\tmemmove(&t->bytes[s][(m->count - 1u) * m->stride],",
		f"\t\t\t\t        &t->bytes[s][{upper}_CAPACITY - m->last_len],",
		"\t\t\t\t        m->last_len);",
		"\t\t\t\tm->last_parked = 0u;",
		"\t\t\t}",
		"\t\t} else if (len != m->stride) {",
		"\t\t\treturn SITU_ERR_CONSTRAINT;",
		"\t\t}",
		f"\t\tmemcpy(&t->bytes[s][{index} * m->stride], data, len);",
		"\t} else {",
		"\t\tif (m->stride_known && len > m->stride) {",
		"\t\t\treturn SITU_ERR_CONSTRAINT;",
		"\t\t}",
		f"\t\tif (len > {upper}_CAPACITY) {{",
		"\t\t\tm->live = 0u;",
		"\t\t\treturn SITU_ERR_BOUNDS;",
		"\t\t}",
		"\t\tm->last_len = len;",
		"\t\tif (m->stride_known) {",
		f"\t\t\tmemcpy(&t->bytes[s][{index} * m->stride], data, len);",
		"\t\t} else {",
		"\t\t\t/* Its place depends on a stride not yet known: held at",
		"\t\t\t * the end, and never the thing that decides the stride. */",
		f"\t\t\tmemcpy(&t->bytes[s][{upper}_CAPACITY - len], data, len);",
		"\t\t\tm->last_parked = 1u;",
		"\t\t}",
		"\t}",
		f"\tt->marks[s][{index} / 8u] |= (uint8_t)(1u << ({index} % 8u));",
		"\tm->held++;",
		"\tif (m->held < m->count) {",
		"\t\treturn SITU_ERR_TRUNCATED;",
		"\t}",
		"\tm->live = 0u;",
		"\tif (m->count == 1u) {",
		"\t\t/* One piece, so no stride: it is where it was held. */",
		f"\t\t*message = &t->bytes[s][{upper}_CAPACITY - m->last_len];",
		"\t\t*length  = m->last_len;",
		"\t} else {",
		"\t\t*message = t->bytes[s];",
		"\t\t*length  = (m->count - 1u) * m->stride + m->last_len;",
		"\t}",
		"\treturn SITU_OK;",
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
		f"\tfor (uint32_t s = 0u; s < {upper}_MESSAGES; s++) {{",
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
		f"/** Drop every message whose first piece came `{upper}_TIMEOUT` ago",
		" *  or more, by the wrapping clock. Answers how many went. */",
		f"static inline uint32_t {name}_expire({name}_t *t, uint32_t now)",
		"{",
		"\tuint32_t gone = 0u;",
		f"\tfor (uint32_t s = 0u; s < {upper}_MESSAGES; s++) {{",
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
