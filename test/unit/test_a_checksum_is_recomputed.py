"""situ-edit recomputes the checksum its schema names (26.579).

A file editor that rewrites a covered byte and leaves a stale CRC behind
has produced a corrupt file and said so in a note nobody acts on. The tool
told the caller *situ does not compute it (14.1)*, which is true of an AEAD
tag and false of a CRC every backend generates four implementations of --
the walker's image carried `auth=Covered` and nothing about what covers
it, so there was nothing to compute from.

Four facts had to reach the image, and the three that were missing are the
three this module pins:

	the kernel        family, width, init, xorout, table, shift, reflect
	the coverage      the first and last MEMBER inside the covered regions
	`[self_as]`       what the tag's own bytes read as while it runs
	`prefix(...)`     bytes the sum covers that the message does not hold

The last two decide the four internet checksums here: ICMP and IPv4 cover
the field they are written into and are computable with a hole, TCP and
UDP also cover a pseudo-header whose addresses belong to a layer the
schema does not describe -- and a reader holding one datagram must DECLINE
rather than sum the single span it has and answer confidently.

WITNESSES, because agreement is only evidence where a case exists that
would disagree and it has been run:

	PNG   zlib.crc32, which is nothing of situ's
	ICMP  RFC 1071's own property -- the sum INCLUDING the checksum
	      field is 0xFFFF -- computed by a different loop from the one
	      under test

Both separated a wrong answer from a right one while this was written. The
coverage span first took the highest-numbered covered member, and all six
of ICMP's variant arms sit after `rest` in the placement table and before
it in the message: the span came out 8 bytes where RFC 792 sums 21, the
checksum `e5ca` where C says `3b5a`, and the property check failed.
"""

from __future__ import annotations

import sys
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "test" / "unit"))

import pytest						# noqa: E402
from situc import pack as packer			# noqa: E402
from situc import parser				# noqa: E402
from situc.diagnostics import Source			# noqa: E402
from situc.layout import solve				# noqa: E402
from situc.resolve import resolve			# noqa: E402
from walker import derived				# noqa: E402
from walker.image import (BIG, KERNEL_POLYNOMIAL, LITTLE, NONE,
                          Image, load)			# noqa: E402
from situc import ast					# noqa: E402

#: A PNG IHDR chunk: length, kind, thirteen bytes, CRC. The CRC is the one
#: zlib computes over `kind + data`, which is PNG's rule and not situ's.
IHDR = bytes.fromhex("0000000d4948445200000010000000100806000000")
IHDR += zlib.crc32(IHDR[4:]).to_bytes(4, "big")

#: An ICMP echo request with a payload, so the coverage runs past the eight
#: header bytes -- which is the whole point of `rest` in that schema.
ECHO = bytes.fromhex("08003b5a12340001") + b"situ-abcdefgh"


def _image(text: str, name: str = "t.situ") -> Image:
	parsed   = parser.parse(Source(name, text))
	resolved = resolve(parsed, solve(parsed))
	blob, _  = packer.pack(parsed, resolved, metadata=True)
	return load(blob)


def _of(path: Path) -> Image:
	return _image(path.read_text(encoding="utf-8"), str(path))


def _tag(image: Image, name: str) -> int:
	return next(i for i, _ in enumerate(image.placements)
	            if i in image.tags and image.placement_names[i] == name)


def _sum_including(message: bytes) -> int:
	"""RFC 1071's property, by a loop that is not the one under test.

	Whole-word `int.from_bytes` over a padded slice rather than two byte
	indices shifted together, and no complement at the end: what this
	returns for a correct message is 0xFFFF, where the function under test
	returns the complement of the sum over a HOLED span. A wrong init, a
	dropped end-around carry or a missing complement each break it.
	"""
	total = sum(int.from_bytes(message[i:i + 2].ljust(2, b"\x00"), "big")
	            for i in range(0, len(message), 2))
	while total >> 16:
		total = (total & 0xFFFF) + (total >> 16)
	return total


def test_a_png_crc_is_what_zlib_says() -> None:
	"""The polynomial family, against an implementation situ does not own."""
	image = _of(ROOT / "example" / "png" / "png.situ")
	tag   = _tag(image, "chunk.crc")

	assert derived.computes(image, tag), "the image names no kernel for a CRC32"
	value = derived.compute(image, tag, IHDR[4:-4])
	assert value == zlib.crc32(IHDR[4:-4]) & 0xFFFFFFFF, (
		f"situ computed {value:#010x} where zlib says "
		f"{zlib.crc32(IHDR[4:-4]) & 0xFFFFFFFF:#010x}")


