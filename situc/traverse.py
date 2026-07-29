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

from collections.abc import Container
from dataclasses import dataclass
from enum import Enum

from situc.ast import Schema
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


class Member(Enum):
	"""What a backend has to emit for one member, and nothing about how.

	The *order* these are tested in is the whole point of this enum. Three
	backends shipped the same two bugs before it existed, because the walk in
	this module handed them the members without saying which question to ask
	first:

	  * `VARIABLE` has to be decided before `UNPLACED`. A member sized by the
	    data usually has a dynamic offset too, so a backend that bails on the
	    offset first silently skips every variable-length member it has.
	  * `NESTED` has to be decided after `ARRAY`. An array of structs names a
	    struct type, and treating it as a nested one emits an accessor that
	    takes no index and a validate call to a method that does not exist.

	`classify` below is that order, written once.
	"""

	#: No accessor; `validate` holds it to the declared pattern.
	RESERVED  = "reserved"
	#: A tag, a checksum, a sealed or authenticated region, a marker, a
	#: variant, an opaque or indexed span. Each needs its own machinery.
	REGION    = "region"
	#: Ends at a delimiter: `x[] until "D"`, and a text number, which is a
	#: delimited run read as digits.
	DELIMITED = "delimited"
	#: A run of records ending where a terminator stands in for one:
	#: `T x[] until "D"` with T a struct.
	RECORD_RUN = "record_run"
	#: Extent decided by the data: `x[n]` or `x[remaining]`.
	VARIABLE  = "variable"
	#: A counted array, of scalars or of structs.
	ARRAY     = "array"
	#: One struct, at a fixed offset.
	NESTED    = "nested"
	#: A field that is none of the above and has no offset either: nothing a
	#: backend can place. Note that a *scalar* at a dynamic offset is `SCALAR`,
	#: not this -- whether the offset can be resolved is the backend's business.
	UNPLACED  = "unplaced"
	#: One value.
	SCALAR    = "scalar"
	#: Nothing to emit.
	NOTHING   = "nothing"


def classify(struct: ResolvedStruct, placement: Placement,
		structs: Container[str]) -> Member:
	"""Which kind of member this is, asked in the order that is safe.

	`structs` is whatever the backend uses to recognise a struct type -- a set
	of names, usually. Passed in rather than reached for, so this stays a
	function of the data.
	"""
	if placement.kind == "reserved":
		return Member.RESERVED
	if placement.kind != "field":
		return Member.REGION

	# Before everything below. A delimited member answers no to every question
	# after this one -- it has no count, no `sized_by`, and a scalar element
	# type -- so without this it comes out SCALAR, and a backend reads the
	# delimiter's own width at a static offset and calls it the field.
	#
	# It used to come out ARRAY instead, because the solver recorded
	# `array_count = 1` for the empty bracket form. That lie is gone; the
	# check stays, because the answer without it is wrong either way.
	if placement.delimiter is not None:
		return (Member.RECORD_RUN if placement.type_name in structs
		        else Member.DELIMITED)

	# Before UNPLACED: a member sized by the data usually has a dynamic offset
	# as well, and asking about the offset first loses it.
	if placement.sized_by is not None:
		return Member.VARIABLE

	# Before NESTED: an array of structs names a struct type and is not one.
	if placement.array_count is not None:
		return Member.ARRAY

	if placement.type_name in structs:
		return Member.NESTED

	# A scalar is a scalar whatever its offset. Whether the backend can
	# *resolve* a dynamic one is the backend's business -- classifying it as
	# unplaced here dropped every field after a variable member, which is a
	# third way to get this wrong and was found the same way as the first two.
	if placement.scalar is not None:
		return Member.SCALAR
	if placement.offset_bits is None:
		return Member.UNPLACED
	return Member.NOTHING


class Check(Enum):
	"""What `validate` has to check for one member.

	A separate order from `Member`, and separate for a reason: an array of
	structs gets an *accessor* per element and no per-element validation,
	because walking every element on every parse is a cost the caller should
	choose. So `REPEATED` comes before `NESTED` here too, but means something
	different from `ARRAY` above.
	"""

	#: Its own `validate`, called through.
	NESTED     = "nested"
	#: An array or a data-sized run: encoding, termination, reserved bytes.
	REPEATED   = "repeated"
	#: A reserved field, held to `must_be_zero` or `must_be_one`.
	RESERVED   = "reserved"
	#: `must_eq`, `min`, `max`, and enum membership.
	CONSTRAINED = "constrained"
	#: The delimiter is there, and for a text number, that the digits parse.
	DELIMITED  = "delimited"
	#: Nothing to check.
	NOTHING    = "nothing"


