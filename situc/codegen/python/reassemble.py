"""One message from many datagrams, in Python: `<basename>_reassemble.py`.

C's table and C's rules (`situc/codegen/c/reassemble.py` says why each is
there), in Python's shape: `add` answers the whole message as `bytes` or
None while more is wanted, and raises where C returns a refusal.
"""

from __future__ import annotations

from situc import __version__, ast
from situc.codegen.c.reassemble import (WHY, _attr, _number, _path,
                                        _relation, per_cap,
                                        refusals)
from situc.codegen.python.converse import _key, _key_bytes
from situc.codegen.python.emit import py_name
from situc.relation import KeyLayout, key_layout
from situc.resolve import ResolvedSchema

__all__ = ["generate"]


def _read(param: str, path: str) -> str:
	"""`f.h.index` reads as written: a nested member is a view."""
	return ".".join([param, *(py_name(part) for part in path.split(".")[1:])])


def _table(schema: ast.Schema, held: ast.Reassemble,
		resolved: ResolvedSchema) -> list[str]:
	param    = held.param.name
	layout   = key_layout(_relation(schema, held), resolved)
	messages = _number(held, "messages")
	capacity = _number(held, "bytes") // messages
	timeout  = _number(held, "timeout")
	unit     = getattr(getattr(_attr(held, "unit"), "value", None), "name",
	                   None) or "ms"
	piece    = _path(held, "piece").split(".")
	struct   = resolved.structs[held.param.type_name]
	sealed   = next((entry.placement for entry in struct.entries
	                 if entry.placement.path == f"{struct.name}.{piece[1]}"
	                 and entry.placement.kind == "sealed"), None)
	kind = py_name(held.param.type_name)
	if sealed is not None:
		# The gate's own properties: a sealed region's interior is read
		# through the gate, and the clear head through the view it holds.
		data = f"{param}.{py_name(piece[2])}"
		view = f"{param}._view"
		kind = f"{kind}._{py_name(piece[1])}_gate"
	else:
		data = _read(param, _path(held, "piece"))
		view = param
	keyed = (_key(layout.request, view, into="raw") if layout.packed
	         else _key_bytes(layout.request, view, into="raw"))
	index = _read(view, _path(held, "index"))
	count = _read(view, _path(held, "count"))
	# A piece's own deadline (fuzznet, inbox 20261122T1804Z): its low 32
	# bits, on the table's wrapping clock, as C reads it.
	expires = (_read(view, _path(held, "expires"))
	           if _attr(held, "expires") is not None else None)
	name  = py_name(held.name)
	per      = per_cap(schema, held, resolved)
	per_most = per[2] if per is not None else None
	return [
		"",
		f"class {name}_table:",
		f'\t"""`{held.name}`: one message from up to {messages} in flight, each',
		f"\tof at most {capacity} bytes, pieces keyed by `{held.relation}`. The",
		f"\tclock is yours, in {unit}; a message whose pieces stop is dropped",
		f"\t{timeout} after its first. The rules are C's (26.1123).",
		'\t"""',
		"",
		f"\tMESSAGES = {messages}",
		f"\tCAPACITY = {capacity}",
		f"\tTIMEOUT  = {timeout}",
		*([f"\tPER_MAX  = {per_most}"] if per_most is not None else []),
		"\t# Why `add` refused, in `why` (fuzznet, 2026-11-22): C's numbers.",
		*[f"\tWHY_{why} = {number}" for number, (why, _) in enumerate(WHY)],
		"",
		"\t__slots__ = (\"_live\", \"why\", \"messages\", \"capacity\", \"timeout\""
		+ (", \"per_max\")" if per_most is not None else ")"),
		"",
		"\tdef __init__(self, messages: int | None = None,",
		"\t\t\tcapacity: int | None = None, timeout: int | None = None"
		+ (",\n\t\t\tper_max: int | None = None" if per_most is not None else "")
		+ ") -> None:",
		'\t\t"""The schema\'s bounds, or smaller ones (fuzznet, inbox',
		"\t\t20261122T1804Z, as C's `_init_in`); BoundsError for zero or one",
		'\t\tabove the schema\'s."""',
		"\t\tself.messages = self.MESSAGES if messages is None else messages",
		"\t\tself.capacity = self.CAPACITY if capacity is None else capacity",
		"\t\tself.timeout  = self.TIMEOUT if timeout is None else timeout",
		*(["\t\tself.per_max  = self.PER_MAX if per_max is None else per_max"]
		  if per_most is not None else []),
		"\t\tfor said, most in ((self.messages, self.MESSAGES),",
		*(["\t\t                   (self.per_max, self.PER_MAX),"]
		  if per_most is not None else []),
		"\t\t                   (self.capacity, self.CAPACITY),",
		"\t\t                   (self.timeout, self.TIMEOUT)):",
		"\t\t\tif not 0 < said <= most:",
		"\t\t\t\traise BoundsError(\"a bound is zero or past the schema's\", most)",
		"\t\tself._live: dict[object, _message] = {}",
		"\t\tself.why = self.WHY_NONE",
		"",
		f"\tdef add(self, {param}: {kind}, now: int) -> bytes | None:",
		'\t\t"""Take one piece: the whole message where it completes one,',
		"",
		"\t\tNone while more is wanted (a resent piece too). Raises",
		"\t\tConstraintError for a piece that cannot be, BoundsError where",
		'\t\tit does not fit or no slot is free; `why` says which."""',
		"\t\tself.why = self.WHY_NONE",
		*keyed,
		f"\t\tindex = int({index})",
		f"\t\tcount = int({count})",
		f"\t\tdata = bytes({data})",
		*([f"\t\tdeadline = int({expires}) & 0xFFFFFFFF"] if expires else []),
		"\t\treturn self.add_parts(raw, index, count, data, now"
		+ (", deadline)" if expires else ")"),
		"",
		f"\tdef add_parts(self, raw: {'int' if layout.packed else 'bytes'},"
		" index: int, count: int,",
		"\t\t\tdata: bytes, now: int"
		+ (", deadline: int" if expires else "") + ") -> bytes | None:",
		'\t\t"""`add` from parts the caller has already read and verified: the',
		"\t\tkey as the conversation table's `key(...)` builds it, the piece's",
		"\t\tindex, count and bytes" + (", its own deadline" if expires else "")
		+ " (fuzznet, inbox",
		'\t\t20261122T1804Z). `add` reads these and calls it."""',
		"\t\tself.why = self.WHY_NONE",
		"\t\tif count == 0 or index >= count:",
		"\t\t\tself.why = self.WHY_INDEX",
		"\t\t\traise ConstraintError(\"a piece's index is past its count\")",
		"\t\tif count > self.capacity:",
		"\t\t\tself.why = self.WHY_TOO_LARGE",
		"\t\t\traise BoundsError(\"more pieces than a slot holds bytes\", self.capacity)",
		*(["\t\tdeadline &= 0xFFFFFFFF",
		   "\t\tif ((now - deadline) & 0xFFFFFFFF) < 0x80000000:",
		   "\t\t\tself.why = self.WHY_EXPIRED",
		"\t\t\traise BoundsError(\"the piece is past its own deadline\","
		   " deadline)"] if expires else []),
		"\t\tm = self._live.get(raw)",
		"\t\tif m is None:",
		*_per_lines(schema, held, resolved, layout),
		"\t\t\tif len(self._live) >= self.messages:",
		"\t\t\t\tself.why = self.WHY_FULL",
		"\t\t\t\traise BoundsError(\"every slot is taken\", self.messages)",
		"\t\t\tm = _message(count, now + self.timeout)",
		"\t\t\tself._live[raw] = m",
		*(["\t\tif ((deadline - m.deadline) & 0xFFFFFFFF) >= 0x80000000:",
		   "\t\t\tm.deadline = deadline"] if expires else []),
		"\t\tif count != m.count:",
		"\t\t\tself.why = self.WHY_COUNT",
		"\t\t\traise ConstraintError(\"a piece's count is not its message's\")",
		"\t\tif index in m.pieces:",
		"\t\t\t# The same bytes are a resend; others a rewrite of what was",
		"\t\t\t# taken, refused so the caller sees it. The first are kept.",
		"\t\t\tif m.pieces[index] != data:",
		"\t\t\t\tself.why = self.WHY_CONFLICT",
		"\t\t\t\traise ConstraintError(\"a piece held already, with other bytes\")",
		"\t\t\treturn None",
		"\t\tlast = index == m.count - 1",
		"\t\tif not last:",
		"\t\t\tif m.stride is None:",
		"\t\t\t\t# The first piece that is not the last sets the stride.",
		"\t\t\t\tif not data or len(data) > self.capacity // m.count:",
		"\t\t\t\t\tdel self._live[raw]",
		"\t\t\t\t\tself.why = self.WHY_TOO_LARGE if data else self.WHY_EMPTY",
		"\t\t\t\t\traise BoundsError(\"the message does not fit a slot\",",
		"\t\t\t\t\t                  self.capacity)",
		"\t\t\t\tif m.last is not None and len(m.last) > len(data):",
		"\t\t\t\t\tdel self._live[raw]",
		"\t\t\t\t\tself.why = self.WHY_LAST_LONGER",
		"\t\t\t\t\traise ConstraintError(\"the last piece is longer than the stride\")",
		"\t\t\t\tm.stride = len(data)",
		"\t\t\telif len(data) != m.stride:",
		"\t\t\t\tself.why = self.WHY_STRIDE",
		"\t\t\t\traise ConstraintError(\"a piece not the last is not the stride\")",
		"\t\telse:",
		"\t\t\tif m.stride is not None and len(data) > m.stride:",
		"\t\t\t\tself.why = self.WHY_LAST_LONGER",
		"\t\t\t\traise ConstraintError(\"the last piece is longer than the stride\")",
		"\t\t\tif len(data) > self.capacity:",
		"\t\t\t\tdel self._live[raw]",
		"\t\t\t\tself.why = self.WHY_TOO_LARGE",
		"\t\t\t\traise BoundsError(\"the message does not fit a slot\",",
		"\t\t\t\t                  self.capacity)",
		"\t\t\tm.last = data",
		"\t\tm.pieces[index] = data",
		"\t\tif len(m.pieces) < m.count:",
		"\t\t\treturn None",
		"\t\tdel self._live[raw]",
		"\t\treturn b\"\".join(m.pieces[i] for i in range(m.count))",
		"",
		f"\tdef missing(self, key: {'int' if layout.packed else 'bytes'}, cap: int,"
		" most: int) -> list[tuple[int, int]]:",
		'\t\t"""The pieces still wanted, as up to `cap` (first, count) ranges',
		"",
		"\t\teach of at most `most`; the key is the conversation table's",
		'\t\t`key(...)`. Empty where no such message is held."""',
		"\t\tm = self._live.get(key)",
		"\t\tif m is None:",
		"\t\t\treturn []",
		"\t\tfound: list[tuple[int, int]] = []",
		"\t\tfor i in range(m.count):",
		"\t\t\tif i in m.pieces:",
		"\t\t\t\tcontinue",
		"\t\t\tif found and found[-1][0] + found[-1][1] == i \\",
		"\t\t\t\t\tand found[-1][1] < most:",
		"\t\t\t\tfound[-1] = (found[-1][0], found[-1][1] + 1)",
		"\t\t\telif len(found) < cap:",
		"\t\t\t\tfound.append((i, 1))",
		"\t\t\telse:",
		"\t\t\t\tbreak",
		"\t\treturn found",
		"",
		f"\tdef forget(self, key: {'int' if layout.packed else 'bytes'}) -> int:",
		'\t\t"""Drop the message held under `key`, as a caller does for an',
		"\t\texchange it has abandoned (fuzznet, inbox 20261122T1804Z); 1",
		'\t\twhere one was held, 0 where none was."""',
		"\t\treturn 1 if self._live.pop(key, None) is not None else 0",
		"",
		"\tdef expire(self, now: int) -> int:",
		f'\t\t"""Drop what is {timeout} past its first piece; how many went."""',
		"\t\tgone = [key for key, m in self._live.items()",
		"\t\t        if ((now - m.deadline) & 0xFFFFFFFF) < 0x80000000]",
		"\t\tfor key in gone:",
		"\t\t\tdel self._live[key]",
		"\t\treturn len(gone)",
	]


