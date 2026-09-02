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

from walker import vm
from walker.image import NONE, Image
from walker.walk import (BITS_PER_BYTE, Refused, Unplaceable, View,
                         acquire, digits_of,
                         parse_digits, read_bytes, read_scalar,
                         _read_at, offset_bits, scan, size_bits,
                         struct_extent, varint, while_count)

#: The probe kinds this walker renders. Named rather than counted so that a
#: kind quietly dropping out cannot look like agreement.
SUPPORTED = ("no-view", "scalar", "bytes", "element", "run_element",
             "arm_value", "sealed", "gated", "delimited", "varint",
             "while_count", "nested", "tag", "marker", "validate",
             "relation")

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
		# An offset the message decides is placed by walking the members
		# before it, which this walker does and did not use here: the filter
		# predates that and was the conservative choice when a wrong line was
		# the risk. It no longer is -- the renderer already answers a member
		# it cannot place by printing nothing, "absent rather than wrong" --
		# so the bar is what the walk can reach rather than what the image
		# states outright.
		#
		# It costs 17 scalars of differential coverage to skip them, and they
		# are the ones after a variable-length member: `adv_report.rssi` sits
		# behind `data[data_length]` and every backend answers it (26.185).
		if index in image.delimiters:
			continue			# ends at a delimiter, so a byte run
		# Behind a *gate*, which is a `sealed` region and not any region.
		# An `authenticated` one has no gate: its members sit where they
		# would have sat anyway and are read directly, which is why 5.3
		# addresses `Packet.hdr.seq`. Skipping them too left every field of
		# `example/icmp`'s `authenticated message` out of the listing, and
		# `example/udp`'s whole header once its checksum covered one.
		if index in image.regions \
				and image.region_flags.get(index, 0) & SEALED:
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


def _trim_span(view: View, index: int, content: int) -> tuple[int, int]:
	"""Where a delimited member's value starts inside its span, and how long.

	`[trim]` says whitespace at either end is framing rather than value. The
	member's *span* is unchanged -- the bytes are still there and still
	partition the struct -- and only what is handed to a caller shrinks,
	which is why this is applied to the answer and not to the extent. C says
	the same thing in two accessors, `situ_trim_start` shifting the pointer
	and `situ_trim_len` shortening the length.

	Both numbers, because a length alone cannot say which bytes: the probe
	wants the second and an owned value wants the span. One derivation, since
	a second copy of "what does `[trim]` remove" is how two readers of one
	attribute start disagreeing.
	"""
	placement = view.image.placements[index]
	if not placement.text_flags & TRIMMED:
		return 0, content
	start = view.at + offset_bits(view, index) // 8
	data  = view.buffer[start:start + content]
	head  = 0
	while head < len(data) and data[head] in OWS:
		head += 1
	tail = len(data)
	while tail > head and data[tail - 1] in OWS:
		tail -= 1
	return head, tail - head


def _trimmed(view: View, index: int, content: int) -> int:
	"""The reported length, with `[trim]` applied."""
	return _trim_span(view, index, content)[1]


def content_bytes(view: View, index: int) -> bytes:
	"""A delimited member's value: its content, without the delimiter.

	What every backend's `_ptr` and `_len` hand back, and not what
	`read_bytes` does -- that answers the member's *span*, which includes the
	delimiter, because the span is what places the member after it. The two
	numbers differ by the delimiter's width and an owned value wants the
	smaller one.
	"""
	content, _ = scan(view, index)
	head, width = _trim_span(view, index, content)
	start = view.at + offset_bits(view, index) // 8 + head
	if start + width > view.limit:
		raise Refused("the frame does not reach this member")
	return bytes(view.buffer[start:start + width])


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
		verdict = _validate(image, view, struct_index)
		if verdict is not None:
			lines.append(f"validate {verdict}")
	return "\n".join(lines) + "\n"


#: `situ_err_t`, which the driver prints as an integer.
OK, ERR_BOUNDS, ERR_CONSTRAINT = 0, 1, 2

#: `image_check`
MUST_EQ, MINIMUM, MAXIMUM, MUST_BE_ZERO, MUST_BE_ONE, ENUM_KNOWN = range(6)
FITS_FRAME, TERMINATED, ARM_SELECTED = 6, 7, 8
DIGITS_VALID, DIGITS_MINIMAL = 9, 10
NUL_TERMINATED, ENCODED_AS, ZERO_RUN = 11, 12, 13

#: `situ_err_t` again: an unknown discriminant is a message this build
#: cannot read rather than one that breaks a rule.
ERR_VERSION = 3


