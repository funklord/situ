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
	"""How many bits a member occupies."""
	placement = view.image.placements[index]
	if placement.size_code != NONE:
		count = _evaluate(view, placement.size_code,
		                  offset_bits(view, index) // BITS_PER_BYTE)
		if count < 0:
			raise Refused("a computed size is negative")
		element = (placement.element_bits
		           if placement.element_bits != NONE else BITS_PER_BYTE)
		return count * element
	if placement.size_bits == NONE:
		raise Refused(f"placement {index} has no size this image carries")
	return placement.size_bits


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
		load_field = lambda i: read_scalar(view, i),
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