def test_a_png_crc_needs_no_hole() -> None:
	"""PNG's CRC follows the bytes it covers, so there is nothing to fill.

	Pinned in both directions. The image must say `NONE`, and a caller
	offering a hole anyway must be refused rather than quietly obeyed:
	filling bytes that are really there would sum a message nobody sent.
	"""
	image = _of(ROOT / "example" / "png" / "png.situ")
	tag   = _tag(image, "chunk.crc")

	assert image.tags[tag].fill == NONE
	assert image.tags[tag].endian == BIG, "PNG stores its CRC big-endian"
	with pytest.raises(derived.Uncomputable):
		derived.compute(image, tag, IHDR[4:-4], (0, 4))


def test_an_icmp_checksum_covers_its_own_bytes() -> None:
	"""The ones'-complement family, with the hole `[self_as = 0]` asks for.

	The span is the whole message -- 21 bytes here, not the 8 of the fixed
	header -- and the two checksum bytes inside it read as zero while the
	sum runs.
	"""
	image = _of(ROOT / "example" / "icmp" / "icmp.situ")
	tag   = _tag(image, "icmp_message.checksum")

	assert image.tags[tag].fill == 0, "`[self_as = 0]` did not reach the image"
	assert not image.tags[tag].prefix, "ICMP's sum covers only the message"

	value = derived.compute(image, tag, ECHO, (2, 2))
	assert value == 0x3b5a, f"computed {value:#06x}"

	stored = ECHO[:2] + value.to_bytes(2, "big") + ECHO[4:]
	assert _sum_including(stored) == 0xFFFF, (
		"RFC 1071's own property fails: the sum of the message including "
		"its checksum is not 0xFFFF")


def test_a_checksum_over_its_own_bytes_needs_the_hole() -> None:
	"""And is refused without it, rather than summing stale bytes.

	The direction that matters: the value is already in the message, so a
	reader that forgot the hole gets a plausible number from a sum over
	the previous checksum. Nothing downstream could tell it from a right
	one, which is why this is a refusal and not a default.
	"""
	image = _of(ROOT / "example" / "icmp" / "icmp.situ")
	tag   = _tag(image, "icmp_message.checksum")

	with pytest.raises(derived.Uncomputable):
		derived.compute(image, tag, ECHO)

	# And the stale sum this avoids really is plausible rather than
	# obviously broken -- it is a 16-bit number like any other.
	holed = derived.compute(image, tag, ECHO, (2, 2))
	stale = derived._ones_complement(image.kernels[image.tags[tag].codec],
	                                 ECHO)
	assert stale != holed, "the hole made no difference to this message"


@pytest.mark.parametrize("schema,tag", [("udp", "udp_header.checksum"),
                                        ("tcp", "tcp_header.checksum")])
def test_a_pseudo_header_is_declined(schema: str, tag: str) -> None:
	"""TCP's and UDP's sums reach bytes no reader of the datagram holds.

	`computes` stays true -- situ does own this kernel -- and `compute`
	refuses, because the honest note names the pseudo-header rather than
	saying situ cannot compute an internet checksum.
	"""
	image = _of(ROOT / "example" / schema / f"{schema}.situ")
	index = _tag(image, tag)

	assert image.tags[index].prefix, "`prefix(...)` did not reach the image"
	assert derived.computes(image, index), "situ owns this kernel"
	with pytest.raises(derived.Uncomputable, match="prefix"):
		derived.compute(image, index, b"\x00" * 12)


#: The case the corpus does not have. Every checksum in this repository is
#: stored big-endian, so a reader hardcoding that is right by luck -- and
#: the schema has carried `tag_codec_endian` separately all along, because
#: WOZ2 stores its CRC little-endian where PNG stores its big. The member
#: itself has no byte order: a `u8 crc[4]` is bytes. What has one is the
#: NUMBER the codec produces.
LITTLE_CRC = """
endian little;

codec crc32 {
	kernel = polynomial(width = 32, poly = 0x04C11DB7, reflect,
	                    init = 0xFFFFFFFF, xorout = 0xFFFFFFFF);
}
impl crc32 derived;

struct frame {
	authenticated summed {
		u32  payload;
	}
	checksum u8 crc[4] covers(summed) is crc32;
}
"""
#: The same schema in the other order, so the writer is checked against a
#: case of each rather than against one and an assumption.
BIG_CRC = LITTLE_CRC.replace("endian little;", "endian big;")


