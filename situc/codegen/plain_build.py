"""Which structs the C++, Rust and Python builders serve (26.855, 26.858).

One class, stated once. C's builder has served far more than this since
26.610; the other three took it up a class of struct at a time, and a
second copy of this rule per language would be three boundaries to keep in
step. Each language's builder asks here and emits its own code.
"""

from __future__ import annotations

from situc import traverse
from dataclasses import dataclass

from situc import ast
from situc.codegen.c.build import (ARG, FRAMED, MARKER, ARMTAG, BITS, CHOSEN, ENDED, LITERAL, NESTED,
                                   PAD, PEEKED, REPEAT, RUN, SCALAR, SIZE, SPAN,
                                   TAG, VARINT, WIDE, ZERO, ArmOf, Choice, Part,
                                   Shape, _supplies_a_value, arm_name, arms,
                                   plan, refusals)
from situc.resolve import ResolvedSchema, ResolvedStruct

__all__ = ["SERVED", "MEASURED", "Unit", "parts", "shape", "units", "plain",
           "declined", "takes_value", "stated_ones", "walked_at_size",
           "counts_elements", "tag_order"]


#: The roles in C's plan these builders write. A part outside them is a
#: struct this class declines, by name.
SERVED = frozenset({ARG, ARMTAG, BITS, CHOSEN, ENDED, FRAMED, LITERAL, MARKER, NESTED, PAD,
                    PEEKED, REPEAT, RUN, SCALAR, SIZE, SPAN, TAG, VARINT, WIDE,
                    ZERO})

#: The roles whose bytes the caller hands over and whose length a size field
#: may measure.
MEASURED = frozenset({ENDED, NESTED, REPEAT, SPAN})


def _serves(part: Part, parts: list[Part]) -> bool:
	"""Whether this part is one the three builders write.

	Each exclusion is a thing C's writer does that these do not yet: digits
	for a text number, a value another arm claims, a run whose length a
	struct argument states, a count of elements rather than of bytes.
	"""
	held = part.placement
	if part.role == CHOSEN:
		return any(_serves_arm(one, parts) for one in part.arms)
	if part.role not in SERVED or part.length:
		return False
	if part.verify:
		return False		# its length is checked against the reader's
					# `_len` afterwards, which is C's (26.1016)
	if part.others and part.role not in (BITS, NESTED, SCALAR, VARINT):
		return False
	if part.role in (ENDED, RUN, SPAN, WIDE) and held.kind != "field" \
			and not (part.role == ENDED and part.code):
		return False		# a coded region is ENDED with its code
	# In a marker's scope where the marker is a member written here, or
	# an argument the caller hands over: its value sets the order the
	# store reads (26.1007, 26.1018).
	marked = held.marker is not None and not any(
		(one.role == MARKER or (one.role == ARG and one.little))
		and one.placement.name == held.marker
		for one in parts)
	if part.role in (ARMTAG, SCALAR, SIZE) \
			and (held.scalar is None or marked
			     # A pad or a run in an arm is a scalar part wider or
			     # narrower than its scalar, and not one value. A text
			     # number is as wide as its digits.
			     or (held.size_bits != held.scalar.bits and not part.radix)):
		return False
	if part.role == BITS and (
			held.scalar is None or held.marker is not None
			or traverse.bit_extractor(held.scalar, held) == "ne"):
		return False
	if part.role in (NESTED, REPEAT) and held.arguments \
			and not (part.role == REPEAT and part.args):
		return False		# a run's are the caller's values (26.1018)
	if part.role == REPEAT and part.value == 1 and sum(
			one.role == REPEAT and one.value == 1 for one in parts) > 1:
		return False		# one walked count to hold the reader's to
	if part.role == VARINT and part.sizes is not None and part.solved:
		return False		# no value to invert through, yet
	if part.role == SIZE or (part.role in (BITS, VARINT)
	                         and part.sizes is not None):
		measured = next((one for one in parts
		                 if (one.role in MEASURED or one.role == WIDE)
		                 and one.placement is part.sizes), None)
		if measured is None or measured.delimiter:
			return False		# the delimiter is not the caller's to count
		if measured.role == WIDE and (measured.bytes or part.solved):
			return False		# a count, with nothing to compute
		return not part.inner or (part.role == SIZE and part.solved is None
		                          and measured.role == REPEAT)
	return True


def _serves_arm(arm: ArmOf, parts: list[Part]) -> bool:
	"""Whether what this arm takes and writes is all in the class."""
	return all(_serves(one, parts) for one in (arm.tag, arm.part)
	           if one is not None)


def tag_order(part: Part) -> str:
	"""`le` or `be`: what C's writer stores a codec's output as."""
	return ("le" if part.placement.tag_codec_endian is ast.Endian.LITTLE
	        else "be")


def walked_at_size(part: Part, parts: list[Part]) -> bool:
	"""Whether a count field already walked this run.

	The count is what the walk finds, so the walk happens at the field that
	states it, and a second one would be two measurements of one thing.
	"""
	return any(one.role == SIZE and one.inner and one.sizes is part.placement
	           for one in parts)


def counts_elements(part: Part, parts: list[Part]) -> bool:
	"""Whether the walk over this run has to keep a count."""
	return part.digits > 0 or part.value == 1 or walked_at_size(part, parts)


