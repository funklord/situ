"""Walking a buffer with an image.

The sixth spelling of `traverse.py`. The four backends and `gen-dissector`
are five renderings of the same questions -- which entries are a struct's own
members, which bytes a placement occupies, what order to ask what kind of
member it is -- and this is those answers read from a table instead of
compiled in.

What it cannot do is the thing decision 0026 says to write down plainly: an
interpreter cannot make an operation *absent*. Every accessor here is a
function that may refuse at run time, where a generated API simply has no
setter for a member it cannot write. Under a walker the capability map stops
being the shape of the interface and becomes data a caller may consult.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Literal

from walker import vm
from walker.image import BIG, LITTLE, NATIVE, NONE, Image, Placement, Struct

BITS_PER_BYTE = 8


class Refused(Exception):
	"""The buffer does not support the question.

	One exception rather than a returned sentinel, because every backend
	refuses these cases and a walker that answered zero where C returns an
	error would disagree with all four while looking like it agreed.
	"""


class Unplaceable(Refused):
	"""...and this one is not about the buffer at all.

	A member nothing can place, whatever bytes it is given: something before
	it has no length in closed form, so no backend emits an offset for it
	and none checks it either. That is a different answer from a frame too
	short to hold it -- one is BOUNDS and the other is silence -- and
	folding the two together made a struct whose members ran off the end
	report OK, because the walk stopped where it should have refused.

	A subclass, so a caller that does not care still catches `Refused`.
	"""


@dataclass
class View:
	"""A struct over a buffer: a base, a limit, and the image behind them."""

	image: Image
	buffer: bytes
	struct: int
	at: int
	limit: int

	@property
	def shape(self) -> Struct:
		return self.image.structs[self.struct]


def acquire(image: Image, buffer: bytes, struct: int) -> View:
	"""The one bounds check, which everything after it trusts.

	A fixed struct needs its whole size present; a frame takes what there is.
	Section 20.2 makes this the check every constant-offset access below it
	depends on, and it is where two backends once disagreed with the other
	two (26.27).
	"""
	shape = image.structs[struct]
	if shape.fixed:
		need = (shape.size_bits + BITS_PER_BYTE - 1) // BITS_PER_BYTE
		if len(buffer) < need:
			raise Refused(f"frame of {len(buffer)} does not reach {need}")
	return View(image, buffer, struct, 0, len(buffer))


def offset_bits(view: View, index: int) -> int:
	"""Where a member starts, in bits from the view's base.

	A constant where the image knows one. Otherwise the members before it
	are summed, which is the answer `offset = Dynamic` names -- and the
	reason a walk costs what the capability map says it costs.
	"""
	placement = view.image.placements[index]
	if placement.located_code != NONE:
		return _evaluate(view, placement.located_code) * BITS_PER_BYTE
	if placement.offset_known:
		return placement.offset_bits

	total = 0
	for before in view.image.members(view.shape):
		if before == index:
			return total
		earlier = view.image.placements[before]
		if earlier.located_code != NONE:
			continue		# a located member joins no offset chain
		total += size_bits(view, before)
	raise Refused(f"placement {index} is not a member of this struct")


def size_bits(view: View, index: int) -> int:
	"""How many bits a member occupies.

	A delimited member's is the scan's answer plus the delimiter itself: the
	content stops where the delimiter starts, and the *member* ends where the
	delimiter does, or the member after it would begin on the terminator.
	Where the delimiter is absent the member is truncated and reaches as far
	as it got, which is what makes everything after it unplaceable rather
	than merely short.
	"""
	placement = view.image.placements[index]
	if placement.repeat_code != NONE and placement.type_struct != NONE:
		# A `while` run's extent is however far the walk got. Falling
		# through to the record's `size_bits` gave the *minimum* -- one
		# element -- so a struct holding one measured a byte where it held
		# two, and dnsname's `qname` sub-view came back the wrong length.
		return _while_walk(view, index)[1] * BITS_PER_BYTE
	if index in view.image.arms:
		return _variant_bits(view, index)
	if index in view.image.varints:
		# A varint's width is in its own bytes. The record's `size_bits` is
		# the minimum -- one byte -- so summing that placed everything after
		# a varint at the wrong offset, which is what kept sqlite's
		# `payload` unrenderable.
		#
		# A *truncated* one is zero bytes wide, not a refusal. This is the
		# lax reader again: C's `_len` returns 0 where the encoding runs off
		# the end and goes on placing what follows, and only the getter
		# refuses. Refusing here made `02 c3 a9` -- a one-byte payload size
		# and a rowid that never terminates -- come back BOUNDS from a
		# struct C reads to the end.
		try:
			consumed, _ = varint(view, index)
		except Refused:
			return 0
		return consumed * BITS_PER_BYTE
	# `type_struct` is what separates the two uses of `until`. A member ends
	# at the first occurrence of its delimiter, anywhere; a *run of records*
	# ends where the terminator stands in for a record, checked at each
	# element boundary and nowhere else -- 8.6.3, and `edges` says it is the
	# one construct in the tree where the delimiter is not looked for
	# anywhere. Scanning for it made `kv_block.payload` 935 bytes long where
	# every backend said nothing.
	# A text number's width is its digits, not its value's. `decimal u32
	# n[4]` is four bytes holding one number, and reading `[4]` as four
	# 32-bit elements made it sixteen -- which put `edges`' `text_driver`
	# tail twelve bytes past where every backend places it. The delimited
	# form is measured by its scan below; this is the fixed-width one,
	# whose digit count the image carries beside the radix.
	if placement.radix and placement.radix_digits \
			and index not in view.image.delimiters:
		return placement.radix_digits * BITS_PER_BYTE
	if index in view.image.delimiters and placement.type_struct == NONE:
		content, terminated = scan(view, index)
		# The delimiter is part of the member only when it is there. An
		# unterminated one reached as far as the cap or the buffer allowed,
		# and the member after it starts at that point rather than being
		# unplaceable: `verb[] until " " max 16` over bytes with no space in
		# them is sixteen bytes long, and smtp's `argument` begins at
		# sixteen. Refusing instead dropped every member after a truncated
		# one, which is not what any backend does.
		width = content + (len(view.image.delimiters[index]) if terminated
		                   else 0)
		return width * BITS_PER_BYTE
	if placement.size_code != NONE:
		count = _evaluate(view, placement.size_code,
		                  offset_bits(view, index) // BITS_PER_BYTE)
		if count < 0:
			raise Refused("a computed size is negative")
		# A counted run of *variable-length* elements. The count says how
		# many, each element says how long it is, and there is no stride to
		# multiply -- so the fallback below is wrong by exactly the factor
		# the elements vary by. C shipped this once and placed whatever
		# followed such a run `count` bytes past its start, "a plausible
		# number, which is the failure this repository rates worst"
		# (`edges`' own comment on `segments`).
		#
		# Refused rather than walked. The walk could add the elements up,
		# and then it would be the only implementation that could: no
		# backend emits a span for one, so nothing after such a run is
		# placed by anybody, and a walk that placed it would disagree with
		# all four about where the next member is.
		if placement.type_struct != NONE and placement.element_bits == NONE:
			raise Unplaceable(
				f"placement {index} is a counted run of variable-length "
				"elements, which has no stride")
		element = (placement.element_bits
		           if placement.element_bits != NONE else BITS_PER_BYTE)
		return count * element
	# A nested struct with no single size is as long as its own bytes turn
	# out to be, and `placement.size_bits` is the *minimum* -- which for
	# dnsname's `question.qname` is one byte, the length of a name holding
	# nothing but its root label. Everything after such a member was then
	# placed inside it: `qtype` at byte 1 instead of 55, comfortably within
	# a frame it does not actually reach, so `validate` said OK where C
	# said BOUNDS.
	#
	# Only where the struct can be measured from its own bytes. Where it
	# cannot, no backend places what follows either, and the minimum is not
	# a better guess than refusing.
	#
	# A member of *this* struct, and the test has to come first. An arm is
	# not one, and asking `offset_bits` for one walks the struct summing
	# every member -- including the variant this arm belongs to, whose
	# extent is the arm's. `_variant_bits` calls straight back into here and
	# the two recur until the stack ends. The arm's extent is the variant
	# member's and `_variant_bits` already knows it, so there is nothing to
	# measure here.
	shape = view.image.structs[view.shape] if isinstance(view.shape, int) \
		else view.shape
	if placement.type_struct != NONE \
			and index in view.image.members(shape) \
			and not view.image.structs[placement.type_struct].fixed:
		if not view.image.structs[placement.type_struct].measurable:
			raise Unplaceable(
				f"placement {index} is a struct that cannot be measured "
				"from its own bytes")
		inner = View(view.image, view.buffer, placement.type_struct,
		             view.at + offset_bits(view, index) // BITS_PER_BYTE,
		             view.limit)
		return struct_extent(inner) * BITS_PER_BYTE

	if placement.size_bits == NONE:
		raise Refused(f"placement {index} has no size this image carries")
	return placement.size_bits


def _variant_bits(view: View, index: int) -> int:
	"""A variant's extent: the arm the discriminant selects, not the worst
	case and not the minimum.

	"It cannot be computed" is often "it is not a constant" (invariant 37).
	A variant's extent is a switch: read the discriminant this message
	carries, take the arm it names, and the answer is that arm's size. Using
	the minimum instead made a dnsname label one byte long and walked
	thirty-nine of them through a thirty-eight byte buffer.
	"""
	selects, arms = view.image.arms[index]
	if selects == NONE:
		raise Refused(f"variant {index} has no discriminant in this image")

	value = read_scalar(view, selects)
	fallback = None
	for case, chosen, flags in arms:
		if flags & 2:				# `default: error` selects nothing
			continue
		if flags & 1:				# the default arm
			fallback = chosen
			continue
		if case == value:
			return 0 if chosen == NONE else size_bits(view, chosen)
	if fallback is not None and fallback != NONE:
		return size_bits(view, fallback)
	# No arm matches and the default selects nothing. The extent is zero
	# rather than a refusal: a discriminant naming no arm is a malformed
	# message, and saying so is `validate`'s job, not the extent's. C's
	# generated extent has the same `: 0u`, and refusing here counted zero
	# dnsname labels where every backend counted one.
	return 0


def _value_of(view: View, index: int) -> int:
	"""A member's value as a size or offset expression reads it.

	The lax half of a distinction every backend makes and this walker did
	not. C emits two readers for a text number: `_get`, which returns an
	error when the bytes are not digits, and `_value`, which cannot fail and
	yields zero when they are not. Expressions call `_value` -- a length is
	needed to place the next member whether or not the field parsed -- and
	`validate` calls `_get`, which is where a field that is not a number is
	called malformed.

	Reading both through the strict one made `edges`' `text_driver` answer
	BOUNDS over a buffer whose four digit bytes were `" 1\n4"`, where C
	sized `d[n]` at zero and said the message was fine.
	"""
	if view.image.placements[index].radix:
		try:
			return parse_digits(view, index)
		except Refused:
			return 0
	return read_scalar(view, index)


def _evaluate(view: View, code_at: int, from_byte: int = 0) -> int:
	"""Run one program, with `remaining` measured from `from_byte`.

	`remaining` is "to the end of the enclosing frame" *from here*, not the
	frame's whole length. Evaluating it as the latter made every
	`[remaining]` run as long as the buffer rather than as long as what is
	left of it, which sqlite and ipv6ext both caught: 44 against C's 37, and
	38 against 46. The member's own offset is the thing the word is relative
	to, so it has to be passed in.
	"""
	return vm.run(
		view.image.code, code_at,
		load_field = lambda i: _value_of(view, i),
		size_of    = lambda i: size_bits(view, i) // BITS_PER_BYTE,
		offset_of  = lambda i: offset_bits(view, i) // BITS_PER_BYTE,
		count_of   = lambda i: _count(view, i),
		remaining  = max(0, view.limit - view.at - from_byte),
	)


def _count(view: View, index: int) -> int:
	placement = view.image.placements[index]
	if placement.array_count != NONE:
		return placement.array_count
	if placement.size_code != NONE:
		return _evaluate(view, placement.size_code,
		                 offset_bits(view, index) // BITS_PER_BYTE)
	raise Refused(f"placement {index} has no count this image carries")


def read_scalar(view: View, index: int) -> int:
	"""One member's value, bounds-checked against the frame.

	Bit-packed members are read from the bytes they straddle, most
	significant bit first where the schema says so, which is the one place a
	walker has to know something an offset table cannot say on its own.
	"""
	placement = view.image.placements[index]
	if placement.radix:
		return parse_digits(view, index)

	# A run is not a scalar, however few bytes it happens to hold. This used
	# to answer one -- `u8 name[n]` over "hello" came back as 448378203247,
	# the five bytes as an integer -- because the width fitted and nothing
	# asked whether the result meant anything. The C walker refused it, and
	# the differential between the two is what surfaced the disagreement;
	# `read_bytes` is the reader for these, and every probe that wants one
	# already calls it.
	if (placement.size_code != NONE or placement.array_count != NONE
			or placement.repeat_code != NONE):
		raise Refused(f"placement {index} is a run, not a scalar; its bytes "
		              f"are `read_bytes`")

	start = offset_bits(view, index)
	width = size_bits(view, index)
	if width <= 0 or width > 64:
		raise Refused(f"a {width}-bit scalar is not one to read")

	end = start + width
	if view.at * BITS_PER_BYTE + end > view.limit * BITS_PER_BYTE:
		raise Refused("the frame does not reach this member")
	return _read_at(view, index, start, width)


def _read_at(view: View, index: int, start: int, width: int) -> int:
	"""One value of `width` bits at `start`, in the member's own terms.

	Split out of `read_scalar` because an element of a run is the same read
	at a different offset, and two spellings of "how do these bits become a
	number" is how a backend and its own run accessor once disagreed.
	"""
	placement = view.image.placements[index]
	end = start + width
	if view.at * BITS_PER_BYTE + end > view.limit * BITS_PER_BYTE:
		raise Refused("the frame does not reach this value")

	if start % BITS_PER_BYTE == 0 and width % BITS_PER_BYTE == 0:
		first = view.at + start // BITS_PER_BYTE
		raw   = view.buffer[first:first + width // BITS_PER_BYTE]
		return _signed(int.from_bytes(raw, _order(placement)), width,
		               placement.signed)

	# Bit-packed: gather the bytes the value touches and shift it out. The
	# block covers whole bytes, so the value sits `after` bits from its end.
	first = start // BITS_PER_BYTE
	last  = (end + BITS_PER_BYTE - 1) // BITS_PER_BYTE
	block = int.from_bytes(view.buffer[view.at + first:view.at + last], "big")
	after = last * BITS_PER_BYTE - end
	return _signed((block >> after) & ((1 << width) - 1), width,
	               placement.signed)


def _order(placement: Placement) -> Literal["little", "big"]:
	"""Which end this member's bytes start at.

	`endian native` is the *host's* order and not a synonym for big: netlink
	is the format whose byte order is the sending machine's, and reading it
	as big-endian is how this walker first disagreed with C about
	`nlmsg_len`. A walk on a machine of the other endianness would have seen
	the mirror of that bug, which is the argument for the marker construct
	rather than for `native` (26.81).
	"""
	if placement.endian == LITTLE:
		return "little"
	if placement.endian == NATIVE:
		return "little" if sys.byteorder == "little" else "big"
	return "big"


def _signed(value: int, width: int, is_signed: bool) -> int:
	"""Two's complement, where the image says the value is signed.

	The image did not carry signedness at all until this walker read a BMP
	and disagreed with C about `i32 width` -- 3136328947 against
	-1158638349, one set of bits under two readings. Which of those is right
	is not something a walk can infer from the bytes, so it is a fact the
	image has to state (26.81).
	"""
	if not is_signed or width <= 0:
		return value
	sign = 1 << (width - 1)
	return value - (1 << width) if value & sign else value


def read_bytes(view: View, index: int) -> bytes:
	"""A member's bytes, for the runs and arrays that have no scalar value."""
	start = offset_bits(view, index)
	width = size_bits(view, index)
	if start % BITS_PER_BYTE or width % BITS_PER_BYTE:
		raise Refused("a byte run that does not start on a byte")
	first = view.at + start // BITS_PER_BYTE
	last  = first + width // BITS_PER_BYTE
	if last > view.limit:
		raise Refused("the frame does not reach this run")
	return view.buffer[first:last]


