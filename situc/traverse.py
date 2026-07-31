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

from collections.abc import Container, Sequence
from dataclasses import dataclass
from enum import Enum

from situc.ast import Schema
from situc.layout import BITS_PER_BYTE, Arm, Placement
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
	#: `x at expr` -- reached from the start of the message rather than from
	#: this frame, so none of the offset machinery applies (9.8).
	LOCATED   = "located"
	#: A `coded` region: bytes on the wire that mean something else, and the
	#: transform between them (13.5).
	CODED     = "coded"
	#: A `tlv` region: a run of self-describing items, walked as the region's
	#: own item grammar says (9.5).
	TLV       = "tlv"
	#: An `indexed` region: an offset table, then the elements it reaches
	#: (9.3).
	INDEXED   = "indexed"
	#: A byte-order marker: the value that says how the rest of the frame is
	#: read (8.3).
	MARKER    = "marker"
	#: A tag, a checksum, a sealed or authenticated region, a marker, a
	#: variant, an opaque span. Each needs its own machinery.
	REGION    = "region"
	#: Ends at a delimiter: `x[] until "D"`, and a text number, which is a
	#: delimited run read as digits.
	DELIMITED = "delimited"
	#: A run of records ending where a terminator stands in for one:
	#: `T x[] until "D"` with T a struct.
	RECORD_RUN = "record_run"
	#: A run ending after the element that fails a predicate:
	#: `T x[] while (cond)`.
	REPEAT_WHILE = "repeat_while"
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
	#: A variable-length integer (8.1.1): one value, whose width is in its own
	#: bytes rather than in the schema.
	VARINT    = "varint"
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

	# Before everything else. A located member may also be an array, or sized
	# by a field, or a run -- and none of that changes the first thing a
	# backend has to know about it, which is that it is not in this frame.
	# Asking later means every branch below needs its own guard.
	if placement.located is not None:
		return Member.LOCATED

	# Before the region check, and only for the two region kinds that can
	# carry one. A `coded` or `sealed` region that ends at a delimiter is
	# framed exactly like any other delimited member -- the scan is over the
	# encoded bytes either way, which is the order SMTP's dot-stuffing
	# specifies (13.6). What the codec does inside is a separate question and
	# a separate note.
	#
	# C reaches `_delimited` for anything with a delimiter and does not use
	# this function, so it has always emitted the scan accessors for one. The
	# other three asked here, got `REGION`, and emitted nothing -- so a
	# dot-stuffed body was unreachable in three backends out of four.
	if placement.kind in ("coded", "sealed") \
			and placement.delimiter is not None:
		return Member.DELIMITED

	# ...and one without a delimiter is still a region of bytes with a
	# transform over them. It answered `REGION` and three backends emitted
	# nothing, so the bytes on the wire were unreachable -- which C, not
	# asking here, has never been.
	if placement.kind == "coded":
		return Member.CODED

	# Before the region check, like the two coded cases above and for the same
	# reason: a tlv region answered REGION, and three backends emitted nothing
	# for the one construct section 9.7 makes the conformance gate. C does not
	# ask here, which is why it was the only one that could walk one.
	if placement.kind == "tlv":
		return Member.TLV

	# Same reason as the two above: it answered REGION, and three backends
	# emitted their fallthrough note for the last construct none of them
	# reached into. C does not ask here, which is why it was the only one that
	# could walk a table.
	if placement.kind == "indexed":
		return Member.INDEXED

	# Before the region check, for the reason the others are: a marker
	# answered REGION, and three backends emitted "not in the static subset
	# yet" for it -- and then read every field it governs big-endian anyway.
	if placement.kind == "marker":
		return Member.MARKER

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
	# Before the delimiter, though the two cannot both be set -- wellformed
	# refuses a member that says twice where its run ends, and the order here
	# is what makes that refusal the only place it has to be said.
	if placement.repeat_while is not None:
		return Member.REPEAT_WHILE

	if placement.delimiter is not None:
		return (Member.RECORD_RUN if placement.type_name in structs
		        else Member.DELIMITED)

	# Before UNPLACED: a member sized by the data usually has a dynamic offset
	# as well, and asking about the offset first loses it.
	#
	# `size_expr` as well as `sized_by`. The first holds a field path and is
	# None for `d[(len + 1) * 8 - 2]`, so a member sized by arithmetic over a
	# field fell past this to SCALAR -- and three backends handed back one
	# byte and called it the field. C escaped only because it does not use
	# this function, which is invariant 20 pointing the other way for once.
	# ...and only where the *data* decides. `sized_by` also names a
	# compile-time constant -- `u8 id[DEVICE_ID_BYTES]` sets it -- and then
	# the count is known, the frame was sized around it, and the accessors
	# are the fixed-array ones. Three backends looked for a field of that
	# name, found none, and dropped the member with a note saying they could
	# not resolve it. `array_count` is the honest question, and is now never
	# a guess.
	#
	# The same conflation `gen-fuzz` had, found the same way: a member the
	# schema sizes is not a member the message sizes.
	if placement.array_count is None \
			and (placement.sized_by is not None
			     or placement.size_expr is not None):
		return Member.VARIABLE

	# Before NESTED: an array of structs names a struct type and is not one.
	if placement.array_count is not None:
		return Member.ARRAY

	if placement.type_name in structs:
		return Member.NESTED

	# Before SCALAR, which it is not -- a varint has no `scalar` and so fell
	# past every branch to NOTHING, and three backends emitted nothing at all
	# for it: not an accessor, and not the note saying why. A member that
	# simply vanishes is the one shape a reader cannot ask about.
	if placement.varint is not None:
		return Member.VARINT

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
	#: The discriminant selects an arm that exists (`default: error`).
	DISCRIMINANT = "discriminant"
	#: Nothing to check.
	NOTHING    = "nothing"


