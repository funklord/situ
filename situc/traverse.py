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

from collections.abc import Callable, Container, Sequence
from dataclasses import dataclass
from enum import Enum

from situc import ast
from situc import kernels
from situc.ast import Schema
from math import lcm

from situc.expr import Env
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
	#: A `tag` or `checksum`: bytes covering other bytes, with a dirty bit
	#: (14.2).
	TAG       = "tag"
	#: An `opaque` region: a size and no interior schema (9.4).
	OPAQUE    = "opaque"
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
	#: A number written as digits in a fixed width: `decimal u16 code[3]`
	#: (8.6.2). The scalar names the value's domain, not its width in the
	#: buffer, which the array bracket gives.
	TEXT_NUMBER = "text_number"
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


def readable_names(struct: ResolvedStruct) -> list[Placement]:
	"""Which fields an expression written in this struct may name.

	Its own scalars, and the scalars of the structs nested inside it: BMP's
	pixel array sits `at file.pixel_offset`, where `file` is a nested header,
	and every backend's name list stopped at the dot. The path was emitted
	verbatim into the generated code, which in C is `file.pixel_offset` as an
	identifier and does not compile.

	A nested member is only readable where its offset is a constant in *this*
	struct's frame -- which is what a nested member's offset is, since a
	struct at a dynamic offset contributes nothing an expression can name at
	compile time.

	A varint has no scalar and is readable all the same: `v n; u16 d[n + 1]`
	names one, and every backend left it as a bare identifier -- generated C,
	C++ and Rust that do not compile, and a Python `NameError`. The count form
	`d[n]` beside it has known about varint drivers since they were added; the
	arithmetic form was never told.

	Its own varints only. A nested struct's varint has no accessor on this
	struct and no `_value` helper either, and claiming it is readable would
	trade a bare identifier for a call to a function nobody emits.

	The constant-offset rule is the *nested* one's, and it was applied to a
	struct's own fields too -- so a discriminant behind a delimited member was
	not a name an expression could use, and `over_fields` passes through what
	it does not recognise (invariant 60). The generated C read `if (which !=
	17u)` with no `which` in scope. A field of this struct has an accessor
	wherever it sits, and every backend has emitted one for a dynamic offset
	since 26.27; the constant is only needed where the read is a load at a
	fixed displacement, which is what a nested member's is.
	"""
	return [entry.placement for entry in struct.entries
	        if (entry.placement.scalar is not None
	            or entry.placement.varint is not None)
	        and ("." not in entry.placement.path[len(struct.name) + 1:]
	             or (entry.placement.offset_bits is not None
	                 and entry.placement.scalar is not None))]


def data_sized(placement: Placement) -> bool:
	"""Whether this member's extent comes from an expression over the data.

	The one question, asked once. It was asked in three places and answered
	differently in each: `classify` learned that `size_expr` counts as well as
	`sized_by` -- a length written as arithmetic over a field, which is about
	as common as a length gets -- and the other two did not.

	`classify_check` therefore called such a member a scalar, so `reserved u8
	[align_up(n, 4) - n]` reached a load at a static offset and crashed the
	compiler in three backends. And `declares_its_own_length` said no, so the
	length check of invariant 41 was never emitted for one: `u8 data[(len + 1)
	* 8 - 2]` could claim two kilobytes inside a forty-byte frame and
	`validate` returned OK, in all four backends, for the shape
	`example/ipv6ext` is built out of.

	`array_count` is the honest question for "did the schema decide this":
	`sized_by` also names a compile-time constant, and `x[remaining]` sets it
	to a word rather than to a path.
	"""
	return (placement.array_count is None
	        and (placement.sized_by is not None
	             or placement.size_expr is not None))


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

	# Before the region check, like the rest: a tag answered REGION and three
	# backends emitted their fallthrough note for it, while emitting the dirty
	# bit it sets and the setters that mark it.
	if placement.kind in ("tag", "checksum"):
		return Member.TAG

	# Treat-as-bytes, which is the whole of what the construct supports -- and
	# three backends supported none of it, the fallthrough note claiming a
	# language limit where C hands back a pointer and a length.
	if placement.kind == "opaque":
		return Member.OPAQUE

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
	if data_sized(placement):
		return Member.VARIABLE

	# Before ARRAY, which it looks exactly like: `decimal u16 code[3]` is one
	# number in three digits, not three numbers. Three backends read the
	# bracket as a count and reported "element type u16 has no fixed size" --
	# about a type that plainly has one -- because the array branch was the
	# only one that could have it. The delimited form of the same construct
	# is caught above by its delimiter.
	if placement.radix is not None and placement.delimiter is None:
		return Member.TEXT_NUMBER

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
	#: A fixed-width text number: the digits parse in its radix and fit the
	#: scalar's domain, `[minimal]` where declared, and whatever `CONSTRAINED`
	#: would have asked of it -- which is the part it was losing.
	TEXT_NUMBER = "text_number"
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

	# Before REPEATED, and for the reason `classify` states one function up:
	# `decimal u16 code[3]` is one number in three digits, not three numbers.
	# The *accessor* classifier learned that -- three backends had read the
	# bracket as a count and said "element type u16 has no fixed size" about a
	# type that plainly has one -- and this one did not. So a text number's
	# check was an array's, which is an encoding and a terminator it has
	# neither of, and its digits were parsed by nothing: cpio's magic is
	# `decimal u32 magic[6] [min = 70701, max = 70702]` and `07070x` validated
	# clean in all four backends. Two classifiers, one fact, learned once.
	if placement.radix is not None and placement.delimiter is None:
		return Check.TEXT_NUMBER

	# Before NESTED: an array of structs is not a nested struct, and calling
	# `self.recs().validate()` on one names a method that takes an index.
	if placement.array_count is not None or data_sized(placement):
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


