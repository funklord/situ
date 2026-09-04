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

#: What a walk may be handed. Read-only either way -- nothing here writes
#: through it -- so the distinction is the caller's storage rather than the
#: walker's behaviour, and refusing a `bytearray` would refuse the buffer an
#: embedded reader is most likely to have: one it just received into.
#:
#: Spelled as a union because `collections.abc.Buffer` says it in one word
#: and arrived in 3.12, which is above the declared floor. mypy promoted
#: `bytearray` to `bytes` for years and stopped, which is how a signature
#: that had always been too narrow became visible.
Bytes = bytes | bytearray

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
	buffer: Bytes
	struct: int
	at: int
	limit: int

	@property
	def shape(self) -> Struct:
		return self.image.structs[self.struct]


def acquire(image: Image, buffer: Bytes, struct: int) -> View:
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
		if earlier.pad_to:
			# `pad_to(n)` advances the total to the next multiple, not by a
			# fixed size (0043) -- the same align every backend's offset
			# function does, spelled in bits here.
			unit = earlier.pad_to * BITS_PER_BYTE
			total = ((total + unit - 1) // unit) * unit
			continue
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
	if placement.pad_to:
		# `align_up(offset, n) - offset` (0043), in bits. The offset is the
		# sum of what precedes this pad, which `offset_bits` already knows.
		unit = placement.pad_to * BITS_PER_BYTE
		off  = offset_bits(view, index)
		return ((off + unit - 1) // unit) * unit - off
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
		# Negative reads as zero and an overflow saturates high (14.2b).
		# The walker evaluates in Python, which does not overflow, so the
		# bound is here to *agree* with the three backends that do rather
		# than for its own sake: a lying varint has to produce the same
		# offset in all four, and a bound applied everywhere but here is a
		# disagreement waiting to happen.
		count = min(max(count, 0), 0xFFFFFFFF)
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
		width = count * element
		# Clamped to a *pin* and to nothing else. `[size = N]` makes the
		# member hold N bytes whatever the length field says, which is what
		# the four backends clamp their accessors to (0039).
		#
		# Clamping to every `size_max_bits` instead looks equivalent and is
		# not: C clamps a length to what is left in the *view* rather than to
		# a declared maximum, so the general form disagreed with it about
		# `arp` and `ble` the moment it was tried. The flag is what tells a
		# pin from an ordinary bound, and it exists because that attempt
		# failed rather than because the shape was foreseen.
		if placement.pinned and placement.size_max_bits != NONE:
			width = min(width, placement.size_max_bits)
		return width
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


def _value_in(view: View, index: int, base: int) -> int:
	"""`_value_of` for a field of a struct nested `base` bytes into the frame.

	A placement's offset is within its own struct, so reading one belonging
	to a nested struct at that offset alone lands at the right offset of the
	wrong struct -- right only where the nested struct sits at 0, and wrong
	by the nesting offset everywhere else. `example/packet` reads a sealed
	region's extent from `hdr.length`, at 2 in `header` and 6 in `packet`
	(26.184).

	The packer emits this form only where the nesting offset is static and
	the target has a static offset of its own, so both are known here; a
	dynamic one is refused at pack time and reported as unencodable.
	"""
	placement = view.image.placements[index]
	if placement.offset_bits is None:
		raise Refused(f"placement {index} has no static offset to base")

	start = placement.offset_bits + base * BITS_PER_BYTE
	width = size_bits(view, index)
	if width <= 0 or width > 64:
		raise Refused(f"a {width}-bit scalar is not one to read")
	if view.at * BITS_PER_BYTE + start + width > view.limit * BITS_PER_BYTE:
		raise Refused("the frame does not reach this member")
	return _read_at(view, index, start, width)


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
		load_field_in = lambda i, base: _value_in(view, i, base),
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

	# A varint *is* a scalar, and its value is what it encodes rather than the
	# bytes it is written in. Falling through read those bytes as an integer:
	# `ac 02` came back as 44034 where leb128 says 300. The byte-run question
	# with the opposite answer -- a run has no single value and is refused, a
	# varint has one -- and decoding is what every compiled backend's `_get`
	# does.
	if index in view.image.varints:
		return varint(view, index)[1]

	# A delimited member is a byte run whose end the data decides, so it has
	# no more of a value than a counted one has. This answered `"GET "` as
	# 1195725856 -- the third construct to reach here as bits where the walk's
	# public answer wants values, and the second to be settled by refusing.
	# `read_bytes` is its reader, and the C walker cannot give it a number
	# either.
	if index in view.image.delimiters and placement.type_struct == NONE:
		raise Refused(f"placement {index} ends at a delimiter, so its bytes "
		              f"are `read_bytes`")

	# A variant is a shape the discriminant chooses, not a number. This read
	# the selected arm's bytes as an integer -- a two-byte arm came back as
	# 43707 and an eight-byte one as 4822678189205111 -- for the same reason
	# a byte run did: the width fitted and nothing asked whether the result
	# meant anything. The fifth construct to reach here as bits, and the
	# third settled by refusing; the arm is what has a value, and it has its
	# own placement to be read through.
	if index in view.image.arms:
		raise Refused(f"placement {index} is a variant, not a scalar; the arm "
		              f"the discriminant selects is what holds a value")

	start = offset_bits(view, index)
	width = size_bits(view, index)
	if width <= 0 or width > 64:
		raise Refused(f"a {width}-bit scalar is not one to read")

	end = start + width
	if view.at * BITS_PER_BYTE + end > view.limit * BITS_PER_BYTE:
		raise Refused("the frame does not reach this member")
	return _read_at(view, index, start, width)


def write_scalar(view: View, index: int, value: int) -> None:
	"""Store one member's value, bounds- and range-checked.

	`read_scalar`'s mirror, and it refuses exactly what that refuses: a run,
	a varint, a delimited member and a variant have no single value to read
	and none to write either, and each says so in the same terms. A text
	number is refused too, and separately: `parse_digits` reads one, and
	writing it back is an encoding rather than a store -- the digits may need
	a different count than the ones there.

	**The range is checked rather than truncated.** Storing 70000 in a `u16`
	is a schema violation, and a writer that masked it would put a number in
	the message that the schema says cannot be there, which is the one thing
	an editor must not do quietly.

	This says nothing about whether the *schema* permits the write. That is
	`mutate`, it lives in the capability vectors, and it is the editor's to
	ask before calling this (26.178).
	"""
	placement = view.image.placements[index]
	if placement.radix:
		raise Refused(f"placement {index} is a text number; writing one is an "
		              f"encoding rather than a store")
	if (placement.size_code != NONE or placement.array_count != NONE
			or placement.repeat_code != NONE):
		raise Refused(f"placement {index} is a run, not a scalar")
	if index in view.image.varints:
		raise Refused(f"placement {index} is a varint; its width depends on "
		              f"the value, so a store may not fit where it sits")
	if index in view.image.delimiters and placement.type_struct == NONE:
		raise Refused(f"placement {index} ends at a delimiter, so its extent "
		              f"is not the schema's to keep")
	if index in view.image.arms:
		raise Refused(f"placement {index} is a variant, not a scalar")

	start = offset_bits(view, index)
	width = size_bits(view, index)
	if width <= 0 or width > 64:
		raise Refused(f"a {width}-bit scalar is not one to write")

	if view.at * BITS_PER_BYTE + start + width > view.limit * BITS_PER_BYTE:
		raise Refused("the frame does not reach this member")

	# Packed decimal is range-checked on what it *stores*, not on what the
	# bits could hold: `bcd2 [bits = 7]` reaches 79 and not 127, because 80
	# encodes to 0x80 and the field is seven bits. C says the same number in
	# a `_MAX` macro; here it falls out of encoding and then measuring, which
	# needs no second statement of the rule.
	stored = _encoded(value, placement)
	low, high = _range(width, placement.signed)
	if placement.text_flags & BCD:
		if value < 0 or stored > high:
			raise Refused(f"{value} does not fit a {width}-bit packed decimal "
			              f"member of {placement.radix_digits} digit(s)")
	elif not low <= value <= high:
		raise Refused(f"{value} does not fit a {width}-bit "
		              f"{'signed' if placement.signed else 'unsigned'} member "
		              f"({low} to {high})")

	# A view may be over `bytes`, which the read path is happy with and this
	# is not. Said here rather than discovered at the store: mypy names it
	# ("unsupported target for indexed assignment"), and without the check
	# the caller gets a `TypeError` from inside the walker instead of a
	# refusal saying what it needs.
	buffer = view.buffer
	if not isinstance(buffer, bytearray):
		raise Refused("this view is over immutable bytes; a write needs a "
		              "`bytearray`")

	_write_at(buffer, view, index, start, width, stored)


def _range(width: int, is_signed: bool) -> tuple[int, int]:
	if is_signed:
		return -(1 << (width - 1)), (1 << (width - 1)) - 1
	return 0, (1 << width) - 1


def _write_at(buffer: bytearray, view: View, index: int, start: int,
		width: int, value: int) -> None:
	"""`_read_at` backwards, and split out for the same reason it was.

	The buffer is passed rather than read off the view because only the
	caller has established that it is writable.
	"""
	placement = view.image.placements[index]
	raw = value & ((1 << width) - 1)		# two's complement for a negative

	if start % BITS_PER_BYTE == 0 and width % BITS_PER_BYTE == 0:
		first = view.at + start // BITS_PER_BYTE
		size  = width // BITS_PER_BYTE
		buffer[first:first + size] = raw.to_bytes(size, _order(placement))
		return

	# Bit-packed: read the bytes the value touches, clear its bits, put it
	# back. The block covers whole bytes, so the value sits `after` bits from
	# its end -- the same arithmetic the read does, in the other direction.
	end   = start + width
	first = start // BITS_PER_BYTE
	last  = (end + BITS_PER_BYTE - 1) // BITS_PER_BYTE
	span  = buffer[view.at + first:view.at + last]
	block = int.from_bytes(span, "big")
	after = last * BITS_PER_BYTE - end
	mask  = ((1 << width) - 1) << after
	block = (block & ~mask) | (raw << after)
	buffer[view.at + first:view.at + last] = \
		block.to_bytes(last - first, "big")


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
		return _decoded(_signed(int.from_bytes(raw, _order(placement)), width,
		                        placement.signed), placement)

	# Bit-packed: gather the bytes the value touches and shift it out. The
	# block covers whole bytes, so the value sits `after` bits from its end.
	first = start // BITS_PER_BYTE
	last  = (end + BITS_PER_BYTE - 1) // BITS_PER_BYTE
	block = int.from_bytes(view.buffer[view.at + first:view.at + last], "big")
	after = last * BITS_PER_BYTE - end
	return _decoded(_signed((block >> after) & ((1 << width) - 1), width,
	                        placement.signed), placement)


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


#: `text_flags` bit 3: the value is packed decimal, a digit per nibble.
BCD = 1 << 3


def _decoded(value: int, placement: "Placement") -> int:
	"""Packed decimal read as the number its nibbles spell.

	The same omission `_signed` records one axis over, and found the same
	way: the image did not carry it, so this walker read `bcd2 seconds`
	holding 0x45 as **69** where the generated C read **45** -- and then
	refused a valid DS1307 reading against `[max = 59]` that C accepted.
	Neither differential could see it: `codegen/differ` skips a BCD member
	outright, so C's side of the comparison never mentions one and the
	intersection the walker is held to never contains it (26.222).

	**`situ_bcd_decode` verbatim, including what it does with a nibble above
	nine.** The runtime multiplies out whatever the nibbles hold --
	`value * 10 + nibble` for `digits` of them -- so `0x2F` at two digits is
	35, not a refusal. The first version of this stopped at such a nibble on
	the reasoning that the bytes can hold what the format cannot mean, and
	the differential caught it on the fourth random buffer: the walker said
	`day 0` where C said `day 35`. That reasoning may even be better; it is
	not what the four backends do, and a fifth description inventing its own
	answer for malformed input is the whole defect this function exists to
	repair.
	"""
	if not placement.text_flags & BCD:
		return value

	out = 0
	for shift in range((placement.radix_digits or 0) - 1, -1, -1):
		out = out * 10 + ((value >> (4 * shift)) & 0xF)
	return out


def _encoded(value: int, placement: "Placement") -> int:
	"""`_decoded`'s mirror: `situ_bcd_encode`, verbatim.

	Needed because fixing only the read side broke the walker's own round
	trip. Before packed decimal was carried at all, read and write were both
	raw and agreed with each other while both disagreed with C; teaching the
	reader to decode without teaching the writer to encode left `write 45`
	storing 0x2D and `read` answering 33 (26.223).
	"""
	if not placement.text_flags & BCD:
		return value

	packed = 0
	for digit in range(placement.radix_digits or 0):
		packed |= (value % 10) << (4 * digit)
		value //= 10
	return packed


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
	# Copied, like every other run this module hands out as a value: the
	# buffer may be a `bytearray`, and a slice of one is a `bytearray` that
	# follows the caller's later writes. Three sites here already spelled it
	# `bytes(...)` and three did not, which nothing could see while mypy
	# promoted the two to one type.
	return bytes(view.buffer[first:last])


def write_bytes(view: View, index: int, value: bytes) -> None:
	"""`read_bytes` backwards, at the same length and no other.

	A run is what a file editor most often edits -- a payload, a name, a
	magic -- and a run written at the length it already has moves nothing,
	which puts it in the same class as a fixed scalar written in place
	(0034's first row) rather than in the shifting one.

	**The length is the whole guard here.** A shorter or longer value is a
	layout change however it is spelled, so it is refused by count rather
	than left for the caller's own check: `read_bytes` says how many bytes
	are there and this writes exactly that many.
	"""
	start = offset_bits(view, index)
	width = size_bits(view, index)
	if start % BITS_PER_BYTE or width % BITS_PER_BYTE:
		raise Refused("a byte run that does not start on a byte")

	first = view.at + start // BITS_PER_BYTE
	last  = first + width // BITS_PER_BYTE
	if last > view.limit:
		raise Refused("the frame does not reach this run")

	size = width // BITS_PER_BYTE
	if len(value) != size:
		raise Refused(f"this run is {size} byte(s) and the value is "
		              f"{len(value)}; changing a run's length moves what "
		              f"follows it")

	buffer = view.buffer
	if not isinstance(buffer, bytearray):
		raise Refused("this view is over immutable bytes; a write needs a "
		              "`bytearray`")
	buffer[first:last] = value


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