def classify_check(struct: ResolvedStruct, placement: Placement,
		structs: Container[str]) -> Check:
	"""What to validate for this member, in the order that is safe."""
	# A delimited member's check is that its delimiter is there, and a run's
	# is nothing -- the walk that finds the terminator is the check. Neither
	# is `REPEATED`, which would validate an encoding over a length that has
	# not been established yet.
	# A run's own check is the walk that ends it; validating each element is
	# the caller's choice, as with any other array of structs.
	if placement.repeat_while is not None:
		return Check.NOTHING

	# `default: error` is the whole of what section 14.5 says a variant does
	# with a discriminant it does not recognise, and no backend emitted it:
	# `SITU_ERR_VERSION` was defined, commented "unknown version or variant
	# discriminant", and returned by nothing. It stayed invisible while a
	# variant's extent was unknowable, because nothing walked one. It stopped
	# being invisible the moment the extent became a switch on the
	# discriminant, which for an unrecognised one selects nothing and reports
	# a length no arm justifies.
	if placement.kind == "variant":
		return (Check.NOTHING if unmatched_values_pass(placement)
		        else Check.DISCRIMINANT)

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


def containment_order(structs: dict[str, "ResolvedStruct"],
		roots: Sequence[str]) -> list[str]:
	"""Struct names, each placed after every struct it names.

	Three backends need this and each had its own answer. A C++ class has to be
	complete before another names it by value; a Python class body names its
	members at definition time; and a C accessor that narrows to an element
	calls that element's `extent`, which is `static inline` and has to be
	defined above the call.

	C had no copy at all: it emitted in the solver's insertion order, on the
	argument that the solver resolves dependencies before their dependents. That
	held while the only dependency was containment -- an inner struct has to be
	laid out before the outer one that contains it. An `indexed` region's
	element is not a layout dependency, because the region's extent does not
	depend on it, so the first schema declaring its element type after its
	container emitted a header that did not compile.

	Every entry rather than `own_entries`: a variant's arms are nested by path
	and are not the struct's own members, but a backend emits an accessor
	handing one back and needs its type first. Both copies of this walked the
	own members only, so an arm type came out in the right place by alphabet --
	`sorted` roots, `A` and `B` before `S` -- and would not have for a schema
	that named them the other way round.

	`roots` is the caller's own top-level order, kept because it is visible in
	the output and each backend had already chosen one.
	"""
	order: list[str] = []
	seen: set[str]   = set()

	def visit(name: str) -> None:
		if name in seen or name not in structs:
			return
		seen.add(name)
		for entry in structs[name].entries:
			named = entry.placement.type_name
			if named is not None and named != name:
				visit(named)
		order.append(name)

	for name in roots:
		visit(name)
	return order


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

	# A delimited member has no fixed extent either, and `size_bits` is its
	# *delimiter's* width -- an honest lower bound, and the one number that is
	# not the answer to this question. Returning it made a variable-length
	# text field come out as a two-byte span, and the dissector declared
	# `ProtoField.uint8` for an HTTP header name: Wireshark would show it as a
	# single decimal number.
	#
	# This is the `array_count` lesson one level down (invariant 25). The
	# count was flatly false and this is a true number answering a different
	# question, which is the harder kind to notice.
	if placement.delimiter is not None:
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


