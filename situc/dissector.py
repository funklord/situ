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

import contextlib
import dataclasses
from typing import Iterator

from situc import traverse
from situc import __version__

import re

from situc import ast
from situc.layout import BITS_PER_BYTE, Placement, ValueRule
from situc.relation import conversation_key
from situc.names import (
	UnknownName, expand_calls, lua_spelling, over_fields, render_delimiter,
	with_sums,
	translate_operators,
)
from situc.resolve import ResolvedSchema, ResolvedStruct
from situc.unparse import expr_to_source
from situc.traverse import (
	summed_width,
	arm_members, byte_span, container_bits, data_sized, element_bytes,
	ends_in_the_frame,
	extent_parts, fixed_span_bits, is_counted_run, is_recursive, local_name,
	nest_of,
	nests_one_struct, occupies_fixed_bytes, own_members, pinned_bytes,
	RunOrdering, run_ordering, span_bits,
	version_steps, versioned_tail,
)
from situc.traverse import messages as messages_of

#: Widths Wireshark has a `ProtoField.uintN` for. Unlike C it has a 24-bit one,
#: so a three-byte scalar is read whole here and bit-assembled there.
FIELD_WIDTHS = (8, 16, 24, 32, 64)

def _proto_name(basename: str, struct: str) -> str:
	"""What Wireshark knows a struct's `Proto` by, and finds its dissector by."""
	return re.sub(r"\W", "_", f"situ_{basename}_{struct}").lower()


#: Lua refuses a function with more than 200 locals, and a chunk is a function.
LUA_LOCALS = 200


def _within_lua_locals(text: str) -> str:
	"""A dissector with more top-level names than a Lua chunk may hold.

	Each struct's extent function and proto is a local, so a large schema met
	"too many local variables" when Wireshark loaded it, and nothing before
	that said so: `edges` reached 201 (26.899). Past the limit they go in a
	table of the chunk's own, which `_ENV` makes the scope of every name, and
	every other dissector stays byte-identical.
	"""
	lines = text.split("\n")
	if sum(line.startswith("local ") for line in lines) < LUA_LOCALS:
		return text
	out: list[str] = []
	for line in lines:
		if not line.startswith("local "):
			out.append(line)
		elif " = " in line or line.startswith("local function "):
			out.append(line[len("local "):])
	first = next(at for at, line in enumerate(out)
	             if not line.startswith("--"))
	out.insert(first, "local _ENV = setmetatable({}, { __index = _G })")
	return "\n".join(out)


def generate(schema: ast.Schema, resolved: ResolvedSchema,
		basename: str) -> str:
	"""The whole schema as a dissector.

	There is no `prefix` here, unlike the C backends: Lua names come from the
	struct names, and Wireshark abbrevs are already namespaced by the protocol
	they hang off.
	"""
	# A `[stream]` parameter reaches a dissector as a preference, which is
	# the shape 0050 names for it: a preference is per-dissector and not
	# per-packet, which is exactly the shape of a per-stream fact.
	#
	# A per-message one cannot reach a dissector at all -- nothing in a
	# capture carries it and no preference varies packet to packet -- so it
	# is refused. `wellformed.check_parameters` already refuses one that
	# MOVES a member; this refuses one that would be read at all, because
	# what would be emitted is `situ_uint(tvb, 0, 1, false)`: a parameter
	# occupies nothing, so it sits at the offset of the member after it and
	# the read measures that member's own first byte.

	_CONSTS.clear()
	_CONSTS.update(resolved.layout.env.consts)
	_ENUMS.clear()
	_ENUMS.update(resolved.layout.env.enums)
	_STRUCTS.clear()
	_STRUCTS.update(resolved.structs)
	_MARKERS.clear()
	_MARKERS.update({decl.name: decl for decl in schema.markers()})
	_CODECS.clear()
	_CODECS.update({decl.name: decl for decl in schema.codecs()})

	lines = _preamble(basename)

	# Only where something scans. A helper nobody calls is dead Lua in a file
	# a user is expected to read before trusting it.
	if any(entry.placement.delimiters
	       for struct in resolved.structs.values()
	       for entry in struct.entries):
		lines.extend(SCAN_HELPER)
	if any(entry.placement.delimiters
	       and entry.placement.type_name in resolved.structs
	       for struct in resolved.structs.values()
	       for entry in struct.entries):
		lines.extend(STANDS_HELPER)

	# Same rule as the scan above: a helper nobody calls is dead Lua in a
	# file a user is expected to read before trusting it.
	if any(entry.placement.trimmed
	       for struct in resolved.structs.values()
	       for entry in struct.entries):
		lines.extend(TRIM_HELPER)

	lines.extend(READ_HELPER)
	if any(entry.placement.skip
	       for struct in resolved.structs.values()
	       for entry in struct.entries):
		lines.extend(SKIP_HELPER)
	if any(entry.placement.kind == "tlv"
	       for struct in resolved.structs.values()
	       for entry in struct.entries):
		lines.extend(FIXED_NUMBER_HELPER)
	lines.extend(_varint_functions(schema, resolved))
	if any(_shows_value(entry.placement)
	       for struct in resolved.structs.values()
	       for entry in struct.entries):
		lines.extend(U64_HELPER)
	# After the varints, which an extent reads: an answer cached before them
	# would be one that could not read a varint.
	_TERMS.clear()
	_MEASURING.clear()
	_DEPTHS.clear()
	_DEPTHS.update({name: traverse.depth_limit(schema, name)
	                for name in resolved.structs
	                if is_recursive(resolved.structs, name)})
	_message_setup(schema, resolved)
	lines.extend(_conversation_setup(schema, resolved))

	# Same rule: only where a number is written as digits.
	if any(entry.placement.radix is not None
	       for struct in resolved.structs.values()
	       for entry in struct.entries):
		lines.extend(DIGITS_HELPER)
	if any(entry.placement.radix_padded
	       for struct in resolved.structs.values()
	       for entry in struct.entries):
		lines.extend(PADDED_HELPER)

	values = _value_strings(schema, resolved)
	if values:
		lines.extend(values)

	roots = [name for name in sorted(resolved.structs)
	         if resolved.structs[name].layout.register is None]

	# Forward-declared where a schema takes an argument, and only there.
	#
	# A `[stream]` parameter is read as `<proto>.prefs.<name>`, and an
	# extent function is emitted BEFORE the `Proto` it names -- so the read
	# would be an index of a nil global, at the first packet. A Lua local
	# declared here is the upvalue those functions close over, and the
	# assignment below fills it before anything calls one.
	#
	# Conditional so that every dissector without a parameter stays
	# byte-identical, which is a cheaper proof than re-reading forty of
	# them.
	takes_arguments = bool(traverse.parameters(schema))
	if takes_arguments:
		lines.extend(["",
		              "-- Declared before the extent functions below, which",
		              "-- read an argument off a protocol's preferences.",
		              *(f"local {_lua(name)}" for name in roots),
		              *_argument_tables(resolved, roots)])

	lines.extend(_extent_functions(resolved))

	for name in roots:
		lines.extend(_proto(resolved, resolved.structs[name], basename,
		                    declared=takes_arguments))

	skipped = [name for name in sorted(resolved.structs)
	           if resolved.structs[name].layout.register is not None]
	if skipped:
		lines.extend(["",
		              "-- Not dissected: " + ", ".join(skipped) + ".",
		              "-- A register is a bus transaction, not something that "
		              "appears on a wire."])

	lines.extend(_registration(resolved, roots))
	text = "\n".join(lines).rstrip() + "\n"
	return _within_lua_locals(re.sub(
		r'Dissector\.get\("([^"]+)"\)',
		lambda found: f'Dissector.get("{_proto_name(basename, found[1])}")',
		text))


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
	"-- `sum(run)`: the elements the frame holds, added (26.896).",
	"local function situ_sum(tvb, at, count, width, little)",
	"\tlocal total = 0",
	"\tfor i = 0, count - 1 do",
	"\t\tif at + (i + 1) * width > tvb:len() then",
	"\t\t\tbreak",
	"\t\tend",
	"\t\ttotal = total + situ_uint(tvb, at + i * width, width, little)",
	"\tend",
	"\treturn total",
	"end",
	"",
]


DIGITS_HELPER = [
	"-- A number written as digits (situ section 8.6.2), never negative.",
	"--",
	"-- `tonumber` reads what LUA calls a number and situ has its own rule,",
	"-- and the gap is wider than it looks. It was met first as a leading",
	"-- minus -- four bytes of \"-26\" gave a length of -26 here and an offset",
	"-- of -48, an error out of Wireshark rather than a short field -- and",
	"-- the clamp below was written for that one symptom. It also skips",
	"-- LEADING AND TRAILING SPACE, so `\"  12\"` read as 12 where every other",
	"-- reader answers 0: `situ_parse_uint` refuses at the first byte that is",
	"-- not a digit and the value accessor keeps the zero it started with.",
	"-- A differential draw found it, five readings to one.",
	"--",
	"-- So the digits are checked here rather than left to `tonumber`, which",
	"-- is the fix the minus should have had: one special case per symptom is",
	"-- a list that only ever grows by being bitten.",
	"--",
	"-- A named helper rather than `math.max`, because the builtin expansion",
	"-- rewrites `max` wherever it finds one -- including in what this",
	"-- returns, which came out as `math.math.max` (invariant 53).",
	"--",
	"-- Bounded like `situ_uint`, and for the same reason: an extent function",
	"-- reads a length field of a record a truncated packet may not carry, and",
	"-- slicing the tvb for the digits raised out of Wireshark before anything",
	"-- could ask how long the field was.",
	"local function situ_digits(tvb, at, width, base)",
	"\tif at < 0 or at + width > tvb:len() then",
	"\t\treturn 0",
	"\tend",
	"\tlocal text = tvb(at, width):string()",
	"\tif #text == 0 then",
	"\t\treturn 0",
	"\tend",
	"\tfor i = 1, #text do",
	"\t\tif tonumber(text:sub(i, i), base) == nil then",
	"\t\t\treturn 0",
	"\t\tend",
	"\tend",
	"\tlocal value = tonumber(text, base) or 0",
	"\tif value < 0 then",
	"\t\treturn 0",
	"\tend",
	"\treturn value",
	"end",
	"",
]


PADDED_HELPER = [
	"-- `[padded]` (situ 26.948): the digits stop at the first NUL or space,",
	"-- and every byte after them to the end of the width must be one too --",
	"-- ustar's `0000644` and a NUL. All padding is zero, as tar reads it, and",
	"-- a malformed padding is 0 as well, the value every other reader keeps.",
	"local function situ_digits_padded(tvb, at, width, base)",
	"\tif at < 0 or at + width > tvb:len() then",
	"\t\treturn 0",
	"\tend",
	"\tlocal stop = width",
	"\tfor i = 0, width - 1 do",
	"\t\tlocal byte = tvb(at + i, 1):uint()",
	"\t\tif stop == width and (byte == 0 or byte == 32) then",
	"\t\t\tstop = i",
	"\t\telseif stop < width and byte ~= 0 and byte ~= 32 then",
	"\t\t\treturn 0",
	"\t\tend",
	"\tend",
	"\treturn situ_digits(tvb, at, stop, base)",
	"end",
	"",
	"-- What such a field shows: its digits, without the padding after them,",
	"-- and `?` for any byte that is no digit -- the message's own bytes",
	"-- would otherwise reach the label as they are, control bytes included.",
	"local function situ_padded_text(tvb, at, width)",
	"\tlocal text = tvb(at, width):string()",
	"\treturn ((text:match(\"^[^\\0 ]*\")):gsub(\"[^%x]\", \"?\"))",
	"end",
	"",
]


TRIM_HELPER = [
	"-- What `[trim]` removes from a member's VALUE (situ section 8.6.2).",
	"--",
	"-- The member's SPAN is unchanged: those bytes are still its own and the",
	"-- next member starts where it ends, whatever this returns. Only what a",
	"-- caller reads back moves, which is what every generated accessor's",
	"-- `_ptr` and `_len` already do -- and what this dissector did not do at",
	"-- all, so a header value was shown with the space still on it while",
	"-- four backends and the walker showed it without.",
	"local function situ_trim(tvb, at, len, set)",
	"\tlocal function is_ws(k)",
	"\t\tlocal byte = tvb(at + k, 1):uint()",
	"\t\tfor i = 1, #set do",
	"\t\t\tif byte == set[i] then return true end",
	"\t\tend",
	"\t\treturn false",
	"\tend",
	"",
	"\tlocal first = 0",
	"\tlocal last  = len",
	"\twhile first < last and is_ws(first) do first = first + 1 end",
	"\twhile last > first and is_ws(last - 1) do last = last - 1 end",
	"\treturn at + first, last - first",
	"end",
	"",
]


SCAN_HELPER = [
	"-- Where a delimited member stops (situ section 8.6.1).",
	"--",
	"-- Three answers, because the caller needs three and this used to give",
	"-- one: the content length, whether a delimiter was actually FOUND, and",
	"-- WHICH alternative matched. A caller with only the first advanced past",
	"-- a delimiter whenever the buffer had room for one, so a member that",
	"-- ran to its cap with nothing in it put every later member a byte late.",
	"--",
	"-- `quote` and `escape` are the same rules `situ_walk_scan` applies and",
	"-- this had neither. JSON's strings carry `[escape = \"\\\\\"]`, so a",
	"-- `\\\\\" inside one is content -- and without that rule this framed",
	"-- `say \\\\\"hi\\\\\"` as five bytes where every other reader frames",
	"-- eleven, then placed everything after it on the wrong offset.",
	"--",
	"-- The alternatives are a LIST and the longest match at a position wins,",
	"-- which is the tie-break the walker makes and for its reason: `\"\\r\"`",
	"-- and `\"\\r\\n\"` can both match in one place, and taking the shorter",
	"-- leaves the newline as the next member's first byte. Scanning only the",
	"-- first alternative ran an HTTP header line ending in a bare newline",
	"-- straight through to the next CRLF.",
	"local function situ_scan(tvb, at, delims, limit, quote, escape)",
	"\tlocal quoted = false",
	"\tlocal i = 0",
	"",
	"\twhile at + i < limit do",
	"\t\tlocal byte = tvb(at + i, 1):uint()",
	"",
	"\t\tif escape and byte == escape then",
	"\t\t\ti = i + 2\t-- the next byte is content, whatever it is",
	"\t\telseif quote and byte == quote then",
	"\t\t\tquoted = not quoted",
	"\t\t\ti = i + 1",
	"\t\telse",
	"\t\t\tlocal best = 0",
	"\t\t\tif not quoted then",
	"\t\t\t\tfor d = 1, #delims do",
	"\t\t\t\t\tlocal one = delims[d]",
	"\t\t\t\t\tlocal n = #one",
	"\t\t\t\t\tif n > best and at + i + n <= limit then",
	"\t\t\t\t\t\tlocal match = true",
	"\t\t\t\t\t\tfor j = 1, n do",
	"\t\t\t\t\t\t\tif tvb(at + i + j - 1, 1):uint() ~= one[j] then",
	"\t\t\t\t\t\t\t\tmatch = false",
	"\t\t\t\t\t\t\t\tbreak",
	"\t\t\t\t\t\t\tend",
	"\t\t\t\t\t\tend",
	"\t\t\t\t\t\tif match then best = n end",
	"\t\t\t\t\tend",
	"\t\t\t\tend",
	"\t\t\tend",
	"\t\t\tif best > 0 then",
	"\t\t\t\treturn i, true, best",
	"\t\t\tend",
	"\t\t\ti = i + 1",
	"\t\tend",
	"\tend",
	"\treturn limit - at, false, 0",
	"end",
	"",
	"-- How many bytes a delimited member occupies: its content, and the",
	"-- delimiter too where it was found and the member owns it. An extent",
	"-- adds this where the row above shows `situ_scan`'s first answer.",
	"local function situ_footprint(consumed, n, found, took)",
	"\tif found and consumed then",
	"\t\treturn n + took",
	"\tend",
	"\treturn n",
	"end",
	"",
]


SKIP_HELPER = [
	"-- How many bytes of a member's lead stand at `at`: the run of bytes",
	"-- from `set` a `skip` member owns in front of its value (26.938).",
	"local function situ_skip(tvb, at, set)",
	"\tlocal n = 0",
	"\twhile at + n < tvb:len() do",
	"\t\tlocal byte = tvb(at + n, 1):uint()",
	"\t\tlocal hit = false",
	"\t\tfor i = 1, #set do",
	"\t\t\tif set[i] == byte then hit = true; break end",
	"\t\tend",
	"\t\tif not hit then break end",
	"\t\tn = n + 1",
	"\tend",
	"\treturn n",
	"end",
	"",
]


U64_HELPER = [
	"-- A varint's value as Wireshark's 64-bit number, which a `uint64` field",
	"-- requires: a Lua number is refused. Split into its two words, each",
	"-- exact whatever the value, as a multiple of 2^32 is exact as a float.",
	"local function situ_u64(v)",
	"\tlocal low = v % 4294967296",
	"\tlocal high = (v - low) / 4294967296",
	"\tif high < 0 then high = high + 4294967296 end",
	"\treturn UInt64(low, high)",
	"end",
	"",
	"-- A signed varint's value, from the zigzag form its read hands on: half",
	"-- of it, complemented where it is odd, worked in the two words so it",
	"-- stays exact, and Wireshark's signed 64-bit number of them.",
	"local function situ_s64(z)",
	"\tlocal low = z % 4294967296",
	"\tlocal high = (z - low) / 4294967296",
	"\tif high < 0 then high = high + 4294967296 end",
	"\tlocal odd = low % 2",
	"\tlow = (low - odd) / 2 + (high % 2) * 2147483648",
	"\thigh = (high - high % 2) / 2",
	"\tif odd == 1 then",
	"\t\tlow, high = 4294967295 - low, 4294967295 - high",
	"\tend",
	"\treturn Int64(low, high)",
	"end",
	"",
]


FIXED_NUMBER_HELPER = [
	"-- A tag or a length a tlv region states in whole bytes: its width, and",
	"-- its value, or 0 where the frame does not hold it.",
	"local function situ_fixed_number(tvb, at, width, little)",
	"\tif at + width > tvb:len() then return 0, 0, 0 end",
	"\tlocal v = situ_uint(tvb, at, width, little)",
	"\treturn width, v, v",
	"end",
	"",
]


