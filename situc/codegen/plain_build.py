"""Which structs the C++, Rust and Python builders serve (26.855, 26.858).

One class, stated once. C's builder has served far more than this since
26.610; the other three took it up a class of struct at a time, and a
second copy of this rule per language would be three boundaries to keep in
step. Each language's builder asks here and emits its own code.
"""

from __future__ import annotations

from situc import traverse
from situc.codegen.c.build import (BITS, LITERAL, RUN, SCALAR, SIZE, SPAN, ZERO,
                                   Part, Shape, _supplies_a_value, plan,
                                   refusals)
from situc.resolve import ResolvedSchema, ResolvedStruct

__all__ = ["SERVED", "parts", "plain", "declined", "takes_value",
           "stated_ones"]


#: The roles in C's plan these builders write. A part outside them is a
#: struct this class declines, by name.
SERVED = frozenset({BITS, LITERAL, RUN, SCALAR, SIZE, SPAN, ZERO})


def _serves(part: Part, parts: list[Part]) -> bool:
	"""Whether this part is one the three builders write.

	Each exclusion is a thing C's writer does that these do not yet: digits
	for a text number, a value another arm claims, a run whose length a
	struct argument states, a count of elements rather than of bytes.
	"""
	held = part.placement
	if part.role not in SERVED or part.radix or part.others or part.length:
		return False
	if part.role in (RUN, SPAN) and held.kind != "field":
		return False
	if part.role in (SCALAR, SIZE) \
			and (held.scalar is None or held.marker is not None):
		return False
	if part.role == BITS and (
			held.scalar is None or held.marker is not None
			or traverse.bit_extractor(held.scalar, held) == "ne"):
		return False
	if part.role == SIZE or (part.role == BITS and part.sizes is not None):
		measured = next((one for one in parts if one.role == SPAN
		                 and one.placement is part.sizes), None)
		return measured is not None and not part.inner
	return True


def takes_value(part: Part) -> bool:
	"""Whether the caller hands this scalar part a value.

	A size field is computed, and a reserved bit field the schema states
	is written from the schema: C's own `_supplies_a_value`, for the two
	roles here that carry a number.
	"""
	return part.role == SCALAR or (part.role == BITS
	                               and _supplies_a_value(part))


def stated_ones(part: Part) -> bool:
	"""A reserved bit field the schema says is all ones.

	`must_be_zero` needs nothing: the group was zeroed when it opened.
	"""
	return part.placement.kind == "reserved" and not part.sizes \
		and traverse.reserved_policy(part.placement.attrs) == "must_be_one"


def parts(struct: ResolvedStruct, shape: Shape) -> list[Part] | None:
	"""C's plan for this struct, where every part is one this class writes.

	The plan rather than a second reading of the placements: it is what
	says a size field is computed and not supplied, a reserved run is the
	schema's to fill, and what a member's parameter is called. A first
	version read the placements and took a run sized by arithmetic for a
	six-byte scalar, its `size_bits` being its minimum (26.858).
	"""
	planned, _ = plan(struct, shape)
	if not planned or not all(_serves(one, planned) for one in planned):
		return None
	return planned


def plain(resolved: ResolvedSchema, shape: Shape) -> list[ResolvedStruct]:
	"""The structs this class builds, in declaration order.

	Intersected with C's own set rather than computed alone: a struct C
	refuses is one 26.610 decided a builder cannot serve, and a second
	opinion here would be a second rule to keep in step.
	"""
	refused = {name for name, _why in refusals(resolved, shape)}
	return [struct for struct in resolved.structs.values()
	        if struct.name not in refused
	        and parts(struct, shape) is not None]


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
	for struct in resolved.structs.values():
		if struct.name in refused or parts(struct, shape) is not None:
			continue
		out.append((struct.name,
		            f"{language}'s builder takes scalars, bit fields, byte "
		            "runs, the size fields measuring them, and stated "
		            "bytes so far; this one has more (26.860)"))
	return out