@dataclass(frozen=True)
class RegionExtent:
	"""How a `coded` or `sealed` region's byte count is computed (13.5).

	Its interior extent put through the codec's expansion, which is the same
	rule the solver applies and the only one available: the region's bytes are
	the transform's output, so nothing in them can be read to find out how many
	there are.

	`constant` is the interior's fixed bytes and `variable` the members whose
	length is a runtime expression -- what that expression *is* stays each
	backend's. The rest is the expansion, resolved to numbers here so that four
	backends do not each reimplement `ratio_padded`'s rounding.
	"""

	constant: int
	variable: tuple[Placement, ...]
	#: "preserving", "add", "ratio" or "padded".
	kind: str
	add: int		= 0
	out: int		= 0
	into: int		= 0
	#: For "padded", in bytes: a partial group still costs a whole one.
	group_in: int		= 0
	group_out: int		= 0


def unmeasurable_inside(structs: dict[str, "ResolvedStruct"],
		member: Placement) -> bool:
	"""Whether this member's extent cannot be had from outside its region.

	Two shapes, and the one question `region_extent` refuses on and the differ
	declines to ask about:

	  * a *walk* -- a delimited run, a `while` run, a counted run of variable
	    records. It reads each element's own length off the wire, and inside a
	    region the wire is the codec's output: stuffed bytes for a stuffing
	    codec, ciphertext for an AEAD;
	  * `[remaining]`, which means "to the end of the frame" and inside a
	    region has nothing to mean. Where the region ends is the question the
	    extent is being asked, so an interior sized by the rest of it is
	    circular.

	A length driven by a field *outside* the region is neither, and is the
	shape 26.35 wrote the extent rule for.
	"""
	return (member.delimiter is not None
	        or member.repeat_while is not None
	        or member.sized_by == "remaining"
	        or is_counted_run(structs, member))