STANDS_HELPER = [
	"-- The longest of a run's terminators standing at `at`, or 0 (26.930).",
	"--",
	"-- Asked only where an element would start: inside one, the same bytes",
	"-- are that element's own. Each alternative needs its own room, so an",
	"-- element in the frame's last bytes, shorter than a terminator, is",
	"-- still the run's.",
	"local function situ_stands(tvb, at, delims)",
	"\tlocal best = 0",
	"\tfor d = 1, #delims do",
	"\t\tlocal one = delims[d]",
	"\t\tlocal n = #one",
	"\t\tif n > best and at + n <= tvb:len() then",
	"\t\t\tlocal match = true",
	"\t\t\tfor j = 1, n do",
	"\t\t\t\tif tvb(at + j - 1, 1):uint() ~= one[j] then",
	"\t\t\t\t\tmatch = false",
	"\t\t\t\t\tbreak",
	"\t\t\t\tend",
	"\t\t\tend",
	"\t\t\tif match then best = n end",
	"\t\tend",
	"\tend",
	"\treturn best",
	"end",
	"",
]


#: Where a varint's VALUE stops growing here: 2^48, a length no frame holds.
#: Saturating rather than wrapping keeps a lying varint from reading as a
#: negative length, which `tvb:len() >= at + n` would let through.
VARINT_SATURATION = 2 ** 48

#: 2^64, which a varint of the four 64-bit-checked forms may not reach.
VARINT_CEILING = "18446744073709551616.0"


def _varint_name(decl: ast.VarintDecl) -> str:
	return f"situ_varint_{_lua(decl.name)}"


def _varint_function(decl: ast.VarintDecl) -> list[str]:
	"""One varint type read in Lua: its width at `at`, and its value.

	`walk.decode_varint` in Lua, one form per declaration, so the walk a
	capture gets is the walker's to the byte. 0 where the frame ends inside
	the number or the bytes are no number in this form -- the walker's
	`Refused`, which the caller answers by showing nothing more.

	Arithmetic rather than bit operators, which decision 0021 does not let
	this file assume. Three answers: the width; the value saturated at
	`VARINT_SATURATION`, for a length; and the value itself, which in Lua
	5.4 is exact modulo 2^64, for a tag whose low bits are read. Saturated
	alone, an eight-byte protobuf tag read wire type 0 where its bits say
	4, and an item was found where the walker stops (26.937). The four
	forms that refuse a number past 2^64 check that on a float beside it.
	"""
	enc  = decl.encoding
	most = decl.max_bytes
	sat  = str(VARINT_SATURATION)
	done = f"(v < 0 or v > {sat}) and {sat} or v, v"
	body: list[str] = []

	def byte(at: str) -> str:
		return f"tvb({at}, 1):uint()"

	if enc is ast.VarintEncoding.QUIC:
		body = [
			"\tlocal size = ({1, 2, 4, 8})[math.floor(b0 / 64) + 1]",
			"\tif size > room then return 0, 0, 0 end",
			"\tlocal v = b0 % 64",
			"\tfor i = 1, size - 1 do",
			f"\t\tv = v * 256 + {byte('at + i')}",
			"\tend",
			f"\treturn size, {done}",
		]
	elif enc in ast.MARKED:
		form   = ast.MARKED[enc]
		widths = ", ".join(str(one) for one in form.widths)
		loop   = "for i = 1, w do" if form.big else "for i = w, 1, -1 do"
		body = [
			f"\tlocal lead = b0 % {form.mask + 1}",
			f"\tif lead < {form.direct} then return 1, lead, lead end",
			f"\tlocal w = ({{{widths}}})[lead - {form.first} + 1]",
			"\tif w == nil or w + 1 > room then return 0, 0, 0 end",
			"\tlocal v = 0",
			f"\t{loop}",
			f"\t\tv = v * 256 + {byte('at + i')}",
			"\tend",
			f"\treturn w + 1, {done}",
		]
	elif enc in (ast.VarintEncoding.EBML, ast.VarintEncoding.EBML_ID):
		# The first set bit says the width: `9 - bit_length(b0)`. Spelled as
		# a chain over constants, so no float enters a width.
		chain = []
		for size in range(1, 9):
			top = 1 << (8 - size)
			chain.append(f"\t{'if' if size == 1 else 'elseif'} b0 >= {top}"
			             f" then size, keep = {size}, {top}")
		keep = "b0" if enc is ast.VarintEncoding.EBML_ID else "b0 % keep"
		body = [
			"\tlocal size, keep",
			*chain,
			"\telse return 0, 0, 0 end",
			"\tif size > room then return 0, 0, 0 end",
			f"\tlocal v = {keep}",
			"\tfor i = 1, size - 1 do",
			f"\t\tv = v * 256 + {byte('at + i')}",
			"\tend",
			f"\treturn size, {done}",
		]
	elif enc is ast.VarintEncoding.UTF8:
		chain = []
		for size, low in ((7, 0xFE), (6, 0xFC), (5, 0xF8), (4, 0xF0),
		                  (3, 0xE0), (2, 0xC0)):
			chain.append(f"\t{'if' if size == 7 else 'elseif'} b0 >= {low}"
			             f" then size = {size}")
		body = [
			"\tif b0 < 128 then return 1, b0, b0 end",
			"\tif b0 == 255 then return 0, 0, 0 end",
			"\tlocal size",
			*chain,
			"\telse return 0, 0, 0 end",
			"\tif size > room then return 0, 0, 0 end",
			"\tlocal v = b0 % ({32, 16, 8, 4, 2, 1})[size - 1]",
			"\tfor i = 1, size - 1 do",
			f"\t\tlocal b = {byte('at + i')}",
			"\t\tif b < 128 or b >= 192 then return 0, 0, 0 end",
			"\t\tv = v * 64 + b % 64",
			"\tend",
			f"\treturn size, {done}",
		]
	elif enc in (ast.VarintEncoding.HPACK, ast.VarintEncoding.BERTAG):
		prefix = 5 if enc is ast.VarintEncoding.BERTAG else decl.prefix
		assert prefix is not None
		mask = (1 << prefix) - 1
		grow = (["v = v * 128 + b % 128", "f = f * 128 + b % 128"]
		        if enc is ast.VarintEncoding.BERTAG else
		        ["v = v + (b % 128) * scale", "f = f + (b % 128) * fscale"])
		add  = ("" if enc is ast.VarintEncoding.BERTAG
		        else f"v = v + {mask}; f = f + {mask}; ")
		body = [
			f"\tif b0 % {mask + 1} ~= {mask} then"
			f" return 1, b0 % {mask + 1}, b0 % {mask + 1} end",
			"\tlocal v, scale, f, fscale = 0, 1, 0.0, 1.0",
			"\tfor i = 1, room - 1 do",
			f"\t\tlocal b = {byte('at + i')}",
			*(f"\t\t{line}" for line in grow),
			"\t\tscale, fscale = scale * 128, fscale * 128",
			"\t\tif b < 128 then",
			f"\t\t\t{add}if f >= {VARINT_CEILING} then return 0, 0, 0 end",
			f"\t\t\treturn i + 1, {done}",
			"\t\tend",
			f"\t\tif f >= {VARINT_CEILING} then return 0, 0, 0 end",
			"\tend",
			"\treturn 0, 0, 0",
		]
	elif enc is ast.VarintEncoding.LEBPREFIX:
		prefix = decl.prefix
		assert prefix is not None
		body = [
			f"\tlocal v, scale = b0 % {1 << prefix}, {1 << prefix}",
			f"\tlocal f, fscale = v * 1.0, {1 << prefix}.0",
			"\tif b0 < 128 then return 1, v, v end",
			"\tfor i = 1, room - 1 do",
			f"\t\tlocal b = {byte('at + i')}",
			"\t\tv, f = v + (b % 128) * scale, f + (b % 128) * fscale",
			"\t\tscale, fscale = scale * 128, fscale * 128",
			f"\t\tif f >= {VARINT_CEILING} then return 0, 0, 0 end",
			f"\t\tif b < 128 then return i + 1, {done} end",
			"\tend",
			"\treturn 0, 0, 0",
		]
	elif enc is ast.VarintEncoding.SLEB128:
		# The zigzag form, as every read of a signed varint hands it on.
		body = [
			"\tlocal v, scale = 0, 1",
			"\tfor i = 0, room - 1 do",
			f"\t\tlocal b = {byte('at + i')}",
			"\t\tlocal g = b % 128",
			"\t\tif i == 9 and g ~= 0 and g ~= 127 then return 0, 0, 0 end",
			"\t\tv = v + g * scale",
			"\t\tscale = scale * 128",
			"\t\tif b < 128 then",
			"\t\t\tif g >= 64 then v = v - scale end",
			"\t\t\tif v >= 0 then v = v * 2 else v = -v * 2 - 1 end",
			f"\t\t\treturn i + 1, {done}",
			"\t\tend",
			"\tend",
			"\treturn 0, 0, 0",
		]
	elif enc is ast.VarintEncoding.BIJECTIVE128:
		body = [
			"\tlocal v, f = 0, 0.0",
			"\tfor i = 0, room - 1 do",
			f"\t\tlocal b = {byte('at + i')}",
			"\t\tif i > 0 then",
			"\t\t\tv, f = (v + 1) * 128 + b % 128, (f + 1) * 128 + b % 128",
			"\t\telse",
			"\t\t\tv, f = b % 128, (b % 128) * 1.0",
			"\t\tend",
			f"\t\tif f >= {VARINT_CEILING} then return 0, 0, 0 end",
			f"\t\tif b < 128 then return i + 1, {done} end",
			"\tend",
			"\treturn 0, 0, 0",
		]
	else:
		big   = enc is ast.VarintEncoding.BE128
		whole = big and decl.terminal_bits == 8
		body = [
			"\tlocal v, scale = 0, 1",
			"\tfor i = 0, room - 1 do",
			f"\t\tlocal b = {byte('at + i')}",
			*([f"\t\tif i + 1 == {most} then v = v * 256 + b;"
			   f" return i + 1, {done} end"]
			  if whole else []),
			("\t\tv = v * 128 + b % 128" if big else
			 "\t\tv = v + (b % 128) * scale"),
			*([] if big else ["\t\tscale = scale * 128"]),
			f"\t\tif b < 128 then return i + 1, {done} end",
			"\tend",
			"\treturn 0, 0, 0",
		]

	return [
		"",
		f"-- `{decl.name}`, a `{enc.value}` varint: how many bytes it occupies"
		" at `at`,",
		"-- the number they spell held to a length, and that number itself.",
		"-- 0 where the frame ends inside it or the bytes are no number here.",
		f"local function {_varint_name(decl)}(tvb, at)",
		f"\tlocal room = math.min(tvb:len() - at, {most})",
		"\tif room < 1 then return 0, 0, 0 end",
		f"\tlocal b0 = {byte('at')}",
		*body,
		"end",
	]


def _varint_functions(schema: ast.Schema,
		resolved: ResolvedSchema) -> list[str]:
	"""Every varint type a member reads, as Lua."""
	named = {name
	         for struct in resolved.structs.values()
	         for entry in struct.entries
	         for name in (entry.placement.varint,
	                      entry.placement.tlv_tag_varint,
	                      *(rule.length_type for rule in
	                        (entry.placement.tlv_grammar.rules
	                         if entry.placement.tlv_grammar else ())),
	                      (entry.placement.tlv_grammar.length_type
	                       if entry.placement.tlv_grammar else None))}
	_VARINTS.clear()
	lines: list[str] = []
	for decl in schema.varints():
		_VARINTS[decl.name] = decl
		if decl.name in named:
			lines.extend(_varint_function(decl))
	return lines


def _shows_value(placement: Placement) -> bool:
	"""Whether a member is one varint, shown as its number."""
	return _one_varint(placement) is not None


def _signed_varint(placement: Placement) -> bool:
	"""Whether a varint's value is the zigzag form of a signed number:
	`sleb128`, which the read hands on that way, or `transform = zigzag`."""
	decl = _one_varint(placement)
	return decl is not None and (
		decl.transform is not None
		or decl.encoding is ast.VarintEncoding.SLEB128)


def _varint_value(placement: Placement) -> ast.VarintDecl | None:
	"""The varint type of a member whose VALUE this can hand a length.

	Not a signed one, whose zigzag form is not the number it means, and
	not one sharing a first byte with fields before it at an offset the
	walk decides: where the cursor stands then is the shared byte only
	if the fields before it say so, which this does not check.
	"""
	decl = _one_varint(placement)
	if decl is None or decl.transform is not None \
			or decl.encoding is ast.VarintEncoding.SLEB128:
		return None
	if decl.shared_bits and placement.offset_bits is None:
		return None
	return decl


#: The schema's varint types by name, for the member that reads one.
_VARINTS: dict[str, ast.VarintDecl] = {}


def _varint_run_count(struct: ResolvedStruct,
		placement: Placement, base: str) -> str | None:
	"""How many varints a counted run holds, as Lua over `base`."""
	if placement.array_count is not None:
		return str(placement.array_count)
	source = placement.size_expr or placement.sized_by
	if source is None or source == "remaining":
		return None
	return _over_fields(struct, source, base)


def _varint_run(resolved: ResolvedSchema, struct: ResolvedStruct,
		placement: Placement, field: str, seek: list[str]) -> list[str]:
	"""A run of varints, each as wide as its own bytes say (26.935).

	Counted, or to the end of the frame, as wasm's type indices and QUIC's
	HTTP/3 settings are. Each element is a row, and where the bytes are no
	number the dissector returns there, as it does for one varint.
	"""
	decl = _VARINTS.get(placement.varint or "")
	name = _lua(_local(struct, placement))
	if decl is None or placement.delimiters \
			or placement.repeat_while is not None:
		return _uncomputable(placement.path, "a run of varints this"
		                     " dissector does not walk")
	if placement.sized_by == "remaining":
		head = ["\twhile at < tvb:len() do"]
	else:
		count = _varint_run_count(struct, placement, "0")
		if count is None:
			return _uncomputable(placement.path, "a run of varints whose"
			                     " count this dissector cannot read")
		head = [f"\tfor _ = 1, {count} do"]
	return [
		f"\t-- {placement.path}: varints, each as wide as its own bytes say",
		*seek,
		*head,
		f"\t\tlocal {name}_w = {_varint_name(decl)}(tvb, at)",
		f"\t\tif {name}_w == 0 then return at end",
		f"\t\tsubtree:add({field}, tvb(at, {name}_w))",
		f"\t\tat = at + {name}_w",
		"\tend",
	]


def _varint_run_span(struct: ResolvedStruct,
		placement: Placement) -> list[str]:
	"""How far a counted run of varints reaches, as a Lua function."""
	decl = _VARINTS.get(placement.varint or "")
	if decl is None or placement.sized_by == "remaining" \
			or placement.delimiters or placement.repeat_while is not None:
		return []
	return [
		"",
		f"-- How far `{placement.path}` reaches from `at`:"
		f" `{placement.size_shown or placement.size_expr or placement.sized_by or placement.array_count}`",
		"-- varints, each as wide as its own bytes say.",
		f"local function {_span_name(struct, placement)}(tvb, at, count)",
		"	local start = at",
		"	for _ = 1, count do",
		f"		local w = ({_varint_name(decl)}(tvb, at))",
		"		if w == 0 then break end",
		"		at = at + w",
		"	end",
		"	return at - start",
		"end",
	]


def _bit_range(node: "ast.Expr", widths: dict[str, int]) -> tuple[int, int] | None:
	"""Which bits an unsigned expression can set: lowest, and one past the
	highest. None where that is not known from the expression alone."""
	if isinstance(node, ast.IntLiteral) and node.value >= 0:
		value = node.value
		return ((value & -value).bit_length() - 1 if value else 0,
		        value.bit_length())
	if isinstance(node, ast.NameRef) and node.name in widths:
		return 0, widths[node.name]
	if not isinstance(node, ast.Binary):
		return None
	left = _bit_range(node.left, widths)
	if left is None:
		return None
	if node.op in ("<<", ">>") and isinstance(node.right, ast.IntLiteral):
		shift = node.right.value if node.op == "<<" else -node.right.value
		return max(left[0] + shift, 0), max(left[1] + shift, 0)
	right = _bit_range(node.right, widths)
	if right is None:
		return None
	if node.op == "&":
		return max(left[0], right[0]), min(left[1], right[1])
	if node.op == "|":
		return min(left[0], right[0]), max(left[1], right[1])
	return None


def _lua_expr(node: "ast.Expr | None", names: dict[str, str],
		widths: dict[str, int] | None = None) -> str | None:
	"""A schema expression over `names` as Lua arithmetic, or None.

	For the integer expressions a tlv region decodes its tag with, which
	read bits: `tag >> 4`, `tag & 0x0F`. Decision 0021 keeps bit operators
	out of this file, so a shift by a constant is a division or a product,
	and a mask of contiguous bits is a difference of remainders. Anything
	else -- a shift by a variable, a mask with holes, `|`, a call -- is
	None, and the caller declines as it would any length it cannot read.
	Every operation is parenthesised, so Lua's precedence never decides.

	`|` where the two sides cannot share a bit, which `widths` -- each
	name's width -- shows: it is then `+`. ID3's synchsafe size is four
	`u7`s shifted by sevens and or-ed, and was declined (26.940).
	"""
	widths = widths or {}
	if isinstance(node, ast.IntLiteral):
		return str(node.value)
	if isinstance(node, ast.CharLiteral):
		return str(node.code)
	if isinstance(node, ast.NameRef):
		return names.get(node.name)
	if isinstance(node, ast.Unary) and node.op == "-":
		inner = _lua_expr(node.operand, names, widths)
		return None if inner is None else f"(-{inner})"
	if not isinstance(node, ast.Binary):
		return None
	left = _lua_expr(node.left, names, widths)
	if left is None:
		return None
	right_literal = node.right.value \
		if isinstance(node.right, ast.IntLiteral) else None
	if node.op in (">>", "<<"):
		if right_literal is None or not 0 <= right_literal < 48:
			return None
		scale = 1 << right_literal
		return (f"math.floor({left} / {scale})" if node.op == ">>"
		        else f"({left} * {scale})")
	if node.op == "&":
		if right_literal is None or right_literal < 0:
			return None
		low  = (right_literal & -right_literal).bit_length() - 1 \
			if right_literal else 0
		high = right_literal.bit_length()
		if right_literal != ((1 << high) - 1) ^ ((1 << low) - 1):
			return None		# holes in the mask: not two remainders
		top = f"({left} % {1 << high})"
		return top if low == 0 else f"({top} - ({left} % {1 << low}))"
	right = _lua_expr(node.right, names, widths)
	if right is None:
		return None
	if node.op in ("+", "-", "*", "%"):
		return f"({left} {node.op} {right})"
	if node.op == "|":
		one, two = (_bit_range(node.left, widths),
		            _bit_range(node.right, widths))
		if one is not None and two is not None \
				and (one[1] <= two[0] or two[1] <= one[0]):
			return f"({left} + {right})"
		return None
	# Floored, which is the backends' truncation only where neither side
	# can be negative: both are unsigned bits, as `_bit_range` knows them.
	if node.op == "/" and _bit_range(node.left, widths) is not None \
			and _bit_range(node.right, widths) is not None:
		return f"math.floor({left} / {right})"
	return None


