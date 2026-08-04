"""`situc gen-dissector` -- a Wireshark dissector, in Lua.

Section 20.3 puts it plainly: debugging an encrypted protocol without a
dissector is painful, and the payoff is large. The argument for generating one
is the argument for generating anything else here -- a hand-written dissector
is a third description of the layout, after the schema and the accessors, and
the one nobody updates. This one is rendered from the same placements the
accessors come from.

Lua rather than C, because a Lua dissector is a file a user drops in a plugins
directory. A C one has to be compiled against the Wireshark headers of whatever
version they happen to be running, which is a build problem in exchange for
speed nobody debugging a protocol needs.

What it emits, per struct:

  * a `ProtoField` for every member, with the bitmask where the member is
    bit-packed and the value string where its type is an enum
  * a dissector function that walks the members in order and adds each to the
    tree, in a subtree of its own where the member is a nested struct
  * for a variable-length member, an offset computed at run time from the field
    that sizes it -- the same arithmetic the C offset functions do

Byte order is per field, which Lua supports directly: `add` reads big endian
and `add_le` little. A `native` field has no fixed answer here, because the
capture and the machine reading it are different machines; those are added as
bytes with a note rather than guessed at.
"""

from __future__ import annotations

import re

from situc import ast
from situc.layout import BITS_PER_BYTE, Placement
from situc.names import (
	expand_calls, lua_spelling, over_fields, render_delimiter,
	translate_operators,
)
from situc.resolve import ResolvedSchema, ResolvedStruct
from situc.traverse import (
	arm_members, byte_span, container_bits, data_sized, element_bytes,
	extent_parts, is_counted_run, local_name, own_members,
)

#: Widths Wireshark has a `ProtoField.uintN` for. Unlike C it has a 24-bit one,
#: so a three-byte scalar is read whole here and bit-assembled there.
FIELD_WIDTHS = (8, 16, 24, 32, 64)

def generate(schema: ast.Schema, resolved: ResolvedSchema,
		basename: str) -> str:
	"""The whole schema as a dissector.

	There is no `prefix` here, unlike the C backends: Lua names come from the
	struct names, and Wireshark abbrevs are already namespaced by the protocol
	they hang off.
	"""
	_CONSTS.clear()
	_CONSTS.update(resolved.layout.env.consts)

	lines = _preamble(basename)

	# Only where something scans. A helper nobody calls is dead Lua in a file
	# a user is expected to read before trusting it.
	if any(entry.placement.delimiter is not None
	       for struct in resolved.structs.values()
	       for entry in struct.entries):
		lines.extend(SCAN_HELPER)

	lines.extend(READ_HELPER)

	# Same rule: only where a number is written as digits.
	if any(entry.placement.radix is not None
	       for struct in resolved.structs.values()
	       for entry in struct.entries):
		lines.extend(DIGITS_HELPER)

	values = _value_strings(schema, resolved)
	if values:
		lines.extend(values)

	lines.extend(_extent_functions(resolved))

	roots = [name for name in sorted(resolved.structs)
	         if resolved.structs[name].layout.register is None]

	for name in roots:
		lines.extend(_proto(resolved, resolved.structs[name], basename))

	skipped = [name for name in sorted(resolved.structs)
	           if resolved.structs[name].layout.register is not None]
	if skipped:
		lines.extend(["",
		              "-- Not dissected: " + ", ".join(skipped) + ".",
		              "-- A register is a bus transaction, not something that "
		              "appears on a wire."])

	lines.extend(_registration(resolved, roots))
	return "\n".join(lines).rstrip() + "\n"


READ_HELPER = [
	"-- One field's bytes as a number, or zero where they are not all there.",
	"--",
	"-- Wireshark raises on a range past the end of the capture, and an extent",
	"-- function reads a length field of a record that a truncated packet may",
	"-- not carry -- so the walk over a short frame died in the dissector",
	"-- rather than showing what it had. Zero is what the C accessors answer",
	"-- for a member the frame does not reach (26.51).",
	"local function situ_uint(tvb, at, width, little)",
	"\tif at < 0 or at + width > tvb:len() then",
	"\t\treturn 0",
	"\tend",
	"\tif little then",
	"\t\treturn tvb(at, width):le_uint()",
	"\tend",
	"\treturn tvb(at, width):uint()",
	"end",
	"",
]


DIGITS_HELPER = [
	"-- A number written as digits (situ section 8.6.2), never negative.",
	"--",
	"-- `tonumber` reads a leading minus and situ has no sign to read: four",
	"-- bytes of \"-26\" gave a length of -26 here and an offset of -48, which",
	"-- is an error out of Wireshark rather than a short field.",
	"--",
	"-- A named helper rather than `math.max`, because the builtin expansion",
	"-- rewrites `max` wherever it finds one -- including in what this",
	"-- returns, which came out as `math.math.max` (invariant 53).",
	"local function situ_digits(text, base)",
	"\tlocal value = tonumber(text, base) or 0",
	"\tif value < 0 then",
	"\t\treturn 0",
	"\tend",
	"\treturn value",
	"end",
	"",
]


SCAN_HELPER = [
	"-- Where a delimited member stops (situ section 8.6.1).",
	"--",
	"-- Returns the content length, or the distance to `limit` when the",
	"-- delimiter is not there -- the same two answers the generated",
	"-- accessors give, so a capture and a parser disagree about nothing.",
	"local function situ_scan(tvb, at, delim, limit)",
	"\tlocal n = #delim",
	"\tif n == 0 or at + n > limit then",
	"\t\treturn limit - at",
	"\tend",
	"\tfor i = at, limit - n do",
	"\t\tlocal match = true",
	"\t\tfor j = 0, n - 1 do",
	"\t\t\tif tvb(i + j, 1):uint() ~= delim[j + 1] then",
	"\t\t\t\tmatch = false",
	"\t\t\t\tbreak",
	"\t\t\tend",
	"\t\tend",
	"\t\tif match then",
	"\t\t\treturn i - at",
	"\t\tend",
	"\tend",
	"\treturn limit - at",
	"end",
	"",
]


def _preamble(basename: str) -> list[str]:
	return [
		f"-- Generated by situc from {basename}.situ -- do not edit.",
		"--",
		"-- A Wireshark dissector. Drop this in your plugins directory",
		"-- (Help > About > Folders > Personal Lua Plugins) and reload with",
		"-- Ctrl-Shift-L.",
		"--",
		"-- Every offset here comes from the same layout the accessors are",
		"-- generated from, so the dissector cannot drift from the parser.",
		"",
	]


