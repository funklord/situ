"""One message from many datagrams, in C++: `<basename>_reassemble.hpp`.

C's table, laid out the same and allocating nothing, and C's rules, which
`situc/codegen/c/reassemble.py` explains one by one. `add` answers
`err::ok` with the whole message in `message`, `err::truncated` while
more is wanted, and `err::constraint` or `err::bounds` where C refuses.
"""

from __future__ import annotations

from situc import __version__, ast
from situc.codegen.c.names import c_name
from situc.codegen.c.reassemble import (_attr, _number, _path, _relation,
                                        refusals)
from situc.codegen.cpp.converse import _key, _key_bytes
from situc.codegen.cpp.names import class_name
from situc.relation import key_layout
from situc.resolve import ResolvedSchema

__all__ = ["generate"]


def _reach(resolved: ResolvedSchema, type_name: str, root: str, path: str,
		local: str) -> tuple[list[str], str]:
	"""`f.h.index`: a declared view per nested step, then the leaf's
	accessor call. A step that cannot be reached is `err::bounds`."""
	parts = path.split(".")[1:]
	struct = resolved.structs[type_name]
	lines: list[str] = []
	source = root
	for depth, step in enumerate(parts[:-1]):
		found = resolved.find(f"{struct.name}.{step}")
		assert found is not None, path
		inner = resolved.structs[found.placement.type_name or ""]
		held = f"{local}{depth}"
		lines += [
			f"\t\t::situ::{class_name(inner)} {held};",
			f"\t\tif ({source}.{c_name(step)}({held}) != ::situ::rt::err::ok) {{",
			"\t\t\treturn ::situ::rt::err::bounds;",
			"\t\t}",
		]
		source, struct = held, inner
	return lines, f"{source}.{c_name(parts[-1])}()"