def _tlv_number(type_name: str | None, placement: Placement,
		at: str) -> str | None:
	"""Reading a tag or a length a tlv region states, as `width, value`.

	A varint type through its own function, and a fixed unsigned scalar of
	whole bytes in the region's byte order. None where the order is the
	message's own (an `endian_marker`), which this does not follow.
	"""
	if type_name in _VARINTS:
		return f"{_varint_name(_VARINTS[type_name])}(tvb, {at})"
	bits = {"u8": 8, "u16": 16, "u24": 24, "u32": 32}.get(type_name or "")
	if bits is None or placement.marker is not None or _host_order(placement):
		return None
	little = "true" if placement.endian is ast.Endian.LITTLE else "false"
	width  = bits // BITS_PER_BYTE
	return (f"situ_fixed_number(tvb, {at}, {width}, {little})")


def _tlv(struct: ResolvedStruct, placement: Placement, field: str,
		seek: list[str]) -> list[str] | None:
	"""A tlv region's items, walked to the end of the frame (26.937).

	`walk._tlv_walk` in Lua: read an item's tag, decode the parts, take the
	rule the selector names, and the rule says how far the value reaches.
	Each item is a row. Where the walker stops -- a tag or a length the
	frame cuts, a value past the frame, a selector no rule answers -- so
	does this, and nothing of that item is shown. None where the grammar
	says something this file cannot render, and the caller declines.
	"""
	grammar = placement.tlv_grammar
	if grammar is None or not grammar.walkable:
		return None
	tag_type = placement.tlv_tag_varint or (
		f"u{placement.tlv_tag_bits}" if placement.tlv_tag_bits else None)
	tag = _tlv_number(tag_type, placement, "at")
	if tag is None:
		return None

	names = {"tag": "tag"}
	# How wide each name can be, so a division over them is known to be
	# unsigned: the tag is its type's width, and a part what its own
	# expression can set.
	widths = {"tag": placement.tlv_tag_bits or (
		_VARINTS[placement.tlv_tag_varint].max_bits
		if placement.tlv_tag_varint in _VARINTS else 64)}
	parts: list[str] = []
	for part in grammar.tag_decode:
		value = _lua_expr(part.value, names, widths)
		if value is None:
			return None
		names[part.name] = f"{_lua(part.name)}_part"
		reach = _bit_range(part.value, widths) if part.value else None
		if reach is not None:
			widths[part.name] = reach[1]
		parts.append(f"\t\tlocal {_lua(part.name)}_part = {value}")

	def rule_lines(rule: "ValueRule | None") -> list[str] | None:
		if rule is None or rule.kind == "error":
			return ["at = start", "break"]
		if rule.kind == "fixed":
			return [f"size = {rule.size or 0}"]
		if rule.kind == "computed":
			value = _lua_expr(rule.value, names, widths)
			if value is None:
				return None
			# Clamped at both ends, as the four backends clamp (14.2b).
			return [f"size = math.max(0, {value})"]
		if rule.kind == "self_delimiting":
			# A value that is a varint, read with the TAG's parameters as
			# the walker and C read it; one the frame cuts is no item.
			return [f"size = ({tag})",
			        "if size == 0 then at = start; break end"]
		if rule.kind == "prefixed":
			length = _tlv_number(rule.length_type, placement, "at")
			if length is None:
				return None
			return [f"local length_w, length = {length}",
			        "if length_w == 0 then at = start; break end",
			        "at = at + length_w",
			        "size = length"]
		return None

	if grammar.rules:
		selector = names.get(grammar.selector or "")
		if selector is None:
			return None
		chain: list[str] = []
		labelled = [rule for rule in grammar.rules if rule.label is not None]
		default  = next((rule for rule in grammar.rules if rule.label is None),
		                None)
		for index, rule in enumerate(labelled):
			body = rule_lines(rule)
			if body is None:
				return None
			chain.append(f"\t\t{'if' if index == 0 else 'elseif'}"
			             f" {selector} == {rule.label} then")
			chain.extend(f"\t\t\t{line}" for line in body)
		body = rule_lines(default)
		if body is None:
			return None
		if chain:
			chain.append("\t\telse")
			chain.extend(f"\t\t\t{line}" for line in body)
			chain.append("\t\tend")
		else:
			chain.extend(f"\t\t{line}" for line in body)
	else:
		body = rule_lines(ValueRule(None, "prefixed",
		                            length_type=grammar.length_type))
		if body is None:
			return None
		chain = [f"\t\t{line}" for line in body]

	return [
		f"\t-- {placement.path}: tag-length-value items, to the end of the"
		" frame", *seek,
		"\twhile at < tvb:len() do",
		"\t\tlocal start = at",
		f"\t\tlocal tag_w, _, tag = {tag}",
		"\t\tif tag_w == 0 then break end",
		"\t\tat = at + tag_w",
		*parts,
		"\t\tlocal size = 0",
		*chain,
		"\t\tif size > tvb:len() - at then at = start; break end",
		"\t\tat = at + size",
		f"\t\tsubtree:add({field}, tvb(start, at - start))",
		"\tend",
	]


def _one_varint(placement: Placement) -> ast.VarintDecl | None:
	"""The varint type of a member that is ONE varint, not a run of them."""
	if placement.varint is None or placement.array_count is not None \
			or data_sized(placement) or placement.repeat_while is not None \
			or placement.delimiters:
		return None
	return _VARINTS.get(placement.varint)


#: (name, request struct, response struct, key pairs) per emittable relation.
#: Filled by `_conversation_setup` and read by `_conversation_calls`, which
#: runs once per struct and needs to know which relations touch it.
_CONVERSATIONS: list[tuple[str, str, str, list[tuple[str, str]]]] = []

#: struct name -> the `when`s it owns (0051). Filled by `_message_setup` and
#: read once per struct, the same shape as `_CONVERSATIONS` above and for the
#: same reason: `_proto` and `_dissector` take a resolved struct rather than
#: the schema, and a message is a fact about the schema.
_MESSAGES: dict[str, list[ast.When]] = {}


#: struct name -> its ordered runs, by placement path (26.989). Filled by
#: `_message_setup` beside `_MESSAGES`, for the same reason: a struct's
#: experts are declared in one place and raised in another.
_ORDERED: dict[str, dict[str, RunOrdering]] = {}


def _message_setup(schema: ast.Schema, resolved: ResolvedSchema) -> None:
	"""Sort the schema's `when`s under the struct each one reads.

	0051's construct is a predicate over ONE message, which `wellformed`
	holds it to -- a predicate over two structs is a relation and 0030 owns
	it -- so the owner is whichever struct its paths name and there is
	exactly one.

	The dissector is the consumer 0051 said gains most: Wireshark's expert
	info is a severity and a sentence attached to a packet, filterable, and
	`situc gen-dissector` emitted none.
	"""
	_MESSAGES.clear()
	_ORDERED.clear()
	for name, struct in resolved.structs.items():
		held = messages_of(schema, name)
		if held:
			_MESSAGES[name] = held
		ordered = {}
		for entry in struct.entries:
			order = run_ordering(resolved.structs, entry.placement)
			if isinstance(order, RunOrdering):
				ordered[entry.placement.path] = order
		if ordered:
			_ORDERED[name] = ordered


def _conversation_setup(schema: ast.Schema,
		resolved: ResolvedSchema) -> list[str]:
	"""A table and two helpers per relation, and the reason this is the
	dissector's biggest single win.

	After decoding fields, the most valuable thing a dissector does is
	conversation tracking -- "response to frame N", and the filter that
	follows one exchange out of a capture. For an encrypted protocol, where
	the payload is opaque anyway, correlation is most of the remaining value
	there is to add.

	**The key needs no declaration.** A relation's equality constraints are
	the key: `must response.hdr.msg == request.hdr.msg` says what identifies
	an exchange, and `situc.relation.conversation_key` reads it back out.
	Nothing in the schema had to be annotated for a dissector.
	"""
	_CONVERSATIONS.clear()
	lines: list[str] = []

	for decl in schema.relations():
		pairs = conversation_key(decl)
		name  = _lua(decl.name)
		request, response = (param.type_name for param in decl.params)

		if not pairs:
			lines += [f"-- relation {decl.name}: no conversation key. It states",
			          "-- rules about a pair without saying what identifies",
			          "-- one, so there is nothing to hash.", ""]
			continue

		reads = [(_key_read(resolved, request, first),
		          _key_read(resolved, response, second))
		         for first, second in pairs]
		if any(a is None or b is None for a, b in reads):
			lines += [f"-- relation {decl.name}: a key field has no constant",
			          "-- offset, so it cannot be read before the walk that",
			          "-- would find it. No conversation for this one.", ""]
			continue

		_CONVERSATIONS.append((name, request, response, pairs))
		joiner       = " .. \":\" .. "
		request_key  = joiner.join(a for a, _ in reads if a is not None)
		response_key = joiner.join(b for _, b in reads if b is not None)

		lines += [
			f"-- relation {decl.name}: {', '.join(f'{a} == {b}' for a, b in pairs)}",
			f"local situ_conv_{name} = {{}}",
			"",
			f"local function situ_conv_{name}_record(tvb, pinfo)",
			"\tif pinfo.visited then return end",
			f"\tlocal key = tostring({request_key})",
			f"\tsitu_conv_{name}[key] = pinfo.number",
			"end",
			"",
			f"local function situ_conv_{name}_lookup(tvb, pinfo, subtree, field)",
			f"\tlocal key = tostring({response_key})",
			f"\tlocal seen = situ_conv_{name}[key]",
			"\tif seen ~= nil and seen ~= pinfo.number then",
			"\t\tsubtree:add(field, seen):set_generated()",
			"\tend",
			"end",
			"",
		]

	if lines:
		lines = ["-- Conversation tracking, from the relations the schema",
		         "-- declares (26.95, decision 0030).",
		         ""] + lines

	return lines


def _conversation_fields(struct: ResolvedStruct) -> list[str]:
	"""The generated `framenum` field, on the proto that reports it.

	A ProtoField Wireshark never sees in a `Proto.fields` table is a field
	nobody can filter on, and filtering one exchange out of a capture is most
	of why this is worth generating.
	"""
	proto = _lua(struct.name)
	return [f"{proto}.fields.{name}_request = ProtoField.framenum("
	        f"\"situ.{name}.request\", \"request in frame\")"
	        for name, _, response, _ in _CONVERSATIONS
	        if response == struct.name]


def _preamble(basename: str) -> list[str]:
	return [
		f"-- Generated by situc {__version__} from {basename}.situ -- do not edit.",
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
		basename: str, declared: bool = False) -> list[str]:
	proto = _lua(struct.name)
	# Under situ's own name and the schema's: Wireshark refuses a second
	# `Proto` of a name, and it already holds `coap`, `radiotap` and `frame`,
	# so those dissectors did not load. The schema's, because two schemas may
	# each have a `header` (26.900).
	scoped = _proto_name(basename, struct.name)
	lines = [
		f"-- struct {struct.name}",
		f"{'' if declared else 'local '}{proto} = "
		f"Proto(\"{scoped}\", \"{basename}.{struct.name} (situ)\")",
		"",
	]

	# A parameter is an argument the caller supplies, not bytes in the
	# packet: it gets no `ProtoField` and no row in the tree, and the
	# dissector body does not advance past it. It reaches the schema's
	# expressions through the preference emitted below (0050).
	members = [held for held in own_members(struct) if not held.parameter]

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
	# names `X.fields` for anything that arrives later, so declaring it
	# costs a line and its absence is a `nil` index at run time.
	#
	# `X.fields` and not a `local X_f` alias. The alias read better and was
	# one local per struct in the MAIN CHUNK, where Lua allows two hundred:
	# `edges` reached 203 the day a struct was added to it and `luac -p`
	# refused the file -- *too many local variables (limit is 200) in main
	# function* (26.518). Block-scoping the alias is not available, because
	# every dissect function closes over it as an upvalue.
	lines.append(f"{proto}.fields = {{}}")
	lines.extend(fields)
	lines.extend(_conversation_fields(struct))
	lines.extend(_experts(struct))
	lines.extend(_preferences(struct))
	lines.append("")

	lines.extend(_dissector(resolved, struct, members))
	lines.append("")
	return lines


