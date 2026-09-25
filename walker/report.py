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

from collections.abc import Sequence

from walker import vm
from walker.image import NONE, Image, _string_at
from walker.walk import (BITS_PER_BYTE, Refused, TooDeep, Unplaceable,
                         Unsupplied, View,
                         acquire, digits_of,
                         parse_digits, parse_scaled_digits,
                         read_bytes, read_scalar,
                         _evaluate, _read_at, offset_bits, record_run_count,
                         scan, size_bits,
                         struct_extent, tlv_count, varint, while_count)

#: The probe kinds this walker renders. Named rather than counted so that a
#: kind quietly dropping out cannot look like agreement.
SUPPORTED = ("no-view", "needs-arguments", "scalar", "bytes", "element",
             "run_element", "arm_value", "sealed", "gated", "delimited",
             "varint", "while_count", "nested", "tag", "marker", "validate",
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
		# ...unless it is one value rather than a byte string. A sub-byte
		# checksum -- USB's five-bit CRC -- is read by every backend with
		# the bit load a plain sub-byte field uses, so the differential
		# compares the VALUE and this has to render one. Rendering
		# `present=` against C's `crc 30` is a difference in what was
		# asked, which is the failure this file's own docstring warns
		# about (26.371).
		if placement.is_tag and placement.size_bits % 8 == 0:
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
#: 8 is BCD, which this file has no question for. 16 is `scaled`
#: (0056): the digits may carry a point and an exponent, and the value
#: is a significand and a power of ten rather than an integer.
SCALED = 16

#: What `[trim]` removes where the image carries no set: HTTP's OWS and
#: SIP's LWS, and deliberately not `isspace`, which is locale dependent and
#: includes CR, LF, VT and FF -- three of which are framing in the protocols
#: this default is for.
#:
#: A default rather than the answer, and the comment below said why while
#: this was the answer: a schema states its set with `whitespace`, the image
#: carries it, and this is what an image written before that section means.
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
	# The MEMBER's set: `whitespace` is per file and an imported struct is
	# written against its own file's. `OWS` where the image carries no row,
	# which is a member the schema did not trim or an image written before
	# the section was keyed this way.
	removes = view.image.whitespace.get(index, OWS)
	head    = 0
	while head < len(data) and data[head] in removes:
		head += 1
	tail = len(data)
	while tail > head and data[tail - 1] in removes:
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
	content, _, _ = scan(view, index)
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
	        and image.placements[index].size_bits % 8 == 0
	        and _offset_computable(image, struct_index, index)]


def _while_runs(image: Image, struct_index: int) -> list[int]:
	"""Runs that end after the element failing a predicate (8.6.6)."""
	return [index for index in image.members(image.structs[struct_index])
	        if image.placements[index].repeat_code != NONE
	        and image.placements[index].type_struct != NONE]


def _record_runs(image: Image, struct_index: int) -> list[int]:
	"""Runs of records that END at a terminator: `T x[] until "D"` (8.6.3).

	The differ asks these `count=` and this walk answered nothing, so the
	key was present on one side only and the comparison skipped it. That is
	the quieter vacuous pass: not a check that examined nothing, but two
	sides talking past each other -- which `_local`'s own docstring names
	and which happened here anyway.
	"""
	return [index for index in image.members(image.structs[struct_index])
	        if index in image.delimiters
	        and image.placements[index].type_struct != NONE
	        and not image.placements[index].is_tag
	        and index not in image.regions]


def _tlv_runs(image: Image, struct_index: int) -> list[int]:
	"""`tlv` regions whose grammar this image describes (9.5).

	The differ asks these `count=` as well. The section existed and nothing
	loaded it, and until 26.343 there was nothing in it worth loading: the
	record said how to read an item's tag and nothing about how far its
	value reaches.
	"""
	return [index for index in image.members(image.structs[struct_index])
	        if index in image.tlvs and index in image.tlv_rules
	        and image.tlvs[index][0] != NONE]


def _indexed_runs(image: Image, struct_index: int) -> list[int]:
	"""`indexed` regions whose entry count this image states.

	The differ asks these `count=` too. The section that says how many
	entries the offset table holds was written by the packer as `none`
	until 26.341 and loaded by nobody, so there was no count to give.
	"""
	return [index for index in image.members(image.structs[struct_index])
	        if index in image.indexes
	        and image.indexes[index][1] != NONE]


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
	design (14.6), which is why this renders none.

	A byte run inside a gate is NOT that case, and this docstring said it
	was from 2026-08-07 to 2026-09-25: "spelled four ways that have not been
	compared -- so the differ asks about neither and this renders neither".
	They were compared on 2026-09-09 (26.320) and the differ asks about them
	as `inside_bytes`, so the second clause was false rather than stale and
	the first clause was this walker's own question wearing the differ's
	clothes. That question is still open: what this renders for a byte run
	behind a gate is nothing, and nothing has decided it should be.
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


