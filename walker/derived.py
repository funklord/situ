"""Computing a derived checksum from what the image carries (26.579).

Every backend generates a `_compute` for a checksum whose schema names the
codec that computes it -- `checksum u8 crc[4] covers(summed) is crc32`,
decision 0053 -- and this walker could not, because an image said a member
was `auth=Covered` and nothing about what covers it. So `situ-edit` wrote a
PNG chunk with a stale CRC and told the caller *situ does not compute it
(14.1)*, which is true of an AEAD tag and false of a CRC situ generates
four implementations of.

**No polynomial is re-derived here.** `situc.codegen.kernel_math` is the one
derivation all four backends share, the packer runs it, and the image
carries the table and the four numbers. A second implementation of a
polynomial is a second answer to what a message's checksum is, and this
walker is meant to be an independent reader of situ's description rather
than of its arithmetic.

What IS written twice is the loop, and deliberately: three shapes, matching
the generated ones exactly --

	reflected                crc = T[(crc ^ b) & 0xFF] ^ (crc >> 8)
	left-aligned register    crc = T[crc ^ b]
	otherwise                crc = T[((crc >> (w-8)) ^ b) & 0xFF] ^ (crc << 8)

-- and what checks them is `test_a_polynomial_agrees_with_long_division`,
which divides bit by bit from the schema's own `poly` rather than from the
table these are given. That reaches all four polynomial kernels in the
corpus, including the two narrower than a byte that run in the
left-aligned register nothing else exercises.

This said the differential oracle compared them against C on every run.
It does not and never did -- nothing outside the editor and that test
calls this module. **A sentence asserting a check that does not exist is
worse than no sentence, because it is the reason nobody writes one.**
"""

from __future__ import annotations

from walker.image import (KERNEL_NONE, KERNEL_ONES_COMPLEMENT,
                          KERNEL_POLYNOMIAL, Image, Kernel, NONE)


class Uncomputable(Exception):
	"""situ does not compute this checksum, and says which it is.

	Raised rather than returned so a caller cannot read it as a value: the
	whole defect this module answers was a tool treating "cannot compute"
	as "wrote something".
	"""


def computes(image: Image, tag: int) -> bool:
	"""Whether the image says what computes this tag's checksum.

	False for an AEAD tag -- situ owns no AES-GCM and 14.1 puts that with
	the caller -- and for an `extern` codec, which is somebody's C
	function. Those are the 18 of 30 tag placements in this repository that
	name no kernel, and the honest note belongs to exactly them.
	"""
	held = image.tags.get(tag)
	if held is None or held.codec == NONE:
		return False
	kernel = image.kernels.get(held.codec)
	return kernel is not None and kernel.family != KERNEL_NONE


def compute(image: Image, tag: int, covered: bytes,
		hole: tuple[int, int] | None = None) -> int:
	"""The checksum of `covered`, by the kernel the image names.

	`hole` is where the tag's OWN bytes sit inside `covered`, as
	`(offset, length)`, for the checksum that covers the field it is
	written into -- every internet checksum does. Those bytes are read as
	the schema's `[self_as]` while the sum runs, which is what the
	generated `_holed` entry point takes as `hole_at`, `hole_len` and
	`fill`: the same three arguments, so the two answers can be compared
	rather than merely both existing.

	The hole is the CALLER's to supply because only it knows where the
	covered span began; what the schema says the bytes read as is the
	IMAGE's. Mismatching the two is refused rather than guessed at, in
	both directions -- a tag that needs a hole and was given none would
	sum its own stale bytes, and one given a hole its schema does not
	declare would sum bytes that are really there.
	"""
	held = image.tags.get(tag)
	if held is None or held.codec == NONE:
		raise Uncomputable("the image names no codec for this tag")

	kernel = image.kernels.get(held.codec)
	if kernel is None or kernel.family == KERNEL_NONE:
		raise Uncomputable("the image names no kernel for this tag's codec")

	# `prefix(...)`: bytes ahead of the message that the message does not
	# contain. TCP's and UDP's pseudo-header carries the IP addresses, and
	# a reader holding one datagram cannot know them -- so this declines,
	# where summing the single span it has would answer confidently and
	# wrongly (14.2a).
	if held.prefix:
		raise Uncomputable("its sum also runs over a prefix the message "
		                   "does not contain -- a pseudo-header, whose "
		                   "bytes come from a layer this schema does not "
		                   "describe (14.2a)")

	if held.fill == NONE and hole is not None:
		raise Uncomputable("its own bytes are inside what it covers and "
		                   "its schema does not say what they read as")
	if held.fill != NONE:
		if hole is None:
			raise Uncomputable("it covers its own bytes and the caller did "
			                   "not say where they sit in the span")
		covered = _filled(covered, hole, held.fill)

	if kernel.family == KERNEL_POLYNOMIAL:
		return _polynomial(image, kernel, covered)
	if kernel.family == KERNEL_ONES_COMPLEMENT:
		return _ones_complement(kernel, covered)
	raise Uncomputable(f"kernel family {kernel.family} is not one this "
	                   f"walker computes")


def _filled(covered: bytes, hole: tuple[int, int], fill: int) -> bytes:
	"""`covered` with the tag's own bytes read as `fill` (14.2).

	A copy, where generated code passes the hole as three arguments and
	never allocates one (invariant 4). The invariant is about the
	GENERATED reader, which may run with no allocator at all; this walker
	is a Python program holding the whole message in a `bytearray`
	already, and a copy here is clearer than threading a hole through two
	loops that would then each have to get it right.
	"""
	at, span = hole
	if at < 0 or span < 0 or at + span > len(covered):
		raise Uncomputable(f"its own bytes ({span} at {at}) do not sit "
		                   f"inside the {len(covered)} it covers")
	return covered[:at] + bytes([fill]) * span + covered[at + span:]


def _polynomial(image: Image, kernel: Kernel, covered: bytes) -> int:
	"""Table-driven, in whichever of the three directions the code runs."""
	if kernel.table == NONE or kernel.table >= len(image.kernel_tables):
		raise Uncomputable("the image carries no table for this kernel")

	table = image.kernel_tables[kernel.table]
	mask  = (1 << kernel.width) - 1 if kernel.shift == 0 else 0xFF
	crc   = kernel.start

	for byte in covered:
		if kernel.reflect:
			crc = (table[(crc ^ byte) & 0xFF] ^ (crc >> 8)) & mask
		elif kernel.shift:
			# A left-aligned register is a byte wide whatever the code's
			# width, so the index is already a byte and the table holds
			# bytes -- which is the whole point of aligning it.
			crc = table[crc ^ byte]
		else:
			crc = (table[((crc >> (kernel.width - 8)) ^ byte) & 0xFF]
			       ^ (crc << 8)) & mask

	held = crc >> kernel.shift if kernel.shift else crc
	return (held ^ kernel.xorout) & ((1 << kernel.width) - 1)


def _ones_complement(kernel: Kernel, covered: bytes) -> int:
	"""The internet checksum (RFC 1071), end-around carry and complement.

	An odd length pads with a zero byte on the right, which is the RFC's
	own rule and not this reader's: the sum is over 16-bit words and the
	last byte of an odd span is the high half of one.
	"""
	if kernel.width != 16:
		raise Uncomputable(f"a {kernel.width}-bit ones' complement sum is "
		                   f"not one this walker computes")

	total = 0
	for at in range(0, len(covered) - 1, 2):
		total += (covered[at] << 8) | covered[at + 1]
	if len(covered) % 2:
		total += covered[-1] << 8

	while total >> 16:
		total = (total & 0xFFFF) + (total >> 16)
	return (~total) & 0xFFFF