def _table(schema: ast.Schema, held: ast.Reassemble,
		resolved: ResolvedSchema) -> list[str]:
	param    = held.param.name
	layout   = key_layout(_relation(schema, held), resolved)
	messages = _number(held, "messages")
	capacity = _number(held, "bytes") // messages
	timeout  = _number(held, "timeout")
	marks    = (capacity + 7) // 8
	unit     = getattr(getattr(_attr(held, "unit"), "value", None), "name",
	                   None) or "ms"
	piece    = _path(held, "piece").split(".")
	struct   = resolved.structs[held.param.type_name]
	view_cls = class_name(struct)
	sealed   = next((entry.placement for entry in struct.entries
	                 if entry.placement.path == f"{struct.name}.{piece[1]}"
	                 and entry.placement.kind == "sealed"), None)
	if sealed is not None:
		kind  = f"::situ::{view_cls}::{c_name(piece[1])}_gate"
		root  = f"{param}_view"
		keyed = (_key(layout.request, param, resolved, held.param.type_name)
		         if layout.packed else
		         _key_bytes(layout.request, param, resolved,
		                    layout.total_bytes, held.param.type_name))
		head  = ([] if any(f"{param}_view" in line for line in keyed)
		         else [f"\t\tconst ::situ::{view_cls} {root} = {param}.view();"])
		walk: list[str] = []
		data = f"{param}.{c_name(piece[2])}()"
	else:
		kind  = f"::situ::{view_cls}"
		root  = param
		keyed = (_key(layout.request, param, resolved) if layout.packed
		         else _key_bytes(layout.request, param, resolved,
		                         layout.total_bytes))
		head  = []
		walk, data = _reach(resolved, held.param.type_name, root,
		                    _path(held, "piece"), "piece")
	index_walk, index = _reach(resolved, held.param.type_name, root,
	                           _path(held, "index"), "at")
	count_walk, count = _reach(resolved, held.param.type_name, root,
	                           _path(held, "count"), "of")
	name = c_name(held.name)
	key_decl = ("std::uint64_t key;" if layout.packed
	            else f"std::uint8_t  key[{layout.total_bytes}u];")
	same = ("slot_[i].key == key" if layout.packed else
	        "[&]() { for (std::uint32_t b = 0u; b < "
	        f"{layout.total_bytes}u; b++) {{ if (slot_[i].key[b] != key[b]) "
	        "return false; } return true; }()")
	keep = (["\t\t\tslot_[s].key = key;"] if layout.packed else
	        [f"\t\t\tfor (std::uint32_t b = 0u; b < {layout.total_bytes}u; b++) {{",
	         "\t\t\t\tslot_[s].key[b] = key[b];",
	         "\t\t\t}"])
	wanted = ("std::uint64_t key" if layout.packed
	          else "const std::uint8_t *key")
	match = ("slot_[s].key != key" if layout.packed else
	         "[&]() { for (std::uint32_t b = 0u; b < "
	         f"{layout.total_bytes}u; b++) {{ if (slot_[s].key[b] != key[b]) "
	         "return true; } return false; }()")
	return [
		f"/** `{held.name}`: one message from up to {messages} in flight, each",
		f" *  of at most {capacity} bytes, pieces keyed by `{held.relation}`.",
		f" *  The clock is yours, in {unit}; a message whose pieces stop is",
		f" *  dropped {timeout} after its first. C's rules (26.1123). */",
		f"class {name}_table {{",
		"public:",
		f"\tstatic constexpr std::uint32_t messages = {messages}u;",
		f"\tstatic constexpr std::uint32_t capacity = {capacity}u;",
		f"\tstatic constexpr std::uint32_t timeout  = {timeout}u;",
		"",
		f"\t{name}_table() noexcept",
		"\t{",
		"\t\tfor (std::uint32_t i = 0u; i < messages; i++) {",
		"\t\t\tslot_[i].live = false;",
		"\t\t}",
		"\t}",
		"",
		"\t/** Take one piece: `err::ok` with the whole message in `message`,",
		"\t *  valid until the next call; `err::truncated` while more is",
		"\t *  wanted (a resent piece too); `err::constraint` for a piece that",
		"\t *  cannot be; `err::bounds` where it does not fit or no slot is",
		"\t *  free. */",
		f"\t[[nodiscard]] ::situ::rt::err add(const {kind} &{param},",
		"\t\tstd::uint32_t now, ::situ::rt::const_bytes &message) noexcept",
		"\t{",
		*head,
		*keyed,
		*index_walk,
		*count_walk,
		*walk,
		f"\t\tconst std::uint32_t index = static_cast<std::uint32_t>({index});",
		f"\t\tconst std::uint32_t count = static_cast<std::uint32_t>({count});",
		f"\t\tconst ::situ::rt::bytes data = {data};",
		"\t\tconst std::uint32_t len = static_cast<std::uint32_t>(data.size());",
		"\t\tif (count == 0u || index >= count) {",
		"\t\t\treturn ::situ::rt::err::constraint;",
		"\t\t}",
		"\t\tif (count > capacity) {",
		"\t\t\treturn ::situ::rt::err::bounds;",
		"\t\t}",
		"\t\tstd::uint32_t s = messages;",
		"\t\tfor (std::uint32_t i = 0u; i < messages; i++) {",
		f"\t\t\tif (slot_[i].live && {same}) {{",
		"\t\t\t\ts = i;",
		"\t\t\t\tbreak;",
		"\t\t\t}",
		"\t\t}",
		"\t\tif (s == messages) {",
		"\t\t\tfor (std::uint32_t i = 0u; i < messages; i++) {",
		"\t\t\t\tif (!slot_[i].live) {",
		"\t\t\t\t\ts = i;",
		"\t\t\t\t\tbreak;",
		"\t\t\t\t}",
		"\t\t\t}",
		"\t\t\tif (s == messages) {",
		"\t\t\t\treturn ::situ::rt::err::bounds;",
		"\t\t\t}",
		*keep,
		"\t\t\tslot_[s].count        = count;",
		"\t\t\tslot_[s].stride       = 0u;",
		"\t\t\tslot_[s].last_len     = 0u;",
		"\t\t\tslot_[s].held         = 0u;",
		"\t\t\tslot_[s].deadline     = now + timeout;",
		"\t\t\tslot_[s].stride_known = false;",
		"\t\t\tslot_[s].last_parked  = false;",
		"\t\t\tslot_[s].live         = true;",
		f"\t\t\tfor (std::uint32_t b = 0u; b < {marks}u; b++) {{",
		"\t\t\t\tmarks_[s][b] = 0u;",
		"\t\t\t}",
		"\t\t}",
		"\t\tslot &m = slot_[s];",
		"\t\tif (count != m.count) {",
		"\t\t\treturn ::situ::rt::err::constraint;\t/* not its message's count */",
		"\t\t}",
		"\t\tif (marked(s, index)) {",
		"\t\t\treturn ::situ::rt::err::truncated;",
		"\t\t}",
		"\t\tif (index != m.count - 1u) {",
		"\t\t\tif (!m.stride_known) {",
		"\t\t\t\t/* The first piece that is not the last sets the stride. */",
		"\t\t\t\tif (len == 0u || len > capacity / m.count) {",
		"\t\t\t\t\tm.live = false;",
		"\t\t\t\t\treturn ::situ::rt::err::bounds;",
		"\t\t\t\t}",
		"\t\t\t\tif (m.last_parked && m.last_len > len) {",
		"\t\t\t\t\tm.live = false;",
		"\t\t\t\t\treturn ::situ::rt::err::constraint;",
		"\t\t\t\t}",
		"\t\t\t\tm.stride = len;",
		"\t\t\t\tm.stride_known = true;",
		"\t\t\t\tif (m.last_parked) {",
		"\t\t\t\t\tcopy(&bytes_[s][(m.count - 1u) * m.stride],",
		"\t\t\t\t\t     &bytes_[s][capacity - m.last_len], m.last_len);",
		"\t\t\t\t\tm.last_parked = false;",
		"\t\t\t\t}",
		"\t\t\t} else if (len != m.stride) {",
		"\t\t\t\treturn ::situ::rt::err::constraint;",
		"\t\t\t}",
		"\t\t\tcopy(&bytes_[s][index * m.stride], data.data(), len);",
		"\t\t} else {",
		"\t\t\tif (m.stride_known && len > m.stride) {",
		"\t\t\t\treturn ::situ::rt::err::constraint;",
		"\t\t\t}",
		"\t\t\tif (len > capacity) {",
		"\t\t\t\tm.live = false;",
		"\t\t\t\treturn ::situ::rt::err::bounds;",
		"\t\t\t}",
		"\t\t\tm.last_len = len;",
		"\t\t\tif (m.stride_known) {",
		"\t\t\t\tcopy(&bytes_[s][index * m.stride], data.data(), len);",
		"\t\t\t} else {",
		"\t\t\t\t/* Held at the end, never the stride's source. */",
		"\t\t\t\tcopy(&bytes_[s][capacity - len], data.data(), len);",
		"\t\t\t\tm.last_parked = true;",
		"\t\t\t}",
		"\t\t}",
		"\t\tmarks_[s][index / 8u] = static_cast<std::uint8_t>(",
		"\t\t\tmarks_[s][index / 8u] | (1u << (index % 8u)));",
		"\t\tm.held++;",
		"\t\tif (m.held < m.count) {",
		"\t\t\treturn ::situ::rt::err::truncated;",
		"\t\t}",
		"\t\tm.live = false;",
		"\t\tif (m.count == 1u) {",
		"\t\t\tmessage = ::situ::rt::const_bytes(&bytes_[s][capacity - m.last_len],",
		"\t\t\t                                  m.last_len);",
		"\t\t} else {",
		"\t\t\tmessage = ::situ::rt::const_bytes(bytes_[s],",
		"\t\t\t\t(m.count - 1u) * m.stride + m.last_len);",
		"\t\t}",
		"\t\treturn ::situ::rt::err::ok;",
		"\t}",
		"",
		"\t/** The pieces still wanted, as up to `cap` ranges each of at most",
		"\t *  `most`; how many. 0 where no such message is held. The key is",
		"\t *  the conversation table's `key(...)`. */",
		f"\t[[nodiscard]] std::uint32_t missing({wanted}, std::uint32_t *first,",
		"\t\tstd::uint32_t *count, std::uint32_t cap,",
		"\t\tstd::uint32_t most) const noexcept",
		"\t{",
		"\t\tfor (std::uint32_t s = 0u; s < messages; s++) {",
		f"\t\t\tif (!slot_[s].live || {match}) {{",
		"\t\t\t\tcontinue;",
		"\t\t\t}",
		"\t\t\tstd::uint32_t n = 0u;",
		"\t\t\tfor (std::uint32_t i = 0u; i < slot_[s].count && n < cap; i++) {",
		"\t\t\t\tif (marked(s, i)) {",
		"\t\t\t\t\tcontinue;",
		"\t\t\t\t}",
		"\t\t\t\tif (n > 0u && first[n - 1u] + count[n - 1u] == i",
		"\t\t\t\t    && count[n - 1u] < most) {",
		"\t\t\t\t\tcount[n - 1u]++;",
		"\t\t\t\t} else {",
		"\t\t\t\t\tfirst[n] = i;",
		"\t\t\t\t\tcount[n] = 1u;",
		"\t\t\t\t\tn++;",
		"\t\t\t\t}",
		"\t\t\t}",
		"\t\t\treturn n;",
		"\t\t}",
		"\t\treturn 0u;",
		"\t}",
		"",
		f"\t/** Drop what is {timeout} past its first piece, by the wrapping",
		"\t *  clock; how many went. */",
		"\tstd::uint32_t expire(std::uint32_t now) noexcept",
		"\t{",
		"\t\tstd::uint32_t gone = 0u;",
		"\t\tfor (std::uint32_t s = 0u; s < messages; s++) {",
		"\t\t\tif (slot_[s].live",
		"\t\t\t    && static_cast<std::int32_t>(now - slot_[s].deadline) >= 0) {",
		"\t\t\t\tslot_[s].live = false;",
		"\t\t\t\tgone++;",
		"\t\t\t}",
		"\t\t}",
		"\t\treturn gone;",
		"\t}",
		"",
		"private:",
		"\tstruct slot {",
		f"\t\t{key_decl}",
		"\t\tstd::uint32_t count;",
		"\t\tstd::uint32_t stride;",
		"\t\tstd::uint32_t last_len;",
		"\t\tstd::uint32_t held;",
		"\t\tstd::uint32_t deadline;",
		"\t\tbool          live;",
		"\t\tbool          stride_known;",
		"\t\tbool          last_parked;",
		"\t};",
		"",
		"\t[[nodiscard]] bool marked(std::uint32_t s, std::uint32_t i)"
		" const noexcept",
		"\t{",
		"\t\treturn ((marks_[s][i / 8u] >> (i % 8u)) & 1u) != 0u;",
		"\t}",
		"",
		"\t/* Overlap-safe, as `memmove`: a parked last piece moves within",
		"\t * its own slot. Byte loops, the runtime being freestanding. */",
		"\tstatic void copy(std::uint8_t *to, const std::uint8_t *from,",
		"\t                 std::uint32_t len) noexcept",
		"\t{",
		"\t\tif (to < from) {",
		"\t\t\tfor (std::uint32_t i = 0u; i < len; i++) {",
		"\t\t\t\tto[i] = from[i];",
		"\t\t\t}",
		"\t\t} else {",
		"\t\t\tfor (std::uint32_t i = len; i > 0u; i--) {",
		"\t\t\t\tto[i - 1u] = from[i - 1u];",
		"\t\t\t}",
		"\t\t}",
		"\t}",
		"",
		"\tslot         slot_[messages];",
		"\tstd::uint8_t bytes_[messages][capacity];",
		f"\tstd::uint8_t marks_[messages][{marks}u];",
		"};",
		"",
	]


def generate(schema: ast.Schema, resolved: ResolvedSchema,
		basename: str) -> dict[str, str]:
	refused = {name for name, _ in refusals(schema, resolved)}
	ready = [held for held in schema.reassemblies() if held.name not in refused]
	if not ready:
		return {}
	guard = f"SITU_{c_name(basename).upper()}_REASSEMBLE_HPP"
	lines = [
		f"/* Generated by situc {__version__} from {basename}.situ -- do not edit.",
		" *",
		" * Reassembly (26.1123): one message from many datagrams. C's rules,",
		" * said in situc/codegen/c/reassemble.py; nothing allocates.",
		" */",
		"",
		f"#ifndef {guard}",
		f"#define {guard}",
		"",
		"#include <cstdint>",
		"",
		f'#include "{basename}.hpp"',
		"",
		"namespace situ {",
		"",
	]
	for held in ready:
		lines += _table(schema, held, resolved)
	lines += ["}  /* namespace situ */", "", f"#endif /* {guard} */"]
	return {f"{basename}_reassemble.hpp": "\n".join(lines) + "\n"}