# ---------------------------------------------------------------------------
# Enums become value strings
# ---------------------------------------------------------------------------


def _value_strings(schema: ast.Schema, resolved: ResolvedSchema) -> list[str]:
	"""An enum is a Wireshark value string: Wireshark shows the name, not the number."""
	lines: list[str] = []

	for decl in schema.enums():
		values = resolved.layout.env.enums.get(decl.name)
		if not values:
			continue
		pairs = ", ".join(f"[{values[member.name]}] = \"{member.name}\""
		                  for member in decl.members)
		lines.append(f"local {_lua(decl.name)}_values = {{ {pairs} }}")

	if lines:
		lines.append("")
	return lines


# ---------------------------------------------------------------------------
# One struct becomes one Proto
# ---------------------------------------------------------------------------


def _proto(resolved: ResolvedSchema, struct: ResolvedStruct,
		basename: str) -> list[str]:
	proto = _lua(struct.name)
	lines = [
		f"-- struct {struct.name}",
		f"local {proto} = Proto(\"{struct.name}\", \"{struct.name} (situ)\")",
		"",
	]

	members = own_members(struct)

	# An arm's member is not an own member -- it lives under the variant's
	# path -- so it got no `ProtoField` and the arm's bytes were shown as
	# nothing at all. Which arm is present is a run-time question; which arms
	# exist is not, so all of them are declared and the dissector picks.
	shown: list[Placement] = []
	for placement in members:
		shown.append(placement)
		if placement.kind == "variant":
			shown.extend(member for _, member in arm_members(struct, placement)
			             if member is not None)

	fields  = [_field(resolved, struct, placement) for placement in shown]
	fields  = [line for line in fields if line]

	# The table always, even when nothing goes in it. A struct whose only
	# member is a nested one shows no field of its own -- and the body still
	# names `X_f` for anything that arrives later, so declaring it costs a
	# line and its absence is a `nil` index at run time.
	lines.append(f"{proto}.fields = {{}}")
	lines.append(f"local {proto}_f = {proto}.fields")
	lines.extend(fields)
	lines.append("")

	lines.extend(_dissector(resolved, struct, members))
	lines.append("")
	return lines


def _field(resolved: ResolvedSchema, struct: ResolvedStruct,
		placement: Placement) -> str:
	"""One `ProtoField`, or "" for a member that gets a subtree instead."""
	name  = _local(struct, placement)
	abbrev = f"{struct.name}.{name}"

	# An array or a run of structs is not a nested struct, which is the order
	# `traverse.Member` puts these two questions in and the order this did
	# not: a run of them was read as one nested struct, declared no field,
	# and the body then added the name anyway -- `subtree:add(nil, ...)`, a
	# Lua error at the first packet rather than a wrong display.
	if placement.type_name in resolved.structs \
			and placement.array_count is None and not data_sized(placement):
		return ""			# a nested struct: its own Proto dissects it

	# Before the array branch, which it looks exactly like from here: the
	# bracket of `hex u32 ino[8]` is a width in bytes and not a count, and
	# reading it as a count declared a field of eight `u32`s -- thirty-two
	# bytes for an eight-byte number, overlapping everything after it. Shown
	# as a string, which is what the bytes are: an analyst reading a cpio
	# header wants to see "070701".
	if placement.radix is not None and placement.delimiter is None:
		return (f"{_lua(struct.name)}_f.{_lua(name)} = "
		        f"ProtoField.string(\"{abbrev}\", \"{name}\")")

	if data_sized(placement) or placement.array_count is not None:
		return (f"{_lua(struct.name)}_f.{_lua(name)} = "
		        f"ProtoField.bytes(\"{abbrev}\", \"{name}\")")

	# A varint and a `coded` or `sealed` region have no scalar, and the
	# dissector body adds both -- so returning "" here declared no field for
	# a member the body then indexed. `subtree:add(nil, ...)` is not a wrong
	# display, it is a Lua error at the first packet, and the three schemas
	# it hit could not be dissected at all (26.35).
	#
	# `bytes` rather than a number: a varint's bytes are not the value it
	# spells, and a region's are the transform's output. Showing them as an
	# integer would be a number nobody wrote.
	if placement.varint is not None or placement.kind in ("coded", "sealed"):
		return (f"{_lua(struct.name)}_f.{_lua(name)} = "
		        f"ProtoField.bytes(\"{abbrev}\", \"{name}\")")

	# Host order: the bytes are what this dissector can honestly show, since
	# the capture does not say which machine wrote them.
	if _host_order(placement):
		return (f"{_lua(struct.name)}_f.{_lua(name)} = "
		        f"ProtoField.bytes(\"{abbrev}\", \"{name}\")")

	scalar = placement.scalar
	if scalar is None:
		return ""

	width = container_bits(placement, FIELD_WIDTHS)
	if width is None and _dynamic_width(placement) is not None:
		# Same reason as `_member_body`: a `u16` after a variable member is
		# still a `u16`. It was declared `ProtoField.bytes` and shown as two
		# hex bytes rather than the number it is.
		width = next((one for one in FIELD_WIDTHS
		              if placement.size_bits <= one), None)
	if width is None:
		return (f"{_lua(struct.name)}_f.{_lua(name)} = "
		        f"ProtoField.bytes(\"{abbrev}\", \"{name}\")")

	kind = ("int" if scalar.signed else "uint") + str(width)

	# Wireshark has no BCD display, but hex is the right one anyway: the
	# nibbles of a packed decimal field read as the digits they spell, so
	# 0x12345678 is the number a reader wants to see. Decimal would show the
	# integer the bytes happen to be, which is not a number anybody wrote.
	base = "base.HEX" if scalar.is_bcd else "base.DEC"
	args = [f"\"{abbrev}\"", f"\"{name}\"", base]

	enum = placement.type_name if placement.type_name in _enum_names(resolved) else None
	mask = _bitmask(placement, width)

	if enum is not None:
		args.append(f"{_lua(enum)}_values")
	elif mask is not None:
		args.append("nil")

	if mask is not None:
		args.append(f"{mask:#x}")

	return (f"{_lua(struct.name)}_f.{_lua(name)} = "
	        f"ProtoField.{kind}({', '.join(args)})")


def _host_order_inside(struct: ResolvedStruct) -> bool:
	"""Whether any of this struct's own scalars is host order.

	Asked of a run's element type: if the length that drives the walk is one
	the capture cannot decide, the run cannot be walked, and saying *that* is
	different from saying the elements have no size (invariant 18).
	"""
	return any(_host_order(entry.placement) for entry in struct.entries)