def relate(image: Image, which: int, request: View, response: View) -> int:
	"""Whether a pair satisfies relation `which` (26.95).

	`OK` or `ERR_CONSTRAINT`, which is the same answer the four compiled
	backends give and deliberately no richer: a relation is a predicate, and
	a walker that reported *which* constraint failed would be answering a
	question the generated code cannot.

	The two views are the caller's, in temporal order. Nothing here decides
	which messages are a pair -- that is the caller's at every rung, and a
	walker has no more standing to invent it than a generated predicate does.
	"""
	relation = image.relations[which]
	views    = (request, response)

	def load_arg(arg: int, index: int) -> int:
		if arg >= len(views):
			raise Refused(f"relation names parameter {arg}, and has two")
		return _read_for_relation(views[arg], index)

	for code_at in relation.musts:
		if vm.run(image.code, code_at,
		          load_field = lambda i: _read_for_relation(request, i),
		          size_of    = lambda i: size_bits(request, i) // BITS_PER_BYTE,
		          offset_of  = lambda i: offset_bits(request, i) // BITS_PER_BYTE,
		          count_of   = lambda i: 0,
		          remaining  = 0,
		          load_arg   = load_arg) == 0:
			return ERR_CONSTRAINT
	return OK


def _read_for_relation(view: View, index: int) -> int:
	"""One placement's value, out of the view the parameter named.

	`read_scalar` is the walk's own reader and is reused rather than
	reimplemented: a relation compares *values*, so whatever byte swapping,
	scaling or digit parsing that member needs is the same work it needs
	anywhere else.
	"""
	return read_scalar(view, index)


