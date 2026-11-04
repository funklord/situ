"""Which structs the C++, Rust and Python builders serve (26.855, 26.858).

One class, stated once. C's builder has served far more than this since
26.610; the other three took it up a class of struct at a time, and a
second copy of this rule per language would be three boundaries to keep in
step. Each language's builder asks here and emits its own code.
"""

from __future__ import annotations

from situc.codegen.c.build import Shape, refusals
from situc.layout import BITS_PER_BYTE, Placement
from situc.resolve import ResolvedSchema, ResolvedStruct
from situc.traverse import is_own_member

__all__ = ["members", "plain", "declined"]


def is_plain(struct: ResolvedStruct) -> bool:
	"""Every member a whole-byte fixed scalar at a static offset.

	The class this serves. It excludes a bit-packed field for a reason worth
	naming rather than deferring: the packed run is written zeroed and then
	set in place, which is a second write to a byte this builder has already
	advanced past, and append-only is the property the rung was given
	permission for.
	"""
	seen = False
	for entry in struct.entries:
		placement = entry.placement
		if not is_own_member(struct, placement):
			continue
		seen = True
		scalar = placement.scalar
		if scalar is None or placement.kind != "field":
			return False
		# `is_fixed_size` as well as the two fields: a run sized by arithmetic
		# has neither (`u8 data[(n + 1) * 8 - 2]` is `size_expr`), and its
		# `size_bits` is only its minimum, so it passed for a six-byte
		# scalar and Python wrote an integer into a variable run. C took a
		# pointer there, which is what showed it (26.858).
		if placement.array_count is not None or placement.sized_by is not None \
				or not placement.is_fixed_size:
			return False
		if placement.radix is not None or placement.varint is not None:
			return False
		if placement.delimiters or scalar.is_bit_packed:
			return False
		if placement.offset_bits is None or not placement.size_bits:
			return False		# a zero-width member has nothing to write

		if placement.offset_bits % BITS_PER_BYTE \
				or placement.size_bits % BITS_PER_BYTE:
			return False
		if placement.parameter or placement.since is not None:
			return False
	return seen


def plain(resolved: ResolvedSchema, shape: Shape) -> list[ResolvedStruct]:
	"""The structs this class builds, in declaration order.

	Intersected with C's own set rather than computed alone: a struct C
	refuses is one 26.610 decided a builder cannot serve, and a second
	opinion here would be a second rule to keep in step.
	"""
	refused = {name for name, _why in refusals(resolved, shape)}
	return [struct for struct in resolved.structs.values()
	        if struct.name not in refused and is_plain(struct)]


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
		if struct.name in refused or is_plain(struct):
			continue
		out.append((struct.name,
		            f"{language}'s builder takes a struct whose every member "
		            "is a whole-byte fixed scalar so far; this one is not "
		            "(26.855)"))
	return out


def members(struct: ResolvedStruct) -> list[Placement]:
	return [entry.placement for entry in struct.entries
	        if is_own_member(struct, entry.placement)]