def test_the_stored_order_is_the_schema_s() -> None:
	"""A little-endian checksum reaches the image as little-endian.

	The assertion is on the image rather than on a written file, because
	what was missing is the fact: a writer cannot honour an order nothing
	carried. The editor reads it in `_store`.
	"""
	image = _image(LITTLE_CRC)
	tag   = _tag(image, "frame.crc")

	assert image.tags[tag].endian == LITTLE, (
		"a schema declaring `endian little` reached the image as "
		f"{image.tags[tag].endian}, so a writer would spell the CRC "
		"in PNG's order")

	value = derived.compute(image, tag, b"\x01\x02\x03\x04")
	assert value == zlib.crc32(b"\x01\x02\x03\x04") & 0xFFFFFFFF


def test_the_editor_writes_the_stored_order() -> None:
	"""And the WRITER reads it, which no assertion on the image can show.

	A separate test because the two halves fail separately: the fact can
	reach the image while `_store` spells the bytes in PNG's order, and
	every assertion above would still pass. Both schemas are checked here,
	since a writer that always reverses is as wrong as one that never
	does and only a case of each separates them.
	"""
	from editor.document import open_document

	for text, order in ((LITTLE_CRC, "little"), (BIG_CRC, "big")):
		image = _image(text)
		blob  = packer.pack(parser.parse(Source("t.situ", text)),
		                    resolve(parser.parse(Source("t.situ", text)),
		                            solve(parser.parse(Source("t.situ",
		                                                      text)))),
		                    metadata=True)[0]
		doc   = open_document(blob, bytearray(8), "frame")
		notes = doc.set("payload", 0x11223344)

		assert any("recomputed" in note for note in notes), notes
		# `doc.buffer`, not the bytes handed in: a document copies, so that
		# an edit which is never saved changes nothing anywhere.
		held = bytes(doc.buffer)
		want = zlib.crc32(held[:4]) & 0xFFFFFFFF
		assert held[4:] == want.to_bytes(4, order), (  # type: ignore[arg-type]
			f"a schema declaring `endian {order}` stored "
			f"{held[4:].hex()} where its own order spells "
			f"{want.to_bytes(4, order).hex()}")  # type: ignore[arg-type]
		assert image.tags[_tag(image, "frame.crc")].endian == (
			LITTLE if order == "little" else BIG)


def test_the_span_comes_from_the_image() -> None:
	"""End to end, because every test above supplies the span itself.

	They call `compute` with the bytes in hand, so none of them can see a
	wrong COVERAGE -- which is the defect that actually happened. The span
	is carried as the first and last covered member, `last` meant the
	highest-numbered one, and all six of ICMP's variant arms sit after
	`rest` in the placement table and before it in the message. The span
	came out 8 bytes where RFC 792 sums 21.

	Removing the arm exclusion from the packer leaves every other test in
	this file green, which is what earns this one its place: the loop and
	the hole were covered and the thing that was wrong was neither.
	"""
	from editor.document import open_document

	path     = ROOT / "example" / "icmp" / "icmp.situ"
	text     = path.read_text(encoding="utf-8")
	parsed   = parser.parse(Source(str(path), text))
	blob, _  = packer.pack(parsed, resolve(parsed, solve(parsed)),
	                       metadata=True)

	# A stale checksum, so a span that sums the wrong bytes cannot be
	# right by having nothing to change.
	stale = bytearray(ECHO)
	stale[2:4] = b"\x00\x00"

	doc   = open_document(blob, stale, "icmp_message")
	notes = doc.set("code", 0)

	assert any("recomputed over 21 bytes at 0" in note for note in notes), (
		f"the span the image names is not RFC 792's: {notes}")
	assert _sum_including(bytes(doc.buffer)) == 0xFFFF, (
		f"the stored checksum fails RFC 1071's property: "
		f"{bytes(doc.buffer).hex()}")
	assert bytes(doc.buffer[2:4]) == b"\x3b\x5a", bytes(doc.buffer).hex()