def classify_check(struct: ResolvedStruct, placement: Placement,
		structs: Container[str]) -> Check:
	"""What to validate for this member, in the order that is safe."""
	# A delimited member's check is that its delimiter is there, and a run's
	# is nothing -- the walk that finds the terminator is the check. Neither
	# is `REPEATED`, which would validate an encoding over a length that has
	# not been established yet.
	if placement.delimiter is not None:
		return (Check.NOTHING if placement.type_name in structs
		        else Check.DELIMITED)

	# Before NESTED: an array of structs is not a nested struct, and calling
	# `self.recs().validate()` on one names a method that takes an index.
	if placement.array_count is not None or placement.sized_by is not None:
		return Check.REPEATED if placement.scalar is not None else Check.NOTHING

	if placement.scalar is None:
		return (Check.NESTED if placement.type_name in structs
		        else Check.NOTHING)

	# The offset is deliberately not consulted. What kind of check a member
	# needs is a fact about the schema; whether a backend can emit it is a fact
	# about the backend, and conflating them made this module state one
	# backend's limit as if it were the language's. Every backend validates a
	# constrained field at a dynamic offset now -- C always did, and the gap in
	# the other three was found by asking this module the wrong question.
	if placement.kind == "reserved":
		return Check.RESERVED
	if placement.kind != "field":
		return Check.NOTHING
	return Check.CONSTRAINED


@dataclass(frozen=True)
class Obligation:
	"""Something that must be recomputed after a write, and its dirty bit.

	Section 11.1 calls the `auth` axis "which obligation covers these bytes"
	rather than "which tag", and there are two kinds: a tag over a region
	(14.2), and an invariant that derives a field from other fields (16.1).
	They differ only in the arithmetic behind them, so they share a dirty word
	and this type.

	The distinction that matters to a backend is `label` versus `name`.
	`label` is what `Placement.covered_by` holds and what a diagnostic prints
	-- "invariant total" reads correctly in a sentence. `name` is an
	identifier. Emitting the label where an identifier belongs produced
	`SITU_S_INVARIANT TOTAL_DIRTY`, a macro with a space in it, in generated C
	that no compiler would accept: the two had been the same string for as
	long as tags were the only obligation.
	"""

	#: "tag" or "invariant".
	kind:  str
	#: An identifier: the tag's member name, or the derived field's.
	name:  str
	#: What `covered_by` holds, and what a human reads.
	label: str
	#: Which bit of the message's dirty word. Position in `obligations()`.
	bit:   int

	@property
	def suffix(self) -> str:
		"""What a backend appends when naming the bit.

		A tag is dirty; a derived field is stale. Different words for the same
		bit, because they are different sentences: a tag no longer matches the
		bytes, and a field no longer equals what it is defined to equal.
		"""
		return "DIRTY" if self.kind == "tag" else "STALE"


def obligations(schema: Schema, struct: ResolvedStruct) -> list[Obligation]:
	"""Every obligation over this struct's bytes, in dirty-bit order.

	Tags first, then invariants, because tags were numbered first and renaming
	a bit would change what an already-generated header means. Backends must
	not number these themselves -- C and Python each did, from different
	lists, and a struct carrying both a tag and an invariant gave the two
	backends different answers for the same schema.
	"""
	found = [Obligation("tag", entry.placement.name, entry.placement.name, bit)
	         for bit, entry in enumerate(entry for entry in struct.entries
	                                     if entry.placement.kind in ("tag", "checksum"))]

	for decl in schema.invariants():
		holder, _, field = decl.derived.partition(".")
		if holder == struct.name:
			found.append(Obligation("invariant", field, f"invariant {field}",
			                        len(found)))
	return found


def obligation(schema: Schema, struct: ResolvedStruct,
		label: str) -> Obligation | None:
	"""The obligation a `covered_by` entry names, or None if it names none."""
	return next((held for held in obligations(schema, struct)
	             if held.label == label), None)


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
