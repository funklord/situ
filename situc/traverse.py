"""Walking a resolved struct, once, for every backend and artifact.

Five places had grown their own copy of the same two rules -- which entries are
a struct's own members, and which bytes a placement occupies -- and the copies
were starting to matter. The `authenticated` exclusion below is a bug fix: a
region names bytes its members already account for, so counting both doubled
every offset after it. That fix had to be made in each copy, and the next such
fix would have to be found in all of them.

With backends planned for C++, Rust and Python (section 20.1), this is the
moment to have one copy rather than the moment to make four more. Nothing here
knows about any target language: it is the shape of the data, which every
backend reads the same way.
"""

from __future__ import annotations

from situc.layout import BITS_PER_BYTE, Placement
from situc.propagate import Resolved
from situc.resolve import ResolvedStruct

#: Entry kinds that are not a member in their own right.
#:
#: `element` describes every element of an array at once, so it has no bytes of
#: its own to place. `authenticated` names bytes its members already own -- it
#: consumes none itself -- and a walk that counts it as well places everything
#: after it one region too far along.
NOT_A_MEMBER = frozenset({"element", "authenticated"})


def local_name(struct: ResolvedStruct, placement: Placement) -> str:
	"""The member's name within its struct, without the struct's own prefix."""
	return placement.path[len(struct.name) + 1:]


def is_own_member(struct: ResolvedStruct, placement: Placement) -> bool:
	"""Whether this placement is one of the struct's own members.

	A nested struct's fields appear in the parent's entries too, under a dotted
	path; they belong to the nested struct's own walk, not this one. A sealed
	region's interior is the exception the callers handle themselves, because
	its accessors take the gated view type and so belong to the parent.
	"""
	return (placement.kind not in NOT_A_MEMBER
	        and "." not in local_name(struct, placement))


def own_entries(struct: ResolvedStruct) -> list[Resolved]:
	"""The struct's own members, in declaration order, with their vectors."""
	return [entry for entry in struct.entries
	        if is_own_member(struct, entry.placement)]


def own_members(struct: ResolvedStruct) -> list[Placement]:
	"""The struct's own members, in order, partitioning its bytes exactly."""
	return [entry.placement for entry in own_entries(struct)]


def byte_span(placement: Placement) -> tuple[int, int] | None:
	"""The bytes a placement touches, as (first, count).

	This is the unit every backend reads in, and it is not the placement's own
	width: a four-bit field is zero bytes wide if you divide, and a thirteen-bit
	field three bits into a byte spans two. Both of those were bugs in emitted
	code before this was one function.

	`None` where the placement has no fixed extent to speak of.
	"""
	if placement.offset_bits is None or not placement.size_bits:
		return None

	first = placement.offset_bits // BITS_PER_BYTE
	last  = (placement.offset_bits + placement.size_bits - 1) // BITS_PER_BYTE
	return first, last - first + 1


def span_bits(placement: Placement) -> int | None:
	"""How many bits of whole bytes the placement sits inside.

	A bit-packed field is read through the bytes it lives in and masked, so
	this is the width its mask is computed against.
	"""
	span = byte_span(placement)
	return None if span is None else span[1] * BITS_PER_BYTE


def container_bits(placement: Placement, widths: tuple[int, ...]) -> int | None:
	"""The smallest of `widths` that covers the placement's bytes.

	The widths are the backend's, not situ's: Wireshark has a 24-bit field type
	and C does not, so C routes a three-byte scalar through the bit path while
	the dissector reads it whole. Hardcoding one list here silently gave the
	dissector the C answer.
	"""
	bits = span_bits(placement)
	if bits is None:
		return None

	for width in widths:
		if bits <= width:
			return width
	return None
