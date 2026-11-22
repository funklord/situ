"""One message from many datagrams, in Rust: `<basename>_reassemble.rs`.

C's table, laid out the same -- fixed arrays the caller owns, since the
runtime is `no_std` and nothing here allocates -- and C's rules, which
`situc/codegen/c/reassemble.py` explains one by one. `add` answers
`Ok(Some(message))` where a piece completes one, `Ok(None)` while more is
wanted, and an `Error` where C refuses.
"""

from __future__ import annotations

from situc import __version__, ast
from situc.codegen.c.reassemble import (_attr, _number, _path, _relation,
                                        per_cap,
                                        refusals)
from situc.codegen.rust.converse import _key, _key_bytes
from situc.codegen.rust.emit import _ident, _pascal
from situc.relation import KeyLayout, key_layout
from situc.resolve import ResolvedSchema

__all__ = ["generate"]


def _reach(root: str, path: str) -> str:
	"""`f.h.index` as `root.h()?.index()`: each nested step a view."""
	parts = path.split(".")[1:]
	reached = root
	for step in parts[:-1]:
		reached += f".{_ident(step)}()?"
	return f"{reached}.{_ident(parts[-1])}()"


def _per_lines(schema: ast.Schema, held: ast.Reassemble,
		resolved: ResolvedSchema, layout: KeyLayout) -> list[str]:
	"""C's `per` cap: a new message refused where one value of the key
	part already holds `per_max` (fuzznet, inbox 20261122T1804Z)."""
	cap = per_cap(schema, held, resolved)
	if cap is None:
		return []
	start, width, _ = cap
	if layout.packed:
		mask = f"{(1 << width) - 1:#x}u64"
		same = (f"((m.key >> {start}) & {mask}) == ((key >> {start}) & {mask})")
	else:
		same = (f"m.key[{start}..{start + width}] == "
		        f"key[{start}..{start + width}]")
	return [
		"\t\t\t\tif self.slot.iter().filter(|m| m.live && "
		f"{same}).count() >= self.per_max {{",
		"\t\t\t\t\treturn Err(Error::Bounds);",
		"\t\t\t\t}",
	]


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
	sealed   = next((entry.placement for entry in struct.entries
	                 if entry.placement.path == f"{struct.name}.{piece[1]}"
	                 and entry.placement.kind == "sealed"), None)
	view_type = _pascal(held.param.type_name)
	if sealed is not None:
		kind  = f"{_pascal(f'{held.param.type_name}_{piece[1]}_gate')}<'_>"
		data  = f"{param}.{_ident(piece[2])}()"
		head  = [f"\t\tlet {param}_view: {view_type}<'_> = {param}.view();"]
		root  = f"{param}_view"
	else:
		kind  = f"{view_type}<'_>"
		data  = _reach(param, _path(held, "piece"))
		head  = []
		root  = param
	keyed = (_key(layout.request, root, resolved) if layout.packed
	         else _key_bytes(layout.request, root, layout.total_bytes,
	                         resolved))
	key_type = "u64" if layout.packed else f"[u8; {layout.total_bytes}]"
	empty_key = "0" if layout.packed else f"[0u8; {layout.total_bytes}]"
	name = _pascal(held.name)
	per      = per_cap(schema, held, resolved)
	per_most = per[2] if per is not None else None
	return [
		f"/// One message in flight for `{held.name}`.",
		"#[derive(Clone, Copy)]",
		f"struct {name}Slot {{",
		f"\tkey: {key_type},",
		"\tcount: u32,",
		"\tstride: u32,",
		"\tlast_len: u32,",
		"\theld: u32,",
		"\tdeadline: u32,",
		"\tlive: bool,",
		"\tstride_known: bool,",
		"\tlast_parked: bool,",
		"}",
		"",
		f"/// `{held.name}`: one message from up to {messages} in flight, each",
		f"/// of at most {capacity} bytes, pieces keyed by `{held.relation}`.",
		f"/// The clock is yours, in {unit}; a message whose pieces stop is",
		f"/// dropped {timeout} after its first. C's rules (26.1123).",
		f"/// It holds every slot's buffer itself, more than {messages * capacity}",
		"/// bytes: keep it in a `static` or behind a `Box` rather than in a",
		"/// stack frame.",
		f"pub struct {name}Table {{",
		f"\tslot: [{name}Slot; {messages}],",
		f"\tbytes: [[u8; {capacity}]; {messages}],",
		f"\tmarks: [[u8; {marks}]; {messages}],",
		"\tmessages: usize,",
		"\tcapacity: usize,",
		"\ttimeout: u32,",
		*(["\tper_max: usize,"] if per_most is not None else []),
		"}",
		"",
		f"impl Default for {name}Table {{",
		"\tfn default() -> Self {",
		"\t\tSelf::new()",
		"\t}",
		"}",
		"",
		f"impl {name}Table {{",
		f"\tpub const MESSAGES: usize = {messages};",
		f"\tpub const CAPACITY: usize = {capacity};",
		f"\tpub const TIMEOUT: u32 = {timeout};",
		*([f"\tpub const PER_MAX: usize = {per_most};"]
		  if per_most is not None else []),
		"",
		"\tpub fn new() -> Self {",
		f"\t\tconst EMPTY: {name}Slot = {name}Slot {{",
		f"\t\t\tkey: {empty_key}, count: 0, stride: 0, last_len: 0,",
		"\t\t\theld: 0, deadline: 0, live: false, stride_known: false,",
		"\t\t\tlast_parked: false,",
		"\t\t};",
		"\t\tSelf {",
		f"\t\t\tslot: [EMPTY; {messages}],",
		f"\t\t\tbytes: [[0u8; {capacity}]; {messages}],",
		f"\t\t\tmarks: [[0u8; {marks}]; {messages}],",
		"\t\t\tmessages: Self::MESSAGES,",
		"\t\t\tcapacity: Self::CAPACITY,",
		"\t\t\ttimeout: Self::TIMEOUT,",
		*(["\t\t\tper_max: Self::PER_MAX,"] if per_most is not None else []),
		"\t\t}",
		"\t}",
		"",
		"\t/// Smaller bounds than the schema's (fuzznet, inbox 20261122T1804Z,",
		"\t/// as C's `_init_in`); `Bounds` for zero or one above the schema's.",
		"\tpub fn with_bounds(messages: usize, capacity: usize, timeout: u32"
		+ (", per_max: usize)" if per_most is not None else ")"),
		"\t\t-> Result<Self> {",
		"\t\tif messages == 0 || messages > Self::MESSAGES || capacity == 0",
		"\t\t\t|| capacity > Self::CAPACITY || timeout == 0",
		*(["\t\t\t|| per_max == 0 || per_max > Self::PER_MAX"]
		  if per_most is not None else []),
		"\t\t\t|| timeout > Self::TIMEOUT {",
		"\t\t\treturn Err(Error::Bounds);",
		"\t\t}",
		"\t\tlet mut t = Self::new();",
		"\t\tt.messages = messages;",
		"\t\tt.capacity = capacity;",
		"\t\tt.timeout = timeout;",
		*(["\t\tt.per_max = per_max;"] if per_most is not None else []),
		"\t\tOk(t)",
		"\t}",
		"",
		"\tfn marked(&self, s: usize, i: u32) -> bool {",
		"\t\t(self.marks[s][(i / 8) as usize] >> (i % 8)) & 1 != 0",
		"\t}",
		"",
		"\t/// Take one piece: `Some` of the whole message where it",
		"\t/// completes one, borrowed until the next call; `None` while more",
		"\t/// is wanted (a resent piece too). `Constraint` for a piece that",
		"\t/// cannot be; `Bounds` where it does not fit or no slot is free.",
		f"\tpub fn add(&mut self, {param}: &{kind}, now: u32)"
		" -> Result<Option<&[u8]>> {",
		*head,
		*keyed,
		f"\t\tlet index = {_reach(root, _path(held, 'index'))} as u32;",
		f"\t\tlet count = {_reach(root, _path(held, 'count'))} as u32;",
		# A piece's own deadline (fuzznet, inbox 20261122T1804Z), its low
		# 32 bits on the table's wrapping clock, as C reads it.
		*([f"\t\tlet deadline = {_reach(root, _path(held, 'expires'))} as u32;"]
		  if _attr(held, "expires") is not None else []),
		f"\t\tlet data: &[u8] = {data};",
		"\t\tself.add_parts(key, index, count, data, now"
		+ (", deadline)" if _attr(held, "expires") is not None else ")"),
		"\t}",
		"",
		"\t/// `add` from parts the caller has already read and verified: the",
		"\t/// key as the conversation table's `key(...)` builds it, the",
		"\t/// piece's index, count and bytes"
		+ (", and its own deadline" if _attr(held, "expires") is not None else "")
		+ " (fuzznet, inbox",
		"\t/// 20261122T1804Z). `add` reads these and calls it.",
		f"\tpub fn add_parts(&mut self, key: {'u64' if layout.packed else key_type},"
		" index: u32, count: u32,",
		"\t\tdata: &[u8], now: u32"
		+ (", deadline: u32" if _attr(held, "expires") is not None else "")
		+ ") -> Result<Option<&[u8]>> {",
		"\t\tlet len = data.len() as u32;",
		"\t\tif count == 0 || index >= count {",
		"\t\t\treturn Err(Error::Constraint);",
		"\t\t}",
		"\t\tif count as usize > self.capacity {",
		"\t\t\treturn Err(Error::Bounds);",
		"\t\t}",
		*(["\t\tif (now.wrapping_sub(deadline) as i32) >= 0 {",
		   "\t\t\treturn Err(Error::Bounds);",
		   "\t\t}"] if _attr(held, "expires") is not None else []),
		"\t\tlet s = match self.slot.iter().position(|m| m.live && m.key == key) {",
		"\t\t\tSome(s) => s,",
		"\t\t\tNone => {",
		*_per_lines(schema, held, resolved, layout),
		"\t\t\t\tlet s = self.slot[..self.messages].iter().position(|m| !m.live)",
		"\t\t\t\t\t.ok_or(Error::Bounds)?;",
		f"\t\t\t\tself.slot[s] = {name}Slot {{",
		"\t\t\t\t\tkey, count, stride: 0, last_len: 0, held: 0,",
		"\t\t\t\t\tdeadline: now.wrapping_add(self.timeout), live: true,",
		"\t\t\t\t\tstride_known: false, last_parked: false,",
		"\t\t\t\t};",
		"\t\t\t\tself.marks[s] = [0u8; " + str(marks) + "];",
		"\t\t\t\ts",
		"\t\t\t}",
		"\t\t};",
		*(["\t\tif (deadline.wrapping_sub(self.slot[s].deadline) as i32) < 0 {",
		   "\t\t\tself.slot[s].deadline = deadline;",
		   "\t\t}"] if _attr(held, "expires") is not None else []),
		"\t\tif count != self.slot[s].count {",
		"\t\t\treturn Err(Error::Constraint); // not its message's count",
		"\t\t}",
		"\t\tif self.marked(s, index) {",
		"\t\t\treturn Ok(None);",
		"\t\t}",
		"\t\tlet cap = self.capacity as u32;",
		"\t\tlet m = self.slot[s];",
		"\t\tif index != m.count - 1 {",
		"\t\t\tif !m.stride_known {",
		"\t\t\t\t// The first piece that is not the last sets the stride.",
		"\t\t\t\tif len == 0 || len > cap / m.count {",
		"\t\t\t\t\tself.slot[s].live = false;",
		"\t\t\t\t\treturn Err(Error::Bounds);",
		"\t\t\t\t}",
		"\t\t\t\tif m.last_parked && m.last_len > len {",
		"\t\t\t\t\tself.slot[s].live = false;",
		"\t\t\t\t\treturn Err(Error::Constraint);",
		"\t\t\t\t}",
		"\t\t\t\tself.slot[s].stride = len;",
		"\t\t\t\tself.slot[s].stride_known = true;",
		"\t\t\t\tif m.last_parked {",
		"\t\t\t\t\tlet from = (cap - m.last_len) as usize;",
		"\t\t\t\t\tlet to = ((m.count - 1) * len) as usize;",
		"\t\t\t\t\tself.bytes[s].copy_within(from..from + m.last_len as usize,"
		" to);",
		"\t\t\t\t\tself.slot[s].last_parked = false;",
		"\t\t\t\t}",
		"\t\t\t} else if len != m.stride {",
		"\t\t\t\treturn Err(Error::Constraint);",
		"\t\t\t}",
		"\t\t\tlet at = (index * self.slot[s].stride) as usize;",
		"\t\t\tself.bytes[s][at..at + len as usize].copy_from_slice(data);",
		"\t\t} else {",
		"\t\t\tif m.stride_known && len > m.stride {",
		"\t\t\t\treturn Err(Error::Constraint);",
		"\t\t\t}",
		"\t\t\tif len > cap {",
		"\t\t\t\tself.slot[s].live = false;",
		"\t\t\t\treturn Err(Error::Bounds);",
		"\t\t\t}",
		"\t\t\tself.slot[s].last_len = len;",
		"\t\t\tif m.stride_known {",
		"\t\t\t\tlet at = (index * m.stride) as usize;",
		"\t\t\t\tself.bytes[s][at..at + len as usize].copy_from_slice(data);",
		"\t\t\t} else {",
		"\t\t\t\t// Held at the end, never the stride's source.",
		"\t\t\t\tlet at = (cap - len) as usize;",
		"\t\t\t\tself.bytes[s][at..at + len as usize].copy_from_slice(data);",
		"\t\t\t\tself.slot[s].last_parked = true;",
		"\t\t\t}",
		"\t\t}",
		"\t\tself.marks[s][(index / 8) as usize] |= 1u8 << (index % 8);",
		"\t\tself.slot[s].held += 1;",
		"\t\tlet m = self.slot[s];",
		"\t\tif m.held < m.count {",
		"\t\t\treturn Ok(None);",
		"\t\t}",
		"\t\tself.slot[s].live = false;",
		"\t\tif m.count == 1 {",
		"\t\t\tlet at = (cap - m.last_len) as usize;",
		"\t\t\treturn Ok(Some(&self.bytes[s][at..at + m.last_len as usize]));",
		"\t\t}",
		"\t\tlet whole = ((m.count - 1) * m.stride + m.last_len) as usize;",
		"\t\tOk(Some(&self.bytes[s][..whole]))",
		"\t}",
		"",
		"\t/// The pieces still wanted, as up to `first.len()` ranges each of at",
		"\t/// most `most`: `first[k]`, `count[k]`. Answers how many; 0 where",
		"\t/// no such message is held. The key is the conversation table's",
		"\t/// `key(...)`.",
		f"\tpub fn missing(&self, key: {'u64' if layout.packed else '&' + key_type},"
		" first: &mut [u32],",
		"\t               count: &mut [u32], most: u32) -> usize {",
		("\t\tlet Some(s) = self.slot.iter().position(|m| m.live && m.key == key)"
		 if layout.packed else
		 "\t\tlet Some(s) = self.slot.iter().position(|m| m.live && &m.key == key)"),
		"\t\telse {",
		"\t\t\treturn 0;",
		"\t\t};",
		"\t\tlet cap = first.len().min(count.len());",
		"\t\tlet mut n = 0usize;",
		"\t\tfor i in 0..self.slot[s].count {",
		"\t\t\tif self.marked(s, i) {",
		"\t\t\t\tcontinue;",
		"\t\t\t}",
		"\t\t\tif n > 0 && first[n - 1] + count[n - 1] == i && count[n - 1] < most {",
		"\t\t\t\tcount[n - 1] += 1;",
		"\t\t\t} else if n < cap {",
		"\t\t\t\tfirst[n] = i;",
		"\t\t\t\tcount[n] = 1;",
		"\t\t\t\tn += 1;",
		"\t\t\t} else {",
		"\t\t\t\tbreak;",
		"\t\t\t}",
		"\t\t}",
		"\t\tn",
		"\t}",
		"",
		f"\t/// Drop what is {timeout} past its first piece, by the wrapping",
		"\t/// clock; how many went.",
		"\t/// Drop the message held under `key`, as a caller does for an",
		"\t/// exchange it has abandoned (fuzznet, inbox 20261122T1804Z).",
		"\t/// `true` where one was held.",
		f"\tpub fn forget(&mut self, key: {'u64' if layout.packed else '&' + key_type}) -> bool {{",
		("\t\tmatch self.slot.iter_mut().find(|m| m.live && m.key == key) {"
		 if layout.packed else
		 "\t\tmatch self.slot.iter_mut().find(|m| m.live && &m.key == key) {"),
		"\t\t\tSome(m) => {",
		"\t\t\t\tm.live = false;",
		"\t\t\t\ttrue",
		"\t\t\t}",
		"\t\t\tNone => false,",
		"\t\t}",
		"\t}",
		"",
		"\tpub fn expire(&mut self, now: u32) -> u32 {",
		"\t\tlet mut gone = 0;",
		"\t\tfor m in self.slot.iter_mut() {",
		"\t\t\tif m.live && (now.wrapping_sub(m.deadline) as i32) >= 0 {",
		"\t\t\t\tm.live = false;",
		"\t\t\t\tgone += 1;",
		"\t\t\t}",
		"\t\t}",
		"\t\tgone",
		"\t}",
		"}",
		"",
	]


def generate(schema: ast.Schema, resolved: ResolvedSchema,
		basename: str) -> dict[str, str]:
	refused = {name for name, _ in refusals(schema, resolved)}
	ready = [held for held in schema.reassemblies() if held.name not in refused]
	if not ready:
		return {}
	lines = [
		f"// Generated by situc {__version__} from {basename}.situ -- do not"
		" edit.",
		"//",
		"// Reassembly (26.1123): one message from many datagrams. C's rules,",
		"// said in situc/codegen/c/reassemble.py; nothing allocates.",
		"",
		f"use crate::{basename}::*;",
		"use crate::situ_rt::{Error, Result};",
		"",
	]
	for held in ready:
		lines += _table(schema, held, resolved)
	return {f"{basename}_reassemble.rs": "\n".join(lines)}