def has_computable_extent(structs: dict[str, ResolvedStruct],
		struct: ResolvedStruct) -> bool:
	"""Whether one instance of `struct` can be measured from its own bytes.

	A fact about the layout rather than about any target, and both the C
	backend and `gen-checks` need it: the first to decide whether to emit an
	extent function and a sub-view, the second to decide whether a check may
	call one. They had it separately, so a struct the emitter declined to
	measure still got a check calling the accessor it had declined to write --
	and the generated suite failing to build is the same class of wrong as the
	header failing to.

	False where any member's own length is unknown. A `[remaining]` member's
	is the view's rather than the struct's, so an instance of the struct is
	exactly whatever view it was handed and there is nothing after it to
	place.

	A `variant` is measurable when every arm is. This read "never" at first,
	on the reasoning that a variant's extent is whichever arm the discriminant
	selects and the arms differ in length -- which is true, and is an argument
	that the extent is a *switch* rather than that there is none. Getting it
	wrong cost the DNS example its point: a compressed name is a run of labels
	and a label is a variant on its top two bits, so refusing variants refused
	the run, and the schema described a name nothing could walk.
	"""
	for placement in own_members(struct):
		if placement.is_fixed_size:
			continue
		if placement.sized_by == "remaining":
			return False
		if placement.kind in ("coded", "sealed") and placement.delimiter is None:
			return False		# the extent is the codec's expansion, not a length

		if placement.kind == "variant":
			if not _variant_is_measurable(structs, struct, placement):
				return False
			continue

		element = structs.get(placement.type_name or "")
		if element is None or element is struct:
			continue		# a scalar run, sized by a field or a delimiter

		# A run or a nested struct is only as measurable as its element.
		if not element.layout.is_fixed_size \
				and not has_computable_extent(structs, element):
			return False
	return True


def unmatched_values_pass(variant: Placement) -> bool:
	"""Whether some arm accepts a discriminant no `case` names.

	True for a `default` arm that selects a member, or an `opaque` one that
	swallows the rest. False for `default: error` and for a variant with no
	default at all, which mean the same thing.
	"""
	return any(arm.value is None and arm.member is not None
	           for arm in variant.arm_cases)


def matched_values(variant: Placement) -> tuple[Arm, ...]:
	"""The arms with a discriminant value of their own, in schema order."""
	return tuple(arm for arm in variant.arm_cases if arm.value is not None)


def arm_members(struct: ResolvedStruct,
		variant: Placement) -> list[tuple[Arm, Placement | None]]:
	"""Each arm of `variant`, against the member it selects."""
	by_path = {held.path: held for held in struct.layout.placements}
	return [(arm, None if arm.member is None else by_path.get(arm.member))
	        for arm in variant.arm_cases]


def _variant_is_measurable(structs: dict[str, ResolvedStruct],
		struct: ResolvedStruct, variant: Placement) -> bool:
	"""Every arm measurable, and none of them unbounded.

	An `opaque` default consumes whatever is left, so a variant carrying one
	is exactly as long as the view it was handed -- `[remaining]` by another
	spelling, and refused for the same reason. The layout already says so by
	leaving the variant with no maximum.
	"""
	if variant.size_max_bits is None:
		return False

	for _, member in arm_members(struct, variant):
		if member is None:
			continue		# `default: error`; no arm, so no length to know
		if member.is_fixed_size:
			continue
		if member.sized_by == "remaining":
			return False

		element = structs.get(member.type_name or "")
		if element is not None and not element.layout.is_fixed_size \
				and not has_computable_extent(structs, element):
			return False
	return True


def extent_parts(structs: dict[str, ResolvedStruct],
		struct: ResolvedStruct) -> tuple[int, list[Placement]] | None:
	"""What one instance of `struct` measures: constant bytes, plus members.

	None where it cannot be measured at all. Otherwise the caller renders a
	length expression for each returned placement in its own language and adds
	them to the constant -- which is the only part of this that differs
	between backends, the arithmetic being arithmetic.

	The constant is accumulated in *bits* and divided once at the end. Divided
	per member it truncates, and a `u2` and a `u6` are one byte together and
	zero apart: a struct opening with a packed pair came out short by exactly
	the byte its discriminant lives in, and a run over those walks the same
	element until it hits `max`. All four backends had this separately.
	"""
	if struct.layout.is_fixed_size or not struct.layout.is_byte_sized:
		return None
	if not has_computable_extent(structs, struct):
		return None

	constant_bits = 0
	variable: list[Placement] = []

	for placement in own_members(struct):
		if placement.is_fixed_size:
			constant_bits += placement.size_bits
		else:
			variable.append(placement)

	if constant_bits % BITS_PER_BYTE:
		return None		# not a whole number of bytes; nothing to walk by
	return constant_bits // BITS_PER_BYTE, variable


