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
                         _read_at, offset_bits, scan, size_bits,
                         struct_extent, varint, while_count)

#: The probe kinds this walker renders. Named rather than counted so that a
#: kind quietly dropping out cannot look like agreement.
SUPPORTED = ("no-view", "scalar", "bytes", "element", "run_element",
             "arm_value", "sealed", "gated", "delimited", "varint",
             "while_count", "nested", "tag")

#: `image_kind`: which placements are plain scalars a walk can read.
FIELD, RESERVED, MARKER, REGION = 0, 1, 2, 3

#: `image_region.region_flags`
SEALED, UNVERIFIED_OK = 1, 2


def _local(image: Image, index: int) -> str:
	"""The name a driver prints for a member: its path after the struct,
	with the dots turned into underscores.

	`packet.sealed.inner_kind` is `sealed_inner_kind`, and an arm's
	`label.body.text` is `body_text`. Getting this wrong does not produce a
	disagreement -- it produces two lines that never meet, so the comparison
	skips them and passes. That is the quieter half of the vacuous pass:
	not a check that examined nothing, but a check whose two sides were
	talking about different names.
	"""
	return image.name_of(index).split(".", 1)[-1].replace(".", "_")


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
		# A varint and a delimiter used to make everything after them
		# unplaceable. The walk decodes both now, so an offset behind one is
		# computable -- which is what took sqlite's `payload` and the two
		# text protocols from unrenderable to compared.
		if earlier.repeat_code != NONE:
			return False
		delimited_member = (before in image.delimiters
		                    and earlier.type_struct == NONE)
		if (not earlier.fixed and earlier.size_code == NONE
				and before not in image.varints
				and not delimited_member):
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


#: `image_placement.text_flags`
MINIMAL, TRIMMED, CASE_INSENSITIVE = 1, 2, 4

#: What `[trim]` removes. HTTP's OWS and SIP's LWS, and deliberately not
#: `isspace`, which is locale dependent and includes CR, LF, VT and FF --
#: three of which are delimiters in the protocols this is for, so trimming
#: them would eat the framing.
OWS = (0x20, 0x09)


def _trimmed(view: View, index: int, content: int) -> int:
	"""The reported length, with `[trim]` applied.

	`[trim]` says whitespace at either end is framing rather than value. The
	member's *span* is unchanged -- the bytes are still there and still
	partition the struct -- and only the length handed to a caller shrinks,
	which is why this is applied to the answer and not to the extent.
	"""
	placement = view.image.placements[index]
	if not placement.text_flags & TRIMMED:
		return content
	start = view.at + offset_bits(view, index) // 8
	data  = view.buffer[start:start + content]
	head  = 0
	while head < len(data) and data[head] in OWS:
		head += 1
	tail = len(data)
	while tail > head and data[tail - 1] in OWS:
		tail -= 1
	return tail - head


def _nested(image: Image, struct_index: int) -> list[int]:
	"""Members that are another struct, asked whether they are there.

	The probe is `ok= extent=` rather than a value: what a caller gets is a
	sub-view, and what every backend can now answer is whether one can be
	made and how many bytes it covers.
	"""
	found = []
	for index in image.members(image.structs[struct_index]):
		placement = image.placements[index]
		if placement.type_struct == NONE or placement.type_struct is None:
			continue
		# A nested *member*, not a region that happens to hold a type.
		# sqlite's `cells` is an `indexed` region of `table_leaf_cell` and
		# the differ asks it `count=`; answering `ok= extent=` is a line
		# about a different question.
		if placement.kind != FIELD:
			continue
		if not _offset_computable(image, struct_index, index):
			continue
		if placement.array_count != NONE or placement.size_code != NONE:
			continue			# a run of them, not one
		if placement.repeat_code != NONE or index in image.delimiters:
			continue
		if index in image.regions or placement.is_tag:
			continue
		found.append(index)
	return found


def _tags(image: Image, struct_index: int) -> list[int]:
	"""Tags and checksums, asked only whether their bytes are there.

	Not what they hold and not whether they verify: situ guards the bytes and
	the caller runs the algorithm (14.1), so `present=` is the whole of what
	a backend answers without one.
	"""
	# Only where the walk can place it. keystore's tag sits after a sealed
	# region whose extent is the codec's, so C's pointer accessor hands back
	# NULL and answers `present=0`; a walker that summed its way to an
	# offset anyway would answer `present=1` about bytes nobody can find.
	return [index for index in image.members(image.structs[struct_index])
	        if image.placements[index].is_tag
	        and _offset_computable(image, struct_index, index)]


def _while_runs(image: Image, struct_index: int) -> list[int]:
	"""Runs that end after the element failing a predicate (8.6.6)."""
	return [index for index in image.members(image.structs[struct_index])
	        if image.placements[index].repeat_code != NONE
	        and image.placements[index].type_struct != NONE]


def _varints(image: Image, struct_index: int) -> list[int]:
	"""Members whose width is in their own bytes.

	Both numbers come off the wire and both are asked at once, because a
	varint that consumed a different number of bytes than it should have is
	a different message from one that decoded to a different value, and a
	probe reporting only the value cannot tell them apart.
	"""
	return [index for index in image.members(image.structs[struct_index])
	        if index in image.varints]