def _unreadable(struct: ResolvedStruct, path: str | None) -> str:
	"""Why a field an expression names cannot be read here.

	Host order is a fact about the format; anything else is a limit of this
	backend. Telling a reader the second when the first is true sends them
	looking for a better dissector generator.
	"""
	held = next((entry.placement for entry in struct.entries
	             if entry.placement.name == path), None)
	if held is not None and _host_order(held):
		return (f"`{path}` is `endian native`, and the capture does not record"
		        " which machine wrote it")
	return "its discriminant is not one this dissector can read"


def _host_order(placement: Placement) -> bool:
	"""Whether this member's byte order is the *sending* machine's (8.3).

	The one thing a capture does not record. Everything else here is decided
	by the schema; `endian native` is decided by whichever machine wrote the
	bytes, and the machine reading the capture is a different one -- so there
	is no answer to guess at and the number would be wrong half the time.

	The module docstring has said "added as bytes with a note rather than
	guessed at" since this backend was written, and the code guessed: the
	order test was `is LITTLE`, so a native field was read big-endian in
	silence. A promise nobody checked, which is section 0's rule 6 again.
	"""
	return placement.endian is ast.Endian.NATIVE


def _enum_names(resolved: ResolvedSchema) -> frozenset[str]:
	return frozenset(resolved.layout.env.enums)


def _bitmask(placement: Placement, width: int) -> int | None:
	"""Which bits of the container the field owns, or None if it owns all of them.

	Derived here from the offset and the declared bit order rather than taken
	from the emitter, for the same reason the conformance checks derive theirs:
	two descriptions that share a derivation agree even when both are wrong.
	"""
	if placement.size_bits == width and placement.offset_bits is not None \
			and placement.offset_bits % BITS_PER_BYTE == 0:
		return None

	if placement.offset_bits is None:
		# A member the data places. Only a bit field needs a mask, and one
		# cannot follow a dynamically sized member -- section 8.2's solver
		# refuses it, because a bit phase across a dynamic boundary is not
		# something it computes. So there is no mask to derive, rather than a
		# mask this cannot derive.
		return None
	first = placement.offset_bits // BITS_PER_BYTE
	skip  = placement.offset_bits - first * BITS_PER_BYTE
	span  = width

	if placement.bit_order is ast.BitOrder.LSB_FIRST:
		shift = skip
	else:
		shift = span - skip - placement.size_bits

	return ((1 << placement.size_bits) - 1) << shift


# ---------------------------------------------------------------------------
# The dissector function
# ---------------------------------------------------------------------------


def _dissector(resolved: ResolvedSchema, struct: ResolvedStruct,
		members: list[Placement]) -> list[str]:
	proto  = _lua(struct.name)
	layout = struct.layout

	lines = [
		f"function {proto}.dissector(tvb, pinfo, tree)",
		f"\tif tvb:len() < {layout.size_bytes} then",
		"\t\treturn 0",
		"\tend",
		"",
		f"\tpinfo.cols.protocol = \"{struct.name}\"",
		f"\tlocal subtree = tree:add({proto}, tvb())",
		"\tlocal at = 0",
		"",
	]

	for placement in members:
		lines.extend(_member(resolved, struct, placement))

	lines.extend(["", "\treturn at", "end"])
	return lines


def _member(resolved: ResolvedSchema, struct: ResolvedStruct,
		placement: Placement) -> list[str]:
	"""Add one member to the tree, and advance `at` past it.

	A versioned member is wrapped in the guard its accessors have. Without it
	the dissector read `tvb(3, 4)` on a three-byte v1 message and Wireshark
	showed the packet as malformed -- blaming the capture for the schema.
	"""
	lines = _member_body(resolved, struct, placement)
	guard = _version_guard(struct, placement)
	if guard is None:
		return lines

	return [f"\t-- present from version {placement.since}", f"\t{guard}",
	        *[f"\t{line}" for line in lines], "\tend"]


def _reaches(placement: Placement) -> str | None:
	"""`tvb:len() >= N`, where N is the byte after this member.

	The version guard says the *version* admits the member. Whether the
	capture is long enough to hold it is a second question, and a truncated
	v3 message answered the first and not the second: `tvb(3, 4)` on three
	bytes is a Lua error, and Wireshark shows the packet as malformed --
	blaming the capture for a message that simply stops early. Which is what
	the comment above this function already said about the *version*, one
	question short.
	"""
	span = byte_span(placement)
	if span is None:
		return None
	first, count = span
	return f"tvb:len() >= {first + count}"


def _version_guard(struct: ResolvedStruct, placement: Placement) -> str | None:
	"""`if <version> >= N then`, reading the version where the accessors do."""
	if placement.since is None or placement.version_field is None:
		return None

	field = next((entry.placement for entry in struct.entries
	              if entry.placement.name == placement.version_field), None)
	if field is None or field.offset_bits is None or field.scalar is None:
		return None

	span = byte_span(field)
	if span is None:
		return None

	if _host_order(field):
		return None		# no answer a capture can give

	first, count = span
	read  = "le_uint" if field.endian is ast.Endian.LITTLE else "uint"
	test  = f"tvb({first}, {count}):{read}() >= {placement.since}"
	holds = _reaches(placement)
	if holds is not None:
		test = f"{holds} and {test}"
	return f"if {test} then"