def scan(view: View, index: int) -> tuple[int, bool]:
	"""How far a delimited member reaches, and whether it was terminated.

	The two answers are separate on purpose, and the C runtime says why: a
	member whose delimiter is absent is *truncated*, not empty, and a getter
	is not the place to decide what to do about that. So this returns the
	content length and the fact, and the caller reports both.

	Naive matching, as the runtime does it: a delimiter is one or two bytes
	in every format this targets, and the generated code stays something a
	reader can check against the spec they are implementing.
	"""
	delim = view.image.delimiters.get(index)
	if not delim:
		raise Refused(f"placement {index} has no delimiter in this image")
	quote, escape, cap = view.image.delimiter_rules.get(index, (NONE, NONE, NONE))

	start = view.at + offset_bits(view, index) // BITS_PER_BYTE
	limit = max(0, view.limit - start)
	if cap != NONE:
		limit = min(limit, cap)

	data = view.buffer[start:start + limit]
	quoted, at = False, 0
	while at + len(delim) <= len(data):
		byte = data[at]
		if escape != NONE and byte == escape:
			at += 2			# the next byte is content, whatever it is
			continue
		if quote != NONE and byte == quote:
			quoted = not quoted
			at += 1
			continue
		if not quoted and data[at:at + len(delim)] == delim:
			return at, True
		at += 1
	return limit, False


