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
from walker.walk import (Refused, View, acquire, read_bytes, read_scalar,
                         _read_at, offset_bits, size_bits)

#: The probe kinds this walker renders. Named rather than counted so that a
#: kind quietly dropping out cannot look like agreement.
SUPPORTED = ("no-view", "scalar", "bytes", "element", "run_element",
             "arm_value", "sealed")

#: `image_kind`: which placements are plain scalars a walk can read.
FIELD, RESERVED, MARKER, REGION = 0, 1, 2, 3

#: `image_region.region_flags`
SEALED, UNVERIFIED_OK = 1, 2


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
		if placement.is_tag:
			continue			# asked `present=`, not for a value
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


def _runs(image: Image, struct_index: int) -> list[tuple[int, str]]:
	"""Members that are a run of elements, and which probe shape each takes.

	`bytes` where the element is a byte: the differ asks for a pointer and a
	length, and prints the first byte or -1. `element` where the element is
	wider and the *schema* gives the count, which is reached by index because
	a converted value has no pointer into it. `run_element` where the message
	gives the count, which is the same question with the count asked first --
	the shape that was compared by nothing until 26.47.
	"""
	shape  = image.structs[struct_index]
	chosen = []
	for index in image.members(shape):
		placement = image.placements[index]
		if placement.kind != FIELD or placement.type_struct != NONE:
			continue
		if index in image.delimiters or index in image.regions:
			continue
		if placement.since or placement.marker_governed or placement.radix:
			continue
		if placement.is_tag:
			continue			# asked `present=`, not for bytes
		if placement.element_bits == NONE:
			continue

		counted = placement.array_count != NONE
		sized   = placement.size_code != NONE
		if not counted and not sized:
			continue			# a plain scalar, not a run
		if not _offset_computable(image, struct_index, index):
			continue
		if placement.element_bits == 8:
			chosen.append((index, "bytes"))
		elif counted and placement.element_bits <= 64:
			chosen.append((index, "element"))
		elif sized and placement.element_bits <= 64:
			chosen.append((index, "run_element"))
	return chosen


def _offset_computable(image: Image, struct_index: int, index: int) -> bool:
	"""Whether a walk can place this member at all.

	A member after one whose width is decoded rather than declared has no
	offset this walker can compute: a varint carries its length in its own
	bytes, a `while` run's length is however many elements passed the
	predicate, and a delimited member ends wherever the delimiter turns out
	to be. sqlite's `payload[payload_size]` sits after two varints and
	ipv6ext's `payload[remaining]` after a `while` run, and both were read at
	the wrong place before this said so.

	Left unrendered rather than guessed. The three shapes are the next things
	for the walk to learn, and each is a real construct rather than an
	oversight.
	"""
	placement = image.placements[index]
	if placement.offset_known:
		return True
	for before in image.members(image.structs[struct_index]):
		if before == index:
			return True
		earlier = image.placements[before]
		if before in image.varints or before in image.delimiters:
			return False
		if earlier.repeat_code != NONE:
			return False
		if not earlier.fixed and earlier.size_code == NONE:
			return False
	return True


def _element(view: View, index: int, at: int) -> int:
	"""One element of a run, by index rather than by pointer."""
	placement = view.image.placements[index]
	width = placement.element_bits
	start = offset_bits(view, index) + at * width
	if (start + width) // 8 > view.limit:
		raise Refused("the frame does not reach this element")
	return _read_at(view, index, start, width)


def _gates(image: Image, struct_index: int) -> list[int]:
	"""Sealed regions whose gate this walker can answer for.

	Not one the schema waived: `[allow_unverified_read]` is the construct
	whose purpose is to give up the guarantee (14.3), and then there is no
	gate to open -- the differ skips those, having once generated a driver
	that named an `_open` no backend emitted.
	"""
	found = []
	for index in image.members(image.structs[struct_index]):
		if image.placements[index].kind != REGION:
			continue
		flags = image.region_flags.get(index, 0)
		if not flags & SEALED or flags & UNVERIFIED_OK:
			continue
		found.append(index)
	return found