def shape(schema: ast.Schema, resolved: ResolvedSchema) -> Shape:
	"""What C's plan needs, with the header that says what frames itself.

	A nested member is refused without it: the header is the artifact that
	says which structs have a `required`, and the builders ask the same
	question of the nested struct in their own language.
	"""
	from situc.codegen.c import generate
	from situc.codegen.c.build import derived_codecs

	return Shape(generate(schema, resolved, "plain").header, "situ",
	             resolved.structs,
	             {decl.name: decl for decl in schema.varints()},
	             derived_codecs(schema),
	             {decl.name: decl for decl in schema.markers()})


def takes_value(part: Part) -> bool:
	"""Whether the caller hands this scalar part a value.

	A size field is computed, and a reserved bit field the schema states
	is written from the schema: C's own `_supplies_a_value`, for the two
	roles here that carry a number.
	"""
	return part.role in (SCALAR, MARKER) or (part.role in (BITS, VARINT)
	                               and _supplies_a_value(part))


def stated_ones(part: Part) -> bool:
	"""A reserved bit field the schema says is all ones.

	`must_be_zero` needs nothing: the group was zeroed when it opened.
	"""
	return part.placement.kind == "reserved" and not part.sizes \
		and traverse.reserved_policy(part.placement.attrs) == "must_be_one"


def parts(struct: ResolvedStruct, shape: Shape,
		arm: Choice | None = None) -> list[Part] | None:
	"""C's plan for this struct, where every part is one this class writes.

	The plan rather than a second reading of the placements: it is what
	says a size field is computed and not supplied, a reserved run is the
	schema's to fill, and what a member's parameter is called. A first
	version read the placements and took a run sized by arithmetic for a
	six-byte scalar, its `size_bits` being its minimum (26.858).
	"""
	planned, _ = plan(struct, shape, arm)
	if not planned or not all(_serves(one, planned) for one in planned):
		return None
	# An arm this class cannot write is left out of what a caller can hand
	# over, as a builder of that arm alone would be.
	for part in planned:
		kept = tuple(one for one in part.arms if _serves_arm(one, planned))
		part.declined = (*part.declined,
		                 *((one.name, f"`{one.name}` is an arm this class "
		                   "does not write yet") for one in part.arms
		                   if one not in kept))
		part.arms = kept
	return planned


@dataclass(frozen=True)
class Unit:
	"""One builder: a struct, or one arm of a struct's variant.

	C writes a builder per arm, the arm being chosen by which one you call,
	so the discriminant is the builder's to write and cannot disagree with
	the body. `suffix` is that arm's name and empty for a plain struct.
	"""

	struct: ResolvedStruct
	parts: tuple[Part, ...]
	suffix: str = ""


def units(resolved: ResolvedSchema, shape: Shape) -> list[Unit]:
	"""Every builder this class writes, in declaration order."""
	refused = {name for name, _why in refusals(resolved, shape)}
	found: list[Unit] = []
	for struct in resolved.structs.values():
		if struct.name in refused:
			continue
		cases = arms(struct)
		if not cases:
			planned = parts(struct, shape)
			if planned is not None:
				found.append(Unit(struct, tuple(planned)))
			continue
		for case in cases:
			planned = parts(struct, shape, case)
			if planned is not None:
				found.append(Unit(struct, tuple(planned), arm_name(case)))
	return found


def plain(resolved: ResolvedSchema, shape: Shape) -> list[ResolvedStruct]:
	"""The structs this class builds, in declaration order.

	Intersected with C's own set rather than computed alone: a struct C
	refuses is one 26.610 decided a builder cannot serve, and a second
	opinion here would be a second rule to keep in step.
	"""
	served = {one.struct.name for one in units(resolved, shape)}
	return [struct for struct in resolved.structs.values()
	        if struct.name in served]


def declined(resolved: ResolvedSchema, shape: Shape,
		language: str) -> list[tuple[str, str]]:
	"""What this class does not build, and why, for stderr.

	C's refusals pass through unchanged -- they are the same decisions --
	and a struct C serves that this does not is named with the class it
	falls outside, so the boundary is readable rather than inferred from an
	absence.
	"""
	out = list(refusals(resolved, shape))
	refused = {name for name, _why in out}
	built = {(one.struct.name, one.suffix) for one in units(resolved, shape)}
	for struct in resolved.structs.values():
		if struct.name in refused:
			continue
		cases = arms(struct)
		if cases:
			out.extend(
				(f"{struct.name}.{arm_name(case)}".rstrip("."),
				 _unwritten(struct, shape, case, language))
				for case in cases
				if (struct.name, arm_name(case)) not in built)
			out.extend(
				(line for unit in units(resolved, shape)
				 if unit.struct is struct for part in unit.parts
				 for line in ((f"{struct.name}.{arm}", why)
				              for arm, why in part.declined)
				 if line not in out))
			continue
		if (struct.name, "") in built:
			continue
		out.append((struct.name, _unwritten(struct, shape, None, language)))
	return out


def _unwritten(struct: ResolvedStruct, shape: Shape, arm: Choice | None,
		language: str) -> str:
	"""Which part keeps this out, so the boundary can be read off the line."""
	planned, why = plan(struct, shape, arm)
	if why:
		return why
	held = next((one for one in planned if not _serves(one, planned)), None)
	if held is None:
		return f"{language}'s builder writes nothing for it"
	name = traverse.local_name(struct, held.placement)
	return f"`{name}` is a {held.role} part, which {language}'s builder " \
	       "does not write yet"