def varint(view: View, index: int) -> tuple[int, int]:
	"""Decode one varint: the bytes it consumed and the value it holds.

	Two encodings, and the difference is which end the groups come from.
	`leb128` puts the low group first; `be128` the high one, which is ASN.1's
	identifier octets, MIDI's delta times and SQLite's record varints.

	`terminal_bits` of eight is the case worth naming: the last permitted
	byte has no spare bit for a continuation flag, so it is read whole and
	ends the value whatever its high bit says. That is SQLite's ninth byte,
	and it is why a nine-byte varint holds sixty-four bits where seven-bit
	groups would need ten.

	Raises where the buffer ends mid-value, which is what the *getter*
	does in every backend. The length accessor does not: it answers zero and
	lets the offset chain carry on, so `size_bits` catches this rather than
	propagating it. Two readers again, as for a text number.
	"""
	rules = view.image.varint_rules.get(index)
	if rules is None:
		raise Refused(f"placement {index} has no varint rules in this image")
	max_bytes, terminal_bits, big = rules

	start = view.at + offset_bits(view, index) // BITS_PER_BYTE
	avail = max(0, view.limit - start)
	data  = view.buffer[start:start + min(avail, max_bytes)]

	acc = 0
	for i, byte in enumerate(data):
		if big:
			if terminal_bits == 8 and i + 1 == max_bytes:
				return i + 1, (acc << 8) | byte
			acc = (acc << 7) | (byte & 0x7F)
		else:
			if i * 7 < 64:
				acc |= (byte & 0x7F) << (i * 7)
		if not byte & 0x80:
			return i + 1, acc
	raise Refused("the buffer ends mid-varint")