def _bitwise(width: int, poly: int, init: int, xorout: int,
		reflect: bool, message: bytes) -> int:
	"""A CRC by its definition: polynomial division, one bit at a time.

	A SECOND WITNESS for the table-driven loops, and an independent one:
	`kernel_math` builds the byte table and this divides, so a wrong table,
	a wrong reflection, a wrong initial value or a register sitting at the
	wrong end of its word each make the two disagree. Comparing the walker
	against the table it was given would be asking one witness twice.

	The schema's `poly` and `init` as written, not the loop-direction forms
	`crc_start` and `crc_table` produce -- which is the point: this reads
	the declaration where the code under test reads the derivation.
	"""
	top = 1 << (width - 1)
	mask = (1 << width) - 1
	crc = init & mask

	for byte in message:
		if reflect:
			crc ^= byte
			for _ in range(8):
				crc = (crc >> 1) ^ (_rev(poly, width) if crc & 1 else 0)
		else:
			crc ^= (byte << (width - 8)) & mask if width >= 8 else 0
			if width < 8:
				# A code narrower than a byte takes its input a bit at a
				# time from the top; there is no byte-aligned shift to
				# make. usb's 5-bit token CRC and mmc's 7-bit are the
				# cases, and they are the two the corpus has that the
				# generated code runs in a byte-wide register.
				for bit in range(8):
					high = crc & top
					crc = (crc << 1) & mask
					if bool(byte & (0x80 >> bit)) != bool(high):
						crc ^= poly
				continue
			for _ in range(8):
				crc = ((crc << 1) ^ poly) & mask if crc & top \
					else (crc << 1) & mask
	return (crc ^ xorout) & mask


def _rev(value: int, width: int) -> int:
	out = 0
	for _ in range(width):
		out = (out << 1) | (value & 1)
		value >>= 1
	return out


#: Enough bytes to carry the register round several times, and an odd
#: length so the ones'-complement path's final half-word is exercised.
PROBE = bytes(range(0, 256, 7)) + b"\x00\xff\x7f"


@pytest.mark.parametrize("schema", ["png", "usb"])
def test_a_polynomial_agrees_with_long_division(schema: str) -> None:
	"""Every polynomial kernel in the corpus, against its own definition.

	This module otherwise reaches only `crc32`, which is reflected --
	leaving BOTH non-reflected loop shapes untested, including the
	left-aligned register a code narrower than a byte runs in. usb's token
	CRC is 5 bits wide and is that shape.

	`walker/derived.py` claimed the differential oracle compared these
	against C on every run. Nothing called them: the module was new and
	the sentence described a check that did not exist, which is the kind
	of claim that stops anybody writing one.
	"""
	from situc import kernels
	from situc.codegen import kernel_math

	path   = ROOT / "example" / schema / f"{schema}.situ"
	text   = path.read_text(encoding="utf-8")
	parsed = parser.parse(Source(str(path), text))
	image  = _of(path)

	seen = 0
	# `codecs()` rather than `decls`: it is typed as returning
	# `CodecDecl`, which is what `apply_to` takes, and it is the
	# enumeration the packer itself walks to build these rows.
	for decl in parsed.codecs():
		kernel = decl.kernel
		if kernel is None or kernel.family is not ast.KernelFamily.POLYNOMIAL:
			continue
		applied = kernels.apply_to(decl)
		width   = kernel_math.crc_width(applied)
		if width is None:
			continue

		# The image drops a codec's NAME -- the row interns one and the
		# walker reads past it -- so the kernel is matched on family and
		# width, and the match is asserted unique rather than taken from
		# `next()`. A test that silently picked the wrong kernel would be
		# comparing two codecs and reporting on one.
		same = [i for i, held in image.kernels.items()
		        if held.family == KERNEL_POLYNOMIAL and held.width == width]
		assert len(same) == 1, (
			f"{len(same)} polynomial kernels in {schema}.situ are "
			f"{width} bits wide, so this test cannot say which row is "
			f"`{decl.name}`")
		index = same[0]

		want = _bitwise(width,
		                kernel_math.number(applied, "poly"),
		                kernel_math.number(applied, "init"),
		                kernel_math.number(applied, "xorout"),
		                # `flag`, not `argument`: a bare `reflect` has no
		                # value, so `argument("reflect")` returns None
		                # whether it is there or not -- which read as
		                # NOT reflected and made this reference disagree
		                # with zlib's own answer for PNG.
		                bool(kernel.flag("reflect")),
		                PROBE)
		got = derived._polynomial(image, image.kernels[index], PROBE)
		assert got == want, (
			f"{decl.name} ({width}-bit): the table-driven loop says "
			f"{got:#x} where long division says {want:#x}")
		seen += 1

	assert seen, f"no polynomial kernel found in {schema}.situ"