def region_extent(struct: "ResolvedStruct", region: Placement,
		codec: object,
		structs: dict[str, "ResolvedStruct"] | None = None) -> RegionExtent | None:
	"""The extent rule for a coded region, or None where there is none.

	None where the expansion has no closed form -- a bounded ratio or an
	unbounded one -- because the length genuinely is not computable without
	decoding, and a wrong number would silently misplace every member after it.

	And None where measuring the interior would mean *reading* it. The rule
	26.35 established is that a region's extent is its interior's extent put
	through the codec's expansion, and that holds while the interior's extent
	comes from fields outside the region -- `u8 content[n]` with `n` declared
	before it, which is the shape it was written for. A *run* inside a region
	is not that: how far it reaches is a walk, and a walk reads each element's
	own length off the wire, where the bytes are the codec's output. For a
	stuffing codec they are stuffed and for an AEAD they are ciphertext, so
	the number that comes back is not a length of anything.

	Four backends named a span for such a run and none of them emitted one, so
	this arrived as a header that would not compile -- which is the good
	failure, and it hid the real answer: the extent is not computable, and
	every member after the region has to say so.
	"""
	if codec is None:
		return None

	prefix   = region.path + "."
	interior = [entry.placement for entry in struct.entries
	            if entry.placement.path.startswith(prefix)
	            and "." not in entry.placement.path[len(prefix):]
	            and entry.placement.kind != "element"]

	constant = 0
	variable = []
	for member in interior:
		if member.is_fixed_size:
			constant += member.size_bits // BITS_PER_BYTE
			continue
		if unmeasurable_inside(structs or {}, member):
			return None		# a walk over the codec's output
		variable.append(member)

	expansion = getattr(codec, "expansion", None)
	name      = getattr(expansion, "value", None)
	ratio     = getattr(codec, "ratio", None)

	if name == "length_preserving":
		return RegionExtent(constant, tuple(variable), "preserving")
	if name == "fixed_add":
		return RegionExtent(constant, tuple(variable), "add",
		                    add=getattr(codec, "expansion_add", 0) or 0)
	if name == "ratio_exact" and ratio is not None:
		return RegionExtent(constant, tuple(variable), "ratio",
		                    out=ratio[0], into=ratio[1])
	if name == "ratio_padded" and ratio is not None:
		out, into = ratio
		group_in  = lcm(BITS_PER_BYTE, into)
		return RegionExtent(constant, tuple(variable), "padded",
		                    out=out, into=into,
		                    group_in=group_in // BITS_PER_BYTE,
		                    group_out=group_in // into * out // BITS_PER_BYTE)

	return None


@dataclass(frozen=True)
class OffsetStep:
	"""One step of resolving every dynamic offset in a struct, in order.

	`kind` is "record" -- this member's offset is the running total -- or
	"advance", which moves the total on by `size` bytes where that is a
	constant and by `placement`'s own length where it is not, or "align",
	which moves it to the next multiple of `size` (0043).
	"""

	kind: str
	placement: Placement | None	= None
	size: int			= 0


def offset_plan(struct: "ResolvedStruct", members: Sequence[Placement],
		has_length: "Callable[[Placement], bool]") -> list[OffsetStep] | None:
	"""How to resolve every dynamic offset in one pass, or None.

	`_offset` resolves one member by summing what precedes it, so reading
	three members of an HTTP request line rescans the target twice. This is
	that sum once, for all of them -- the other half of what
	`access = Sequential` costs.

	None where some member's length cannot be computed: the offsets after it
	cannot be resolved in one pass any more than one at a time.

	The order and the arithmetic are shared; what a length expression *is*
	stays each backend's, being the one part that differs. A running constant
	is flushed where it belongs rather than summed up front -- a fixed member
	after a variable one is not part of the offsets before it, and totalling
	first put every recorded offset ahead of itself by the width of everything
	that followed.
	"""
	dynamic = {held.path for held in members
	           if held.offset_bits is None and held.located is None}
	if not dynamic:
		return []		# every offset is already a constant

	steps: list[OffsetStep] = []
	pending = 0

	def flush() -> None:
		nonlocal pending
		if pending:
			steps.append(OffsetStep("advance", size=pending))
			pending = 0

	for held in members:
		if held.path in dynamic:
			flush()
			steps.append(OffsetStep("record", held))
		pad = pad_alignment(held)
		if pad is not None:
			# A pad advances the running total to the next multiple, not by a
			# fixed size (0043). A *static* pad has a fixed `size_bits` and
			# takes the constant path below; only a dynamic one aligns.
			if held.offset_bits is not None:
				pending += held.size_bits // BITS_PER_BYTE
				continue
			flush()
			steps.append(OffsetStep("align", held, size=pad))
			continue
		if held.is_fixed_size:
			pending += held.size_bits // BITS_PER_BYTE
			continue
		if not has_length(held):
			return None
		flush()
		steps.append(OffsetStep("advance", held))

	# Trailing advances move a total nobody reads again. Harmless in C and an
	# `unused_assignments` error in Rust, which builds under `-D warnings` --
	# and dead arithmetic either way.
	while steps and steps[-1].kind == "advance":
		steps.pop()

	return steps


def covered_run(struct: "ResolvedStruct",
		tag: Placement) -> tuple[Placement, Placement] | None:
	"""The first and last region a tag authenticates, if they are contiguous.

	Only a contiguous run has a single byte range. Nested coverage is
	contiguous by construction and disjoint coverage of adjacent regions
	usually is, but nothing guarantees it -- so a gap is reported as no run
	rather than papered over with a range covering bytes the tag does not.

	Structural, so it is asked once: which regions, in what order, and whether
	each ends where the next begins. What the two endpoints are *called* is the
	backend's, because `view.limit` and `self.bytes.len()` are the same fact
	spelled four ways.
	"""
	regions = [entry.placement for entry in struct.entries
	           if entry.placement.name in tag.tag_covers
	           and entry.placement.kind in ("authenticated", "sealed")]
	if not regions:
		return None

	ordered = sorted(regions, key=lambda p: struct.layout.placements.index(p))

	for earlier, later in zip(ordered, ordered[1:]):
		if (earlier.offset_bits is None or later.offset_bits is None
				or not earlier.is_fixed_size
				or earlier.offset_bits + earlier.size_bits != later.offset_bits):
			return None

	return ordered[0], ordered[-1]


def coded_spans(struct: "ResolvedStruct",
		region: Placement) -> list[tuple[Placement, Placement]] | None:
	"""The runs of bytes a `coded` region's transform covers (13.2b).

	The region itself plus everything its `covers` clause names, in offset
	order, with adjacent members merged into one run. Each run is a first and
	last placement, exactly as `covered_run` reports one. `None` where a
	covered span's offset is not statically known, which is the one shape the
	scattered ABI still cannot express -- a span list has to be built before
	the codec is called, and an offset nobody can compute is not a span.

	Without a `covers` clause the answer is the region alone, which is what
	makes the ordinary contiguous case fall out of the same function.

	Merging matters. A clause naming a member that happens to sit against the
	region produces one run rather than two, so the contiguous case reaches
	the codec as a single span and the count a caller sees is the number of
	*separated* pieces rather than the number of names written down.

	Structural, and here rather than in a backend, for `covered_run`'s reason:
	which spans, in what order, and where each one ends is one question with
	one answer, and four backends disagreeing about it would be four dialects
	of the language.
	"""
	if not region.coded_covers:
		return [(region, region)]

	# The struct's own members only. A nested struct's fields appear in these
	# entries too, under a dotted path, and share their bare names -- so an
	# unrestricted search could bind `covers(flags)` to `sub.flags` while
	# `wellformed` validated the clause against the top-level namespace, and
	# the two would disagree about which bytes the transform ran over.
	#
	# The test is the dotted path, not `is_own_member`: that also excludes a
	# region by kind, and a region is exactly what `covers(first)` names in
	# the case this clause exists for. Filtering by it emitted the ordinary
	# accessor for a coverage that should have been refused -- the clause
	# silently ignored, which is the failure this whole feature guards.
	named = [entry.placement for entry in struct.entries
	         if entry.placement.name in region.coded_covers
	         and "." not in local_name(struct, entry.placement)]
	ordered = sorted([*named, region],
	                 key=lambda p: struct.layout.placements.index(p))

	if any(one.offset_bits is None for one in ordered):
		return None

	runs: list[tuple[Placement, Placement]] = []
	first = last = ordered[0]
	for later in ordered[1:]:
		assert last.offset_bits is not None
		assert later.offset_bits is not None
		if (last.is_fixed_size
				and last.offset_bits + last.size_bits == later.offset_bits):
			last = later
			continue
		runs.append((first, last))
		first = last = later
	runs.append((first, last))

	return runs


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


def element_bytes(placement: Placement) -> int:
	"""How wide one element of this member is, in bytes.

	`T name[N]` counts elements and never bytes -- the solver has always read
	it that way, and `sized_by` renders as `count * width` in every backend.
	The `size_expr` branch beside it did not: `u32 d[n + 1]` advanced `n + 1`
	bytes where the layout said `(n + 1) * 4`, so the compiler disagreed with
	its own accessors by a factor of the element width, and every member after
	such an array was read out of its bytes.

	It went unseen because the shape that found `size_expr` in the first place
	was `u8 d[(len + 1) * 8 - 2]`, where the factor is one.

	One byte where there are no elements to count: an `opaque` region's size
	expression is already a byte count.
	"""
	return (placement.element_bits or BITS_PER_BYTE) // BITS_PER_BYTE


def dynamic_frame_owner(struct: "ResolvedStruct",
		placement: Placement) -> Placement | None:
	"""The member whose frame a dotted placement's offset is measured in.

	`s.head.seq` is a member of `inner` seen from `s`, and its `offset_bits`
	answers two different questions depending on where `head` sits. Where the
	nested struct has a constant offset the copy carries an absolute one --
	`frame_relative` is False -- and the parent can address it directly. Where
	the nested struct sits behind a variable-length member the copy is
	*frame-relative*: the offset is measured from `head`, and reaching it from
	the parent means adding wherever `head` turned out to be.

	Every backend addressed it directly regardless, so the flattened setter for
	a tag-covered field wrote at the parent's base plus a frame-relative
	offset -- over the top of whatever is actually there. C emitted that from
	the start; the other three crashed before they got far enough to emit it,
	which is the only reason it was not four.

	None where the question does not arise, or where the owner is not a member
	of this struct.
	"""
	if not (placement.frame_relative and placement.frame_base_dynamic):
		return None
	local = placement.path[len(struct.name) + 1:]
	if "." not in local:
		return None
	owner = f"{struct.name}.{local.split('.', 1)[0]}"
	for candidate in own_members(struct):
		if candidate.path == owner:
			return candidate
	return None


def indexed_elements(placement: Placement) -> bool:
	"""Whether this member's values are reached by index rather than as bytes.

	An element wider than a byte is `ValueConverted`: the bytes on the wire are
	not the values, so a span of them is not the member. Every backend states
	that rule for `u16 x[4]` -- three of them in a comment in the generated
	code, saying "bytes handed back whole would not be the values" -- and every
	backend broke it for `u16 x[n]`, which is the same array with its count in
	the message rather than in the schema. Python handed back a `memoryview`,
	C++ a `bytes`, Rust a `&[u8]`; a caller who cast any of them got host byte
	order for a schema that names its own.

	C did emit the indexed getter for `x[n]` and not for `x[n + 1]`, where a
	member sized by arithmetic reached the *scalar* branch and got one getter
	returning the first element: no index, no count, and the rest of the array
	unreachable in the only backend that decoded it at all.

	Not the element width alone, because two constructs wear the same bracket.
	A text number is one value written as digits (`decimal u16 code[3]`,
	invariant 57) and a BCD field is one value written as nibbles, and neither
	is a run of anything.
	"""
	scalar = placement.scalar
	return (scalar is not None
	        and placement.radix is None
	        and not scalar.is_bcd
	        and scalar.bits > BITS_PER_BYTE
	        and scalar.bits % BITS_PER_BYTE == 0)


def index_entry_bytes(placement: Placement) -> int | None:
	"""How wide one entry of an `indexed` region's offset table is.

	The number every backend needs and none of them had. An `indexed` region's
	`count` counts *entries*, and an entry is an `offset_type` wide -- so the
	bytes the region declares are `count * entry`, and `count` alone is the
	same number in the wrong unit.

	It cost differently in each backend, which is what a fact spelled four
	times does. C multiplied by one, so a page declaring 11786 cells in a
	38-byte frame passed the check that exists to catch exactly that; the
	other three asked their element for a width, found a `table_leaf_cell`
	that has no fixed one, and emitted no check at all. Four answers, and the
	question is the layout's.

	`None` for anything that is not an `indexed` region.
	"""
	table = placement.index_table
	if placement.kind != "indexed" or table is None:
		return None
	return table.entry_bits // BITS_PER_BYTE or None


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


def walk_order(struct: ResolvedStruct,
		placement: Placement) -> list[Placement]:
	"""The members to step over when placing `placement`, in order.

	Its own struct's, normally. For a member inside a sealed region, the
	members before the region and then the region's interior -- the region
	itself contributes no bytes beyond that interior, and what follows the
	region does not precede anything inside it.

	And the same for a member inside a variant arm, where each arm starts at
	the variant's own base: what precedes it is what precedes the variant,
	and then whatever else its *own* arm declares. The other arms do not
	precede it -- they are the alternatives to it.

	The two are one function because they are one question -- where does a
	member that is not the struct's own begin -- and answering it twice is how
	a sealed interior came to be placed seventeen bytes late while an arm
	member named an offset function nobody emitted.
	"""
	members = own_members(struct)
	if placement.kind != "field":
		return members

	container: Placement | None = None
	interior: list[Placement] = []

	if placement.sealed_by is not None:
		container = next((held for held in members
		                  if held.name == placement.sealed_by), None)
		if container is not None and container.path != placement.path:
			interior = [entry.placement for entry in struct.entries
			            if entry.placement.sealed_by == container.name
			            and entry.placement.kind == "field"
			            and entry.placement.path != container.path]
	else:
		found = arm_of(struct, placement)
		if found is not None:
			container = found[0]
			interior  = [held for arm, held in arm_members(struct, container)
			             if held is not None and arm is found[1]]

	if container is None:
		return members

	before: list[Placement] = []
	for held in members:
		if held.path == container.path:
			break
		before.append(held)
	return before + interior


def preceding_parts(struct: ResolvedStruct,
		placement: Placement) -> list[int | Placement] | None:
	"""What lies before `placement` in its struct: runs of fixed bytes, and
	the variable members between them, in order.

	Alternating and always starting and ending with a byte count, so a caller
	either sums the whole thing or emits it term by term.

	The counts are accumulated in *bits* and divided once per run, which is
	the same rule `extent_parts` states and for the same reason: a `u4` and a
	`u4` are one byte together and zero apart. Every backend accumulated this
	one per member and truncated, so a struct opening with a packed pair
	placed everything after its first variable-length member one byte early --
	reading the last byte of the run and the first of whatever follows. All
	four had it, separately, and it is the second time this exact arithmetic
	has been got wrong in this file's neighbourhood.

	None where a run of fixed members is not a whole number of bytes at the
	point a variable one begins, which the solver already refuses.
	"""
	parts: list[int | Placement] = []
	bits  = 0

	# A member *inside* a sealed region is not one of the struct's own, so the
	# walk below never met it and never stopped: it summed every member of the
	# struct, the region it is in and the tag that follows the region, and C --
	# the one backend that emits an accessor for such a member -- read it 17
	# bytes past where it is. What precedes it is what precedes the region,
	# then the region's own earlier members.
	for other in walk_order(struct, placement):
		if other.path == placement.path:
			break
		if other.is_fixed_size:
			bits += other.size_bits
			continue
		if bits % BITS_PER_BYTE:
			return None
		parts.append(bits // BITS_PER_BYTE)
		parts.append(other)
		bits = 0

	if bits % BITS_PER_BYTE:
		return None
	parts.append(bits // BITS_PER_BYTE)
	return parts


def is_counted_run(structs: dict[str, ResolvedStruct],
		placement: Placement) -> bool:
	"""`T x[n]` where `T` is a struct with no single size.

	The count says how many and each element says how long it is: there is no
	stride to multiply, so the run is walked, and how far it reaches is the
	sum of what the walk stepped over.

	Only the C backend had a name for this shape, and only for deciding what
	to emit -- the length question fell through to the counted-array branch,
	which multiplied the count by an element width of one. The other three
	had no name for it at all and declined every member after such a run.

	Not an `indexed` region, which also carries a count and a variable
	element and is not walked at all: its elements are reached through an
	offset table, and `index_entry_bytes` is what says how long *it* is.
	`example/sqlite` is that shape, and calling it a counted run asked for a
	span function the indexed path does not emit.

	`T x[n + 1]` too. The count is a count however it is written, and asking
	only about `sized_by` left the arithmetic spelling to fall through to the
	nested-struct branch -- which names an `_extent` helper, emitted for one
	struct rather than for a run of them. The member after such a run called a
	function nobody defines. The docstring on `data_sized` lists the places
	that have asked this question and answered it for one spelling; this is
	another.
	"""
	if placement.kind == "indexed" or placement.type_name not in structs:
		return False
	if placement.array_count is None and not data_sized(placement):
		return False
	inner = structs.get(placement.type_name or "")
	return inner is not None and not inner.layout.is_fixed_size


def is_run(placement: Placement, structs: Container[str]) -> bool:
	"""A run of elements rather than a run of bytes.

	Both spellings end somewhere the bytes decide -- `T x[] until "D"` at a
	terminator, `T x[] while (c)` after the element failing `c` -- and both
	are walked one element at a time. What separates them from a delimited
	byte array is that the element is a struct, which is what makes framing a
	question about the element rather than about a scan.
	"""
	return (placement.repeat_while is not None
	        or (placement.delimiter is not None
	            and placement.type_name in structs))


def frameable(structs: dict[str, ResolvedStruct], struct: ResolvedStruct,
		seen: frozenset[str] = frozenset()) -> bool:
	"""Whether a whole one can be recognised in a prefix of a stream (20.3).

	Framing a run means framing its elements: a walk over what has arrived
	stops as readily at the end of the bytes as at the end of the run, and
	those are opposite answers. Asking the element's own `required` is what
	tells them apart, so a run is frameable exactly when its element is.

	`seen` cuts the recursion where a struct holds a run of itself. Such a
	schema has no finite frame anyway -- the terminator is the only thing that
	could end it, and asking about it recursively is how a compiler hangs
	rather than how it answers.

	Both the emitters and `gen-checks` need this, and the four backends had
	the record-run half of it written out four times.
	"""
	if struct.layout.register is not None:
		return False
	if struct.name in seen:
		return False
	if struct.layout.is_fixed_size and struct.layout.is_byte_sized:
		return True

	parts = extent_parts(structs, struct)
	if parts is None:
		return False

	constant, variable = parts
	if constant == 0 and not variable:
		# Nothing to frame: every buffer, including an empty one, already
		# holds a complete message.
		return False

	for placement in variable:
		# An `indexed` region's elements are reached through offsets the table
		# holds, and an offset may point anywhere the base allows -- so the
		# region reaches wherever its furthest element ends, which is not a sum
		# over what precedes it. The table is a lower bound and reporting it as
		# the total says a whole message has arrived when the header and the
		# table have.
		#
		# Three backends refused this for the wrong reason and stopped being
		# wrong for it when the table gained a length; C answered all along,
		# and answered with the bound (26.35).
		if placement.kind == "indexed":
			return False
		if not is_run(placement, structs):
			continue
		element = structs.get(placement.type_name or "")
		if element is None:
			return False
		if not frameable(structs, element, seen | {struct.name}):
			return False
	return True


def declared_value_bounds(placement: Placement,
		env: Env) -> tuple[int | None, int | None]:
	"""`[min]` and `[max]` folded to integers, for export as constants.

	The bounds are already stated once, formally, and enforced in `validate`
	-- and were reachable from nowhere else, so hand-written code validating
	the same value (a CLI flag that fills this field, a config key) restated
	the number and drifted. Exporting them is the single-source rule applied
	to the value domain (26.125).

	One decision, four spellings: each backend renders what this returns and
	none re-derives it. Only integer-domain scalars answer -- an unsigned or
	signed integer, a bit run, or a text number, where the schema's bound and
	the getter's value are the same number. Fixed point and BCD are excluded
	because the getter's value is scaled or decoded, so exporting the raw
	bound would hand a caller a constant in the wrong domain -- worse than
	none.

	A bound that does not fold (it references a field, say) is skipped rather
	than refused: `validate` still enforces it, and a constant that cannot be
	computed at compile time is not a constant.
	"""
	from situc.expr import evaluate
	from situc.types import ScalarKind

	if placement.kind != "field":
		return (None, None)
	scalar = placement.scalar
	if scalar is None or scalar.kind not in (
			ScalarKind.UINT, ScalarKind.SINT, ScalarKind.BIT):
		return (None, None)

	found: dict[str, int] = {}
	for attr in placement.attrs:
		if attr.name in ("min", "max") and attr.value is not None:
			try:
				found[attr.name] = evaluate(attr.value, env)
			except Exception:
				continue
	return (found.get("min"), found.get("max"))


def pad_alignment(placement: Placement) -> int | None:
	"""`pad_to(n)`'s n, or None where the member is not padding (0043).

	The decision layer answers this once so the offset sum and the length
	accessor spell one rule: a pad member advances the running offset to the
	next multiple of n rather than by a fixed size, and its own length is the
	distance covered.
	"""
	return placement.pad_to


def pinned_bytes(placement: Placement) -> int | None:
	"""The footprint `[size = N]` pinned, in bytes, or None where none is.

	The decision layer answers this once so four backends and two walkers
	spell one rule rather than each deriving it (0039). A pinned member is
	`Fixed(N)` in the layout while its length stays whatever the message says,
	so "does the declared length fit the footprint" is a question no other
	check asks: the frame check compares against what is left in the *view*,
	which is larger than the member whenever anything follows it.
	"""
	if placement.pinned_bits is None:
		return None
	return placement.pinned_bits // 8


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

	Through `data_sized`, which is the fix for what this asked before: only
	`sized_by`, so a length written as arithmetic -- `u8 data[(len + 1) * 8 -
	2]`, the shape `example/ipv6ext` is made of -- was not a length the
	message declares as far as this was concerned, and no backend emitted the
	check for one.
	"""
	return (data_sized(placement)
	        and placement.sized_by != "remaining"
	        and placement.delimiter is None
	        and placement.located is None)


def enclosing_arm(struct: ResolvedStruct,
		placement: Placement) -> tuple[Placement, Arm] | None:
	"""The arm this member is *inside*, which is not the same question.

	`arm_of` asks whether a placement **is** an arm's member; this asks
	whether it is one or lives within one. A reserved field inside a struct
	an arm selects is neither in the enclosing struct's bytes nor out of
	them: it is there only when the discriminant says so.

	`gen-checks` needed the difference. It poked the bytes of an arm nobody
	had selected and asserted `validate` would refuse them -- which it will
	not, because those bytes belong to whichever arm *is* selected, and the
	one selected by a zeroed discriminant had nothing to say about them.
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
		if member is not None and (placement.path == member.path
		                           or placement.path.startswith(member.path + ".")):
			return variant, arm
	return None


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


#: Stuffing codes a derived implementation exists for.
#:
#: The names are the key set of `kernels.STUFFING_BOUNDS`, which records what
#: each code's overhead actually is so that the derivation can refuse a schema
#: declaring one the implementation does not have. A name in that table and a
#: name in this list are the same claim, so they are one object rather than
#: two that agree today.
#:
#: The direction is the only one available. `codegen.c.derived` imports this
#: module, so the list could never have lived there; `kernels` imports nothing
#: from here, so it can own what both read.
#:
#: It was in both, and the comment here said the C generator owned it -- which
#: was true of nothing, since neither read the other. `derived.py` carries a
#: note saying "one list because there were two", written when a dispatch and
#: a prototype gate were consolidated; this was a third copy that pass did not
#: find. Adding `slip` and `ppp_async` to the C generator's copy left this one
#: at three codes, so every backend asked whether a SLIP region had a settled
#: decode shape and was told no, and declined an accessor it could have
#: emitted. `test_the_stuffing_code_list_has_one_home` is the guard.
DERIVED_STUFFING = tuple(sorted(kernels.STUFFING_BOUNDS))


def extern_symbol(schema: Schema, codec: str) -> str | None:
	"""The symbol a tier-1 codec's implementation is bound to (13.1, 13.2a).

	`impl x extern "my_x"` binds one, and the two functions situ calls are
	`my_x_encode` and `my_x_decode` -- the tier-1 ABI, which is the one shape
	a harness and an accessor can both assume because the compiler never
	learns what the algorithm does.

	`None` for a codec bound to a derived implementation, or to none at all: a
	signature may exist with no implementation (13.1), and asking for a symbol
	that was never named is how a header comes to declare a function nobody
	agreed to write.

	It was nobody's, and that is why `gen-codec-tests` invented
	`situ_codec_<codec>_encode` while the accessors called something else and
	`impl x extern "my_x"` named a symbol appearing nowhere at all (26.35).
	"""
	for decl in schema.impls():
		if decl.codec != codec:
			continue
		if getattr(decl.kind, "value", None) != "extern":
			return None
		return decl.symbol
	return None


def codec_entry_point(schema: Schema, codec: str) -> str | None:
	"""The decode symbol a consumer must call, where an accessor will not.

	A backend that declines to generate a decode -- because the region's
	encoded extent is the codec's to report, so there is no length to hand a
	buffer -- still owes the caller the name of what to call. Without it the
	generated module says a decode is somebody else's job and does not say
	whose, which is a refusal that names no remedy.

	Measured before this existed: for a length-changing region the four
	backends gave three answers about which entry point a consumer could
	reach. C declared the derived pair because C defines them; C++ and Rust
	declared the *tier-1* symbol and not the derived one, which is backwards
	-- the declared symbol is the one situ does not control and the omitted
	one is the C function it generated itself; and Python declared neither,
	though for a region it *can* decode it already names the symbol and the
	buffer size in a comment. That last is the shape worth having everywhere,
	and this is where the answer is decided so it stays one answer.

	`None` where there is no implementation to name -- a signature with no
	`impl` (13.1). A refusal that invents a symbol nobody agreed to write is
	worse than one that says only that the region is not decoded here.
	"""
	symbol = extern_symbol(schema, codec)
	if symbol is not None:
		return f"{symbol}_decode"

	for decl in schema.impls():
		if decl.codec == codec:
			return f"situ_{codec}_decode"
	return None


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
	# `permutation` and `shift_register` emit the same `(in, len, out) ->
	# count` shape as `table`, so a region decode over them is not a guess.
	# They were excluded by a docstring that named Hamming's nibble-in,
	# byte-out interface as the reason and then generalised it to "the other
	# families" -- which is the mistake this function's own history records
	# one step earlier, when `stuffing` was in the same position.
	family = getattr(getattr(kernel, "family", None), "value", None)
	if family in ("table", "permutation", "shift_register"):
		return True
	if family != "stuffing":
		return False

	named = kernel.argument("code")
	return getattr(named, "name", None) in DERIVED_STUFFING


def table_is_padded(codec: object) -> bool:
	"""Whether a table kernel fills out a partial final group (13.2).

	`pad` is what separates base64 from Manchester: both are symbol maps, and
	one of them emits whole groups and fills the last. That changes the shape
	of the generated loop -- a symbol at a time cannot express it, because the
	last group's symbol count depends on how much input was left -- and with
	it the unit the generated function counts.
	"""
	kernel = getattr(codec, "kernel", None)
	if kernel is None:
		return False
	if getattr(getattr(kernel, "family", None), "value", None) != "table":
		return False
	return kernel.argument("pad") is not None


def decode_counts_bits(codec: object) -> bool:
	"""Whether the decoder's count is bits rather than bytes.

	A `table` kernel is bit-oriented by construction -- *unless* it pads, and
	then it is not: the padded loop walks whole input bytes into whole output
	groups, and counts both in bytes. A `stuffing` kernel declares `unit`,
	because HDLC counts bits where COBS scans bytes.

	Getting this wrong is not a wrong answer, it is a buffer overrun. Every
	padded codec in `std/kernels.situ` -- base32, base64, base64url -- was
	declared in bits and defined in bytes, so a coded region using one passed
	`encoded * 8` to a loop that reads that many *bytes*: eight times the
	region in, eight times the output written, past whatever the caller
	supplied. The prototype and the definition differ in a parameter *name*,
	which C does not check, and no schema in the tree used such a region so
	nothing ran it (26.35).
	"""
	kernel = getattr(codec, "kernel", None)
	if kernel is None:
		return False
	if getattr(getattr(kernel, "family", None), "value", None) == "table":
		return not table_is_padded(codec)

	unit = kernel.argument("unit")
	return getattr(unit, "name", None) == "bit"


def decode_ratio(codec: object) -> tuple[int, int] | None:
	"""How the decoded length follows the encoded one, as a ratio.

	The codec's declared ratio, or `1:1` where it declares none and preserves
	length. A length-preserving kernel computes no ratio -- there is nothing
	for one to say -- and four backends read `codec.ratio` directly and
	emitted no decode accessor at all for `permutation` and `shift_register`
	as a result. The buffer size was never the unknown: it is the encoded
	length, exactly.

	`None` still, where neither a ratio nor length-preservation is declared:
	then the decoded size genuinely is not computable without decoding, and a
	wrong number would size a buffer the decode overruns.
	"""
	ratio: tuple[int, int] | None = getattr(codec, "ratio", None)
	if ratio is not None:
		return ratio if ratio[0] else None

	expansion = getattr(codec, "expansion", None)
	return (1, 1) if expansion is ast.Expansion.PRESERVING else None


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
	ratio = decode_ratio(codec)
	if ratio is None or placement.size_max_bits is None:
		return None

	bound = (placement.size_max_bits // BITS_PER_BYTE) * ratio[1] // ratio[0]

	# None rather than a number no target can hold. A region sized by a varint
	# has a maximum of 2^64 bytes -- the varint's own -- and doubling it is a
	# constant that does not fit the type it is emitted as: Rust said `literal
	# out of range for usize` and refused to compile, which is the loud end;
	# C would have wrapped it. A bound that cannot be represented is not a
	# bound, and the accessor that sizes a buffer from it is better absent
	# (26.53).
	return None if bound >= 1 << 32 else bound