def digits_of(view: View, index: int) -> bytes:
	"""The bytes a text number is written in.

	Split out of `parse_digits` because `[minimal]` is a question about the
	spelling rather than the value -- a leading zero and an upper-case digit
	are both numbers that parse perfectly well -- and the two checks must
	read exactly the same bytes or they are checking different fields.
	"""
	if index in view.image.delimiters:
		content, _ = scan(view, index)
	else:
		content = size_bits(view, index) // BITS_PER_BYTE
	start = view.at + offset_bits(view, index) // BITS_PER_BYTE
	data  = bytes(view.buffer[start:start + content])
	if not data:
		raise Refused("a text number with no digits")
	return data


def parse_digits(view: View, index: int) -> int:
	"""A text number's value: digits, not bits (section 8.6.2).

	The scalar type beside it gives the value's domain rather than its width
	in the buffer, which for a text number depends on the number. Reading the
	bytes as an integer instead is what made `edges`' `texty.body` 38 bytes
	long over a buffer with no digits in it at all, where every backend said
	zero.

	A byte that is not a digit of this radix fails the whole parse, as the
	runtime does: a field that is meant to be a number and is not is a
	malformed message, not a number with some of it ignored.
	"""
	placement = view.image.placements[index]
	radix = placement.radix
	if radix < 2 or radix > 16:
		raise Refused(f"radix {radix} is not one to parse")

	data = digits_of(view, index)

	value = 0
	for byte in data:
		if 0x30 <= byte <= 0x39:
			digit = byte - 0x30
		elif 0x61 <= byte <= 0x66:
			digit = byte - 0x61 + 10
		elif 0x41 <= byte <= 0x46:
			digit = byte - 0x41 + 10
		else:
			raise Refused("a byte that is not a digit of this radix")
		if digit >= radix:
			raise Refused("a digit outside this radix")
		value = value * radix + digit
	return value