def declares_its_own_length(placement: Placement) -> bool:
	"""Whether the message, rather than the schema, says how long this is.

	The distinction the length checks turn on: a count from the schema is a
	number the frame was sized around, and a count from a field is a number an
	attacker chooses. Only the second can exceed the frame it sits in.

	`[remaining]` is excluded because it *is* what is left -- it cannot claim
	more -- and a delimited member because its extent is where the scan
	stopped, which is inside the view by construction.

	A *located* member is excluded too, and for a different reason: its bytes
	are not in this frame at all. Asking whether its length fits the frame is
	the wrong question, and the right one -- does it fit the message -- is
	asked by its accessor, on every call, because the offset is the message's
	as well (section 9.8).
	"""
	return (placement.sized_by is not None
	        and placement.sized_by != "remaining"
	        and placement.array_count is None
	        and placement.delimiter is None
	        and placement.located is None)


def arm_of(struct: ResolvedStruct,
		placement: Placement) -> tuple[Placement, Arm] | None:
	"""The variant this member is an arm of, and which arm, or None.

	A fact about the layout, so it is asked once rather than in each backend:
	four of them need it to guard an arm's accessor, and the only part that
	differs between them is how the discriminant is read.

	None where the member is not in an arm at all, or where the variant has
	no discriminant this can name.
	"""
	local = placement.path[len(struct.name) + 1:]
	if "." not in local:
		return None

	head = local.split(".")[0]
	variant = next((held for held in own_members(struct)
	                if held.kind == "variant" and held.name == head), None)
	if variant is None or variant.discriminant is None:
		return None

	for arm, member in arm_members(struct, variant):
		if member is not None and member.path == placement.path:
			return variant, arm
	return None


#: Stuffing codes a derived implementation exists for. The C generator owns the
#: list; this is the shape question, which every backend asks.
DERIVED_STUFFING = ("cobs", "hdlc", "smtp_dot")


def decodes_here(codec: object) -> bool:
	"""Whether a decode accessor has a settled shape to call.

	`(in, count, out) -> count` for a `table` kernel and for a `stuffing` one
	whose named code is generated. The other families emit implementations too,
	but their interfaces differ enough -- a Hamming codeword is a nibble in and
	a byte out, with a correction flag -- that a generic region decode would be
	guessing.

	Asked here because four backends ask it, and it was `family is TABLE` in
	each of them with a comment saying the rest were "described and not yet
	generated". They were generated, and none of the four comments noticed.
	"""
	kernel = getattr(codec, "kernel", None)
	if kernel is None:
		return False
	family = getattr(kernel, "family", None)
	if getattr(family, "value", None) == "table":
		return True
	if getattr(family, "value", None) != "stuffing":
		return False

	named = kernel.argument("code")
	return getattr(named, "name", None) in DERIVED_STUFFING


def decode_counts_bits(codec: object) -> bool:
	"""Whether the decoder's count is bits rather than bytes.

	A `table` kernel is bit-oriented by construction. A `stuffing` one declares
	`unit`, because HDLC counts bits where COBS scans bytes -- and passing a
	byte count to a bit loop decodes an eighth of the region and returns
	confidently.
	"""
	kernel = getattr(codec, "kernel", None)
	if kernel is None:
		return False
	if getattr(getattr(kernel, "family", None), "value", None) == "table":
		return True

	unit = kernel.argument("unit")
	return getattr(unit, "name", None) == "bit"


def decode_bound(codec: object, placement: Placement) -> int | None:
	"""How many bytes the decoded form of this region can occupy.

	The codec's declared ratio against the region's largest encoded form, so
	a caller can size the buffer the decode writes into. None where either is
	unknown -- and a bound nobody can compute is a decode nobody can call
	safely, which is why the accessor is not emitted then.

	Here rather than in four backends because it is arithmetic over two
	declared numbers, and the only thing that differs is how each spells the
	result.
	"""
	ratio: tuple[int, int] | None = getattr(codec, "ratio", None)
	if ratio is None or not ratio[0] or placement.size_max_bits is None:
		return None
	return (placement.size_max_bits // BITS_PER_BYTE) * ratio[1] // ratio[0]