def _delimited(image: Image, struct_index: int) -> list[int]:
	"""Members that end at a delimiter rather than at a length.

	The probe asks two things at once -- how far it reached and whether it
	was terminated -- because a member whose delimiter is absent is truncated
	rather than empty, and the difference is the whole of what a hostile
	message does to a text protocol.
	"""
	found = []
	for index in image.members(image.structs[struct_index]):
		if index not in image.delimiters:
			continue
		placement = image.placements[index]
		if placement.is_tag or index in image.regions:
			continue
		if placement.radix:
			continue		# a delimited text *number*: value, not bytes
		if placement.type_struct != NONE:
			# `header_field fields[] until "\r\n"` is a run of records that
			# *ends* at a terminator, not a member that ends at a delimiter
			# -- 8.6.3's distinction, and the differ asks it `count=`. The
			# two spellings share a keyword and nothing else.
			continue
		found.append(index)
	return found


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


def _gated(image: Image, gate: int) -> list[int]:
	"""Plain scalars inside one sealed region, in declaration order.

	Only the scalars. A `[secret]` member has no debug accessor at all by
	design (14.6), and a byte run inside a gate is spelled four ways that
	have not been compared -- so the differ asks about neither and this
	renders neither.
	"""
	found = []
	for index, placement in enumerate(image.placements):
		if index == gate or image.region_owner.get(index) != gate:
			continue
		if placement.kind != FIELD or placement.is_tag:
			continue
		if placement.array_count != NONE or placement.size_code != NONE:
			continue			# a run inside a gate, not a scalar
		if index in image.delimiters or placement.radix:
			continue
		if placement.element_bits == NONE or placement.element_bits > 64:
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
			# A run of values wider than a byte carries neither a count nor
			# a size program in its own record -- `edges`' `body.wide` is
			# `size 0, max 48, element 16` -- so "is this one value" has to
			# be asked directly. The differ probes that shape `ok= count=
			# [0]=`, and answering it `ok= value=` is a line that looks like
			# an answer to a question nobody asked.
			if not arm.fixed or arm.size_bits != arm.element_bits:
				continue
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
		local = _local(image, index)
		try:
			lines.append(f"{local} {read_scalar(view, index)}")
		except Refused:
			# The frame does not reach it. Every backend refuses too, and
			# says so in its own way; what is comparable is that the line
			# is absent rather than wrong, so nothing is printed.
			continue

	for index in _nested(image, struct_index):
		local = _local(image, index)
		try:
			sub = View(image, view.buffer, image.placements[index].type_struct,
			           view.at + offset_bits(view, index) // 8, view.limit)
			extent = struct_extent(sub)
			if extent < 0 or sub.at + extent > view.limit:
				raise Refused("the frame does not hold the nested struct")
			lines.append(f"{local} ok=1 extent={extent}")
		except Refused:
			# Every backend can refuse a sub-view now, and says so the same
			# way: the answer is `ok=0`, not an absent line.
			lines.append(f"{local} ok=0 extent=0")

	for index in _tags(image, struct_index):
		local = _local(image, index)
		try:
			read_bytes(view, index)
			lines.append(f"{local} present=1")
		except Refused:
			lines.append(f"{local} present=0")

	for index in _while_runs(image, struct_index):
		local = _local(image, index)
		try:
			lines.append(f"{local} count={while_count(view, index)}")
		except Refused:
			continue

	for index in _varints(image, struct_index):
		local = _local(image, index)
		try:
			consumed, value = varint(view, index)
		except Refused:
			continue
		lines.append(f"{local} len={consumed} value={value}")

	for index in _delimited(image, struct_index):
		local = _local(image, index)
		try:
			content, terminated = scan(view, index)
			content = _trimmed(view, index, content)
		except Refused:
			continue
		lines.append(f"{local} len={content} term={1 if terminated else 0}")

	for index in _gates(image, struct_index):
		local = _local(image, index)
		# The gate's whole claim, and the one every backend can answer: it
		# refuses a failed verification and admits a passed one (14.3). The
		# answer does not depend on the bytes, which is why it is comparable
		# without the walker running anybody's cipher -- situ guards the
		# bytes and the caller runs the transform.
		lines.append(f"{local} refused=1 opened=1")
		# The interior, read through the gate the line above opened. This is
		# the half a tag exists to protect, so it is the half worth
		# comparing -- and it is why the gate probe carries its scalars
		# rather than standing alone.
		for inside in _gated(image, index):
			# The name *inside* the gate: the member's local name with the
			# region's stripped, which is what three backends call it. C
			# spells it `sealed_inner_kind` for want of a scope to put it
			# in, and the driver strips the same prefix.
			held = _local(image, inside)
			if held.startswith(local + "_"):
				held = held[len(local) + 1:]
			try:
				lines.append(f"{held} {read_scalar(view, inside)}")
			except Refused:
				continue

	for arm, selects, case in _arm_values(image, struct_index):
		local = _local(image, arm)
		try:
			chosen = read_scalar(view, selects) == case
			value  = read_scalar(view, arm) if chosen else 0
		except Refused:
			continue
		lines.append(f"{local} ok={1 if chosen else 0} value={value}")

	for index, shape in _runs(image, struct_index):
		local = _local(image, index)
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