def _validate(image: Image, view: View, struct_index: int) -> int | None:
	"""What `validate` returns, or None where this image cannot say.

	The whole answer or nothing. Every other probe can be rendered for the
	members the image describes and skipped for the rest, because each is a
	separate line -- `validate` is one line about the whole struct, so a
	partial one reports OK where the schema refuses. The packer sets a bit
	per struct saying every check is carried, and this declines otherwise.

	Order matters because the first failure is the answer: a member the
	frame does not reach is `BOUNDS` and a constraint that fails is
	`CONSTRAINT`, and which comes first decides which is returned.
	"""
	if not image.structs[struct_index].validatable:
		return None

	for index in image.members(image.structs[struct_index]):
		# A `[since]` member is there only in a message whose own version
		# reaches it, and a field that is not there is not a field that is
		# wrong. So the version is read first and the member skipped
		# entirely -- not merely left unchecked, because placing it would
		# ask the frame for bytes the message never claimed to carry and
		# answer BOUNDS where C answers OK.
		#
		# Nothing after it moves: `[since]` is append-only by construction,
		# so every member keeps the offset it would have had and only its
		# presence varies. That is why this needs a scalar read and not a
		# second offset chain.
		since = image.placements[index].since
		if since:
			carries = image.versions.get(struct_index)
			if carries is None:
				return None		# the packer should not have said yes
			try:
				if read_scalar(view, carries) < since:
					continue
			except Refused:
				return ERR_BOUNDS

		# Every member is *placed*, not only the constrained ones: a struct
		# whose members after a coded region cannot be reached fails with
		# BOUNDS before any constraint is asked, and checking only the
		# constrained ones answered OK for `edges`' `coded_run` where every
		# backend answered 1.
		try:
			at   = offset_bits(view, index)
			wide = size_bits(view, index)
		except Unplaceable:
			# Not BOUNDS, and the distinction is the whole of this branch.
			# A member nothing can place -- something before it has no
			# length in closed form -- is one no backend emits an offset
			# for and none checks. `segments.after` sits behind a counted
			# run of variable-length elements and C emits a comment where
			# its offset would be.
			#
			# Nothing after it can be placed either, so the walk stops
			# rather than skipping one member and carrying on:
			# `_offset_blocker` scans every earlier member, so once one
			# blocks, all of them do.
			break
		except Refused:
			# The ordinary case: the frame does not reach it.
			return ERR_BOUNDS

		# A member of *fixed* size at a *dynamic* offset. Its offset is a
		# sum of lengths the message chose, so the bounds check that
		# acquired the view never answered for it: `example/packet`'s tag
		# is sixteen fixed bytes and was 65 kilobytes past a 62-byte view.
		# Both facts are flags the image already carries, so this is the
		# walk's own arithmetic rather than a constraint the packer emits
		# -- which is what the C backend does too, from the same pair.
		#
		# A member that declares its own length is the case below and is
		# never fixed-size, so the two do not overlap.
		placed = image.placements[index]
		if placed.fixed and not placed.offset_known:
			if view.at * 8 + at + wide > view.limit * 8:
				return ERR_BOUNDS

		# A run whose length the message declares has to fit the frame.
		# The accessor clamps; `validate` is where a message that does not
		# fit is called malformed, and it answers BOUNDS rather than
		# CONSTRAINT. udp's `payload[length - 8]` is the shape. Which runs
		# carry this is the packer's to say -- `remaining` and a `while`
		# run do not.
		if any(check == FITS_FRAME
		       for check, _ in image.constraints.get(index, ())):
			if view.at * 8 + at + wide > view.limit * 8:
				return ERR_BOUNDS

		# A nested member is `validate` called through, and its error is
		# returned as it stands: C propagates the inner code rather than
		# folding it into CONSTRAINT.
		placement = image.placements[index]
		# One nested member, not a run of them. `nl_message.attrs` is a
		# `while` run of `nlattr` and gets the repeated check, not the
		# nested one -- recursing into it validated element zero as though
		# it were the member and answered BOUNDS where C answered OK.
		if placement.type_struct != NONE and placement.kind == FIELD \
				and placement.array_count == NONE \
				and placement.size_code == NONE \
				and placement.repeat_code == NONE \
				and index not in image.delimiters:
			inner = View(image, view.buffer, placement.type_struct,
			             view.at + offset_bits(view, index) // 8, view.limit)
			verdict = _validate(image, inner, placement.type_struct)
			if verdict:
				return verdict

		held = image.constraints.get(index)
		if not held:
			continue

		# `fits_frame` is about a run and the rest are about a value, so
		# reading one out of the other is how this asked a byte array for
		# a scalar and called the refusal BOUNDS.
		if any(check == TERMINATED for check, _ in held):
			try:
				if not scan(view, index)[1]:
					return ERR_CONSTRAINT
			except Refused:
				return ERR_BOUNDS

		if any(check == ARM_SELECTED for check, _ in held):
			verdict = _arm_selects(image, view, index)
			if verdict:
				return verdict

		# A text number, in the runtime's own order: the spelling before
		# the value. `situ_digits_minimal` is what this mirrors, including
		# the part that is easy to miss -- above radix ten an upper-case
		# digit is the second spelling too, and `A` and `a` are one number
		# written two ways.
		for check, against in held:
			if check != DIGITS_MINIMAL:
				continue
			try:
				digits = digits_of(view, index)
			except Refused:
				return ERR_CONSTRAINT
			if not digits:
				return ERR_CONSTRAINT
			if len(digits) > 1 and digits[0:1] == b"0":
				return ERR_CONSTRAINT
			if against > 10 and any(0x41 <= one <= 0x46 for one in digits):
				return ERR_CONSTRAINT

		for check, against in held:
			if check != DIGITS_VALID:
				continue
			# CONSTRAINT and not BOUNDS, even where the frame is what ran
			# out: C reaches this through the getter, whose `_ptr` and
			# `_len` clamp, so a text number nobody can read is a field
			# that is not a number rather than a field that is not there.
			try:
				value = parse_digits(view, index)
			except Refused:
				return ERR_CONSTRAINT
			if value > against:
				return ERR_CONSTRAINT

		# The byte-run checks: a terminator inside the field, an
		# encoding the bytes are actually in, and a reserved run that is
		# all zero. Each reads the member's whole span rather than a
		# value, which is what separates them from the comparisons below
		# -- and why the capacity is the member's own size and not
		# something the constraint has to carry.
		span = [pair for pair in held
		        if pair[0] in (NUL_TERMINATED, ENCODED_AS, ZERO_RUN)]
		if span:
			start = view.at + at // BITS_PER_BYTE
			# A delimited member's `wide` is content *plus* its delimiter,
			# because that is where the next member starts. What the schema
			# called text is the content, and that is what every backend
			# passes to the check -- `_len`, not `_span`.
			if index in image.delimiters:
				try:
					content = scan(view, index)[0]
				except Refused:
					return ERR_BOUNDS
			else:
				content = wide // BITS_PER_BYTE
			data = bytes(view.buffer[start:start + content])
			if len(data) != content:
				return ERR_BOUNDS
			for check, against in span:
				if check == NUL_TERMINATED and 0 not in data:
					return ERR_CONSTRAINT
				if check == ZERO_RUN and any(data):
					return ERR_CONSTRAINT
				if check != ENCODED_AS:
					continue
				if against == 0:
					if any(one > 0x7F for one in data):
						return ERR_CONSTRAINT
					continue
				# Decoded rather than re-implemented. Each runtime validator
				# -- `situ_utf8_valid`, `situ_utf16le_valid`,
				# `situ_utf16be_valid` -- refuses exactly the set Python's
				# strict decoder does: an overlong form or surrogate half for
				# utf8, a lone surrogate or odd byte count for utf16 (0044).
				# Restating the state machine here would be a second chance to
				# get it wrong. The codes match `pack.ENCODING_CODE`.
				codec = {1: "utf-8", 2: "utf-16-le", 3: "utf-16-be"}[against]
				try:
					data.decode(codec)
				except UnicodeDecodeError:
					return ERR_CONSTRAINT


		value_checks = [pair for pair in held
		                if pair[0] not in (FITS_FRAME, TERMINATED,
		                                   ARM_SELECTED, DIGITS_VALID,
		                                   DIGITS_MINIMAL, NUL_TERMINATED,
		                                   ENCODED_AS, ZERO_RUN)]
		if not value_checks:
			continue
		try:
			value = read_scalar(view, index)
		except Refused:
			return ERR_BOUNDS
		for check, against in value_checks:
			if check == MUST_EQ and value != against:
				return ERR_CONSTRAINT
			if check == MINIMUM and value < against:
				return ERR_CONSTRAINT
			if check == MAXIMUM and value > against:
				return ERR_CONSTRAINT
			if check == MUST_BE_ZERO and value != 0:
				return ERR_CONSTRAINT
			if check == MUST_BE_ONE and value != against:
				return ERR_CONSTRAINT
			if check == ENUM_KNOWN \
					and value not in image.enum_values.get(against, set()):
				return ERR_CONSTRAINT
			if check == FITS_FRAME:
				continue		# handled above, before the value is read
	return OK


def _arm_selects(image: Image, view: View, index: int) -> int:
	"""Whether the discriminant names an arm that exists.

	`default: error` is the refusal this asks about: a value naming no arm is
	a message this build cannot read, which is `VERSION` and not
	`CONSTRAINT`. The distinction is the schema's -- 14.5 makes refusing an
	unknown discriminant the default rather than a choice.
	"""
	selects, arms = image.arms.get(index, (NONE, []))
	if selects == NONE:
		return OK
	try:
		value = read_scalar(view, selects)
	except Refused:
		return ERR_BOUNDS
	for case, chosen, flags in arms:
		if flags & 1:
			continue		# the default arm names no case
		if case != value:
			continue
		# The arm the discriminant selects has to fit the frame. The
		# accessor clamps; this is where a message that does not fit is
		# called malformed, and it is BOUNDS rather than VERSION -- the
		# discriminant was fine and the bytes behind it were not.
		if chosen == NONE:
			return OK

		# An arm whose type cannot be measured from its own bytes has no
		# sub-view, so nothing asks whether it fits the frame either --
		# there is nothing to compare against. `packet.body.publish` is
		# that arm, and measuring it anyway called a three-byte MQTT
		# publish malformed where C called it fine.
		arm_type = image.placements[chosen].type_struct
		if arm_type != NONE and not image.structs[arm_type].measurable:
			return OK

		# Measured through the variant *member*, not the arm. An arm is
		# not in the struct's member chain, so asking `offset_bits` for
		# one refuses -- which made every mqtt packet BOUNDS where C said
		# OK. `size_bits` on the member routes to `_variant_bits`, which
		# resolves the same arm this loop just chose.
		try:
			at   = offset_bits(view, index)
			wide = size_bits(view, index)
		except Refused:
			return ERR_BOUNDS
		if view.at * 8 + at + wide > view.limit * 8:
			return ERR_BOUNDS

		# A struct-typed arm carries its own constraints and its own
		# validator is what knows them. A different arm is nothing to
		# check, which is why this asks only the one selected.
		if arm_type != NONE:
			inner = View(image, view.buffer, arm_type,
			             view.at + at // 8, view.limit)
			return _validate(image, inner, arm_type) or OK
		return OK
	return ERR_VERSION


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

	for index in image.markers:
		if index not in image.members(image.structs[struct_index]):
			continue
		local = _local(image, index)
		try:
			# Read big-endian whatever the marker turns out to say: the
			# marker is what decides byte order, so it cannot be read in
			# the order it is about. The generated C reads `be` here too.
			start = view.at + offset_bits(view, index) // 8
			width = size_bits(view, index) // 8
			if start + width > view.limit:
				raise Refused("the frame does not reach the marker")
			held = int.from_bytes(view.buffer[start:start + width], "big")
		except Refused:
			continue
		little = 1 if held == image.markers[index] else 0
		lines.append(f"{local} little={little}")

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
			inside_name = _local(image, inside)
			if inside_name.startswith(local + "_"):
				inside_name = inside_name[len(local) + 1:]
			try:
				lines.append(f"{inside_name} {read_scalar(view, inside)}")
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
		return bytes(view.buffer[first:last])

	# Copied for the reason `walk.byte_run` gives: a slice of a `bytearray`
	# is a `bytearray`, and this is handed out as a value.
	want = size_bits(view, index) // 8
	return bytes(view.buffer[first:min(first + want, view.limit)])


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