def listing(image: Image, buffer: bytes, args: Sequence[int] = ()) -> str:
	"""What this schema says about this buffer, one struct at a time.

	`args` are the schema's `parameter` members (0050), positionally. One
	list for the whole listing, which is what a per-stream fact is (26.394)
	-- and a struct that takes a different number of them gets
	`needs-arguments` rather than a view, because the alternative is reading
	a layout the caller did not choose.
	"""
	lines: list[str] = []
	for struct_index, _ in enumerate(image.structs):
		name = image.struct_name(struct_index)
		lines.append(f"-- {name}")
		try:
			view = acquire(image, buffer, struct_index, args)
		except Unsupplied:
			# Its own line and not `no-view`, because the two send a reader
			# somewhere different: `no-view` is a frame too short for the
			# struct, and this is a caller who has not said which layout the
			# bytes are in. Reporting the second as the first is a verdict
			# on bytes nobody read, which is the shape 26.394 moved `situ
			# verify`'s own refusal for.
			lines.append("needs-arguments")
			continue
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

#: `image_severity`'s `refuse`, which is the only severity a verdict
#: reads: the other two say something about a message that conforms.
REFUSE = 0

#: The placement a message refusal names, which is none: a `when` is a
#: predicate over the whole struct. Kept out of the `image_check` space on
#: purpose -- a message is not a check, and numbering it as one would hand a
#: caller an id that resolves to whichever kind sits at that number.
BY_MESSAGE_INDEX = -2

#: `image_check`
MUST_EQ, MINIMUM, MAXIMUM, MUST_BE_ZERO, MUST_BE_ONE, ENUM_KNOWN = range(6)
FITS_FRAME, TERMINATED, ARM_SELECTED = 6, 7, 8
DIGITS_VALID, DIGITS_MINIMAL = 9, 10
NUL_TERMINATED, ENCODED_AS, ZERO_RUN = 11, 12, 13
PINNED_RUN = 14
#: `[encoding = from(f)]` (0058), and the mapping it reads. ENCODED_FROM sits
#: on the governed member and its value is the SOURCE's placement; ENCODING_ARM
#: sits on the source, one row per arm of the token set in declaration order,
#: and is data rather than a check -- a walk skips it where it reads the rest.
ENCODED_FROM, ENCODING_ARM = 15, 16
#: `pad_random(min, max)` (0045): the pad's length in BYTES against its
#: bounds. Span checks rather than value comparisons -- the length is the
#: run's own and a run has no value to read, which is the distinction a
#: pinned row falling into the value loop already cost once (26.350).
PAD_LENGTH_MIN, PAD_LENGTH_MAX = 17, 18

#: `situ_err_t` again: an unknown discriminant is a message this build
#: cannot read rather than one that breaks a rule.
ERR_VERSION = 3


def _span_bytes(image: Image, view: View, index: int) -> bytes:
	"""The bytes a member spans, as the byte-run checks read them.

	A delimited member's `size_bits` is content *plus* its delimiter, because
	that is where the next member starts. What the schema called text is the
	content, and that is what every backend passes to the check -- `_len`,
	not `_span`.

	Separate from the loop that uses it because `encoded_from` reads a
	SECOND member's bytes: the encoding is whatever the source names, so the
	check has to look where the governed member is not.
	"""
	start   = view.at + offset_bits(view, index) // BITS_PER_BYTE
	if index in image.delimiters:
		content = scan(view, index)[0]
	else:
		content = size_bits(view, index) // BITS_PER_BYTE
	data = bytes(view.buffer[start:start + content])
	if len(data) != content:
		raise Refused(f"placement {index}: {content} bytes wanted, "
		              f"{len(data)} in the frame")
	return data


def _encoded_ok(data: bytes, code: int) -> bool:
	"""Whether `data` is in the encoding `code` names.

	Decoded rather than re-implemented. Each runtime validator --
	`situ_utf8_valid`, `situ_utf16le_valid`, `situ_utf16be_valid` -- refuses
	exactly the set Python's strict decoder does: an overlong form or
	surrogate half for utf8, a lone surrogate or odd byte count for utf16
	(0044). Restating the state machine here would be a second chance to get
	it wrong. The codes match `pack.ENCODING_CODE`.
	"""
	if code == 0:
		return not any(one > 0x7F for one in data)
	codec = {1: "utf-8", 2: "utf-16-le", 3: "utf-16-be"}.get(code)
	if codec is None:
		return True
	try:
		data.decode(codec)
	except UnicodeDecodeError:
		return False
	return True


def _arm_encoding(image: Image, source: int, said: bytes) -> int | None:
	"""Which encoding the source member's bytes name, or None for no arm.

	Two tables meet here and neither was written for this. The pinned runs
	are the token set's arms, written for its own membership check; the
	ENCODING_ARM rows are the encoding each arm names, written in the same
	declaration order. The arm is found by position in the first and read by
	position in the second, so the mapping needs no third table and no name.

	A set the packer could not write both halves for disowns the struct
	rather than arriving here short, so a length mismatch is this walk
	misreading the image rather than the message being wrong.
	"""
	arms  = image.pinned_runs.get(source, [])
	codes = [value for check, value in image.constraints.get(source, ())
	         if check == ENCODING_ARM]
	if len(codes) != len(arms):
		raise Refused(f"placement {source}: {len(arms)} arm(s) pinned, "
		              f"{len(codes)} encoding(s) named")
	folded = image.placements[source].text_flags & CASE_INSENSITIVE
	for position, arm in enumerate(arms):
		if said == arm or (folded and said.lower() == arm.lower()):
			return codes[position]
	return None


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