def _arm_values(image: Image, struct_index: int) -> list[tuple[int, int, int]]:
	"""A variant's scalar arms, as (arm placement, discriminant, case).

	The question a probe asks about an arm is reachability: is *this* arm the
	one the discriminant selects, for a discriminant the message chose. So
	the walker needs the case value, the member the arm names, and the field
	the switch is over -- and the last of those was missing from the image
	until arms were rendered (26.82).
	"""
	found = []
	for index in image.members(image.structs[struct_index]):
		if index not in image.arms:
			continue
		selects, arms = image.arms[index]
		if selects == NONE:
			continue
		for case, chosen, flags in arms:
			if flags or chosen == NONE:
				continue		# the default arm, or `default: error`
			arm = image.placements[chosen]
			if arm.element_bits == NONE or arm.element_bits > 64:
				continue
			if arm.array_count != NONE or arm.size_code != NONE:
				continue		# a byte run or an indexed run, not a value
			if chosen in image.delimiters or arm.is_tag:
				continue
			found.append((chosen, selects, case))
	return found


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

	for index in _gates(image, struct_index):
		local = image.name_of(index).split(".", 1)[-1]
		# The gate's whole claim, and the one every backend can answer: it
		# refuses a failed verification and admits a passed one (14.3). The
		# answer does not depend on the bytes, which is why it is comparable
		# without the walker running anybody's cipher -- situ guards the
		# bytes and the caller runs the transform.
		lines.append(f"{local} refused=1 opened=1")

	for arm, selects, case in _arm_values(image, struct_index):
		local = image.name_of(arm).split(".", 1)[-1]
		try:
			chosen = read_scalar(view, selects) == case
			value  = read_scalar(view, arm) if chosen else 0
		except Refused:
			continue
		lines.append(f"{local} ok={1 if chosen else 0} value={value}")

	for index, shape in _runs(image, struct_index):
		local = image.name_of(index).split(".", 1)[-1]
		try:
			lines.append(_run_line(view, index, local, shape))
		except Refused:
			if shape == "bytes":
				# A pointer accessor that refuses hands back NULL, and the
				# driver prints the empty answer rather than nothing. Saying
				# it the same way is the difference between agreeing and
				# being absent.
				lines.append(f"{local} len=0 first=-1")
	return lines


def _run_line(view: View, index: int, local: str, shape: str) -> str:
	count = _run_count(view, index)
	if shape == "bytes":
		raw = _run_bytes(view, index)
		first = raw[0] if raw else -1
		return f"{local} len={len(raw)} first={first}"
	if shape == "element":
		return f"{local}[0] {_element(view, index, 0)}"
	# `[0]=` is printed even for an empty run, as zero. The count and the
	# element are one line because the count is what says whether there is
	# an element at all, and the four backends spell "there is not" four
	# ways -- so the line has a fixed shape and a placeholder rather than a
	# shape that varies with the answer.
	first = _element(view, index, 0) if count > 0 else 0
	return f"{local} count={count} [0]={first}"


def _run_bytes(view: View, index: int) -> bytes:
	"""A byte run's bytes, under the rule the four backends settled on.

	The two shapes refuse differently and that is not an accident. A count
	the *schema* gives is a promise about the message: a frame that cannot
	hold it is malformed, the pointer accessor hands back NULL, and the
	length is zero. A count the *message* gives is a claim by an attacker,
	so it is clamped to what the frame actually holds -- which is 26.35's
	fix, after an accessor handed a caller fifty-five bytes out of a
	five-byte frame.
	"""
	placement = view.image.placements[index]
	start = offset_bits(view, index)
	if start % 8:
		raise Refused("a byte run that does not start on a byte")
	first = view.at + start // 8

	if placement.array_count != NONE:
		last = first + placement.array_count
		if last > view.limit:
			raise Refused("the frame does not hold the declared array")
		return view.buffer[first:last]

	want = size_bits(view, index) // 8
	return view.buffer[first:min(first + want, view.limit)]


def _run_count(view: View, index: int) -> int:
	"""How many elements a run has, clamped the way the backends clamp.

	A count the message gives is a claim by whoever sent the bytes, so it is
	held to what the frame can actually hold -- the same rule as a byte run,
	one element wide instead of one byte. Reporting the declared count
	instead said 228 where C said 22, over a buffer with room for 22.
	"""
	placement = view.image.placements[index]
	if placement.array_count != NONE:
		return placement.array_count

	width  = max(1, placement.element_bits) // 8
	want   = size_bits(view, index) // max(1, placement.element_bits)
	start  = view.at + offset_bits(view, index) // 8
	spare  = max(0, view.limit - start) // max(1, width)
	return min(want, spare)