def while_count(view: View, index: int) -> int:
	"""How many elements a `while` run holds."""
	return _while_walk(view, index)[0]


def _while_walk(view: View, index: int) -> tuple[int, int]:
	"""How many elements a `while` run holds.

	The predicate is asked about the element *just parsed*, which is the
	whole difference from `until`: `until` asks about the position before
	each element -- is the terminator standing where one would start -- and
	`while` asks about the one behind it. So a `while` run is never empty:
	the first element is parsed before anything is asked.

	The predicate's names resolve in the *element's* struct rather than in
	the struct holding the run, which is netlink's `nla_ok`: `nla_len`
	belongs to the attribute and not to the message.
	"""
	placement = view.image.placements[index]
	if placement.repeat_code == NONE or placement.type_struct == NONE:
		raise Refused(f"placement {index} is not a `while` run")

	element = placement.type_struct
	cap     = placement.repeat_cap or 0xFFFF
	at      = view.at + offset_bits(view, index) // BITS_PER_BYTE
	count   = 0

	start = at
	while count < cap and at < view.limit:
		sub = View(view.image, view.buffer, element, at, view.limit)
		try:
			extent = struct_extent(sub)
		except Refused:
			break
		# The element has to *be there* before it counts. netlink's first
		# attribute declares a length past the end of the buffer, and every
		# backend answers zero: an element whose bytes the frame does not
		# hold is not a short element, it is not an element.
		if extent <= 0 or at + extent > view.limit:
			break
		count += 1
		at    += extent
		if not _evaluate(sub, placement.repeat_code):
			break
	return count, at - start


def struct_extent(view: View) -> int:
	"""How many bytes one instance of a struct occupies, from its own bytes.

	A fixed struct answers from the image. Anything else is the sum of what
	its members turn out to be, which is what makes a run of them walkable
	rather than indexable -- and is the cost `access = Sequential` records.
	"""
	shape = view.shape
	if shape.fixed:
		return (shape.size_bits + BITS_PER_BYTE - 1) // BITS_PER_BYTE
	total = 0
	for member in view.image.members(shape):
		if view.image.placements[member].located_code != NONE:
			continue
		total += size_bits(view, member)
	# Zero is an answer, not a refusal. A `name` whose first label does not
	# fit holds no labels and is zero bytes long, and C makes a zero-length
	# sub-view of it -- `ok=1 extent=0`. The guard against a zero extent
	# belongs where it stops something: the run walk below, which would
	# otherwise never advance.
	return (total + BITS_PER_BYTE - 1) // BITS_PER_BYTE
