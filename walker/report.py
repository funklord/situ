"""The listing a walk prints, in the differ's own format.

`situc/codegen/differ.py` generates a driver per backend that prints one
canonical listing per buffer, and the differential check diffs the four. This
renders the same lines from an image, so the walker can be a fifth column
(26.33's requirement 2) rather than a program nobody compares.

**It renders a subset, and says which.** The differ has sixteen probe kinds;
the image carries what a walk needs for some of them and not yet for others
-- `validate` needs the constraint set, a marker needs its predicate, a gate
needs its verification. A fifth column that guessed at those would report
disagreements that are not there, which is the failure the differ's own
docstring warns about for its probe list.

So the comparison is over the lines this file claims, `SUPPORTED` names them,
and the test asserts the claim is non-empty and that every line rendered here
appears in all four backends' output. Growing the subset is the work; passing
while rendering nothing is the failure mode that has to be impossible.
"""

from __future__ import annotations

from walker.image import NONE, Image
from walker.walk import Refused, View, acquire, read_scalar, size_bits

#: The probe kinds this walker renders. Named rather than counted so that a
#: kind quietly dropping out cannot look like agreement.
SUPPORTED = ("no-view", "scalar")

#: `image_kind`: which placements are plain scalars a walk can read.
FIELD, RESERVED, MARKER = 0, 1, 2


def _scalars(image: Image, struct_index: int) -> list[int]:
	"""The members this walker will answer for, in declaration order.

	Only whole scalars: a run, a region and a variant each need a probe shape
	the image does not yet carry enough to render, and a wrong line is worse
	than a missing one.
	"""
	shape = image.structs[struct_index]
	chosen = []
	for index in image.members(shape):
		placement = image.placements[index]
		if placement.kind != FIELD:
			continue			# reserved and markers are not probed
		if placement.type_struct != NONE:
			continue			# a nested struct, not a scalar
		if not placement.fixed:
			continue			# a size the message decides
		if not placement.offset_known:
			continue			# an offset the message decides
		if index in image.delimiters:
			continue			# ends at a delimiter, so a byte run
		if index in image.regions:
			continue			# read through a gate, not directly
		if placement.size_code != NONE or placement.array_count != NONE:
			continue			# a run, not a scalar
		if placement.located_code != NONE or placement.repeat_code != NONE:
			continue
		if placement.radix:
			continue			# a text number: digits, not bits
		if placement.since:
			continue			# `[since]`: present only from a
						# version, which the differ probes
						# for presence rather than value
		if placement.marker_governed:
			continue			# byte order the message decides
		if placement.size_bits == NONE or placement.size_bits > 64:
			continue
		chosen.append(index)
	return chosen


def listing(image: Image, buffer: bytes) -> str:
	"""What this schema says about this buffer, one struct at a time."""
	lines: list[str] = []
	for struct_index, _ in enumerate(image.structs):
		name = image.struct_name(struct_index)
		lines.append(f"-- {name}")
		try:
			view = acquire(image, buffer, struct_index)
		except Refused:
			lines.append("no-view")
			continue
		lines.extend(_members(image, view, struct_index))
	return "\n".join(lines) + "\n"


def _members(image: Image, view: View, struct_index: int) -> list[str]:
	lines = []
	for index in _scalars(image, struct_index):
		local = image.name_of(index).split(".", 1)[-1]
		try:
			lines.append(f"{local} {read_scalar(view, index)}")
		except Refused:
			# The frame does not reach it. Every backend refuses too, and
			# says so in its own way; what is comparable is that the line
			# is absent rather than wrong, so nothing is printed.
			continue
	return lines