def _member_body(resolved: ResolvedSchema, struct: ResolvedStruct,
		placement: Placement) -> list[str]:
	name  = _local(struct, placement)
	field = f"{_lua(struct.name)}_f.{_lua(name)}"

	# A static offset is assigned; a dynamic one is already in `at` from the
	# member before it, and `at = at` is noise.
	start = placement.offset_bits
	at    = "at" if start is None else str(start // BITS_PER_BYTE)
	seek  = [] if start is None else [f"\tat = {at}"]

	# Before the nested-struct case. A run of records names a struct type and
	# is not one, and this used to reach the delimiter branch only because a
	# delimited member carried `array_count = 1` -- so the check below failed
	# for the wrong reason and the right thing happened by accident. Removing
	# that lie from the solver is what exposed it.
	if placement.delimiter is not None:
		return _delimited(resolved, struct, placement, field, seek)

	if placement.kind == "variant":
		return _variant(resolved, struct, placement, seek)

	if placement.repeat_while is not None:
		return _run(resolved, struct, placement, field, seek)

	# A nested struct gets its own dissector and its own subtree.
	if placement.type_name in resolved.structs and placement.array_count is None \
			and placement.sized_by is None:
		nested = resolved.structs[placement.type_name]

		# A variable-size struct was handed `size_bytes`, which is its
		# *minimum* -- so a whole DNS name was dissected as its first byte and
		# every member after it placed on top of the rest. Its extent is a
		# question the bytes answer, and now one this file can ask.
		size = str(nested.layout.size_bytes)
		if not nested.layout.is_fixed_size:
			if not _extent_terms(resolved, nested):
				return [f"\t-- {placement.path}: one `{placement.type_name}`"
				        " has no extent this dissector can compute"]
			size = "size"

		lines = [f"\t-- {placement.path}", *seek]
		if size == "size":
			lines.append(f"\tlocal size = {_extent_name(nested)}(tvb, at)")
		lines.extend([
			f"\tif tvb:len() >= at + {size} then",
			f"\t\tDissector.get(\"{placement.type_name}\"):call("
			f"tvb(at, {size}):tvb(), pinfo, subtree)",
			"\tend",
			f"\tat = at + {size}",
		])
		return lines

	# Before `_repeated`, for the reason `_field` is: the bracket is a width.
	if placement.radix is not None:
		span = byte_span(placement)
		if span is not None:
			first, count = span
			return [
				f"\tsubtree:add({field}, tvb({first}, {count}))",
				f"\tat = {first + count}",
			]

	# `data_sized` rather than `sized_by`, which holds a path and holds nothing
	# for `body[n + 1]`. Both asked the question themselves and both missed the
	# arithmetic form, so a run the data sizes was shown at its *minimum* --
	# one byte for `[n + 1]` -- and everything after it was placed on top of
	# its bytes. Silently: the note above the member still said "after a member
	# the data sizes", which is the one part of the output that was right.
	if data_sized(placement) or placement.array_count is not None:
		return _repeated(resolved, struct, placement, field, seek)

	add  = "add_le" if placement.endian is ast.Endian.LITTLE else "add"
	span = byte_span(placement)

	# The note the module docstring has promised since this backend was
	# written. `add` on a `ProtoField.bytes` shows the bytes whichever way it
	# is called, so the order argument is moot here -- what matters is that
	# the reader is told why they are looking at bytes rather than a number.
	note = ([f"\t-- {placement.path}: `endian native`. The capture does not"
	         " record which",
	         "\t-- machine wrote these, so they are shown as bytes rather"
	         " than guessed at."]
	        if _host_order(placement) else [])

	if span is None:
		# `byte_span` is None for a dynamic offset, and *where* it sits is the
		# only thing unknown -- how wide it is was never in doubt. Reported as
		# "no bytes of its own", which is a strange thing to say about a u16.
		width = _dynamic_width(placement)
		if width is None:
			# ...and where the *length* is an expression too, it is still a
			# run of bytes and the walk still has to step over it. A cpio
			# entry's padding is `align_up(110 + namesize, 4) - (110 +
			# namesize)`, and reporting "no bytes of its own" left `at` three
			# bytes short of the next entry for the whole archive.
			# Base "0": the fields this length reads are members of *this*
			# struct, at offsets from its own start, which is where the tvb
			# begins. `at` is the running cursor and would read the driver
			# from wherever the walk happens to have got to.
			length = _length(resolved, struct, placement, "0")
			if length is None:
				return [f"\t-- {placement.path}: no bytes of its own"]
			name = _lua(_local(struct, placement))
			return [
				f"\t-- {placement.path}: a length the data decides",
				*seek,
				f"\tlocal {name}_n = {length}",
				# The declared length is the message's, so the frame need not
				# hold it. Advancing by it anyway walked a UDP header's cursor
				# sixteen bytes past an eight-byte packet -- and `consumed`
				# is what a caller chains dissectors on.
				f"\tif tvb:len() >= at + {name}_n then",
				f"\t\tsubtree:add({field}, tvb(at, {name}_n))",
				f"\t\tat = at + {name}_n",
				"\telse",
				"\t\tat = tvb:len()",
				"\tend",
			]
		return [
			f"\t-- {placement.path}: after a member the data sizes",
			*note,
			*seek,
			f"\tif tvb:len() >= at + {width} then",
			f"\t\tsubtree:{add}({field}, tvb(at, {width}))",
			"\tend",
			f"\tat = at + {width}",
		]

	first, count = span
	return [
		*note,
		f"\tsubtree:{add}({field}, tvb({first}, {count}))",
		f"\tat = {first + count}",
	]


def _dynamic_width(placement: Placement) -> int | None:
	"""A fixed-width member's bytes, where its offset is not fixed."""
	if placement.scalar is None or placement.array_count is not None:
		return None
	if not placement.is_fixed_size or placement.size_bits % BITS_PER_BYTE:
		return None
	return placement.size_bits // BITS_PER_BYTE



def _at(base: str, offset: int) -> str:
	"""`base + offset`, without the halves that are zero."""
	if base == "0":
		return str(offset)
	return base if offset == 0 else f"{base} + {offset}"


def _read(placement: Placement, base: str) -> str | None:
	"""Reading one field's value in Lua, from `base`.

	Arithmetic rather than a bit library. Wireshark ships Lua BitOp in most
	builds and not in all of them, and a dissector that fails to load is worse
	than one that divides: these are field widths, not hot code.
	"""
	if placement.scalar is None or placement.offset_bits is None:
		return None

	width = placement.size_bits
	off   = placement.offset_bits

	if off % BITS_PER_BYTE == 0 and width % BITS_PER_BYTE == 0:
		first = off // BITS_PER_BYTE
		count = width // BITS_PER_BYTE

		# Digits are parsed, not loaded. `tvb(94, 8):uint()` over eight ASCII
		# characters is a number nobody wrote -- and Wireshark's `uint` tops
		# out at four bytes, so for a cpio header it is an error rather than a
		# wrong answer. Lua's own `tonumber` takes the base.
		if placement.radix is not None:
			# ...and never negative. Lua's `tonumber` takes a leading minus,
			# so four bytes reading "-26" gave a length of -26, an offset of
			# -48, and `tvb(-48, 2)` -- an arithmetic-on-nil out of the
			# harness rather than a short field. A text number in situ is
			# digits (8.6.2); the compiled backends have no sign to read and
			# this had one.
			return (f"situ_digits(tvb({_at(base, first)}, {count}):string(),"
			        f" {placement.radix})")

		if count > 4:
			return None		# `uint` tops out at four bytes; `uint64` is a
					# different type and no length field needs it
		if _host_order(placement):
			return None		# no answer a capture can give
		little = "true" if placement.endian is ast.Endian.LITTLE else "false"
		return f"situ_uint(tvb, {_at(base, first)}, {count}, {little})"

	# A bit field, and only one that sits inside a single byte: a straddling
	# one is refused rather than guessed at, and no length field straddles.
	position = placement.bit_position
	if position is None or position.straddles:
		return None

	byte = f"situ_uint(tvb, {_at(base, position.byte)}, 1, false)"
	if position.shift:
		byte = f"math.floor({byte} / {1 << position.shift})"
	return f"({byte} % {1 << position.width})"


#: Every `const` the schema declares, so an expression naming one folds rather
#: than reaching Lua as a global. Set once per `generate`, because a dissector
#: is rendered for one schema at a time and threading it through nine call
#: sites would be nine parameters for one dictionary.
_CONSTS: dict[str, int] = {}


#: Operators the schema has and Lua either spells differently or does not have,
#: on the oldest Lua this backend targets (decision 0021: Wireshark bundles 5.2
#: in older builds, and a dissector that fails to load is worse than one that
#: divides).
#:
#:   `/`   Lua divides in floating point. `tvb(at, 2.5)` is not a short read,
#:         it is `bad argument #1 to 'format'` at the first packet.
#:   `^`   is exponentiation, not exclusive or. `(units ^ 1) + 1` is a number
#:         Lua computes happily and nobody meant -- the one silently wrong
#:         answer in this list.
#:   `<< >> & |`  arrived in Lua 5.3. This file reads bit-packed fields with
#:         arithmetic for that reason; an expression is no different.
#:
#: 26.37 recorded that no schema in this repository has a bare `/`. It was true
#: of `examples/`, and `tests/schemas/edges.situ` has one in every operator on
#: this list -- which is what it is for. Nothing reached them, because until
#: now every member sized by arithmetic was declined for a different reason.
#: `&&` and `||` are not on it: `translate_operators` renders those as `and`
#: and `or`, and every `while` condition in this repository has one -- the
#: first version of this matched the halves and declined every run walk in the
#: file.
_UNSPELLABLE = re.compile(r"/|\^|<<|>>|(?<!&)&(?!&)|(?<!\|)\|(?!\|)")


def _over_fields(struct: ResolvedStruct, source: str, base: str) -> str | None:
	"""A schema expression rewritten as Lua reads over `base`.

	`align_up` comes out as `math.floor` arithmetic rather than as `//`, for
	decision 0021's reason: Wireshark bundles Lua 5.2 in older builds, `//`
	is a syntax error there, and a dissector that fails to load is worse than
	one that divides. A bare `/` in a schema expression is the one place this
	file still parts from the compiled backends -- Lua's is float division,
	theirs truncates -- and no schema in this repository has one (26.37).
	"""
	reads: dict[str, str] = {}
	for entry in struct.entries:
		held = entry.placement
		if held.scalar is None:
			continue
		# Nested scalars too: a cpio entry's padding is an expression over
		# `header.namesize`, and stopping at the dot left the dotted path in
		# the emitted Lua as a global that does not exist.
		local = held.path[len(struct.name) + 1:]
		if "." in local and held.offset_bits is None:
			continue
		one = _read(held, base)
		if one is not None:
			reads[local] = one

	for name, value in _CONSTS.items():
		reads.setdefault(name, str(value))

	if not reads:
		return None

	# Every name the expression uses has to be one of them. `over_fields`
	# substitutes what it is given and leaves the rest alone, so a field this
	# backend declines to read -- a host-order length, say -- came out as a
	# bare identifier and Lua saw a global: `attempt to perform arithmetic on
	# a nil value (global 'nla_len')`, at the first packet. Declining the
	# whole expression is the honest answer, and every caller already handles
	# it.
	for entry in struct.entries:
		local = entry.placement.path[len(struct.name) + 1:]
		if entry.placement.scalar is None or local in reads:
			continue
		if re.search(rf"\b{re.escape(local)}\b", source):
			return None

	if _UNSPELLABLE.search(source):
		return None
	try:
		return expand_calls(
			over_fields(sorted(reads), source, lambda name: reads[name]),
			lua_spelling)
	except KeyError:
		return None		# names something this cannot read


def _extent_functions(resolved: ResolvedSchema) -> list[str]:
	"""Every measurable struct's extent, in an order Lua accepts.

	`local function` binds where it is written, so one calling another has to
	come after it. Containment is acyclic -- a struct cannot contain itself --
	so a struct is emitted once everything it names has been.
	"""
	pending = dict(sorted(resolved.structs.items()))
	lines: list[str] = []
	done: set[str] = set()

	while pending:
		ready = [name for name, struct in pending.items()
		         if all(held.type_name in done
		                or held.type_name not in resolved.structs
		                for held in own_members(struct))]
		if not ready:
			break			# a cycle the resolver should have refused
		for name in ready:
			struct = pending.pop(name)
			# The run's walk first: the struct's own extent sums it, and the
			# walk needs the element's extent, which is already emitted
			# because the element is a struct this one names.
			for placement in own_members(struct):
				lines.extend(_run_span(resolved, struct, placement))
			lines.extend(_extent_function(resolved, struct))
			done.add(name)
	return lines


def _span_name(struct: ResolvedStruct, placement: Placement) -> str:
	return f"{_lua(struct.name)}_{_lua(_local(struct, placement))}_span"


def _run_span(resolved: ResolvedSchema, struct: ResolvedStruct,
		placement: Placement) -> list[str]:
	"""How far a `while` run reaches, as a Lua function.

	A loop, not an expression: how many elements there are is whichever one
	first fails the condition, so the only way to know where the run ends is
	to walk it. The same shape the C backend emits, and bounded the same two
	ways -- by the buffer, and by refusing to advance on a zero-extent
	element.
	"""
	counted = is_counted_run(resolved.structs, placement)
	if placement.repeat_while is None and not counted:
		return []

	element = resolved.structs.get(placement.type_name or "")
	if element is None or not _extent_terms(resolved, element):
		return []

	# A run the message *counts*, whose elements each say how long they are.
	# There is no stride to multiply, so the only way to know where it ends is
	# the same walk -- with the count as the stopping rule rather than a
	# condition. Without it this fell through to the length branch below and
	# measured a run of `n` records as `n` bytes: the wrong-answer half of what
	# 26.46 found in C, in the artifact that has no compiler to catch it.
	if counted:
		return _counted_span(resolved, struct, placement, element)

	# Not None past the `counted` return above: a run is one or the other, and
	# `wellformed` refuses a member that says twice where its run ends.
	assert placement.repeat_while is not None
	if _over_fields(element, placement.repeat_while, "last") is None:
		return []
	condition = _run_condition(resolved, struct, placement)

	name = _span_name(struct, placement)
	cap  = placement.repeat_cap
	guard = "" if cap is None else f" and n < {cap}"
	return [
		"",
		f"-- How far `{placement.path}` reaches from `at`.",
		f"-- The run ends after the element for which `{placement.repeat_while}`",
		"-- is false -- that element is part of it, the condition being asked",
		"-- once it has been read.",
		f"local function {name}(tvb, at, at_struct)",
		"	local _ = at_struct",
		"	local start = at",
		"	local n = 0",
		f"	while at < tvb:len(){guard} do",
		f"		local size = {_extent_name(element)}(tvb, at)",
		"		if size == 0 or at + size > tvb:len() then break end",
		"		local last = at",
		"		n = n + 1",
		"		at = at + size",
		f"		if not ({condition}) then break end",
		"	end",
		"	return at - start",
		"end",
	]


def _extent_name(struct: ResolvedStruct) -> str:
	return f"{_lua(struct.name)}_extent"


def _counted_span(resolved: ResolvedSchema, struct: ResolvedStruct,
		placement: Placement, element: ResolvedStruct) -> list[str]:
	"""How far a counted run of variable records reaches, as a Lua function.

	The four backends' `_span_from` in Lua: walk until the count runs out,
	and stop early on a zero-length element or one the frame does not hold --
	the two bounds every generated walk carries (invariant 24).
	"""
	count = (_over_fields(struct, placement.size_expr, "at_struct")
	         if placement.size_expr is not None
	         else _over_fields(struct, placement.sized_by or "", "at_struct"))
	if count is None:
		return []

	name = _span_name(struct, placement)
	return [
		"",
		f"-- How far `{placement.path}` reaches from `at`.",
		"-- The count says how many, and each element says how long it is, so",
		"-- the run is walked rather than multiplied.",
		f"local function {name}(tvb, at, at_struct)",
		"	local start = at",
		"	local n = 0",
		f"	while n < ({count}) and at < tvb:len() do",
		f"		local size = {_extent_name(element)}(tvb, at)",
		"		if size == 0 or at + size > tvb:len() then break end",
		"		at = at + size",
		"		n = n + 1",
		"	end",
		"	return at - start",
		"end",
	]


def _extent_function(resolved: ResolvedSchema,
		struct: ResolvedStruct) -> list[str]:
	"""How many bytes one instance occupies, as a Lua function.

	The same question `situ_<s>_extent` answers in C, and the same parts:
	`traverse.extent_parts` says what to add up, and only the reads are Lua's.
	Emitted as a function rather than inlined because a run calls it once per
	element, which is exactly what the C backend does and for the same reason.
	"""
	terms = _extent_terms(resolved, struct)
	if terms is None:
		return []

	constant, parts = terms
	body = " + ".join([str(constant), *parts]) if parts else str(constant)
	return [
		"",
		f"-- How many bytes one `{struct.name}` occupies at `at`.",
		f"local function {_extent_name(struct)}(tvb, at)",
		f"	return {body}",
		"end",
	]


def _extent_terms(resolved: ResolvedSchema,
		struct: ResolvedStruct) -> tuple[int, list[str]] | None:
	parts = extent_parts(resolved.structs, struct)
	if parts is None:
		return None

	constant, variable = parts
	terms: list[str] = []
	for placement in variable:
		one = _length(resolved, struct, placement)
		if one is None:
			return None
		terms.append(one)
	return constant, terms


def _length(resolved: ResolvedSchema, struct: ResolvedStruct,
		placement: Placement, base: str = "at") -> str | None:
	"""How many bytes one variable member occupies, in Lua.

	`base` is where the *struct* starts. In an extent function that is `at`,
	which is what the function is handed; in a dissector it is 0, because the
	member is dissected from a tvb of its own and `at` has already walked past
	the fields the length is read from. Getting that wrong read the
	discriminant from the byte after itself.
	"""
	if placement.kind == "variant":
		return _variant_length(resolved, struct, placement, base)

	if placement.repeat_while is not None \
			or is_counted_run(resolved.structs, placement):
		# Before the nested-struct case below, which named the element's
		# extent and so measured a run of labels as one label.
		if not _run_span(resolved, struct, placement):
			return None
		# From where the *run* starts, not where the struct does. Every run in
		# this repository is its struct's first member, so the two coincided
		# and the walk was handed the struct's base -- which for a run at any
		# other offset walks the members before it as though they were
		# elements. A composed schema put one at offset 3 and said so.
		if placement.offset_bits is None:
			return None		# and where the run itself moves, decline
		if placement.offset_bits % BITS_PER_BYTE:
			return None
		start = _at(base, placement.offset_bits // BITS_PER_BYTE)
		return f"{_span_name(struct, placement)}(tvb, {start}, {base})"

	if placement.size_expr is not None:
		rendered = _over_fields(struct, placement.size_expr, base)
		each     = element_bytes(placement)
		return None if rendered is None else (
			rendered if each == 1 else f"({rendered}) * {each}")

	if placement.sized_by is not None and placement.sized_by != "remaining":
		count = _over_fields(struct, placement.sized_by, base)
		if count is None:
			return None
		width = _element_bytes(resolved, placement)
		return None if width is None else (
			count if width == 1 else f"({count}) * {width}")

	nested = resolved.structs.get(placement.type_name or "")
	if nested is not None and not nested.layout.is_fixed_size \
			and placement.array_count is None and placement.delimiter is None:
		return f"{_extent_name(nested)}(tvb, {base})"

	if placement.is_fixed_size and placement.size_bits % BITS_PER_BYTE == 0:
		return str(placement.size_bits // BITS_PER_BYTE)
	return None


def _variant_length(resolved: ResolvedSchema, struct: ResolvedStruct,
		placement: Placement, base: str = "at") -> str | None:
	"""The selected arm's length, as nested Lua `and`/`or`.

	Lua has no conditional expression, and `a and b or c` is the idiom -- safe
	here because every `b` is a length, and a length is never `false` or `nil`
	even when it is zero.
	"""
	if not placement.arm_cases or placement.discriminant is None:
		return None

	held = _over_fields(struct, placement.discriminant, base)
	if held is None:
		return None

	chain = "0"
	for arm, member in reversed(arm_members(struct, placement)):
		if member is None:
			continue		# `default: error`; falls to the zero above
		one = _length(resolved, struct, member, base)
		if one is None:
			return None
		chain = one if arm.value is None \
			else f"(({held}) == {arm.value} and {one} or {chain})"
	return chain


def _variant(resolved: ResolvedSchema, struct: ResolvedStruct,
		placement: Placement, seek: list[str]) -> list[str]:
	"""The arm the discriminant selects, and only that one.

	It showed nothing at all before -- `no bytes of its own` -- so a reader
	saw the discriminant and not the bytes it discriminates, which is the half
	that matters. Every arm has a `ProtoField`; which one is added is a
	question about the packet.
	"""
	held = _over_fields(struct, placement.discriminant or "", "0")
	if held is None:
		return [f"\t-- {placement.path}: {_unreadable(struct, placement.discriminant)}"]

	lines = [f"\t-- {placement.path}: whichever arm"
	         f" `{placement.discriminant}` selects", *seek,
	         f"\tlocal arm = {held}"]

	first = True
	for arm, member in arm_members(struct, placement):
		if member is None:
			continue		# `default: error`; nothing to show
		length = _length(resolved, struct, member, "0")
		if length is None:
			return [f"\t-- {placement.path}: one arm has no length this"
			        " dissector can compute"]

		name  = _lua(_local(struct, member))
		field = f"{_lua(struct.name)}_f.{name}"
		test  = ("else" if arm.value is None
		         else f"{'if' if first else 'elseif'} arm == {arm.value} then")
		lines.extend([
			f"\t{test}" if arm.value is None else f"\t{test}",
			f"\t\tlocal n = {length}",
			"\t\tif n > 0 and tvb:len() >= at + n then",
			f"\t\t\tsubtree:add({field}, tvb(at, n))",
			"\t\tend",
			"\t\tat = at + n",
		])
		first = False

	if first:
		return [f"\t-- {placement.path}: every arm selects nothing"]

	lines.append("\tend")
	return lines


def _run(resolved: ResolvedSchema, struct: ResolvedStruct,
		placement: Placement, field: str, seek: list[str]) -> list[str]:
	"""A `while` run, walked and dissected element by element.

	It showed nothing before -- `elements of no fixed size` -- because the
	elements have no *fixed* size, which is a different thing from having no
	size. Each one is measured and handed to its own dissector, which is the
	whole reason a record run is worth generating one for.
	"""
	element = resolved.structs.get(placement.type_name or "")
	if element is not None and _host_order_inside(element):
		return [f"\t-- {placement.path}: an element's length is `endian"
		        " native`, and the",
		        "\t-- capture does not record which machine wrote it."]
	if element is None or not _run_span(resolved, struct, placement):
		return [f"\t-- {placement.path}: elements of no size this dissector"
		        " can compute"]

	cap   = placement.repeat_cap
	guard = "" if cap is None else f" and n < {cap}"
	return [
		f"\t-- {placement.path}: walked, not indexed", *seek,
		"\tlocal n = 0",
		f"\twhile at < tvb:len(){guard} do",
		f"\t\tlocal size = {_extent_name(element)}(tvb, at)",
		"\t\tif size == 0 or at + size > tvb:len() then break end",
		"\t\tlocal last = at",
		"\t\tn = n + 1",
		f"\t\tDissector.get(\"{placement.type_name}\"):call("
		"tvb(at, size):tvb(), pinfo, subtree)",
		"\t\tat = at + size",
		f"\t\tif not ({_run_condition(resolved, struct, placement)}) then"
		" break end",
		"\tend",
	]


def _run_condition(resolved: ResolvedSchema, struct: ResolvedStruct,
		placement: Placement) -> str:
	element = resolved.structs[placement.type_name or ""]
	source  = _over_fields(element, placement.repeat_while or "", "last")
	assert source is not None, "_run_span checks this first"
	return translate_operators(source, conj=" and ", disj=" or ",
	                           ne=" ~= ", neg="not ")


def _delimited(resolved: ResolvedSchema, struct: ResolvedStruct,
		placement: Placement, field: str, seek: list[str]) -> list[str]:
	"""A member that ends at a delimiter (section 8.6.1).

	The scan is emitted rather than the length, because there is no length:
	Wireshark has the same bytes situ does and has to look for the same thing
	in them. A run of records is not unrolled here -- the terminator ends the
	run and each element is dissected by its own dissector -- so this handles
	the byte case and says so for the other.
	"""
	name  = _lua(_local(struct, placement))
	delim = placement.delimiter
	assert delim is not None

	if placement.type_name in resolved.structs:
		return [
			f"\t-- {placement.path}: a run of {placement.type_name} to "
			f"{render_delimiter(delim)}.",
			"\t-- Not unrolled: the terminator ends the run rather than each",
			"\t-- element, so where it stops is a walk this does not do.",
		]

	bytes_ = ", ".join(str(byte) for byte in delim)
	cap    = ("tvb:len()" if placement.delimiter_cap is None
	          else f"math.min(at + {placement.delimiter_cap}, tvb:len())")

	return [
		f"\t-- {placement.path}, to the first {render_delimiter(delim)}",
		*seek,
		f"\tlocal {name}_len = situ_scan(tvb, at, {{{bytes_}}}, {cap})",
		f"\tif {name}_len > 0 then",
		f"\t\tsubtree:add({field}, tvb(at, {name}_len))",
		"\tend",
		f"\tat = at + {name}_len",
		f"\tif at + {len(delim)} <= tvb:len() then",
		f"\t\tat = at + {len(delim)}",
		"\tend",
	]


def _repeated(resolved: ResolvedSchema, struct: ResolvedStruct,
		placement: Placement, field: str, seek: list[str]) -> list[str]:
	"""An array, or a member whose length the data decides.

	Where the elements are a struct this loops and calls their dissector, which
	is the whole reason to have one: a repeated record shown as a run of bytes
	tells a reader nothing they could not get from the hex pane.
	"""
	name    = _lua(_local(struct, placement))
	element = resolved.structs.get(placement.type_name or "")

	count = _count_expression(resolved, struct, placement)
	if count is None:
		# Whichever way the length is written. It said "sized by `None`" for
		# every member sized by arithmetic, `sized_by` holding a path and
		# holding nothing for those -- a note naming the field it could not
		# read, naming instead the fact that it had not looked.
		written = placement.size_expr or placement.sized_by
		return [
			f"\t-- {placement.path}: sized by `{written}`, which this",
			"\t-- dissector cannot locate; the rest of the frame is shown raw",
			*seek,
			"\tif tvb:len() > at then",
			f"\t\tsubtree:add({field}, tvb(at))",
			"\tend",
			"\tat = tvb:len()",
		]

	if placement.sized_by == "remaining":
		return [
			f"\t-- {placement.path}: to the end of the frame",
			*seek,
			"\tif tvb:len() > at then",
			f"\t\tsubtree:add({field}, tvb(at))",
			"\tend",
			"\tat = tvb:len()",
		]

	each = 1 if placement.kind == "opaque" else _element_bytes(resolved, placement)
	if each is None:
		return [f"\t-- {placement.path}: elements of no fixed size"]

	lines = [f"\t-- {placement.path}", *seek, f"\tlocal {name}_n = {count}"]

	if element is None:
		# Scalar elements: one range, shown as bytes.
		scale = "" if each == 1 else f" * {each}"
		lines.extend([
			f"\tlocal {name}_len = {name}_n{scale}",
			f"\tif tvb:len() >= at + {name}_len then",
			f"\t\tsubtree:add({field}, tvb(at, {name}_len))",
			"\tend",
			# Clamped, because the length is the message's claim and not the
			# frame's: the `if` above already declines to *show* bytes that
			# are not there, and the advance below went on to count them
			# anyway. A dissector returns what it consumed, so an eight-byte
			# UDP header declaring a length of 24 reported consuming 24 --
			# and the run walking it is the same shape.
			f"\tat = math.min(at + {name}_len, tvb:len())",
		])
		return lines

	lines.extend([
		f"\tfor i = 0, {name}_n - 1 do",
		f"\t\tif tvb:len() < at + {each} then break end",
		f"\t\tDissector.get(\"{placement.type_name}\"):call("
		f"tvb(at, {each}):tvb(), pinfo, subtree)",
		f"\t\tat = at + {each}",
		"\tend",
	])
	return lines


def _count_expression(resolved: ResolvedSchema, struct: ResolvedStruct,
		placement: Placement) -> str | None:
	"""How many elements there are, as Lua.

	A static count is a literal. A dynamic one is read back out of the buffer
	the same way the generated C reads it -- from a field parsed earlier, which
	is therefore at a static offset.
	"""
	if placement.array_count is not None:
		return str(placement.array_count)
	if placement.sized_by == "remaining":
		return "remaining"

	# An arithmetic size. `sized_by` holds a path and holds nothing for it, so
	# the lookup below found no driver and the member was shown as "sized by
	# `None`, which this dissector cannot locate" -- for `body[n + 1]`, whose
	# driver is `n` and is right there.
	if placement.size_expr is not None:
		# `"0"`, matching the absolute offset the driver read below uses: a
		# length field precedes the member it sizes, so it is at a constant
		# offset from the struct, and the struct a dissector dissects starts
		# at the frame.
		return _over_fields(struct, placement.size_expr, "0")

	driver = resolved.find(f"{struct.name}.{placement.sized_by}")
	if driver is None or driver.placement.offset_bits is None:
		return None
	if driver.placement.offset_bits % BITS_PER_BYTE:
		return None

	if _host_order(driver.placement):
		return None		# no answer a capture can give

	byte  = driver.placement.offset_bits // BITS_PER_BYTE
	width = driver.placement.size_bits // BITS_PER_BYTE

	# A driver written as digits is parsed, not loaded. `_read` above says the
	# same thing for the same reason; this is the copy that decides how long a
	# *run* is, and it read a cpio name length as an eight-byte integer --
	# which Wireshark's `uint` refuses outright, above four.
	if driver.placement.radix is not None:
		# ...and never negative, for the reason `_read` gives: a minus sign is
		# a number to `tonumber` and is not a digit to anything else here.
		return (f"situ_digits(tvb({byte}, {width}):string(),"
		        f" {driver.placement.radix})")

	read  = "le_uint" if driver.placement.endian is ast.Endian.LITTLE else "uint"
	return f"tvb({byte}, {width}):{read}()"


def _element_bytes(resolved: ResolvedSchema, placement: Placement) -> int | None:
	element = resolved.structs.get(placement.type_name or "")
	if element is not None:
		return int(element.layout.size_bytes) if element.layout.is_fixed_size else None
	if placement.scalar is not None and placement.scalar.bits % BITS_PER_BYTE == 0:
		return max(placement.scalar.bits // BITS_PER_BYTE, 1)
	return None


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def _outermost(resolved: ResolvedSchema, names: list[str]) -> str | None:
	"""The struct nothing else contains, which is the one on the wire.

	`record` is not the protocol; `message` is. Suggesting the wrong one in the
	registration example sends a reader to bind a dissector for a struct that
	only ever appears inside another.
	"""
	contained = {
		entry.placement.type_name
		for name in names
		for entry in resolved.structs[name].entries
		if entry.placement.type_name in resolved.structs
	}
	free = [name for name in names if name not in contained]
	if not free:
		return names[-1] if names else None

	# Among the free ones, the largest: a schema can describe several
	# independent messages, and the biggest is the likeliest to be the point.
	return max(free, key=lambda name: resolved.structs[name].layout.size_bytes)


def _registration(resolved: ResolvedSchema, names: list[str]) -> list[str]:
	"""How a user reaches the dissector.

	Nothing here knows which UDP port or EtherType the protocol lives on --
	that is not in the schema and guessing it would be wrong. So the dissectors
	register by name, which makes them available to "Decode As" and to any
	other dissector that calls them, and the file says how to bind one.
	"""
	outermost = _outermost(resolved, names)
	if outermost is None:
		return []

	return [
		"",
		"-- The schema does not say which port or EtherType this protocol runs",
		"-- on, so nothing is bound automatically. Either use Decode As..., or",
		"-- bind it here, for example:",
		"--",
		f"--     DissectorTable.get(\"udp.port\"):add(9999, {_lua(outermost)})",
	]


# ---------------------------------------------------------------------------


def _local(struct: ResolvedStruct, placement: Placement) -> str:
	"""The member's name, with the synthetic brackets a reserved field carries
	stripped: `<reserved0>` is not a name Wireshark accepts in an abbrev."""
	return local_name(struct, placement).strip("<>")


def _lua(name: str) -> str:
	"""A Lua identifier. `::` from a namespace is the only thing that appears
	in a situ name and not in a Lua one."""
	return "".join(char if char.isalnum() or char == "_" else "_" for char in name)