def _per_lines(schema: ast.Schema, held: ast.Reassemble,
		resolved: ResolvedSchema, layout: KeyLayout) -> list[str]:
	"""C's `per` cap: a new message refused where one value of the key
	part already holds `per_max` (fuzznet, inbox 20261122T1804Z)."""
	cap = per_cap(schema, held, resolved)
	if cap is None:
		return []
	start, width, _ = cap
	part = (f"(({{key}} >> {start}) & {(1 << width) - 1:#x})" if layout.packed
	        else f"{{key}}[{start}:{start + width}]")
	return [
		f"\t\t\tif sum(1 for held in self._live"
		f" if {part.format(key='held')} == {part.format(key='raw')})"
		" >= self.per_max:",
		"\t\t\t\tself.why = self.WHY_PER",
		"\t\t\t\traise BoundsError(\"one sender holds as many messages as"
		" `per_max` allows\", self.per_max)",
	]


def generate(schema: ast.Schema, resolved: ResolvedSchema,
		basename: str) -> dict[str, str]:
	refused = {name for name, _ in refusals(schema, resolved)}
	ready = [held for held in schema.reassemblies() if held.name not in refused]
	if not ready:
		return {}
	lines = [
		f'"""Generated by situc {__version__} from {basename}.situ -- do not edit.',
		"",
		"Reassembly (26.1123): one message from many datagrams. The rules",
		'are C\'s, said in `situc/codegen/c/reassemble.py`."""',
		"",
		"from __future__ import annotations",
		"",
		"from situ_runtime import BoundsError, ConstraintError",
		"",
		f"from {basename} import *  # noqa: F403",
		"",
		"",
		"class _message:",
		'\t"""One message in flight: its pieces by index, and the stride the',
		'\tfirst piece not the last set."""',
		"",
		"\t__slots__ = (\"count\", \"deadline\", \"stride\", \"last\", \"pieces\")",
		"",
		"\tdef __init__(self, count: int, deadline: int) -> None:",
		"\t\tself.count = count",
		"\t\tself.deadline = deadline & 0xFFFFFFFF",
		"\t\tself.stride: int | None = None",
		"\t\tself.last: bytes | None = None",
		"\t\tself.pieces: dict[int, bytes] = {}",
	]
	for held in ready:
		lines += ["", *_table(schema, held, resolved)]
	return {f"{basename}_reassemble.py": "\n".join(lines).rstrip("\n") + "\n"}