def _validate(image: Image, view: View, struct_index: int,
		found: list[tuple[int, int]] | None = None) -> int | None:
	"""What `validate` returns, or None where this image cannot say.

	`found`, where a caller passes one, collects `(placement, check)` for
	the failure that decided the answer -- the identity 0051 asks for. It is
	recorded on the way out rather than reconstructed afterwards, because
	the walk already knows which check answered and rebuilding it would be a
	second implementation of the order this function's own docstring says
	matters. `check` is `NONE` where the failure is not a declared check but
	the frame running out under one.

	The whole answer or nothing. Every other probe can be rendered for the
	members the image describes and skipped for the rest, because each is a
	separate line -- `validate` is one line about the whole struct, so a
	partial one reports OK where the schema refuses. The packer sets a bit
	per struct saying every check is carried, and this declines otherwise.

	Order matters because the first failure is the answer: a member the
	frame does not reach is `BOUNDS` and a constraint that fails is
	`CONSTRAINT`, and which comes first decides which is returned.
	"""
	def fail(code: int, index: int = NONE, check: int = NONE) -> int:
		"""Record which check answered, then return its code."""
		if found is not None:
			found.append((index, check))
		return code

	if not image.structs[struct_index].validatable:
		return None

	# The frame has to hold the struct's own minimum before anything in it
	# is placed -- section 20.2's check, which `acquire` makes for the
	# OUTERMOST struct and which a nested view never goes through: a nested
	# `View` is constructed directly, so nothing asked.
	#
	# Both the C walker's `validate_deep` and every generated `check` test
	# it first, and skipping it here did not make this walk more permissive
	# -- it made it answer the WRONG REFUSAL. `cpio_entry` over 42 bytes is
	# BOUNDS in all five other readers; this descended into `cpio_header`,
	# found its `magic` wrong and said CONSTRAINT. A short frame's members
	# are not wrong, they are absent.
	#
	# `size_bits` is `NONE` for a variable struct, which is the C walker's
	# own condition: the image does not carry a minimum for one, so neither
	# reader checks it and the four backends -- which have `SIZE_MIN` from
	# the compiler -- can still refuse where these two cannot.
	shape = image.structs[struct_index]
	if shape.size_bits != NONE:
		need = (shape.size_bits + BITS_PER_BYTE - 1) // BITS_PER_BYTE
		if view.limit - view.at < need:
			return fail(ERR_BOUNDS)

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
				return fail(ERR_BOUNDS, index)

		# Every member is *placed*, not only the constrained ones: a struct
		# whose members after a coded region cannot be reached fails with
		# BOUNDS before any constraint is asked, and checking only the
		# constrained ones answered OK for `edges`' `coded_run` where every
		# backend answered 1.
		try:
			at   = offset_bits(view, index)
			wide = size_bits(view, index)
		except TooDeep:
			# The ceiling, which is a statement about this walker and not
			# about the message: past `[limit]` a message is well formed
			# and refused anyway (26.284), which is why the four backends
			# spell it `SITU_ERR_DEPTH` rather than a constraint error.
			# There is no such verdict here and there should not be, so
			# the answer is the one this function already has for "this
			# image cannot say" -- `cannot-say`, which is what the C
			# walker returns for the same bytes.
			#
			# Before the split this arrived as `Unplaceable` and hit the
			# `break` below, so a twelve-level chain under `[limit = 8]`
			# validated **clean** off a walk that had stopped. C said
			# `cannot-say` throughout, and the disagreement was invisible
			# because the validate differential's corpus has no recursion
			# in it.
			return None
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
			return fail(ERR_BOUNDS, index)

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
		#
		# NOT a located member, whatever the pair says. `at off` is a
		# dynamic offset and is usually fixed-size, so it satisfies both
		# flags -- and "a `located` member reaches past the frame by
		# construction (9.8)". Its accessor asks the message on every
		# call, which is where that question belongs; asking it here
		# refused frames all four backends accept, because none of them
		# emits this check for one. Three other artifacts had already made
		# exactly this assumption (26.446).
		placed = image.placements[index]
		if placed.fixed and not placed.offset_known \
				and placed.located_code == NONE:
			if view.at * 8 + at + wide > view.limit * 8:
				return fail(ERR_BOUNDS, index)

		# An `indexed` region's offset table is `count` entries of
		# `entry_bits` and has to fit the frame, which is the one check
		# every backend makes about such a table:
		#
		#     if (situ_remaining_u32(view.limit, 8u) < count * 2u)
		#             return SITU_ERR_BOUNDS;
		#
		# This walk could not ask it -- the image wrote an INDEXES section
		# and nothing loaded it -- so the packer deferred `validate` on any
		# struct holding one. A sqlite page declaring 2644 cells in a
		# 46-byte frame was refused by all four backends and clean here,
		# and had been since indexed regions arrived; no draw had reached
		# it. The section is loaded now and carries the count's bytecode,
		# which the packer had been writing as `none`.
		#
		# `NONE` in either field is still a deferral rather than a pass: a
		# table whose entry width or whose size nothing states is one
		# nobody can bounds-check.
		table = image.indexes.get(index)
		if table is not None:
			entry_bits, count_code, _measured_from = table
			if entry_bits == NONE or count_code == NONE:
				return None
			# `from_byte` is an offset WITHIN the view -- `_evaluate`
			# computes `remaining` as `limit - at - from_byte` -- while
			# `room` is measured between two absolute bytes. Passing the
			# absolute one for both subtracts `view.at` twice, which is
			# invisible for a top-level struct and wrong for every nested
			# one.
			here  = at // BITS_PER_BYTE
			start = view.at + here
			room  = view.limit - start if view.limit > start else 0
			count = _evaluate(view, count_code, here)
			if count < 0 or room < count * (entry_bits // BITS_PER_BYTE):
				return fail(ERR_BOUNDS, index)

		# A run whose length the message declares has to fit the frame.
		# The accessor clamps; `validate` is where a message that does not
		# fit is called malformed, and it answers BOUNDS rather than
		# CONSTRAINT. udp's `payload[length - 8]` is the shape. Which runs
		# carry this is the packer's to say -- `remaining` and a `while`
		# run do not.
		if any(check == FITS_FRAME
		       for check, _ in image.constraints.get(index, ())):
			if view.at * 8 + at + wide > view.limit * 8:
				return fail(ERR_BOUNDS, index, FITS_FRAME)

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
					return fail(ERR_CONSTRAINT, index, TERMINATED)
			except Refused:
				return fail(ERR_BOUNDS, index, TERMINATED)

		for check, against in held:
			if check != ARM_SELECTED:
				continue
			verdict = _arm_selects(image, view, index, bool(against))
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
				return fail(ERR_CONSTRAINT, index, DIGITS_MINIMAL)
			# A leading `-` belongs to the spelling of a signed number and
			# is skipped before the question is asked, exactly as
			# `situ_digits_minimal` skips it: `-042` is non-minimal for the
			# same reason `042` is.
			if digits[:1] == b"-":
				digits = digits[1:]
			if not digits:
				return fail(ERR_CONSTRAINT, index, DIGITS_MINIMAL)
			if len(digits) > 1 and digits[0:1] == b"0":
				return fail(ERR_CONSTRAINT, index, DIGITS_MINIMAL)
			if against > 10 and any(0x41 <= one <= 0x46 for one in digits):
				return fail(ERR_CONSTRAINT, index, DIGITS_MINIMAL)

		for check, against in held:
			if check != DIGITS_VALID:
				continue
			# CONSTRAINT and not BOUNDS, even where the frame is what ran
			# out: C reaches this through the getter, whose `_ptr` and
			# `_len` clamp, so a text number nobody can read is a field
			# that is not a number rather than a field that is not there.
			# A scaled member takes the other parse. Without this the walk
			# ran the INTEGER one over `12.5` and refused a frame all four
			# backends accept -- and the differential did not catch it,
			# because random bytes almost never spell a valid decimal and
			# the two agreed on refusing everything else (0056).
			try:
				if placement.text_flags & SCALED:
					value, _ = parse_scaled_digits(view, index)
				else:
					value = parse_digits(view, index)
			except Refused:
				return fail(ERR_CONSTRAINT, index, DIGITS_VALID)
			# The row carries the ceiling. A signed text number's floor is
			# derived from it rather than packed beside it: in two's
			# complement the two are one number, and a second field would
			# be a second thing to be wrong.
			floor = -(against + 1) if placement.signed else 0
			if value > against or value < floor:
				return fail(ERR_CONSTRAINT, index, DIGITS_VALID)

		# The byte-run checks: a terminator inside the field, an
		# encoding the bytes are actually in, and a reserved run that is
		# all zero. Each reads the member's whole span rather than a
		# value, which is what separates them from the comparisons below
		# -- and why the capacity is the member's own size and not
		# something the constraint has to carry.
		# The pad's length against its bounds, before the byte-run checks:
		# a pad of the wrong length is wrong whatever its bytes are.
		#
		# Measured the way the four backends measure it -- what is left of
		# the frame from where the pad starts -- rather than from
		# `size_bits`. A `[remaining]` run carries no size program, so
		# `size_bits` answers the static MINIMUM, which for this member is
		# one of the bounds being checked: comparing a bound against itself
		# accepted every pad of every length.
		if any(pair[0] in (PAD_LENGTH_MIN, PAD_LENGTH_MAX) for pair in held):
			start = view.at + at // BITS_PER_BYTE
			length = max(0, view.limit - start)
			for check, against in held:
				if check == PAD_LENGTH_MIN and length < against:
					return fail(ERR_CONSTRAINT, index, check)
				if check == PAD_LENGTH_MAX and length > against:
					return fail(ERR_CONSTRAINT, index, check)

		span = [pair for pair in held
		        if pair[0] in (NUL_TERMINATED, ENCODED_AS, ZERO_RUN,
		                       PINNED_RUN, ENCODED_FROM)]
		if span:
			try:
				data = _span_bytes(image, view, index)
			except Refused:
				return fail(ERR_BOUNDS, index)
			for check, against in span:
				if check == NUL_TERMINATED and 0 not in data:
					return fail(ERR_CONSTRAINT, index, NUL_TERMINATED)
				if check == ZERO_RUN and any(data):
					return fail(ERR_CONSTRAINT, index, ZERO_RUN)
				# `against` is the row rather than the bytes: a byte run
				# packed into an `i64` would have an endianness the literal
				# does not (0052). The length was checked against the run at
				# compile time, so a mismatch here is the message's.
				if check == PINNED_RUN:
					arms = image.pinned_runs.get(index, [])
					# `against` is how many the packer wrote. A different
					# number here means this walk misread the section, which
					# is not the same as the message being wrong -- so it is
					# an error about the image rather than a refusal.
					if len(arms) != against:
						raise Refused(
							f"placement {index}: {against} pinned run(s) "
							f"declared, {len(arms)} found")
					# Folded where the member is case-insensitive, which
					# for a token set is a property of the SET rather than
					# of the member's attributes (0055). Without it this
					# refused `helo` where all four backends take it, and
					# a fifth description disagreeing wrongly is exactly
					# what the differential exists to surface.
					if placement.text_flags & CASE_INSENSITIVE:
						if data.lower() not in [arm.lower() for arm in arms]:
							return fail(ERR_CONSTRAINT, index, PINNED_RUN)
						continue
					if data not in arms:
						return fail(ERR_CONSTRAINT, index, PINNED_RUN)
					continue
				if check == ENCODED_FROM:
					# `[encoding = from(f)]` (0058): the encoding is not the
					# schema's to state, so `against` is where the message
					# states it. The source is earlier in placement order and
					# carries the set's own membership check, so by the time
					# this runs an unmatched source has already been refused
					# -- which is why an arm that matches nothing here is a
					# silence rather than a verdict, exactly as C's `default:`
					# arm is.
					try:
						said = _span_bytes(image, view, against)
					except Refused:
						return fail(ERR_BOUNDS, against)
					code = _arm_encoding(image, against, said)
					if code is None:
						continue
					if not _encoded_ok(data, code):
						return fail(ERR_CONSTRAINT, index, ENCODED_FROM)
					continue
				if check != ENCODED_AS:
					continue
				if not _encoded_ok(data, against):
					return fail(ERR_CONSTRAINT, index, ENCODED_AS)


		value_checks = [pair for pair in held
		                if pair[0] not in (FITS_FRAME, TERMINATED,
		                                   ARM_SELECTED, DIGITS_VALID,
		                                   DIGITS_MINIMAL, NUL_TERMINATED,
		                                   ENCODED_AS, ZERO_RUN,
		                                   PINNED_RUN, ENCODED_FROM,
		                                   ENCODING_ARM, PAD_LENGTH_MIN,
		                                   PAD_LENGTH_MAX)]
		if not value_checks:
			continue
		try:
			value = read_scalar(view, index)
		except Refused:
			return fail(ERR_BOUNDS, index)
		for check, against in value_checks:
			if check == MUST_EQ and value != against:
				return fail(ERR_CONSTRAINT, index, check)
			if check == MINIMUM and value < against:
				return fail(ERR_CONSTRAINT, index, check)
			if check == MAXIMUM and value > against:
				return fail(ERR_CONSTRAINT, index, check)
			if check == MUST_BE_ZERO and value != 0:
				return fail(ERR_CONSTRAINT, index, check)
			if check == MUST_BE_ONE and value != against:
				return fail(ERR_CONSTRAINT, index, check)
			if check == ENUM_KNOWN \
					and value not in image.enum_values.get(against, set()):
				return fail(ERR_CONSTRAINT, index, check)
			if check == FITS_FRAME:
				continue		# handled above, before the value is read

	# Last, after every member check, and the four backends put theirs in
	# the same place. A `when` is a predicate over the whole struct, so a
	# member that is wrong is the more specific answer and order decides
	# which code comes back -- five descriptions agreeing about the verdict
	# and not about the order would disagree on any frame that trips both.
	#
	# `refuse` only. `warn` and `note` say something about a message that
	# conforms (0051), and the sibling reports all three.
	for refusal in image.messages:
		if refusal.owner != struct_index or refusal.severity != REFUSE:
			continue
		try:
			if _evaluate(view, refusal.code):
				return fail(ERR_CONSTRAINT, BY_MESSAGE_INDEX)
		except (Refused, vm.VmError):
			# The packer clears `validatable` for a struct whose `refuse`
			# it could not encode, so reaching here means a program that
			# was encodable and would not run on these bytes. Saying OK
			# would be the partial verdict this function's own docstring
			# refuses.
			return None
	return OK


#: What `failed_check` answers with when a struct validates, and when the
#: image cannot say. Distinguished, because "nothing failed" and "this
#: walker cannot tell you" are the two states invariant 154 is about.
CLEAN, CANNOT_SAY = "clean", "cannot-say"

#: What `failed_check` answers when a `when` refused rather than a check
#: (0051). Neither of the two above: the walk made the refusal and there is
#: no member behind it.
BY_MESSAGE = "by-message"


#: What each severity means to a verdict. `refuse` makes a message
#: malformed; the other two say something about one that is well formed.
SEVERITIES = ("refuse", "warn", "note")



def messages(image: Image, view: View,
		struct_index: int) -> list[tuple[str, str, str]]:
	"""Every `when` this struct states whose predicate holds (0051).

	`(severity, name, text)` per message, in declaration order. The NAME is
	what a consumer keys on and the text is a default rendering it may
	replace -- both are returned because a caller with no catalogue needs
	the second and one with a catalogue needs the first.

	All three severities, and no short circuit. `validate` stops at the
	first failure "because the first failure is the answer", which is right
	for a verdict and wrong for collecting messages: a caller asking what a
	message says wants everything it says. That is why 0051 puts this beside
	`validate` rather than inside it.

	A predicate that cannot be evaluated is dropped rather than reported.
	The image says what it can answer -- `situc pack` records the rest as
	unencodable -- and a message inferred from a program that did not run
	would be the compiler speaking with more authority than it has.
	"""
	found: list[tuple[str, str, str]] = []
	for held in image.messages:
		if held.owner != struct_index:
			continue
		try:
			if not _evaluate(view, held.code):
				continue
		except (Refused, vm.VmError):
			continue
		found.append((SEVERITIES[held.severity]
		              if held.severity < len(SEVERITIES) else "note",
		              _string_at(image.strings, held.name),
		              _string_at(image.strings, held.text)))
	return found


def failed_check(image: Image, view: View,
		struct_index: int) -> tuple[str, str] | str:
	"""Which check refused this struct, as `(member, check)`.

	`CLEAN` where it validates and `CANNOT_SAY` where the image was packed
	without every check this struct states -- the same two answers
	`_validate` already separates, kept apart here for the reason it keeps
	them apart there.

	**The verdict is unchanged and this is a second question about it.**
	`validate` returns a code and short-circuits, "because the first failure
	is the answer"; this names that same first failure. Nothing re-runs: the
	walk records the identity on its way out, so the two cannot disagree
	about which check answered (0051, 26.231).
	"""
	found: list[tuple[int, int]] = []
	verdict = _validate(image, view, struct_index, found)
	if verdict is None:
		return CANNOT_SAY
	if verdict == OK:
		return CLEAN
	if not found:
		return CANNOT_SAY
	index, check = found[-1]
	if index == BY_MESSAGE_INDEX:
		# A third answer, and not a shade of the other two. `CANNOT_SAY`
		# means the image was packed without every check this struct
		# states; this walk HAS the refusal and ran it, and there is simply
		# no member to name -- `messages` is what names one of these.
		return BY_MESSAGE
	member = _local(image, index) if index != NONE else "?"
	return member, CHECK_NAMES.get(check, "bounds")


#: `image_check`, by name, for a reader. The numbers are the packer's and
#: the names are what a consumer keys on -- 0051's split between an identity
#: that is the contract and a rendering that is not.
CHECK_NAMES = {
	MUST_EQ: "must_eq", MINIMUM: "min", MAXIMUM: "max",
	MUST_BE_ZERO: "must_be_zero", MUST_BE_ONE: "must_be_one",
	ENUM_KNOWN: "enum_known", FITS_FRAME: "fits_frame",
	TERMINATED: "terminated", NUL_TERMINATED: "nul_terminated",
	ENCODED_AS: "encoding", ZERO_RUN: "zero_run", PINNED_RUN: "must_eq",
	DIGITS_VALID: "digits", DIGITS_MINIMAL: "digits_minimal",
	ARM_SELECTED: "arm_selected",
	# `[encoding = from(f)]` (0058) and `pad_random(min, max)` (0045). Named
	# late: each was added as a kind without a name here, so a refusal
	# reported "bounds" -- the fallback, which is what an UNNAMED kind and a
	# refusal with no identity both render as. The completeness test below
	# is what stops a third one doing it.
	ENCODED_FROM: "encoding", ENCODING_ARM: "encoding",
	PAD_LENGTH_MIN: "pad_min", PAD_LENGTH_MAX: "pad_max",
}


#: Every `image_check` kind this module knows, so a kind added without a name
#: is a failure here rather than a refusal that renders as `bounds`.
ALL_CHECKS = frozenset({
	MUST_EQ, MINIMUM, MAXIMUM, MUST_BE_ZERO, MUST_BE_ONE, ENUM_KNOWN,
	FITS_FRAME, TERMINATED, ARM_SELECTED, DIGITS_VALID, DIGITS_MINIMAL,
	NUL_TERMINATED, ENCODED_AS, ZERO_RUN, PINNED_RUN, ENCODED_FROM,
	ENCODING_ARM, PAD_LENGTH_MIN, PAD_LENGTH_MAX,
})


def _arm_selects(image: Image, view: View, index: int,
		permissive: bool = False) -> int:
	"""Whether the discriminant names an arm, and whether that arm validates.

	`default: error` is the refusal the first half asks about: a value naming
	no arm is a message this build cannot read, which is `VERSION` and not
	`CONSTRAINT`. The distinction is the schema's -- 14.5 makes refusing an
	unknown discriminant the default rather than a choice.

	`permissive` says the schema wrote `default: <member>`, so no value can
	be wrong. **That is a statement about the first half only.** The arm the
	discriminant selects still carries its own constraints, and the packer
	used to omit this check entirely for that shape -- dropping the second
	job with the first. json is that shape: its `yes` arm carries
	`[must_eq = "rue"]` and the walk accepted `txyz`, silently, for as long
	as the file has existed. Invisible while the literal arms were rare in a
	random draw, and constant the moment `default:` became the arm almost
	every draw takes.
	"""
	selects, arms = image.arms.get(index, (NONE, []))
	if selects == NONE:
		return OK
	try:
		value = read_scalar(view, selects)
	except Refused:
		return ERR_BOUNDS
	fallback = next((chosen for case, chosen, flags in arms if flags & 1),
	                None)

	for case, chosen, flags in arms:
		if flags & 1:
			continue		# the default arm names no case
		if case != value:
			continue
		# AND THE MESSAGE'S OWN VERSION HAS TO REACH IT (26.424). An arm
		# carrying `[since]` is absent from a message older than that, in
		# exactly the sense an unselected arm is absent -- so it is nothing
		# to check rather than something that failed, which is what the
		# member loop above says in its own words: a field that is not
		# there is not a field that is wrong.
		#
		# The four backends fold this into the discriminant test itself, so
		# their accessor answers VERSION for both reasons at once. This
		# walk asks separately because the discriminant has already been
		# matched by the time the arm is known.
		since = image.placements[chosen].since if chosen != NONE else 0
		if since:
			carries = image.versions.get(view.struct)
			if carries is None:
				return OK	# the packer should not have said yes
			try:
				if read_scalar(view, carries) < since:
					return OK
			except Refused:
				return ERR_BOUNDS

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
		return _arm_constraints(image, view, chosen)

	# No `case` matched. Where the schema wrote `default: <member>` that is
	# not a refusal, and the member it selects is the one that has to
	# validate -- the same descent as above, through the arm this value
	# actually reaches.
	if permissive and fallback is not None:
		return _arm_validates(image, view, index, fallback)

	return ERR_VERSION


def _arm_constraints(image: Image, view: View, chosen: int) -> int:
	"""The selected SCALAR arm's own constraints (26.418).

	Reached only from `_arm_selects`, and only for the arm the discriminant
	actually chose. That gate is the whole of why reading the arm's bytes
	here is safe: every arm of a variant is placed at the same offset, so
	the readers answer for an unselected arm exactly as readily as for a
	selected one -- and answer nonsense. Measured on `signed_kind`, whose
	arms are a two-byte `marker` and a one-byte `flag`: with `kind = 2` the
	marker's span reads the flag and the trailer as `b'\\x05\\t'`, and with
	`kind = 1` the flag reads `marker`'s first byte as 66. Neither is a
	value the message states, and checking either would refuse frames all
	four backends accept.

	**What the four backends check on an arm, and no more.** A pinned span,
	a run's span checks (26.425), a delimited arm's delimiter and encoding
	(26.423), the value comparisons, and an enum's membership. An arm shape outside those gets no
	check in any backend either; 26.410 records that rather than this
	half-answering it, because a fifth description refusing what the other
	four accept is the disagreement the differential exists to find.

	The examples that sentence used to name have all moved, which is why it
	no longer names any: a run of wide values and a delimited arm are
	checked (26.422, 26.423), an arm behind `[since]` is gated (26.424), and
	a varint arm is refused outright, no backend having ever emitted the
	length accessor all four called (26.433).

	The list grows from the backend side, and this sentence has twice been
	the thing that was stale: it said "the two families ... and no more"
	while the backends had learned a third. So read it as a claim about what
	the emitters do, and check the emitters rather than the sentence.
	"""
	held = image.constraints.get(chosen)
	if not held:
		return OK

	placement = image.placements[chosen]

	for check, against in held:
		if check != PINNED_RUN:
			continue
		try:
			data = _span_bytes(image, view, chosen)
		except Refused:
			return ERR_BOUNDS
		arms = image.pinned_runs.get(chosen, [])
		# `against` is how many the packer wrote, and a different number
		# here means this walk misread the section rather than that the
		# message is wrong -- an error about the image, as the member path
		# spells it.
		if len(arms) != against:
			raise Refused(
				f"placement {chosen}: {against} pinned run(s) declared, "
				f"{len(arms)} found")
		# Case folding belongs to the SET rather than to the member (0055),
		# so an arm of that set folds for the same reason a member does.
		if placement.text_flags & CASE_INSENSITIVE:
			if data.lower() not in [one.lower() for one in arms]:
				return ERR_CONSTRAINT
		elif data not in arms:
			return ERR_CONSTRAINT

	# The member path's list, kept whole rather than trimmed to what the
	# packer writes today -- which for an arm is `must_eq`, `min` and `max`
	# and nothing else, the three C's own `arm_constraints` switch accepts.
	# The other three are therefore unreachable here and are left in so that
	# this stays one copy of the member path's decision rather than a second
	# one that has to be kept in step with it.
	#
	# ENUM_KNOWN was the interesting ABSENCE and is now simply read. This
	# comment used to say no schema with an enum-typed scalar arm could
	# exist, one not compiling in C++ at all (26.411), so the corpus could
	# not carry the case and the differential could not pose it. **All three
	# clauses have since stopped being true**: 26.420 made the construct
	# buildable, 26.421 taught the image to carry the row, and
	# `edges.typed_kind` is the case. Measured 2026-09-19 -- four placements
	# in `edges` carry an ENUM_KNOWN row, and a minimal enum arm compiles in
	# all four backends with no errors.
	#
	# The C walk's own comment had the corrected story and this one did not,
	# which is the two-copies failure: `situ_walk.c` says exactly what
	# 26.420 and 26.421 changed, a few lines above the same dispatch.
	# A DELIMITED arm's delimiter is THERE (26.423). The member path asks
	# this with the same scan a few hundred lines above; an arm had no row
	# to read and no reader for it, so a frame whose arm ran to the end of
	# the buffer was reported by its FOLLOWING member's bounds rather than
	# by this one's missing delimiter -- `walker: 1  C: 2`.
	#
	# Before the span checks below, and before the value ones, because that
	# is the order C emits: the terminator decides whether there is a
	# content span to ask anything else about.
	if any(check == TERMINATED for check, _ in held):
		try:
			if not scan(view, chosen)[1]:
				return ERR_CONSTRAINT
		except Refused:
			return ERR_BOUNDS

	# A RUN arm's span checks -- a terminator, a declared encoding
	# (26.425). Read over the ARM's own span, which is safe here for the
	# reason the whole function is: the discriminant selected this arm.
	span = [pair for pair in held if pair[0] in (NUL_TERMINATED, ENCODED_AS)]
	if span:
		try:
			data = _span_bytes(image, view, chosen)
		except Refused:
			return ERR_BOUNDS
		for check, against in span:
			if check == NUL_TERMINATED and 0 not in data:
				return ERR_CONSTRAINT
			if check == ENCODED_AS and not _encoded_ok(data, against):
				return ERR_CONSTRAINT

	values = [pair for pair in held
	          if pair[0] in (MUST_EQ, MINIMUM, MAXIMUM, MUST_BE_ZERO,
	                         MUST_BE_ONE, ENUM_KNOWN)]
	if not values:
		return OK
	try:
		value = read_scalar(view, chosen)
	except Refused:
		return ERR_BOUNDS
	for check, against in values:
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
	return OK


def _arm_validates(image: Image, view: View, index: int, chosen: int) -> int:
	"""The selected arm's own `validate`, through its own type.

	Split out because the matched and the default paths reach it the same
	way and a second copy would be a second thing to be wrong -- the rule
	being the one this file states above it: an arm whose type cannot be
	measured from its own bytes has no sub-view, so nothing asks whether it
	fits either.
	"""
	if chosen == NONE:
		return OK

	arm_type = image.placements[chosen].type_struct
	if arm_type == NONE or not image.structs[arm_type].measurable:
		return OK

	# Measured through the variant *member*, not the arm: an arm is not in
	# the struct's member chain, so asking `offset_bits` for one refuses.
	try:
		at   = offset_bits(view, index)
		wide = size_bits(view, index)
	except Refused:
		return ERR_BOUNDS
	if view.at * 8 + at + wide > view.limit * 8:
		return ERR_BOUNDS

	inner = View(image, view.buffer, arm_type, view.at + at // 8, view.limit)
	return _validate(image, inner, arm_type) or OK


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
			# The LENGTH and the first byte as well as the presence, which
			# is where the bytes are rather than whether they verify. A tag
			# read at the wrong offset answers `present=1` in every reader,
			# so presence alone could not separate five descriptions that
			# disagreed about which bytes the tag IS -- and a tag's location
			# is what 26.448 got wrong one accessor away (26.450).
			#
			# `bytes=` and NOT `len=`: the dissector comparison harvests
			# `len=` from any line into `<name>#len` and compares it with
			# a Wireshark row's length, and its own docstring says why
			# that is the wrong question here -- "`present=` is a tag's
			# presence and not its bytes". Spelling this `len=` forced a
			# comparison that test deliberately does not make, and two
			# tags after a sealed region failed it: the walker calls them
			# unplaceable and the dissector shows a row.
			span = read_bytes(view, index)
			lines.append(f"{local} present=1 bytes={len(span)} "
			             f"first={span[0] if span else -1}")
		except Refused:
			lines.append(f"{local} present=0 bytes=0 first=-1")

	for index in _while_runs(image, struct_index):
		local = _local(image, index)
		try:
			lines.append(f"{local} count={while_count(view, index)}")
		except Refused:
			continue

	for index in _record_runs(image, struct_index):
		local = _local(image, index)
		try:
			lines.append(f"{local} count={record_run_count(view, index)}")
		except (Refused, Unplaceable):
			continue

	for index in _tlv_runs(image, struct_index):
		local = _local(image, index)
		try:
			lines.append(f"{local} count={tlv_count(view, index)}")
		except (Refused, Unplaceable):
			continue

	for index in _indexed_runs(image, struct_index):
		local = _local(image, index)
		try:
			# `from_byte` is an offset within the view, which is what
			# `_evaluate` measures `remaining` from -- the same pairing
			# `_validate` makes for this section's bounds check.
			here = offset_bits(view, index) // BITS_PER_BYTE
		except (Refused, Unplaceable):
			continue
		try:
			count = _evaluate(view, image.indexes[index][1], here)
		except Refused:
			continue
		lines.append(f"{local} count={count}")

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
			content, terminated, _took = scan(view, index)
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