def _field(resolved: ResolvedSchema, struct: ResolvedStruct,
		placement: Placement) -> str:
	"""One `ProtoField`, or "" for a member that gets a subtree instead."""
	name  = _local(struct, placement)
	# Under `situ.`: Wireshark holds `radiotap.vendor_namespace` as something
	# else and refuses a second field of that name and another type (26.900).
	owner  = re.sub(r"\W", "_", struct.name)
	abbrev = f"situ.{owner}.{name}"

	# An array or a run of structs is not a nested struct, which is the order
	# `traverse.Member` puts these two questions in and the order this did
	# not: a run of them was read as one nested struct, declared no field,
	# and the body then added the name anyway -- `subtree:add(nil, ...)`, a
	# Lua error at the first packet rather than a wrong display.
	# ...except one placed by `at`, whose body is a row of its bytes where
	# the message puts them: the body adds `<struct>.fields.<name>`, and no
	# field was declared, so the first packet that reached it was a Lua
	# error (26.968).
	if placement.type_name in resolved.structs \
			and placement.array_count is None and not data_sized(placement) \
			and placement.located is None:
		return ""			# a nested struct: its own Proto dissects it

	# -- what is shown as CHARACTERS, and where that stops -----------------
	#
	# The rule is the schema's own say-so and nothing softer: a member is
	# shown as text where the schema states that its bytes spell the value
	# in characters. Two constructs say that and no others do.
	#
	# It is worth stating as a rule because it was twice arrived at as a
	# special case -- once for `hex u32 ino[8]` and once for a token set --
	# and the second did not notice that the first had excluded delimited
	# members for a reason that had nothing to do with text.
	#
	# **A text number, however it is framed.** `decimal`, `hex` and
	# `scaled` all say the bytes are digits (8.6.2), so there is no capture
	# in which they are not. This branch also has to come before the array
	# one, which it looks exactly like from here: the bracket of
	# `hex u32 ino[8]` is a width in bytes and not a count, and reading it
	# as a count declared a field of eight `u32`s -- thirty-two bytes for an
	# eight-byte number, overlapping everything after it.
	#
	# `and not placement.delimiters` stood here until 26.340 and was a
	# leftover of that array fix rather than a claim about text: a DELIMITED
	# text number is digits by the same definition, and those came out as
	# hex. HTTP's status code showed as `323030` where an analyst wants
	# `200`. The body needed nothing -- `_delimited` already adds the
	# scanned span -- so the member was declared one type and drawn as
	# another.
	#
	# Measured over the corpus: 597 field declarations, of which 7 moved
	# from `bytes` to `string` and nothing else moved at all. The rule
	# swept exactly the members it names.
	if placement.radix is not None:
		return (f"{_lua(struct.name)}.fields.{_lua(name)} = "
		        f"ProtoField.string(\"{abbrev}\", \"{name}\")")

	# **A token set.** Its arms are keywords, so an analyst reading an SMTP
	# session wants `HELO` and not five byte values (0055). It has no scalar
	# either, and the body adds the member regardless -- the shape the
	# varint and region branch below was written for: no field declared,
	# `subtree:add(nil, ...)`, a Lua error at the first packet rather than a
	# wrong display.
	if placement.type_name in resolved.layout.env.token_sets:
		return (f"{_lua(struct.name)}.fields.{_lua(name)} = "
		        f"ProtoField.string(\"{abbrev}\", \"{name}\")")

	# **And it stops there, which is the half worth writing down.** The
	# obvious next step is that a delimited byte run in a text protocol is
	# text too -- HTTP's header values and json's strings are, and they
	# come out as hex. `delimiters` does not license it: slip's `datagram`
	# is delimited and carries an IP packet. Measured over the corpus, 39
	# delimited byte runs and not one of them carries an `[encoding]`, so
	# there is nothing here to read yet. That is 26.330's question, and
	# this branch is not the place to guess at it.

	if data_sized(placement) or placement.array_count is not None:
		return (f"{_lua(struct.name)}.fields.{_lua(name)} = "
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
	#
	# A varint's bytes are not its value, but the dissector READS the value
	# now, so an unsigned one is declared a number and shown as the one it
	# spells (26.941). A signed one stays bytes: its zigzag form is not the
	# number it means.
	if _shows_value(placement):
		kind = "int64" if _signed_varint(placement) else "uint64"
		return (f"{_lua(struct.name)}.fields.{_lua(name)} = "
		        f"ProtoField.{kind}(\"{abbrev}\", \"{name}\", base.DEC)")
	#
	# A tlv region too, whose items are each a row of it (26.937).
	if placement.varint is not None \
			or placement.kind in ("coded", "sealed", "tlv") \
			or (placement.located is not None
			    and placement.type_name in resolved.structs):
		return (f"{_lua(struct.name)}.fields.{_lua(name)} = "
		        f"ProtoField.bytes(\"{abbrev}\", \"{name}\")")

	# Host order: the bytes are what this dissector can honestly show, since
	# the capture does not say which machine wrote them.
	if _host_order(placement):
		return (f"{_lua(struct.name)}.fields.{_lua(name)} = "
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
		return (f"{_lua(struct.name)}.fields.{_lua(name)} = "
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

	return (f"{_lua(struct.name)}.fields.{_lua(name)} = "
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
	if placement.endian is not ast.Endian.NATIVE:
		return False
	# A field lying entirely inside one byte has no byte order to not know.
	# `endian native` is a fact about a MULTI-byte number, and asking the
	# question of a `u8` answered yes: netlink's `ifa_family`, `ifa_prefixlen`,
	# `ifa_flags` and `ifa_scope` were all drawn as opaque bytes under "the
	# capture does not record which machine wrote these", when one byte reads
	# the same on every machine there has ever been. The offset matters as
	# well as the width, because a byte-wide field straddling two bytes does
	# draw from two.
	size, offset = placement.size_bits, placement.offset_bits
	if size is not None and offset is not None and size <= BITS_PER_BYTE \
			and offset % BITS_PER_BYTE + size <= BITS_PER_BYTE:
		return False
	return True


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
	# The bytes the field is added over, which is narrower than its type
	# where no type is that wide: flac's `u36 total_samples` at bit 108 is a
	# `uint64` added over five bytes, and Wireshark masks the forty-bit
	# value those hold. Shifting for sixty-four put the mask past it.
	span  = span_bits(placement) or width

	if placement.bit_order is ast.BitOrder.LSB_FIRST:
		shift = skip
	else:
		shift = span - skip - placement.size_bits

	return ((1 << placement.size_bits) - 1) << shift


# ---------------------------------------------------------------------------
# The dissector function
# ---------------------------------------------------------------------------


#: Emitted in place of a member whose extent this dissector cannot compute.
#: `_dissector` reads it and stops placing members that follow the cursor.
_LOST = "\t-- (the cursor stops here)"


def _argument_tables(resolved: ResolvedSchema, roots: list[str]) -> list[str]:
	"""Where a struct's reads of its parameters go (0068).

	`<s>_arg.<name>` rather than `<s>.prefs.<name>`: the preference is
	the answer at the top of a capture, and a parent that hands the
	argument sets it here for the length of one call. Wireshark's
	`Dissector:call` takes no arguments, so a value travels this way or
	not at all -- set before the call and restored after it, by `handing`,
	which restores even when the call raises.
	"""
	taking = [name for name in roots
	          if traverse.arguments(resolved.structs[name])]
	if not taking:
		return []
	lines = ["",
	         "-- What a parent hands a child (0068): read through these, which",
	         "-- fall back to the child's own preferences.",
	         *(f"local {_lua(name)}_arg = setmetatable({{}}, {{__index ="
	           f" function(_, k) return {_lua(name)}.prefs[k] end}})"
	           for name in taking),
	         "",
	         "local function handing(args, values, f, ...)",
	         "\tlocal saved = {}",
	         "\tfor k, v in pairs(values) do",
	         "\t\tsaved[k] = rawget(args, k)",
	         "\t\targs[k] = v",
	         "\tend",
	         "\tlocal result = table.pack(pcall(f, ...))",
	         "\tfor k in pairs(values) do",
	         "\t\targs[k] = saved[k]",
	         "\tend",
	         "\tif not result[1] then",
	         "\t\terror(result[2], 0)",
	         "\tend",
	         "\treturn table.unpack(result, 2, result.n)",
	         "end"]
	return lines


def _handed(struct: ResolvedStruct, placement: Placement,
		base: str, remaining: str | None = None) -> str | None:
	"""What a member hands its child, as a Lua table over `base` (0068).

	None where an argument reads something this file cannot, and the
	caller then declines the member, as it does any length it cannot read.
	"""
	parts = []
	inner = {one.name: one for one in traverse.arguments(
		_STRUCTS[placement.type_name or ""])}
	for name, source in placement.arguments:
		# `remaining` from the member's start, where the caller knows it
		# (26.993): elsewhere the argument declines as any unread name does.
		value = _over_fields(struct, source, base,
		                     extra = (None if remaining is None
		                              else {"remaining": remaining}))
		if value is None:
			return None
		# Held as the backends hold it (`traverse.handed_range`, 26.904).
		bounds = traverse.handed_range(struct, inner[name], source)
		if bounds is not None:
			value = f"math.max({bounds[0]}, math.min({bounds[1]}, {value}))"
		parts.append(f"{_lua(name)} = {value}")
	return "{" + ", ".join(parts) + "}"


def _handing_call(struct: ResolvedStruct, placement: Placement, base: str,
		call: str, *args: str) -> str | None:
	"""`call(args...)` with this member's arguments handed to its type."""
	values = _handed(struct, placement, base)
	if values is None:
		return None
	table = f"{_lua(placement.type_name or '')}_arg"
	return f"handing({table}, {values}, {', '.join([call, *args])})"


def _handing_run(resolved: ResolvedSchema, struct: ResolvedStruct,
		placement: Placement) -> list[str]:
	"""A run whose elements its parent hands arguments, dissected (0068).

	Every element gets the same values (rule 4), so the run's own lines
	are emitted as for any run and run inside one `handing` call: each
	element's extent and dissector read `<type>_arg` and find the parent's
	values there. `at` is the enclosing function's, so the closure moves
	it as the lines would have.
	"""
	values = _handed(struct, placement, "0", "math.max(0, tvb:len() - at)")
	if values is None:
		return _uncomputable(placement.path, "an argument it is handed"
		                     " reads what this dissector cannot")
	inner = _member_body(resolved, struct,
	                     dataclasses.replace(placement, arguments=()))
	lost  = _LOST in inner
	body  = [line for line in inner if line != _LOST]
	return [f"\thanding({_lua(placement.type_name or '')}_arg, {values},"
	        " function()",
	        *(f"\t{line}" if line else line for line in body),
	        "\tend)",
	        *([_LOST] if lost else [])]


def _uncomputable(path: str, why: str) -> list[str]:
	"""A member this dissector cannot step over, said once and acted on.

	Four branches declined a member by returning a comment and nothing else,
	which left `at` where it was -- and the walk carried on placing every
	member after it at a cursor that was now wrong, with full confidence.
	`test/schema/edges.situ`'s `beats.after` was shown at offset 0 on all
	eight probe packets, where the generated C walks the `while` run first
	(`situ_beats_pulse_span_from`) and reads it at 2; the walker agrees with
	C. A wrong line is worse than a missing one, and this is the file that
	says so about a located member three hundred lines up.
	"""
	return [f"\t-- {path}: {why}", _LOST]


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

	# Once a member's extent cannot be computed, the cursor is wrong and
	# every member declared after it is declined by name.
	#
	# Two shapes could in principle survive that -- a member with a static
	# offset, which assigns `at` outright, and a located one, which reads at
	# its driver's offset without consulting the cursor at all (9.8). Neither
	# is special-cased. A static offset cannot follow a member of unknown
	# extent in the first place, and no schema in this repository puts a
	# located member after one, so the exemption would be a branch of a code
	# generator that nothing in the tree exercises -- which is the thing this
	# file has been wrong about twice already.
	lost = False
	capturable = {held.path for held in _capturable(struct, members)}
	_captured.clear()
	for placement in members:
		if placement.path in capturable and not lost:
			lines.append(f"\t{_capture_line(placement, 'at')}")
		if lost:
			lines.append(f"\t-- {placement.path}: not shown, because the"
			             " member above it has no extent this")
			lines.append("\t-- dissector can compute, so this one's"
			             " offset is unknown")
			continue

		body = _member(resolved, struct, placement)
		order = _ORDERED.get(struct.name, {}).get(placement.path)
		if order is not None and _LOST not in body:
			lines.append(f"\tlocal {_lua(_local(struct, placement))}_first = at")
		lines.extend(line for line in body if line != _LOST)
		lost = _LOST in body
		if order is not None and not lost:
			lines.extend(_order_check(struct, placement, order))

	lines.extend(_conversation_calls(resolved, struct))
	lines.extend(_message_calls(struct, lost))
	lines.extend(["", "\treturn at", "end"])
	_captured.clear()
	return lines


#: 0051's three severities as Wireshark's. Wireshark has four and situ has
#: three, and the record refuses a fourth: adding one to match a consumer
#: would put situ in the business of somebody else's presentation model. So
#: `chat` is the level nothing maps onto, which is the right way round --
#: a schema saying less than a display can show costs nothing.
#:
#: `refuse` is MALFORMED rather than PROTOCOL, because that is what it means:
#: the message does not conform. The other two are remarks about one that
#: does.
_EXPERT = {
	ast.Severity.REFUSE: ("expert.group.MALFORMED", "expert.severity.ERROR"),
	ast.Severity.WARN:   ("expert.group.PROTOCOL", "expert.severity.WARN"),
	ast.Severity.NOTE:   ("expert.group.PROTOCOL", "expert.severity.NOTE"),
}


def _lua_string(text: str) -> str:
	"""A schema's sentence inside a Lua double-quoted string.

	A message may say anything -- it is prose a person wrote -- so the two
	characters that would end the string or start an escape are spelled out.
	Without this a text carrying a quote produces Lua that does not parse,
	and a dissector that fails to load is worse than one that divides.
	"""
	return text.replace("\\", "\\\\").replace('"', '\\"')


def _preferences(struct: ResolvedStruct) -> list[str]:
	"""One preference per `[stream]` parameter this struct takes (0050).

	The first preference this generator has ever emitted, and the case
	0050 says would justify one: a preference is per-dissector rather than
	per-packet, which is exactly the shape of a fact negotiated once and
	then fixed -- a cipher suite, a card class, a block size.

	The default is zero, and that is a deliberate refusal to guess. A
	dissector cannot know the argument; a person opening a capture sets it,
	and until they do the layout is described under whatever the schema's
	own expressions make of a zero. A guessed default would describe some
	other capture confidently.
	"""
	held = [placement for placement in own_members(struct)
	        if placement.parameter]
	if not held:
		return []

	proto = _lua(struct.name)
	lines = ["",
	         "-- Arguments this format's shape follows, which the message",
	         "-- does not carry (0050). A preference rather than a field:",
	         "-- the fact is negotiated once and fixed for a stream, and",
	         "-- nothing in a capture says what it was."]
	for placement in held:
		# A per-message argument is HANDED by the parent that reads it out
		# of the capture (0068), and `handing` sets it for the length of the
		# child's call; the preference is only what a struct dissected on
		# its own -- by Decode As -- reads in its place, zero by default
		# for the reason above. It was refused outright before 26.994, as
		# though no capture could carry one: a parent's always can.
		stream = any(one.name == "stream" for one in placement.attrs)
		lines.append(
			f"{proto}.prefs.{_lua(placement.name)} = Pref.uint("
			f'"{placement.name}", 0, '
			f'"{struct.name}.{placement.name}: '
			+ ("an argument the layout follows" if stream else
			   "handed by the message around it; this value only where "
			   "it is dissected alone")
			+ '")')
	return lines


def _experts(struct: ResolvedStruct) -> list[str]:
	"""One `ProtoExpert` per message this struct carries (0051).

	Declared whether or not the body can evaluate the predicate. The
	abbreviation is what a person filters a capture on, so it is derived from
	the NAME the schema gave -- which is the identity 0051 makes the contract
	-- and the text is the default rendering registered beside it.
	"""
	held    = _MESSAGES.get(struct.name, [])
	ordered = _ORDERED.get(struct.name, {})
	if not held and not ordered:
		return []

	proto = _lua(struct.name)
	lines = [f"{proto}.experts = {{}}",
	         f"local {proto}_e = {proto}.experts"]
	# A run held in order and out of it is malformed, as a `refuse` is: the
	# six other readers refuse it (26.985, 26.988).
	for path in ordered:
		run = _local(struct, _placement_at(struct, path))
		lines.append(
			f"{proto}_e.{_lua(run)}_out_of_order = ProtoExpert.new("
			f'"{struct.name}.{run}_out_of_order", '
			f'"`{run}` is not in ascending order", '
			"expert.group.MALFORMED, expert.severity.ERROR)")
	for when in held:
		group, severity = _EXPERT[when.severity]
		lines.append(
			f"{proto}_e.{_lua(when.name)} = ProtoExpert.new("
			f'"{struct.name}.{when.name}", "{_lua_string(when.text)}", '
			f"{group}, {severity})")
	return lines


def _placement_at(struct: ResolvedStruct, path: str) -> Placement:
	return next(entry.placement for entry in struct.entries
	            if entry.placement.path == path)


def _order_check(struct: ResolvedStruct, placement: Placement,
		order: RunOrdering) -> list[str]:
	"""The run just dissected, its keys compared as the other six compare
	them, and expert info where one is not above the one before (26.989).

	Byte by byte from the most significant, which is the comparison for
	every kind: a run of bytes first byte first, an integer in the order
	its bytes are significant -- reversed for little-endian -- and a signed
	integer with its first such byte's sign bit flipped, so that bytes
	compare as the numbers do. One loop, and no 64-bit arithmetic for a Lua
	without it.
	"""
	run    = _lua(_local(struct, placement))
	proto  = _lua(struct.name)
	key    = order.key
	little = key is not None and key.endian is ast.Endian.LITTLE
	signed = key is not None and key.scalar is not None and key.scalar.signed
	width  = order.key_bytes
	index  = f"({width - 1} - k)" if little else "k"
	# No bare `do ... end` around it: a dissector's blocks are counted by
	# their opening keyword, and `do` is not one (the `for ... do` form
	# would count twice). The locals are named for the run instead.
	return [
		f"\t-- {placement.path}: each key above the one before (26.989)",
		f"\tlocal {run}_count = math.floor((at - {run}_first) / {order.stride})",
		f"\tlocal {run}_i = 1",
		f"\twhile {run}_i < {run}_count do",
		f"\t\tlocal a = {run}_first + ({run}_i - 1) * {order.stride}"
		f" + {order.key_at}",
		f"\t\tlocal b = a + {order.stride}",
		f"\t\tif b + {width} > tvb:len() then",
		"\t\t\tbreak",
		"\t\tend",
		"\t\tlocal rank = 0",
		"\t\tlocal k = 0",
		f"\t\twhile k < {width} and rank == 0 do",
		f"\t\t\tlocal x = tvb(a + {index}, 1):uint()",
		f"\t\t\tlocal y = tvb(b + {index}, 1):uint()",
		*([
			"\t\t\tif k == 0 then",
			"\t\t\t\tx = (x + 128) % 256",
			"\t\t\t\ty = (y + 128) % 256",
			"\t\t\tend",
		] if signed else []),
		"\t\t\trank = y - x",
		"\t\t\tk = k + 1",
		"\t\tend",
		"\t\tif rank <= 0 then",
		f"\t\t\tsubtree:add_proto_expert_info({proto}_e.{run}_out_of_order)",
		"\t\t\tbreak",
		"\t\tend",
		f"\t\t{run}_i = {run}_i + 1",
		"\tend",
	]


def _message_calls(struct: ResolvedStruct, lost: bool = False) -> list[str]:
	"""Add the expert info for every message whose predicate holds.

	At the end of the dissection rather than beside a member, because a
	`when` is a predicate over the whole struct and there is no member it
	hangs off. No short circuit either: 0051 puts the reporting sibling
	beside `validate` rather than inside it for exactly this reason, and a
	dissector that stopped at the first would hide the rest of what the
	schema had to say about the packet.

	A predicate this backend cannot read comes out as a comment naming what
	stopped it, which is what every other declined expression here does. The
	expert is still registered, so a filter on its name is valid and simply
	never matches -- the alternative, a name that does not exist, is an error
	in the user's filter rather than an honest silence.

	`size(s)`, the struct's own extent, is the cursor here: every member
	above has advanced `at` past itself, and a located one leaves it alone
	(9.8). Unless a member above had no extent and `lost` the cursor, when
	it declines with the rest (26.981).
	"""
	held = _MESSAGES.get(struct.name)
	if not held:
		return []

	proto = _lua(struct.name)
	lines = ["", "\t-- what the schema says about this message (0051)"]
	for when in held:
		source = expr_to_source(when.expr, explicit=True)
		local  = re.sub(rf"\b{re.escape(struct.name)}\.", "", source)
		extra  = {"remaining": "tvb:len()"}
		if not lost:
			local = re.sub(rf"\bsize\(\s*{re.escape(struct.name)}\s*\)",
			               "situ_struct_extent", local)
			extra["situ_struct_extent"] = "at"
		# `remaining` from the struct's start is its whole frame, as both
		# walkers and the four backends read it in a `when`.
		shown  = _over_fields(struct, local, "0", extra = extra)
		if shown is None:
			lines.append(f"\t-- {when.name}: {_unreadable(struct, local)}")
			continue
		# Lua spells these otherwise, and `&&` here is a file that does not
		# load, for every message in it and not this one (26.905).
		shown = translate_operators(shown, conj=" and ", disj=" or ",
		                            ne=" ~= ", neg="not ")
		lines.append(f"\tif {shown} then")
		lines.append(f"\t\tsubtree:add_proto_expert_info("
		             f"{proto}_e.{_lua(when.name)})")
		lines.append("\tend")
	return lines


def _key_read(resolved: ResolvedSchema, struct_name: str,
		path: str) -> str | None:
	"""Lua reading one key field, or None where its offset is not a constant.

	A conversation key has to be readable before the walk that would find it
	dynamically has run, so a field the *data* positions cannot serve as one.
	Declining says so rather than hashing a byte range that moves.
	"""
	struct = resolved.structs.get(struct_name)
	if struct is None:
		return None

	member = path.partition(".")[2]
	for entry in struct.entries:
		placement = entry.placement
		if placement.path != f"{struct_name}.{member}":
			continue
		if placement.offset_bits is None or placement.size_bits is None:
			return None
		if placement.offset_bits % 8 or placement.size_bits % 8:
			return None
		width = placement.size_bits // 8
		if width > 4:
			return None	# `uint` tops out at four bytes, as elsewhere here
		if _host_order(placement):
			return None	# no answer a capture can give
		little = _little(placement)
		if little is None:
			return None
		return (f"situ_uint(tvb, {placement.offset_bits // 8}, "
		        f"{width}, {little})")
	return None


def _conversation_calls(resolved: ResolvedSchema,
		struct: ResolvedStruct) -> list[str]:
	"""Look this frame up as a reply, then record it as a request.

	In that order, so a frame does not match itself where both parameters
	name one struct -- which is the ordinary shape, `response_to(request:
	frame, response: frame)`.
	"""
	lines: list[str] = []
	for name, request, response, _pairs in _CONVERSATIONS:
		if struct.name == response:
			lines += ["",
			          f"	situ_conv_{name}_lookup(tvb, pinfo, subtree, "
			          f"{_lua(struct.name)}.fields.{name}_request)"]
		if struct.name == request:
			lines += [f"	situ_conv_{name}_record(tvb, pinfo)"]
	return lines


def _member(resolved: ResolvedSchema, struct: ResolvedStruct,
		placement: Placement) -> list[str]:
	"""Add one member to the tree, and advance `at` past it.

	A versioned member is wrapped in the guard its accessors have. Without it
	the dissector read `tvb(3, 4)` on a three-byte v1 message and Wireshark
	showed the packet as malformed -- blaming the capture for the schema.
	"""
	lines = _member_body(resolved, struct, placement)
	held  = traverse.held_here(struct, placement)
	if held is not None:
		# The run's own row stands, and what it holds goes under it, framed
		# by exactly its bytes (26.898).
		start = f"{_lua(_local(struct, placement))}_held_at"
		lines = [f"\tlocal {start} = at", *lines,
		         f"\tif at > {start} then",
		         f"\t\tDissector.get(\"{held}\"):call("
		         f"tvb({start}, at - {start}):tvb(), pinfo, subtree)",
		         "\tend"]
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
	if field.marker is not None:
		little = _little(field)
		if little is None or count > 4:
			return None
		test = f"situ_uint(tvb, {first}, {count}, {little}) >= {placement.since}"
	holds = _reaches(placement)
	if holds is not None:
		test = f"{holds} and {test}"
	return f"if {test} then"


def _located_body(resolved: ResolvedSchema, struct: ResolvedStruct,
		placement: Placement, field: str, name: str) -> list[str]:
	"""A member the data positions, read where the data says.

	The driver is named relative to the struct -- `file.pixel_offset` inside
	`bitmap_file` -- so the entry to read is `bitmap_file.file.pixel_offset`,
	which `_key_read` cannot find because it only ever looks one level down.

	Declines rather than guessing where the driver is not at a constant
	offset, which is the same answer `_key_read` gives and for the same
	reason: a byte range that moves is not one to read from.
	"""
	target = f"{struct.name}.{placement.located}"
	driver = next((entry.placement for entry in struct.entries
	               if entry.placement.path == target), None)

	if driver is None or driver.offset_bits is None \
			or driver.offset_bits % BITS_PER_BYTE \
			or driver.size_bits is None or driver.size_bits % BITS_PER_BYTE \
			or driver.size_bits // BITS_PER_BYTE > 4 or _host_order(driver):
		return [f"\t-- {placement.path}: placed by `{placement.located}`, "
		        f"which this dissector cannot read at a constant offset"]

	little = _little(driver)
	if little is None:
		return [f"\t-- {placement.path}: placed by `{placement.located}`, "
		        f"whose byte order this dissector cannot read"]
	where  = (f"situ_uint(tvb, {driver.offset_bits // BITS_PER_BYTE}, "
	          f"{driver.size_bits // BITS_PER_BYTE}, {little})")

	local  = _lua(name)
	# Measured where it is placed, not at the cursor: a struct the data
	# sizes was measured from `at`, which is wherever the walk had got to
	# -- a TIFF directory at `ifd_offset` 44 was measured from byte 8 and
	# never shown (26.968).
	length = _length(resolved, struct, placement, "0", f"{local}_at")
	extent = f"{local}_n" if length is not None else None

	lines = [f"\t-- {placement.path}: placed at `{placement.located}`",
	         f"\tlocal {local}_at = {where}"]
	if extent is None:
		lines.append(f"\tlocal {local}_n = tvb:len() - {local}_at")
		extent = f"{local}_n"
	else:
		lines.append(f"\tlocal {extent} = {length}")

	# A STRUCT is dissected there too, as a nested member is: its row was
	# the bytes alone, so a TIFF directory reached Wireshark as a run of
	# hex with no entry in it (26.978). Under the bytes' row, and handed
	# the member's arguments where it takes any.
	inner = resolved.structs.get(placement.type_name or "")
	nested: str | None = None
	if inner is not None:
		nested = (f"Dissector.get(\"{placement.type_name}\"):call("
		          f"tvb({local}_at, {extent}):tvb(), pinfo, {local}_tree)")
		if placement.arguments:
			nested = _handing_call(struct, placement, "0",
			                       f"function() return {nested} end")
	lines.extend([
		f"\tif {local}_at + {extent} <= tvb:len() then",
		*([f"\t\tlocal {local}_tree = subtree:add({field},"
		   f" tvb({local}_at, {extent}))",
		   f"\t\t{nested}"] if nested is not None else
		  [f"\t\tsubtree:add({field}, tvb({local}_at, {extent}))"]),
		"\tend",
		# `at` is deliberately untouched: 9.8 again. The member after this one
		# sits where it would if this one were not declared.
	])
	return lines


def _lead(placement: Placement) -> list[str]:
	"""The Lua that steps the cursor past a member's lead.

	A membership test repeated, which is what `skip` is: it stops at the
	first byte that is not in the set. Bounded by the tvb, since a lead that
	runs to the end of the capture leaves no member after it and reading one
	byte further is what raises inside Wireshark rather than in situ.
	"""
	if not placement.skip:
		return []

	test = " and ".join(f"byte ~= 0x{one:02X}" for one in placement.skip)
	return [
		f"\t-- {placement.path}: the whitespace in front of it is its own",
		"\twhile at < tvb:len() do",
		"\t\tlocal byte = tvb(at, 1):uint()",
		f"\t\tif {test} then break end",
		"\t\tat = at + 1",
		"\tend",
	]


def _member_body(resolved: ResolvedSchema, struct: ResolvedStruct,
		placement: Placement) -> list[str]:
	name  = _local(struct, placement)
	field = f"{_lua(struct.name)}.fields.{_lua(name)}"

	# A static offset is assigned; a dynamic one is already in `at` from the
	# member before it, and `at = at` is noise.
	start = placement.offset_bits
	at    = "at" if start is None else str(start // BITS_PER_BYTE)
	seek  = [] if start is None else [f"\tat = {at}"]

	# `skip`: the whitespace in front of the member is the member's own, so
	# the cursor steps over it before anything is shown -- and it steps over
	# it exactly once, which is why this is threaded into `seek` rather than
	# repeated in each branch below. A dissector that read the member at the
	# cursor showed the first space and placed everything after it one run
	# early, which is the disagreement with the walker that found this.
	seek = seek + _lead(placement)

	# A member its parent hands arguments (0068). The child's reads of its
	# parameters are its own PREFERENCES here, which is right when it is the
	# top of a capture and wrong under a parent that says what they are --
	# so this read `entry.prefs.block` where the message said `vol.block`,
	# and placed everything after the member on that. Declined, which
	# stops the walk honestly, until the arguments are passed.
	if placement.arguments \
			and not nests_one_struct(resolved.structs, placement):
		return _handing_run(resolved, struct, placement)

	# `at expr`: the message says where this member is, so the cursor is the
	# wrong answer and advancing past it is a second wrong answer. Section 9.8
	# is explicit -- a located member "joins no offset chain: it contributes
	# nothing to the enclosing struct's extent, and the member declared after
	# it sits where it would if the located member were not there".
	#
	# `offset_bits` is None for one, so it fell through to the running cursor
	# and was dissected wherever the walk had reached. For a typical BMP that
	# is 54 and `file.pixel_offset` is also 54, which is why nothing noticed:
	# right by coincidence in the common case, and wrong in exactly the case
	# the construct exists for -- a gap, or a colour table, between the
	# headers and the pixels.
	if placement.located is not None:
		return _located_body(resolved, struct, placement, field, name)

	# `pad_to(n)`: the bytes from here to the next multiple of `n` (8.4,
	# decision 0043). There is nothing to show -- that is what padding is --
	# but it moves the cursor, and every member declared after it sits where
	# it leaves them.
	#
	# It was not handled at all, so the cursor walked straight past the
	# padding and `byte_run.trailer` was dissected wherever the run happened
	# to end. On a 14-byte run the generated C aligns to 16
	# (`situ_align_up_u32(offset, 4u, view.limit)`) and the walker reads 16;
	# this read 15, one byte early, and showed a value straddling the pad.
	# Relative to the current tvb rather than to the whole capture, which is
	# what C does with its own view base.
	if placement.pad_to:
		unit = placement.pad_to
		return [f"\t-- {placement.path}: pad to the next multiple of {unit}",
		        *seek,
		        f"\tat = at + (({unit} - at % {unit}) % {unit})"]

	# Before the nested-struct case. A run of records names a struct type and
	# is not one, and this used to reach the delimiter branch only because a
	# delimited member carried `array_count = 1` -- so the check below failed
	# for the wrong reason and the right thing happened by accident. Removing
	# that lie from the solver is what exposed it.
	if placement.delimiters:
		return _delimited(resolved, struct, placement, field, seek)

	# A VARINT's width is in its own bytes, and `byte_span` answers with the
	# scalar's MINIMUM -- one byte -- so the generic branch below showed a
	# three-byte SQLite varint as one, with the confidence of a decode, and
	# then declined every member after it. The decline was right and the row
	# was not: a wrong line is worse than a missing one, which is what
	# `_uncomputable` exists to say. The bytes are walked now, by the form's
	# own rules, so the members after it are placed too (26.932).
	decl = _one_varint(placement)
	if decl is not None:
		name = _lua(_local(struct, placement))
		return [
			f"\t-- {placement.path}: a varint, as wide as its own bytes say",
			*seek,
			f"\tlocal {name}_w, _, {name}_v = {_varint_name(decl)}(tvb, at)",
			# Where the bytes are no number, nothing after is placed: the
			# walker refuses the message there, and a cursor guessed past
			# it is the confident wrong line `_uncomputable` exists for.
			# Returned from rather than run to the frame's end, which
			# still drew a zero-length row for a run the number sized.
			f"\tif {name}_w == 0 then",
			"\t\treturn at",
			"\tend",
			(f"\tsubtree:add({field}, tvb(at, {name}_w),"
			 f" {'situ_s64' if _signed_varint(placement) else 'situ_u64'}"
			 f"({name}_v))"),
			f"\tat = at + {name}_w",
		]
	if placement.varint is not None:
		return _varint_run(resolved, struct, placement, field, seek)

	if placement.kind == "tlv":
		walked = _tlv(struct, placement, field, seek)
		if walked is None:
			return _uncomputable(placement.path, "tag-length-value items"
			                     " whose grammar this dissector cannot"
			                     " render")
		return walked

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
				return _uncomputable(
					placement.path,
					f"one `{placement.type_name}` has no extent this"
					" dissector can compute")
			size = "size"

		lines = [f"\t-- {placement.path}", *seek]
		dissect = (f"Dissector.get(\"{placement.type_name}\"):call("
		           f"tvb(at, {size}):tvb(), pinfo, subtree)")
		measure = f"{_extent_name(nested)}(tvb, at)"
		if placement.arguments:
			# The child's reads go through `<type>_arg`, so the call is
			# made with this member's arguments set there (0068).
			got = _handing_call(
				struct, placement, "0", "function()"
				f" return {dissect} end")
			sized = _handing_call(struct, placement, "0",
			                      _extent_name(nested), "tvb", "at")
			if got is None or sized is None:
				return _uncomputable(placement.path, "an argument it is"
				                     " handed reads what this dissector"
				                     " cannot")
			dissect, measure = got, sized
		if size == "size":
			lines.append(f"\tlocal size = {measure}")
		lines.extend([
			f"\tif tvb:len() >= at + {size} then",
			f"\t\t{dissect}",
			"\tend",
			f"\tat = at + {size}",
		])
		return lines

	# Before `_repeated`, for the reason `_field` is: the bracket is a width.
	if placement.radix is not None:
		span = byte_span(placement)
		if span is not None:
			first, count = span
			# The digits as the value, over the whole width: shown as the
			# width's string, ustar's `chksum` -- six digits, a NUL and a
			# space -- drew Wireshark's "trailing stray characters" on
			# every header, measured with tshark (26.948).
			shown = (f", situ_padded_text(tvb, {first}, {count})"
			         if placement.radix_padded else "")
			return [
				f"\tsubtree:add({field}, tvb({first}, {count}){shown})",
				f"\tat = {first + count}",
			]
		# At an offset the walk decides, the same width from `at`. It fell
		# to `_repeated` and took `decimal u32 n[4]` for four u32s, sixteen
		# bytes where the digits are four -- unseen while the varint in
		# front of `edges.text_driver_moved` stopped the cursor (26.932).
		if placement.is_fixed_size and not placement.delimiters \
				and placement.size_bits % BITS_PER_BYTE == 0:
			digits = placement.size_bits // BITS_PER_BYTE
			shown = (f", situ_padded_text(tvb, at, {digits})"
			         if placement.radix_padded else "")
			return [
				f"\t-- {placement.path}: {digits} digits", *seek,
				f"\tif tvb:len() >= at + {digits} then",
				f"\t\tsubtree:add({field}, tvb(at, {digits}){shown})",
				"\tend",
				f"\tat = math.min(at + {digits}, tvb:len())",
			]

	# `data_sized` rather than `sized_by`, which holds a path and holds nothing
	# for `body[n + 1]`. Both asked the question themselves and both missed the
	# arithmetic form, so a run the data sizes was shown at its *minimum* --
	# one byte for `[n + 1]` -- and everything after it was placed on top of
	# its bytes. Silently: the note above the member still said "after a member
	# the data sizes", which is the one part of the output that was right.
	if data_sized(placement) or placement.array_count is not None:
		return _repeated(resolved, struct, placement, field, seek)

	# A bit field whose numbering runs from the least significant bit reads
	# its container the other way round: earlier bytes carry the less
	# significant bits, so the mask below applies to a LITTLE-endian
	# assembly. `add` reads big-endian and showed `usb_token.endpoint` --
	# four bits across a byte boundary -- as 4 where every other reading
	# says 14. Only a straddling field is affected: within one byte the two
	# assemblies are the same byte (26.375).
	#
	# And the other half: a BIT-PACKED field is assembled by its bit order
	# and never by `endian`, which orders whole-byte scalars -- the rule
	# every backend's load follows (`scalar.is_bit_packed`). An MSB-first
	# bit field in an `endian little` schema was assembled little-endian
	# here, and every straddling field of five layouts showed a number no
	# other reader gives (26.960).
	scalar = placement.scalar
	if scalar is not None and scalar.is_bit_packed:
		add = ("add_le" if placement.bit_order is ast.BitOrder.LSB_FIRST
		       and (placement.size_bits or 0) % BITS_PER_BYTE else "add")
	elif placement.bit_order is ast.BitOrder.LSB_FIRST \
			and (placement.size_bits or 0) % BITS_PER_BYTE:
		add = "add_le"
	else:
		add = "add_le" if placement.endian is ast.Endian.LITTLE else "add"
	# Where a marker decides, the message does: the method is chosen as
	# the member is shown (26.963).
	opener = f"subtree:{add}("
	if placement.marker is not None and not (scalar is not None
	                                         and scalar.is_bit_packed):
		little = _little(placement)
		if little is not None:
			opener = (f"subtree[{little} and \"add_le\" or \"add\"]"
			          "(subtree, ")
	# A coded or sealed region the data sizes has no span, whatever its
	# minimum is: `byte_span` answers with `size_bits`, which for one is
	# the fixed part of its interior, and the member after it was placed
	# there -- `coded body(c) { u16 x; u8 data[n]; }` showed `k` two bytes
	# in for any `n` (26.965).
	span = (None if placement.kind in ("coded", "sealed")
	        and not placement.is_fixed_size else byte_span(placement))

	# The note the module docstring has promised since this backend was
	# written. `add` on a `ProtoField.bytes` shows the bytes whichever way it
	# is called, so the order argument is moot here -- what matters is that
	# the reader is told why they are looking at bytes rather than a number.
	note = ([f"\t-- {placement.path}: `endian native`. The capture does not"
	         " record which",
	         "\t-- machine wrote these, so they are shown as bytes rather"
	         " than guessed at."]
	        if _host_order(placement) else [])

	# Read at the frame's end and not at the cursor, which it leaves alone
	# (26.905).
	if placement.from_end is not None:
		back = placement.from_end
		wide = placement.size_bits // BITS_PER_BYTE
		return [
			f"\t-- {placement.path}: {back} bytes from the end of the frame",
			*note,
			f"\tif tvb:len() >= {back} then",
			f"\t\t{opener}{field}, tvb(tvb:len() - {back}, {wide}))",
			"\tend",
		]

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
			length = _length(resolved, struct, placement, "0", "at")
			if length is None:
				return _uncomputable(placement.path,
				                     "a length this dissector cannot render")
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
			f"\t\t{opener}{field}, tvb(at, {width}))",
			"\tend",
			# A peeked member is SHOWN and not spent (0057): its lead is
			# gone -- `seek` took it -- and its own byte belongs to whatever
			# comes next, which is the arm a variant selects. Advancing here
			# put every member after it one byte late. Invisible in
			# `edges.kinded`, whose variant assigns `at` outright and
			# overwrote the mistake; visible in `example/json`, where the
			# discriminant is one this dissector cannot read, so the step
			# was all that was left and `value` reported a byte too many.
			*([] if placement.peek or placement.shares_byte
			  else [f"\tat = at + {width}"]),
		]

	first, count = span
	return [
		*note,
		f"\t{opener}{field}, tvb({first}, {count}))",
		# `peek` for the reason above. This branch appears to be
		# unobservable rather than merely untested: a peeked member is
		# always a discriminant, a variant either follows and assigns `at`
		# outright or cannot read it, and a discriminant at a static offset
		# is always readable. Sabotaging it leaves the suite green. Carried
		# anyway, because two branches of one rule that disagree are how the
		# reachable one gets "fixed" back to the wrong answer later.
		f"\tat = {first if placement.peek or placement.shares_byte else first + count}",
	]


def _region_length(resolved: ResolvedSchema, struct: ResolvedStruct,
		region: Placement, base: str) -> str | None:
	"""A coded or sealed region's bytes on the wire, in Lua (26.965).

	The backends' rule, `traverse.region_extent`: the interior's extent --
	its fixed part and each member the data sizes -- put through the
	codec's expansion, in the same integer arithmetic C's
	`_region_length` uses. None where that rule has no closed form, or
	where the interior's extent is versioned, and the caller declines.
	"""
	codec = _CODECS.get(region.type_name or "")
	rule  = traverse.region_extent(struct, region, codec, resolved.structs)
	if rule is None or rule.version is not None:
		return None
	parts = [str(rule.constant)]
	for member in rule.variable:
		length = _length(resolved, struct, member, base)
		if length is None:
			return None
		parts.append(f"({length})")
	inner = "(" + " + ".join(parts) + ")"
	if rule.kind == "preserving":
		return inner
	if rule.kind == "add":
		return f"({inner} + {rule.add})"
	if rule.kind == "ratio":
		return f"math.floor({inner} * {rule.out} / {rule.into})"
	return (f"(math.floor(({inner} + {rule.group_in - 1}) / {rule.group_in})"
	        f" * {rule.group_out})")


def _dynamic_width(placement: Placement) -> int | None:
	"""A fixed-width member's bytes, where its offset is not fixed."""
	if placement.scalar is None or placement.array_count is not None:
		return None
	if not placement.is_fixed_size or placement.size_bits % BITS_PER_BYTE:
		return None
	return placement.size_bits // BITS_PER_BYTE



def _scan_call(placement: Placement, at: str) -> str:
	"""`situ_scan(...)` for one member, from `at`.

	Shared with `_delimited` so that the scan a length expression performs
	and the scan the member's own row performs cannot describe the member
	differently -- which is the shape of every defect in this file today.
	"""
	alts = ", ".join("{" + ", ".join(str(byte) for byte in one) + "}"
	                 for one in placement.delimiters)
	cap  = ("tvb:len()" if placement.delimiter_cap is None
	        else f"math.min({at} + {placement.delimiter_cap}, tvb:len())")
	quote  = ("nil" if placement.delimiter_quote is None
	          else str(placement.delimiter_quote))
	escape = ("nil" if placement.delimiter_escape is None
	          else str(placement.delimiter_escape))
	return f"situ_scan(tvb, {at}, {{{alts}}}, {cap}, {quote}, {escape})"


def _footprint_call(placement: Placement, at: str) -> str:
	"""How many bytes a delimited member occupies from `at`, in Lua.

	`_scan_call`'s content length, and the delimiter where it was found and
	the member owns it -- what `_delimited` advances by, as an expression.
	Without it a struct holding a delimited member had no extent here, so
	an HTTP header line could not be an element of anything this walks.
	"""
	consumed = "true" if placement.delimiter_consumed else "false"
	return f"situ_footprint({consumed}, {_scan_call(placement, at)})"


def _terminators(placement: Placement) -> str:
	"""A run's terminators as a Lua table of byte tables."""
	return "{" + ", ".join("{" + ", ".join(str(byte) for byte in one) + "}"
	                       for one in placement.delimiters) + "}"


def _record_walk(resolved: ResolvedSchema, placement: Placement,
		element: ResolvedStruct, dissect: str | None) -> list[str]:
	"""A run of records to its terminator, walked from `at` (26.930).

	The walk every reader makes: where a terminator stands the run ends,
	stepping past the longest one only where the run owns it; elsewhere an
	element starts, is measured, and is dissected when `dissect` names the
	call. An element of no extent or one the frame does not hold ends the
	walk, the two bounds every generated walk carries (invariant 24).
	"""
	return [
		"\twhile at < tvb:len() do",
		f"\t\tlocal took = situ_stands(tvb, at, {_terminators(placement)})",
		"\t\tif took > 0 then",
		*(["\t\t\tat = at + took"] if placement.delimiter_consumed else
		  ["\t\t\t-- `before`: the terminator is the next member's"]),
		"\t\t\tbreak",
		"\t\tend",
		f"\t\tlocal size = {_element_stride(resolved, element)}",
		"\t\tif size == 0 or at + size > tvb:len() then break end",
		*([f"\t\t{dissect}"] if dissect else []),
		"\t\tat = at + size",
		"\tend",
	]


def _at(base: str, offset: int) -> str:
	"""`base + offset`, without the halves that are zero."""
	if base == "0":
		return str(offset)
	return base if offset == 0 else f"{base} + {offset}"


#: Length fields the body being written has read at the cursor: path -> local.
#: Empty outside `_dissector`, because only its walk has a cursor (26.900).
_captured: dict[str, str] = {}


def _capturable(struct: ResolvedStruct,
		members: list[Placement]) -> list[Placement]:
	"""What drives a later member from an offset the message decided.

	`u8 n; u8 s[n]; u16 m; u8 t[m];` has `m` where only the walk knows, so
	no constant offset reads it. Everything after `t` then went unshown, and
	that is most formats past their first variable member.
	"""
	# And what a variant switches on: `cluster` peeks a byte after a
	# varint ID, at an offset only the walk knows, and its variant was
	# declined as reading what this cannot (26.932).
	# And what a member hands its child (0068): DNS hands every `rdata`
	# arm `size = rdlength`, past an owner name only the walk measures, and
	# the variant was declined as having an arm of no length.
	named = " ".join([*(held.sized_by or held.size_expr or held.discriminant
	                    or "" for held in members),
	                  *(source for entry in struct.entries
	                    for _, source in entry.placement.arguments)])
	return [held for held in members
	        if _varint_value(held) is not None and held.offset_bits is None
	        and re.search(rf"\b{re.escape(held.name)}\b", named)
	        or held.kind == "field" and held.scalar is not None
	        and held.offset_bits is None and held.array_count is None
	        and not held.scalar.signed and held.radix is None
	        and held.located is None and held.since is None
	        and held.size_bits in (8, 16, 24, 32)
	        and not _host_order(held)
	        and held.marker is None
	        and re.search(rf"\b{re.escape(held.name)}\b", named)]


def _capture_line(placement: Placement, spot: str) -> str:
	"""`local <name>_value = ...`, a length's driver read where it stands.

	Shared by a dissector's body, where it stands at `at`, and an extent
	function, where it stands at the running `n`, and recorded in
	`_captured` so that `_read` names the local rather than an offset.
	"""
	name = f"{_lua(placement.name)}_value"
	_captured[placement.path] = name
	# Where its lead ends, for a member that skips one: read at the cursor,
	# JSON's `kind` read the space in front of `[` and chose the number arm
	# for an array (26.938).
	if placement.skip:
		lead = "{" + ", ".join(str(one) for one in placement.skip) + "}"
		spot = f"{spot} + situ_skip(tvb, {spot}, {lead})"
	decl = _varint_value(placement)
	if decl is not None:
		return f"local {name} = select(2, {_varint_name(decl)}(tvb, {spot}))"
	little = "true" if placement.endian is ast.Endian.LITTLE else "false"
	if placement.from_end is not None:
		spot = f"tvb:len() - {placement.from_end}"
	return (f"local {name} = situ_uint(tvb, {spot}, "
	        f"{placement.size_bits // BITS_PER_BYTE}, {little})")


def _read(placement: Placement, base: str) -> str | None:
	"""Reading one field's value in Lua, from `base`.

	Arithmetic rather than a bit library. Wireshark ships Lua BitOp in most
	builds and not in all of them, and a dissector that fails to load is worse
	than one that divides: these are field widths, not hot code.
	"""
	if placement.path in _captured:
		return _captured[placement.path]
	# A varint's value is what it spells, which only a walk of its bytes
	# says. At a fixed offset, from the byte it starts in -- the one it
	# shares with the fields before it, for the forms that share one.
	decl = _varint_value(placement)
	if decl is not None and placement.offset_bits is not None:
		start = placement.offset_bits // BITS_PER_BYTE
		return f"select(2, {_varint_name(decl)}(tvb, {_at(base, start)}))"
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
			#
			# A DELIMITED one is as wide as its digits and `count` above is
			# the scalar's width, so this read a fixed two-byte window at
			# the member's start. `texty` is `decimal u16 count until
			# "\r\n"` driving `u8 body[count]`, and a frame declaring 123
			# was dissected as 12 -- a valid message, every member after the
			# run placed on the wrong bytes, and Lua's `tonumber` tolerating
			# a trailing `\r` is the only reason one and two digits worked.
			#
			# Scanned inline rather than declined: the parentheses truncate
			# `situ_scan` to its first return, and the helper is emitted for
			# any schema that has a delimiter to scan for.
			if placement.delimiters:
				return (f"situ_digits(tvb, {_at(base, first)}, "
				        f"({_scan_call(placement, _at(base, first))}), "
				        f"{placement.radix})")
			helper = ("situ_digits_padded" if placement.radix_padded
			          else "situ_digits")
			return (f"{helper}(tvb, {_at(base, first)}, {count},"
			        f" {placement.radix})")

		if _host_order(placement):
			return None		# no answer a capture can give
		little = _little(placement, base)
		if little is None or (count > 4 and placement.marker is not None):
			return None
		if count > 4:
			# `uint` tops out at four bytes, so a wider one is two reads
			# and arithmetic: exact as a Lua 5.4 integer, and as near as a
			# float is in 5.2. Matroska's `wide_cluster` switches on a
			# peeked u64 -- the unknown size, 0x01FFFFFFFFFFFFFF -- and was
			# declined as reading what this cannot (26.939).
			if count > 8 or placement.scalar is None \
					or placement.scalar.signed:
				return None
			rest = count - 4
			if little == "true":
				return (f"(situ_uint(tvb, {_at(base, first + 4)}, {rest},"
				        f" true) * 4294967296 + situ_uint(tvb,"
				        f" {_at(base, first)}, 4, true))")
			return (f"(situ_uint(tvb, {_at(base, first)}, 4, false) *"
			        f" {1 << (8 * rest)} + situ_uint(tvb,"
			        f" {_at(base, first + 4)}, {rest}, false))")
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

#: Each enum's members by name, for an expression that names one.
_ENUMS: dict[str, dict[str, int]] = {}

#: Every struct, for what a member hands its type, set beside `_CONSTS`.
_STRUCTS: dict[str, ResolvedStruct] = {}

#: Every codec, for a region's expansion (26.965).
_CODECS: dict[str, ast.CodecDecl] = {}


#: Every `endian_marker`, for the literal a governed member is compared against.
_MARKERS: dict[str, ast.EndianMarkerDecl] = {}


def _little(placement: Placement, base: str = "0") -> str | None:
	"""Whether `placement` reads little-endian, as a Lua boolean expression.

	`true` or `false` where the schema decides. Where an `endian_marker`
	does, the message decides, and this read the member big-endian whatever
	the marker said: a little-endian TIFF showed `magic` as 10752, and a
	pcap file's `version_major` as 512 (26.963). So the marker is read
	here, big-endian as every reader reads one, and compared against its
	`little` literal -- or, where the struct takes its order as an argument
	(26.961), the argument is.

	None where the marker is somewhere this cannot read from the struct's
	own frame, and the caller declines as for any read it cannot make.
	"""
	if placement.marker is None:
		return "true" if placement.endian is ast.Endian.LITTLE else "false"
	owner = _STRUCTS.get(placement.path.partition(".")[0])
	if owner is None:
		return None
	given = traverse.marker_argument(owner, placement)
	if given is not None:
		littles = _little_literals(_MARKERS.get(given.type_name or ""))
		if not littles:
			return None
		held = f"{_lua(owner.name)}_arg.{_lua(given.name)}"
		return "(" + " or ".join(f"{held} == {one}" for one in littles) + ")"
	marker = next((held for held in own_members(owner)
	               if held.kind == "marker" and held.name == placement.marker),
	              None)
	decl    = None if marker is None else _MARKERS.get(marker.type_name or "")
	# The literals as the packer takes them for the walkers' table.
	littles = _little_literals(decl)
	if marker is None or not littles or marker.offset_bits is None \
			or marker.offset_bits % BITS_PER_BYTE \
			or marker.size_bits not in (8, 16, 24, 32):
		return None
	held = (f"situ_uint(tvb, {_at(base, marker.offset_bits // BITS_PER_BYTE)},"
	        f" {marker.size_bits // BITS_PER_BYTE}, false)")
	return "(" + " or ".join(f"{held} == {one}" for one in littles) + ")"


def _little_literals(decl: ast.EndianMarkerDecl | None) -> list[int]:
	"""Every literal value that says little, where all are plain numbers:
	a format may write more than one for an order (26.975)."""
	if decl is None:
		return []
	found = [getattr(one, "value", None) for one in decl.literals("little")]
	return [int(one) for one in found if one is not None] \
		if all(one is not None for one in found) else []


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
#: of `example/`, and `test/schema/edges.situ` has one in every operator on
#: this list -- which is what it is for. Nothing reached them, because until
#: now every member sized by arithmetic was declined for a different reason.
#: `&&` and `||` are not on it: `translate_operators` renders those as `and`
#: and `or`, and every `while` condition in this repository has one -- the
#: first version of this matched the halves and declined every run walk in the
#: file.
_UNSPELLABLE = re.compile(r"/|\^|<<|>>|(?<!&)&(?!&)|(?<!\|)\|(?!\|)")


def _over_fields(struct: ResolvedStruct, source: str, base: str,
		extra: dict[str, str] | None = None,
		tree: "ast.Expr | None" = None) -> str | None:
	"""A schema expression rewritten as Lua reads over `base`.

	`extra` carries names that are not fields of `struct` -- `remaining` is
	the only one, and only a repeat-while asks for it. The four code backends
	took this parameter when respec reported `remaining` reaching `wire` and
	dying in the backends; the dissector is the fifth description and was not
	given it then, so it declined the whole condition and the run came out as
	a comment. A gap in one description is invisible to a differential that
	compares only what both sides speak about.

	`align_up` comes out as `math.floor` arithmetic rather than as `//`, for
	decision 0021's reason: Wireshark bundles Lua 5.2 in older builds, `//`
	is a syntax error there, and a dissector that fails to load is worse than
	one that divides. A bare `/` in a schema expression is the one place this
	file still parts from the compiled backends -- Lua's is float division,
	theirs truncates -- and no schema in this repository has one (26.37).
	"""
	if "sum(" in source:
		try:
			source, sums = with_sums(
				source, lambda run: _sum_read(struct, run, base))
		except UnknownName:
			return None
		extra = {**(extra or {}), **sums}

	reads: dict[str, str] = {}
	for entry in struct.entries:
		held = entry.placement
		if held.scalar is None and _varint_value(held) is None:
			continue
		# Counted from a struct the message places, and `_read` takes the
		# number as a place in the frame. Left out, so an expression naming
		# it is declined rather than read wrong (26.916).
		if nest_of(struct, local_name(struct, held)) is not None:
			continue
		# Nested scalars too: a cpio entry's padding is an expression over
		# `header.namesize`, and stopping at the dot left the dotted path in
		# the emitted Lua as a global that does not exist.
		local = held.path[len(struct.name) + 1:]
		if "." in local and held.offset_bits is None:
			continue
		# A `[stream]` parameter is read from the dissector's own
		# preference rather than from the capture (0050). Nothing in a
		# capture carries an argument, which is why a per-message one
		# cannot reach a dissector at all and is refused before this.
		if held.parameter:
			reads[local] = f"{_lua(struct.name)}_arg.{_lua(held.name)}"
			continue
		one = _read(held, base)
		if one is not None:
			reads[local] = one

	for name, value in _CONSTS.items():
		reads.setdefault(name, str(value))
	# And an enum's members, which every other description folds as the
	# constants they are: `span_base.narrow + n` was "a size this
	# dissector cannot read" for the enumerator, not for `n`.
	for enum, members in _ENUMS.items():
		for member, value in members.items():
			reads.setdefault(f"{enum}.{member}", str(value))
	if extra is not None:
		reads.update(extra)

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
		# The tree, where the caller has it: shifts by constants, masks of
		# contiguous bits and `|` over disjoint bits are arithmetic, and
		# anything else is still declined (26.940).
		if tree is None:
			return None
		widths = {entry.placement.path[len(struct.name) + 1:]:
		          entry.placement.size_bits
		          for entry in struct.entries
		          if entry.placement.scalar is not None
		          and not entry.placement.scalar.signed}
		return _lua_expr(tree, reads, widths)
	try:
		return expand_calls(
			over_fields(sorted(reads), source, lambda name: reads[name]),
			lua_spelling)
	except KeyError:
		return None		# names something this cannot read


def _sum_read(struct: ResolvedStruct, run: str, base: str) -> str | None:
	"""`sum(run)` as Lua, where the run sits at a constant offset.

	A table of lengths comes before what it sizes and usually first, and one
	placed by the data has no offset an expression can name, so that declines.
	"""
	member = next((entry.placement for entry in struct.entries
	               if entry.placement.path == f"{struct.name}.{run}"), None)
	if member is None or member.offset_bits is None:
		return None
	count = (str(member.array_count) if member.array_count is not None
	         else _over_fields(struct, member.size_expr or member.sized_by or "",
	                           base))
	if count is None:
		return None
	width  = summed_width(member)
	little = _little(member, base)
	if little is None:
		return None
	return (f"situ_sum(tvb, {_at(base, member.offset_bits // BITS_PER_BYTE)},"
	        f" {count}, {width}, {little})")


def _extent_functions(resolved: ResolvedSchema) -> list[str]:
	"""Every measurable struct's extent, in an order Lua accepts.

	`local function` binds where it is written, so one calling another has to
	come after it, and a struct is emitted once everything it names has been.
	That means its arms too: reading members alone, `ipv4_option` came before
	the `sized_option` an arm names, and `tcp_option` worked because of how
	its name sorts (26.913). A struct that can contain itself has no extent,
	so waiting on one would be a cycle and is not asked for.
	"""
	pending = dict(sorted(resolved.structs.items()))
	lines: list[str] = []
	done: set[str] = set()

	cycle = [struct for name, struct in pending.items()
	         if is_recursive(resolved.structs, name)
	         and _extent_terms(resolved, struct) is not None]
	if cycle:
		lines.extend([
			"",
			"-- A struct that can contain itself, declared ahead so that its",
			"-- cycle's extents can call each other, and how deep each is now.",
			"local " + ", ".join(_extent_name(one) for one in cycle),
			*(f"local {_nesting_name(one)} = 0" for one in cycle),
		])

	while pending:
		ready = [name for name, struct in pending.items()
		         if all(held.placement.type_name in done
		                or held.placement.type_name not in resolved.structs
		                or is_recursive(resolved.structs,
		                                held.placement.type_name)
		                for held in struct.entries)]
		if not ready:
			break			# a cycle the resolver should have refused
		for name in ready:
			struct = pending.pop(name)
			# The run's walk first: the struct's own extent sums it, and the
			# walk needs the element's extent, which is already emitted
			# because the element is a struct this one names.
			for entry in struct.entries:
				lines.extend(_run_span(resolved, struct, entry.placement))
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
	if placement.varint is not None and _one_varint(placement) is None:
		return _varint_run_span(struct, placement)
	counted = is_counted_run(resolved.structs, placement)
	record  = bool(placement.delimiters) \
		and placement.type_name in resolved.structs
	if placement.repeat_while is None and not counted and not record:
		return []

	element = resolved.structs.get(placement.type_name or "")
	if element is None or _element_stride(resolved, element) is None:
		return []

	if record:
		return [
			"",
			f"-- How far `{placement.path}` reaches from `at`: its records,"
			" and the",
			"-- terminator that ends them where the run owns it.",
			f"local function {_span_name(struct, placement)}"
			"(tvb, at, at_struct)",
			"	local _ = at_struct",
			"	local start = at",
			*_record_walk(resolved, placement, element, None),
			"	return at - start",
			"end",
		]

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
	if _over_fields(element, placement.repeat_while, "last",
			_condition_reads(element)) is None:
		return []
	condition = _run_condition(resolved, struct, placement)

	name = _span_name(struct, placement)
	cap  = placement.repeat_cap
	guard = "" if cap is None else f" and n < {cap}"
	return [
		"",
		f"-- How far `{placement.path}` reaches from `at`.",
		f"-- The run ends after the element for which"
		f" `{placement.repeat_shown or placement.repeat_while}`",
		"-- is false -- that element is part of it, the condition being asked",
		"-- once it has been read.",
		f"local function {name}(tvb, at, at_struct)",
		"	local _ = at_struct",
		"	local start = at",
		"	local n = 0",
		f"	while at < tvb:len(){guard} do",
		f"		local size = {_element_stride(resolved, element)}",
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


def _element_stride(resolved: ResolvedSchema,
		element: ResolvedStruct) -> str | None:
	"""One element's size in bytes, as Lua over `tvb` and `at`.

	`traverse.extent_parts` answers None for two different structs: a fixed
	one, because there is nothing to compute, and an unmeasurable one,
	because there is nothing to compute it from. Reading the first as the
	second is what made a `while` run over fixed elements dissect as
	`elements of no size this dissector can compute` -- which was false, the
	element being eight bytes. Two states with one piece of evidence, which
	invariant 154 says to tell apart rather than to collapse.

	The four code backends had the fixed arm already: C walks the same run
	with `SITU_CHUNK_SIZE_FIXED`. This is that arm in Lua, and the reason it
	is a function rather than a line at each site is that three sites ask.
	"""
	if element.layout.is_fixed_size:
		return (str(element.layout.size_bytes)
		        if element.layout.is_byte_sized else None)
	if _extent_terms(resolved, element) is None:
		return None
	return f"{_extent_name(element)}(tvb, at)"


def _counted_count(struct: ResolvedStruct, placement: Placement,
		base: str) -> str | None:
	"""How many records a counted run holds, as Lua over `base`.

	A count the SCHEMA gives as well: neither `size_expr` nor `sized_by`
	holds one, so `_over_fields` was handed "" and answered "" -- not None,
	so the guard let it through and the walk came out `while n < () and
	...`, which is not Lua (26.267).
	"""
	if placement.array_count is not None:
		return str(placement.array_count)
	source = placement.size_expr or placement.sized_by
	if not source:
		return None
	count = _over_fields(struct, source, base,
	                     tree=placement.size_ast if placement.size_expr
	                     else None)
	return f"({count})" if count else None


def _counted_span(resolved: ResolvedSchema, struct: ResolvedStruct,
		placement: Placement, element: ResolvedStruct) -> list[str]:
	"""How far a counted run of variable records reaches, as a Lua function.

	The four backends' `_span_from` in Lua: walk until the count runs out,
	and stop early on a zero-length element or one the frame does not hold --
	the two bounds every generated walk carries (invariant 24).
	"""
	# The count is the caller's, rendered where it is readable: a count
	# standing past a variable member is readable only as a value the
	# caller's walk captured, which this function, emitted once for every
	# caller, cannot name. Reading it from `at_struct` here named a span
	# that was then never written when the count was such a one (26.936).
	name = _span_name(struct, placement)
	return [
		"",
		f"-- How far `{placement.path}` reaches from `at`.",
		"-- The count says how many, and each element says how long it is, so",
		"-- the run is walked rather than multiplied.",
		f"local function {name}(tvb, at, count)",
		"	local start = at",
		"	local n = 0",
		"	while n < count and at < tvb:len() do",
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
	if not parts:
		return [
			"",
			f"-- How many bytes one `{struct.name}` occupies at `at`.",
			f"local function {_extent_name(struct)}(tvb, at)",
			f"	return {_versioned_constant(struct, constant)}",
			"end",
		]

	# Accumulating, not a sum of independent terms. Each term is measured
	# where its member starts, and where a member starts is everything before
	# it -- so a flat sum measured the second variable member, and every
	# nested struct after the first, from the start of the frame. It read as
	# one expression and was M answers about one offset.
	body = ["	local n = at", *_extent_steps(resolved, struct)]
	if struct.name in _DEPTHS:
		# Declared ahead of every extent, so the cycle's functions can name
		# each other, and bounded the way a walker is: past the struct's
		# depth it measures nothing, and a run or a nested member asked
		# about a zero-length element stops there.
		depth = _DEPTHS[struct.name]
		count = _nesting_name(struct)
		return [
			"",
			f"-- How many bytes one `{struct.name}` occupies at `at`, to"
			f" {depth} deep.",
			f"{_extent_name(struct)} = function(tvb, at)",
			f"	if {count} >= {depth} then return 0 end",
			f"	{count} = {count} + 1",
			*body,
			f"	{count} = {count} - 1",
			"	return n - at",
			"end",
		]
	return [
		"",
		f"-- How many bytes one `{struct.name}` occupies at `at`.",
		"--",
		"-- Accumulating, because a term is measured where its member starts",
		"-- and that is the sum of everything before it.",
		f"local function {_extent_name(struct)}(tvb, at)",
		*body,
		"	return n - at",
		"end",
	]


def _nesting_name(struct: ResolvedStruct) -> str:
	"""How deep the walk is inside `struct` right now, a Lua local."""
	return f"{_lua(struct.name)}_nesting"


def _extent_steps(resolved: ResolvedSchema,
		struct: ResolvedStruct) -> list[str]:
	"""Each member's bytes, in order, as Lua measured from the running `n`.

	Runs of fixed members collapse into one number, which is what
	`traverse.extent_parts` does for the constant and what keeps the emitted
	arithmetic the shape a reader can check against the map.
	"""
	steps: list[str] = []
	bits  = 0
	tail  = versioned_tail(resolved.structs, struct)
	added = _version_term(tail) if tail is not None else None

	with _capturing():
		capturable = {held.path
		              for held in _capturable(struct, own_members(struct))}
		for placement in own_members(struct):
			if added is not None and placement.since:
				continue		# by version, below (26.768)
			# A field a later length reads, at an offset only this walk
			# knows: read where it stands, as the body reads it at `at`.
			if placement.path in capturable \
					and bits % BITS_PER_BYTE == 0:
				spot = "n" if not bits else f"n + {bits // BITS_PER_BYTE}"
				steps.append(f"\t{_capture_line(placement, spot)}")
			# The SPAN, as every other function that adds members up asks
			# it (`traverse.fixed_span_bits`): a `parameter` and a `peek`
			# occupy nothing. This added `size_bits`, so `entry`'s extent
			# counted its `parameter u16 block` as two bytes (26.808).
			if occupies_fixed_bytes(placement):
				bits += fixed_span_bits(placement)
				continue
			if bits:
				steps.append(f"\tn = n + {bits // BITS_PER_BYTE}")
				bits = 0
			one = _length(resolved, struct, placement, "at", "n")
			steps.append(f"\tn = n + {'0' if one is None else one}")
	if bits:
		steps.append(f"\tn = n + {bits // BITS_PER_BYTE}")
	if added is not None:
		steps.append(f"\tn = n + {added}")
	return steps


@contextlib.contextmanager
def _capturing() -> Iterator[None]:
	"""A fresh `_captured` for one walk, and the caller's back after it.

	An extent is asked for in the middle of a dissector's body -- a run's
	stride, a nested member's size -- whose own captures name locals of
	that body, which mean nothing inside the extent function.
	"""
	saved = dict(_captured)
	_captured.clear()
	try:
		yield
	finally:
		_captured.clear()
		_captured.update(saved)


def _read_version(field: Placement) -> str | None:
	"""The version field read at `at`, or None where a capture cannot say.

	`situ_uint` answers zero for a field the frame does not reach, which
	is version zero and so the shortest length -- the answer the four
	backends give a frame too short to say.
	"""
	span = byte_span(field)
	if span is None or _host_order(field):
		return None
	first, count = span
	little = _little(field, "at")
	if little is None:
		return None
	return f"situ_uint(tvb, at + {first}, {count}, {little})"


def _versioned_constant(struct: ResolvedStruct, constant: int) -> str:
	"""A fixed-offset struct's length at the message's own version.

	`extent_parts` answers the newest version's for one whose members all
	sit at static offsets, and the four backends answer by version since
	26.766 (`traverse.version_steps`). Without it a version-1 struct nested
	in another placed everything after it where a newest one ends.
	"""
	by_version = version_steps(struct)
	if by_version is None:
		return str(constant)
	field, base, after = by_version
	read = _read_version(field)
	if read is None:
		return str(constant)
	chain = str(base)
	for since, end in after:
		chain = f"(({read} >= {since}) and {end} or {chain})"
	return chain


def _version_term(tail: tuple[Placement, int,
                              tuple[tuple[int, int], ...]]) -> str | None:
	"""The bytes a variable struct's `[since]` tail carries at the
	message's version (26.768), or None where the version is unreadable
	and the tail is summed as it was."""
	field, _, steps = tail
	read = _read_version(field)
	if read is None:
		return None
	chain   = "0"
	carried = 0
	for since, added in steps:
		carried += added
		chain = f"(({read} >= {since}) and {carried} or {chain})"
	return chain


def _extent_terms(resolved: ResolvedSchema,
		struct: ResolvedStruct) -> tuple[int, list[str]] | None:
	# A struct that can contain itself is measured on the assumption that
	# its own cycle measures, which is `traverse.has_computable_extent`'s
	# `seen`: asked without it, the question did not terminate through an
	# arm that is the recursion, and `gen-dissector` died on the stack
	# (26.904). Its function is declared ahead of every extent and bounded
	# by the struct's `[depth]` or `[limit]` (26.936).
	if struct.name in _MEASURING:
		return 0, []
	if not _MEASURING and struct.name in _TERMS:
		return _TERMS[struct.name]
	_MEASURING.add(struct.name)
	try:
		answer = _measured_terms(resolved, struct)
	finally:
		_MEASURING.discard(struct.name)
	if not _MEASURING:
		_TERMS[struct.name] = answer
	return answer


#: Structs whose extent is being asked for, outermost first, so a cycle is
#: assumed to measure rather than asked again forever.
_MEASURING: set[str] = set()

#: Each struct's extent terms, asked once per schema: a recursive struct's
#: question reaches every struct in its cycle, every time it is asked.
_TERMS: dict[str, tuple[int, list[str]] | None] = {}

#: How deep the dissector follows each recursive struct (`[limit]`, else
#: `[depth]`), filled once per schema.
_DEPTHS: dict[str, int] = {}


def _measured_terms(resolved: ResolvedSchema,
		struct: ResolvedStruct) -> tuple[int, list[str]] | None:
	"""`_extent_terms`, asked afresh."""
	parts = extent_parts(resolved.structs, struct)
	if parts is None:
		return None

	constant, variable = parts
	terms: list[str] = []
	# In order, capturing as `_extent_steps` will, so a length reading a
	# field past a variable member renders: DNS's `rdata` arms read
	# `rdlength`, which stands after the owner name (26.934).
	with _capturing():
		capturable = {held.path
		              for held in _capturable(struct, own_members(struct))}
		for placement in own_members(struct):
			if placement.path in capturable:
				_capture_line(placement, "n")
			if not any(one is placement for one in variable):
				continue
			one = _length(resolved, struct, placement)
			if one is None:
				return None
			terms.append(one)
	return constant, terms


def _length(resolved: ResolvedSchema, struct: ResolvedStruct,
		placement: Placement, base: str = "at",
		where: str | None = None) -> str | None:
	"""How many bytes one variable member occupies, in Lua.

	`base` is where the *struct* starts. In an extent function that is `at`,
	which is what the function is handed; in a dissector it is 0, because the
	member is dissected from a tvb of its own and `at` has already walked past
	the fields the length is read from. Getting that wrong read the
	discriminant from the byte after itself.

	`where` is where the *member* starts, which is a different question and was
	answered with the first: a length field is read at the struct's base plus
	its own constant offset, and a *measurement* -- a nested struct's extent, a
	scan, a walk -- begins where the member does. The two coincide only for a
	struct's first member, which every variable member in this repository
	happens to be. A composed schema put a nested struct after a
	variable-length one and `s_extent` measured it from the start of the
	frame.
	"""
	where = base if where is None else where
	# A run measures every element with what the parent hands it (0068):
	# the whole measurement runs with the values set, as `_handing_run`
	# dissects it.
	if placement.arguments \
			and not nests_one_struct(resolved.structs, placement):
		values = _handed(struct, placement, base)
		plain  = _length(resolved, struct,
		                 dataclasses.replace(placement, arguments=()),
		                 base, where)
		if values is None or plain is None:
			return None
		return (f"handing({_lua(placement.type_name or '')}_arg, {values},"
		        f" function() return {plain} end)")
	# A member's lead, and then the member where the lead ends. It had no
	# length, so a JSON member -- `u8 open skip`, whitespace and a quote --
	# left `member`, `object`, `array` and every value holding one without
	# an extent (26.938). The member itself by its SPAN: a peek that skips
	# owns its lead and occupies nothing after it.
	if placement.skip:
		lead  = ("situ_skip(tvb, {at}, {{"
		         + ", ".join(str(one) for one in placement.skip) + "}})")
		after = f"({where} + {lead.format(at=where)})"
		inner = dataclasses.replace(placement, skip=())
		if occupies_fixed_bytes(inner):
			span = fixed_span_bits(inner)
			if span % BITS_PER_BYTE:
				return None
			rest: str | None = str(span // BITS_PER_BYTE)
		else:
			rest = _length(resolved, struct, inner, base, after)
		if rest is None:
			return None
		return f"({lead.format(at=where)} + {rest})"

	if placement.kind == "variant":
		return _variant_length(resolved, struct, placement, base, where)

	if placement.kind in ("coded", "sealed"):
		return _region_length(resolved, struct, placement, base)

	# A pad, to the next multiple of its unit from where the struct starts,
	# as the body's own pad counts from the start of the struct's tvb. An
	# arm that is only a pad -- radiotap's `[align = 8]` before `mactime`
	# -- had no length, so the variant holding it stopped the cursor and no
	# field after the presence words was shown (26.932).
	if placement.pad_to:
		unit = placement.pad_to
		into = where if base == "0" else f"({where} - {base})"
		return f"(({unit} - {into} % {unit}) % {unit})"

	if placement.varint is not None and _one_varint(placement) is None:
		count = _varint_run_count(struct, placement, base)
		if count is None or not _varint_run_span(struct, placement):
			return None
		return f"{_span_name(struct, placement)}(tvb, {where}, {count})"

	decl = _one_varint(placement)
	if decl is not None:
		# The parentheses keep the width and drop the value.
		return f"({_varint_name(decl)}(tvb, {where}))"

	if placement.repeat_while is not None \
			or is_counted_run(resolved.structs, placement) \
			or (placement.delimiters
			    and placement.type_name in resolved.structs):
		# Before the nested-struct case below, which named the element's
		# extent and so measured a run of labels as one label.
		if not _run_span(resolved, struct, placement):
			return None
		if is_counted_run(resolved.structs, placement):
			count = _counted_count(struct, placement, base)
			if count is None:
				return None
			return (f"{_span_name(struct, placement)}(tvb, {where},"
			        f" {count})")
		# From where the *run* starts, not where the struct does. Every run in
		# this repository is its struct's first member, so the two coincided
		# and the walk was handed the struct's base -- which for a run at any
		# other offset walks the members before it as though they were
		# elements. A composed schema put one at offset 3 and said so.
		return f"{_span_name(struct, placement)}(tvb, {where}, {base})"

	# Measured where the member starts, as its row scans it.
	if placement.delimiters:
		return _footprint_call(placement, where)

	if placement.size_expr is not None:
		rendered = _over_fields(struct, placement.size_expr, base,
		                        tree=placement.size_ast)
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
			and placement.array_count is None and not placement.delimiters:
		# ...and only where that extent function exists. A struct ending in
		# `[remaining]` has none -- where it ends is where the *frame* does,
		# which its own bytes never say -- and naming one anyway is a Lua
		# global that is nil, which is an error at the first packet rather
		# than a bad number. MQTT's SUBSCRIBE body is that shape and is an
		# arm of a variant, so the variant's own length reached for it.
		if _extent_terms(resolved, nested) is None:
			return None
		if placement.arguments:
			return _handing_call(struct, placement, base,
			                     _extent_name(nested), "tvb", where)
		return f"{_extent_name(nested)}(tvb, {where})"

	if placement.is_fixed_size and placement.size_bits % BITS_PER_BYTE == 0:
		return str(placement.size_bits // BITS_PER_BYTE)
	return None


def _variant_length(resolved: ResolvedSchema, struct: ResolvedStruct,
		placement: Placement, base: str = "at",
		where: str | None = None) -> str | None:
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
		# Where the variant starts, as `_length` measures any member: an arm
		# after a variable member was measured from the struct's start.
		one = _length(resolved, struct, member, base, where)
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
	that matters. Which arm is added is a question about the packet.

	An arm whose type is a STRUCT has no `ProtoField` of its own: `_field`
	returns "" for one, because a nested struct gets a subtree instead. This
	said "every arm has a ProtoField" and added one anyway, so such an arm
	emitted `subtree:add(nil, ...)` -- a Lua error at the first packet
	rather than a wrong display. No schema in the tree had the shape until
	0057's corpus struct, so nothing had ever run it.

	Asked through `_field` rather than by repeating its predicate here: two
	copies of one rule is a second thing to be wrong, and the first version
	of this fix repeated it.
	"""
	# Both declines below go through `_uncomputable`, which stops the
	# cursor: a bare comment left `at` where the variant began, and every
	# member after it was placed there (26.803).
	held = _over_fields(struct, placement.discriminant or "", "0")
	if held is None:
		return _uncomputable(placement.path,
		                     _unreadable(struct, placement.discriminant))

	lines = [f"\t-- {placement.path}: whichever arm"
	         f" `{placement.discriminant}` selects", *seek,
	         f"\tlocal arm = {held}"]
	last = own_members(struct)[-1] is placement

	first = True
	for arm, member in arm_members(struct, placement):
		if member is None:
			continue		# `default: error`; nothing to show
		length = _length(resolved, struct, member, "0", "at")
		# An arm that runs to the frame's end has the rest of the buffer,
		# where nothing follows the variant -- fuzznet's call `frame`, whose
		# body ends in `u8 media[remaining]`, and with it the whole variant
		# was declined (26.981).
		if length is None and last \
				and member.type_name in resolved.structs \
				and ends_in_the_frame(resolved.structs,
				                      resolved.structs[member.type_name]):
			length = "math.max(0, tvb:len() - at)"
		if length is None:
			return _uncomputable(placement.path, "one arm has no length"
			                     " this dissector can compute")

		name  = _lua(_local(struct, member))
		field = f"{_lua(struct.name)}.fields.{name}"
		test  = ("else" if arm.value is None
		         else f"{'if' if first else 'elseif'} arm == {arm.value} then")
		shown = bool(_field(resolved, struct, member))
		# Little-endian as a member outside an arm is: this said `add`
		# for every arm, so radiotap's `mactime` read byte-reversed in
		# Wireshark once its arms were reached (26.932).
		add   = ("add_le" if member.scalar is not None
		         and member.endian is ast.Endian.LITTLE else "add")
		adder = f"subtree:{add}("
		little = (_little(member) if member.scalar is not None
		          and member.marker is not None else None)
		if little is not None:
			adder = (f"subtree[{little} and \"add_le\" or \"add\"]"
			         "(subtree, ")
		# One struct goes to its own dissector, as a nested member does;
		# it was stepped over, so radiotap's channel was never shown.
		nested = (not shown
		          and nests_one_struct(resolved.structs, member))
		call   = (f"Dissector.get(\"{member.type_name}\"):call("
		          "tvb(at, n):tvb(), pinfo, subtree)")
		if nested and member.arguments:
			# Inside `handing`, as a nested member with arguments is: the
			# child reads `<type>_arg` and finds what this arm hands it.
			call = _handing_call(struct, member, "0",
			                     f"function() return {call} end") or ""
			nested = bool(call)
		lines.extend([
			f"\t{test}" if arm.value is None else f"\t{test}",
			f"\t\tlocal n = {length}",
			*([
				"\t\tif n > 0 and tvb:len() >= at + n then",
				f"\t\t\t{adder}{field}, tvb(at, n))",
				"\t\tend",
			] if shown else [
				"\t\tif n > 0 and tvb:len() >= at + n then",
				f"\t\t\t{call}",
				"\t\tend",
			] if nested else [
				f"\t\t-- `{member.name}` has no field of its own to add",
				"\t\t-- here. The bytes are still stepped over, or every",
				"\t\t-- member after this one would be placed on the wrong",
				"\t\t-- ones.",
			]),
			"\t\tat = at + n",
		])
		first = False

	if first:
		return [f"\t-- {placement.path}: every arm selects nothing"]

	lines.append("\tend")
	return lines


#: The field names an arithmetic size expression mentions. `relation.paths_in`
#: walks an AST and this has only the rendered text, which is what the
#: placement carries by the time a dissector sees it.
def paths_in_expr(source: str) -> list[str]:
	return re.findall(r"[A-Za-z_][A-Za-z0-9_.]*", source)


def _count_declined(resolved: ResolvedSchema, struct: ResolvedStruct,
		placement: Placement) -> str:
	"""Why a run's count cannot be read, in the reader's terms.

	One message covered five refusals: a driver at a sub-byte offset, a
	varint driver, an operator decision 0021 declines, a host-order driver,
	and a driver genuinely absent. Only the last is "cannot locate", and the
	other four were reported as it -- so nine members in the corpus named
	the wrong cause, and `edges`' `arithmetic` sent a reader looking for a
	field that is right there. `_run_declined` above argues this for the
	other half of the same question; this is the half it did not cover.
	"""
	written = placement.size_shown or placement.size_expr or placement.sized_by
	if placement.size_expr is not None and _UNSPELLABLE.search(placement.size_expr):
		return (f"sized by `{written}`, whose operator this dissector does"
		        " not spell (decision 0021)")

	# The driver of an arithmetic size is named inside the expression rather
	# than in `sized_by`, which holds a bare path and holds nothing for one.
	# Looking only there reported `varint_driver.d`, sized by `n + 1`, as a
	# field nobody could find -- when `n` is right there and is a varint.
	named = placement.sized_by
	if named is None and placement.size_expr is not None:
		for path in paths_in_expr(placement.size_expr):
			if resolved.find(f"{struct.name}.{path}") is not None:
				named = path
				break

	driver = resolved.find(f"{struct.name}.{named}") if named else None
	if driver is None:
		return f"sized by `{written}`, which this dissector cannot locate"
	if driver.placement.varint is not None:
		return (f"sized by `{written}`, a varint, whose width is in its own"
		        " bytes")
	if _host_order(driver.placement):
		return (f"sized by `{written}`, which is `endian native`: the capture"
		        " does not record which machine wrote it")
	return f"sized by `{written}`, which this dissector cannot read"


def _run_declined(resolved: ResolvedSchema, placement: Placement,
		element: ResolvedStruct | None) -> str:
	"""Why a `while` run is not walked, in the reader's terms.

	One message covered two refusals: an element whose size this cannot
	compute, and a *condition* it cannot render. They want opposite looks --
	the first sends a reader to the element, the second to the operator --
	and a run over a two-byte element reported the size as the reason. A
	specific wrong cause is more actionable than a vague one and sends the
	next person somewhere, which is why it is worth separating rather than
	softening.
	"""
	if element is None:
		return "elements of a type this dissector does not know"
	if _element_stride(resolved, element) is None:
		return "elements of no size this dissector can compute"
	if placement.repeat_while is not None and _over_fields(
			element, placement.repeat_while, "last",
			_condition_reads(element)) is None:
		return "a condition this dissector cannot render"
	return "elements of no size this dissector can compute"


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
		return _uncomputable(placement.path,
		                     _run_declined(resolved, placement, element))

	cap   = placement.repeat_cap
	guard = "" if cap is None else f" and n < {cap}"
	return [
		f"\t-- {placement.path}: walked, not indexed", *seek,
		"\tlocal n = 0",
		f"\twhile at < tvb:len(){guard} do",
		f"\t\tlocal size = {_element_stride(resolved, element)}",
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


#: What `remaining` means where a repeat-while asks it: the bytes after the
#: element just read, the condition being asked once the cursor has passed it.
#:
#: The other four saturate -- `situ_remaining_u32` in C, `saturating_sub` in
#: Rust, `max(0, ...)` in Python -- and this does not, because it cannot go
#: negative and because spelling it `math.max` here does not survive. Both
#: walks break when `at + size > tvb:len()`, so `at` is at most the length
#: after the advance the condition is asked about. And the builtin expansion
#: rewrites `max` wherever it finds one, including inside what it has just
#: produced, which is `math.math.max` (invariant 53) -- the same trap
#: `situ_digits` carries a comment about.
RUN_EXTRA = {"remaining": "(tvb:len() - at)"}


def _condition_reads(element: ResolvedStruct) -> dict[str, str]:
	"""What a `while` condition may read of the element just walked.

	`RUN_EXTRA`, and every field at an offset the walk decides that stands
	a fixed distance from the element's END: the condition is asked once
	the cursor has passed the element, so `at` is its end. JSON's
	`member entries[] while (sep == ',')` reads `sep`, the member's last
	byte, behind a key and a value of any length; with no constant offset
	it could not be read, and objects, arrays and every value holding one
	had no extent here (26.938).
	"""
	reads = dict(RUN_EXTRA)
	behind = 0
	for held in reversed(own_members(element)):
		# A member that skips a lead is its value's width from wherever the
		# lead ends, so its value stands that far from the end -- and
		# nothing before it does, the lead being any length. JSON's `sep`
		# is `u8 sep skip`.
		lead = bool(held.skip)
		inner = dataclasses.replace(held, skip=()) if lead else held
		if not occupies_fixed_bytes(inner):
			break
		span = fixed_span_bits(inner)
		if span % BITS_PER_BYTE:
			break
		behind += span // BITS_PER_BYTE
		if held.offset_bits is not None or held.scalar is None \
				or held.scalar.signed or held.size_bits not in (8, 16, 24, 32) \
				or _host_order(held) or held.radix is not None \
				or held.marker is not None:
			if lead:
				break
			continue
		little = "true" if held.endian is ast.Endian.LITTLE else "false"
		# `behind` counts this field and everything after it, so the
		# field starts that far back from the element's end.
		reads.setdefault(
			held.name,
			f"situ_uint(tvb, at - {behind}, "
			f"{held.size_bits // BITS_PER_BYTE}, {little})")
		if lead:
			break
	return reads


def _run_condition(resolved: ResolvedSchema, struct: ResolvedStruct,
		placement: Placement) -> str:
	element = resolved.structs[placement.type_name or ""]
	source  = _over_fields(element, placement.repeat_while or "", "last",
	                       _condition_reads(element))
	assert source is not None, "_run_span checks this first"
	return translate_operators(source, conj=" and ", disj=" or ",
	                           ne=" ~= ", neg="not ")


def _delimited(resolved: ResolvedSchema, struct: ResolvedStruct,
		placement: Placement, field: str, seek: list[str]) -> list[str]:
	"""A member that ends at a delimiter (section 8.6.1).

	The scan is emitted rather than the length, because there is no length:
	Wireshark has the same bytes situ does and has to look for the same thing
	in them. A run of records is walked instead: where a terminator stands
	the run ends, and elsewhere an element starts and goes to its own
	dissector. It said "Not unrolled" and stopped the cursor until 26.931,
	so nothing after an HTTP request's headers was ever shown.
	"""
	name  = _lua(_local(struct, placement))
	delim = placement.delimiter
	assert delim is not None

	element = resolved.structs.get(placement.type_name or "")
	if element is not None:
		shown = " or ".join(render_delimiter(one)
		                    for one in placement.delimiters)
		if _host_order_inside(element):
			return _uncomputable(placement.path, "an element's length is"
			                     " `endian native`, and the capture does"
			                     " not record which machine wrote it")
		# Declined with `_LOST`, which stops the cursor: advancing by
		# nothing drew `edges.kv_block.payload` from offset 0, over the
		# run it follows.
		if _element_stride(resolved, element) is None:
			return _uncomputable(placement.path,
			                     _run_declined(resolved, placement, element))
		return [
			f"\t-- {placement.path}: a run of {placement.type_name} to "
			f"{shown}, walked", *seek,
			*_record_walk(resolved, placement, element,
			              f"Dissector.get(\"{placement.type_name}\"):call("
			              "tvb(at, size):tvb(), pinfo, subtree)"),
		]

	# EVERY alternative, not just the first. `placement.delimiter` is the
	# one this used to read, and `until "\r\n" | "\n"` has two: a header
	# line ending in a bare newline was never found, so the member ran to
	# the end of the buffer and everything after it moved.
	return [
		f"\t-- {placement.path}, to the first {render_delimiter(delim)}",
		*seek,
		f"\tlocal {name}_len, {name}_end, {name}_took =",
		f"\t\t{_scan_call(placement, 'at')}",
		# `[trim]` moves the VALUE and not the frame, so the advance below
		# reads `_len` either way. Shown trimmed because that is what every
		# generated accessor hands a caller: `_ptr` starts past the
		# whitespace and `_len` stops before it.
		*([f"\tlocal {name}_at, {name}_shown = situ_trim(tvb, at, "
		   f"{name}_len,",
		   "\t\t{" + ", ".join(str(byte) for byte in placement.trim_set)
		   + "})",
		   f"\tif {name}_shown > 0 then",
		   f"\t\tsubtree:add({field}, tvb({name}_at, {name}_shown))",
		   "\tend"] if placement.trimmed else
		  [f"\tif {name}_len > 0 then",
		   f"\t\tsubtree:add({field}, tvb(at, {name}_len))",
		   "\tend"]),
		f"\tat = at + {name}_len",
		# Three conditions, and this had none of them. The delimiter is
		# consumed because it was FOUND rather than because the buffer had
		# room; by the width that MATCHED rather than the first
		# alternative's; and only where the member owns it -- `before` says
		# the delimiter belongs to what follows, so consuming it there put
		# the next member one byte on.
		(f"\tif {name}_end then" if placement.delimiter_consumed else
		 f"\t-- `before`: the delimiter belongs to the member after this"
		 f" one,\n\t-- so `at` stops in front of it rather than past it."),
		*([f"\t\tat = at + {name}_took", "\tend"]
		  if placement.delimiter_consumed else []),
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
		# The shown form: this is a note for a person reading the dissector,
		# and `size_expr` is parenthesised at every operator so that a host
		# compiler cannot regroup it.
		return [
			f"\t-- {placement.path}: "
			f"{_count_declined(resolved, struct, placement)};",
			"\t-- the rest of the frame is shown raw",
			*seek,
			"\tif tvb:len() > at then",
			f"\t\tsubtree:add({field}, tvb(at))",
			"\tend",
			"\tat = tvb:len()",
		]

	if placement.sized_by == "remaining" \
			and (placement.tail_bytes or placement.tail_source is not None):
		# The frame less the fixed members after it (26.894), and less what
		# a value read earlier says follows it (26.905).
		left: int | str = placement.tail_bytes
		if placement.tail_source is not None:
			left = (f"({placement.tail_bytes} + "
			        f"{_over_fields(struct, placement.tail_source, '0')})")
		return [
			f"\t-- {placement.path}: to {left} bytes from the end of the frame",
			*seek,
			f"\tif tvb:len() - {left} > at then",
			f"\t\tsubtree:add({field}, tvb(at, tvb:len() - {left} - at))",
			f"\t\tat = tvb:len() - {left}",
			"\tend",
		]

	if placement.sized_by == "remaining" and element is not None:
		stride = _element_stride(resolved, element)
		if stride is not None:
			# Each element dissected, as a counted run's is: a whole list of
			# records in one raw row says nothing about any of them.
			return [
				f"\t-- {placement.path}: walked to the end of the frame",
				*seek,
				"\twhile at < tvb:len() do",
				f"\t\tlocal size = {stride}",
				"\t\tif size == 0 or at + size > tvb:len() then break end",
				f"\t\tDissector.get(\"{placement.type_name}\"):call("
				"tvb(at, size):tvb(), pinfo, subtree)",
				"\t\tat = at + size",
				"\tend",
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
	stride = None if element is None else _element_stride(resolved, element)
	if each is None and stride is not None and element is not None \
			and not _host_order_inside(element):
		# A count of records each saying how long it is: walked, as the
		# extent's `_counted_span` walks it, each element to its own
		# dissector. It declined, so a Bitcoin transaction showed its
		# version and nothing after its inputs (26.932).
		return [
			f"\t-- {placement.path}: "
			f"{placement.size_shown or placement.size_expr or placement.sized_by or placement.array_count}"
			" records, each as long as it says", *seek,
			f"\tlocal {name}_n = {count}",
			f"\tlocal {name}_k = 0",
			f"\twhile {name}_k < {name}_n and at < tvb:len() do",
			f"\t\tlocal size = {stride}",
			"\t\tif size == 0 or at + size > tvb:len() then break end",
			f"\t\tDissector.get(\"{placement.type_name}\"):call("
			"tvb(at, size):tvb(), pinfo, subtree)",
			"\t\tat = at + size",
			f"\t\t{name}_k = {name}_k + 1",
			"\tend",
		]
	if each is None:
		return _uncomputable(placement.path, "elements of no fixed size")

	lines = [f"\t-- {placement.path}", *seek, f"\tlocal {name}_n = {count}"]

	if element is None:
		# Scalar elements: one range, shown as bytes.
		scale = "" if each == 1 else f" * {each}"
		# `[size = N]` pins the FOOTPRINT while the length stays whatever
		# the message says, so the member is `min(declared, N)` bytes and
		# the next member starts N on regardless. `traverse.pinned_bytes`
		# is the shared decision and its docstring counts six readers; this
		# was a seventh that never asked. `pinned.body[used] [size = 16]`
		# with `used = 48` was shown as 48 bytes, three times the member.
		footprint = pinned_bytes(placement)
		lines.extend([
			f"\tlocal {name}_len = {name}_n{scale}",
			*([f"\tif {name}_len > {footprint} then",
			   f"\t\t{name}_len = {footprint}\t-- `[size = {footprint}]`",
			   "\tend"] if footprint is not None else []),
			f"\tif tvb:len() >= at + {name}_len then",
			f"\t\tsubtree:add({field}, tvb(at, {name}_len))",
			"\tend",
			# Clamped, because the length is the message's claim and not the
			# frame's: the `if` above already declines to *show* bytes that
			# are not there, and the advance below went on to count them
			# anyway. A dissector returns what it consumed, so an eight-byte
			# UDP header declaring a length of 24 reported consuming 24 --
			# and the run walking it is the same shape.
			(f"\tat = math.min(at + {footprint}, tvb:len())"
			 if footprint is not None else
			 f"\tat = math.min(at + {name}_len, tvb:len())"),
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
		return _over_fields(struct, placement.size_expr, "0",
		                    tree=placement.size_ast)

	driver = resolved.find(f"{struct.name}.{placement.sized_by}")
	if driver is None:
		return None

	# A `[stream]` driver is an argument rather than bytes, so `_read`
	# below would read the buffer at its offset -- which is where this very
	# member begins, a parameter occupying nothing. Read from the
	# preference instead, which is what `_over_fields` does for the
	# arithmetic form a few lines above; without this the two forms of one
	# construct disagreed, and the plain `body[n]` was the one that was
	# wrong (0050).
	if driver.placement.parameter:
		return f"{_lua(struct.name)}_arg.{_lua(driver.placement.name)}"

	# `_read`, rather than a second reader beside it. This hand-rolled the
	# load and said so -- "`_read` above says the same thing for the same
	# reason; this is the copy that decides how long a *run* is" -- and the
	# copy knew less than the original. It refused a driver at a sub-byte
	# offset outright, so `edges`' `packed_driver.d`, sized by a nibble, was
	# declined with "sized by `n`, which this dissector cannot locate" while
	# `_read` spells that very nibble as
	# `(situ_uint(tvb, 0, 1, false) % 16)` and the same generated file shows
	# the field two lines above the claim. A parallel accessor family is a
	# backend that has to be finished twice (invariant 77), and this is the
	# half that was not.
	#
	# `_read` answers None for what it genuinely cannot read -- a host-order
	# field, a width Wireshark's `uint` cannot take -- so the refusals that
	# were true survive and only the false ones go.
	# Counted from a struct the message places, and `_read` takes the number
	# as a place in the frame: declined rather than read wrong (26.916).
	if nest_of(struct, local_name(struct, driver.placement)) is not None:
		return None
	return _read(driver.placement, "0")


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
	return re.sub("[<>]", "", local_name(struct, placement))


#: Lua's own words. A schema may name a field `function` or `end` -- situ
#: reserves neither -- and Lua will not take one as a table key or a local, so
#: the generated file does not parse at all. Modbus is the first schema here
#: with such a field: `function_code function;` is what section 4.1 of the
#: application protocol calls it, and calling it anything else in the schema
#: to suit the dissector would be the tail wagging the protocol (26.57).
#: What a dissector function names itself: its parameters and its cursor. A
#: proto of one of these was hidden inside its own dissector, so `struct tree`
#: made `tree:add(tree, tvb())` add the tree to itself and not load (26.908).
LUA_TAKEN = frozenset({"tree", "tvb", "pinfo", "subtree", "at"})

LUA_KEYWORDS = frozenset({
	"and", "break", "do", "else", "elseif", "end", "false", "for", "function",
	"goto", "if", "in", "local", "nil", "not", "or", "repeat", "return",
	"then", "true", "until", "while",
})


def _lua(name: str) -> str:
	"""A Lua identifier. `::` from a namespace is the only thing that appears
	in a situ name and not in a Lua one -- and a keyword, which is not a
	spelling problem but a parse error.

	Suffixed rather than renamed: what Wireshark *shows* is the abbrev and the
	display name, which are strings and keep the schema's spelling, so the
	underscore is visible only to somebody reading the Lua.
	"""
	flattened = "".join(char if char.isalnum() or char == "_" else "_"
	                    for char in name)
	return f"{flattened}_" if flattened in LUA_KEYWORDS | LUA_TAKEN \
		else flattened
