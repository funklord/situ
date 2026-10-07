"""A document can start part-way into a file (26.581).

`situ-edit` read every message from byte zero, so a chunked container --
a PNG, an ID3 tag, a TIFF directory -- had to be cut up with `dd` before
any of it could be read. And the tool said so itself: a nested member's
note read

	a nested struct; open it as its own document

which names neither the struct nor an offset, and asked for something
no option provided. **The one instruction this tool gives a reader was
for a thing it did not support.**

`View` has carried a base and a limit from the beginning. What was
missing was a way to ask for them, so `acquire` -- the one bounds check
everything below it trusts -- learns `at` and `limit` rather than
callers building their own views and skipping it.

THE HAZARD IS THAT `offset_bits` IS VIEW-RELATIVE and a buffer is not.
Every place the editor mixes the two had to be converted at one
boundary, and the checksum recompute of 26.579 is where it would have
been silent: a chunk opened at offset 8 summed the eight bytes of the
PNG signature and stopped eight bytes short of the chunk, producing a
plausible CRC-32 over the wrong span. Reverting that one line takes
`b3b35cad` to `4e957ab4` on the fixture below, which is what
`test_a_recompute_at_an_offset_sums_the_right_bytes` holds.
"""

from __future__ import annotations

import sys
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "test" / "unit"))

import pytest					# noqa: E402
from editor.document import open_document		# noqa: E402
from editor.text import render			# noqa: E402
from situc import pack as packer			# noqa: E402
from situc import parser				# noqa: E402
from situc.diagnostics import Source		# noqa: E402
from situc.layout import solve			# noqa: E402
from situc.resolve import resolve			# noqa: E402
from walker.walk import Refused			# noqa: E402

#: RFC 2083 section 3.1, and the same eight bytes `png.vectors` carries.
SIGNATURE = bytes.fromhex("89504e470d0a1a0a")

#: An IHDR for a 22x22 8-bit RGBA image -- the shape of the file
#: `png.vectors` was read off. Built here rather than read from an
#: installed icon so the test needs nothing on the machine, and its CRC
#: comes from `zlib` rather than from situ, so the fixture is not this
#: project's opinion of itself.
IHDR_BODY = bytes.fromhex("49484452") + bytes.fromhex(
	"00000016000000160806000000")
IHDR = (len(IHDR_BODY) - 4).to_bytes(4, "big") + IHDR_BODY \
	+ (zlib.crc32(IHDR_BODY) & 0xFFFFFFFF).to_bytes(4, "big")

#: Signature, IHDR, and an IEND so the file is a plausible PNG rather
#: than a chunk with a header glued on.
IEND_BODY = b"IEND"
PNG = SIGNATURE + IHDR + (b"\x00" * 4) + IEND_BODY \
	+ (zlib.crc32(IEND_BODY) & 0xFFFFFFFF).to_bytes(4, "big")


#: A nested struct at a static offset, so the note's number is exact. Two
#: of the corpus's forty-five have a nested struct in their FIRST struct and
#: both are recursive, which makes the offset dynamic and the assertion
#: about the walk rather than about the note.
NESTED = """
endian big;

struct inner {
	u16  value;
}

struct outer {
	u8     kind;
	u8     flags;
	inner  held;
}
"""


def _image_of(text: str) -> bytes:
	parsed   = parser.parse(Source("t.situ", text))
	resolved = resolve(parsed, solve(parsed))
	return packer.pack(parsed, resolved, metadata=True)[0]


def _image(path: Path) -> bytes:
	text     = path.read_text(encoding="utf-8")
	parsed   = parser.parse(Source(str(path), text))
	resolved = resolve(parsed, solve(parsed))
	return packer.pack(parsed, resolved, metadata=True)[0]


@pytest.fixture(scope="module")
def png() -> bytes:
	return _image(ROOT / "example" / "png" / "png.situ")


def test_a_chunk_at_an_offset_reads_as_one_on_its_own(png: bytes) -> None:
	"""The same chunk, two ways, and the fields must agree.

	A relationship rather than pinned values: the extracted chunk at byte
	zero is what the tool could already do, and reading it in place is
	what it could not. Values pinned on one side only would go stale
	together and say nothing about the pair.
	"""
	whole = open_document(png, PNG, "chunk", 8, len(IHDR))
	alone = open_document(png, IHDR, "chunk")

	assert [(f.name, f.offset, f.size, f.value) for f in whole.fields()] == \
	       [(f.name, f.offset, f.size, f.value) for f in alone.fields()], (
		"a chunk read in place disagrees with the same bytes read alone")
	assert whole.extent == alone.extent == len(IHDR)


def test_the_window_is_reported_and_not_the_file(png: bytes) -> None:
	"""The header line says how long the WINDOW is.

	It said `len(buffer)`, which is the file -- so a 25-byte chunk inside
	a 1227-byte image announced 1227 above a list of offsets that were
	all about the chunk.
	"""
	held  = open_document(png, PNG, "chunk", 8, len(IHDR))
	first = render(held)[0]

	assert f"{len(IHDR)} bytes" in first, first
	assert "at 8" in first, first
	assert str(len(PNG)) not in first, (
		f"the header names the file's length rather than the window's: "
		f"{first}")


def test_a_recompute_at_an_offset_sums_the_right_bytes(png: bytes) -> None:
	"""The hazard this feature could have introduced, held by zlib.

	`offset_bits` answers from the view's base and the buffer is indexed
	absolutely, so a document opened at 8 would sum from the file's byte 4
	-- inside the signature -- and stop eight bytes short of the chunk.
	The result is a well-formed CRC-32 of the wrong seventeen bytes, which
	nothing downstream could tell from a right one.
	"""
	held  = open_document(png, PNG, "chunk", 8, len(IHDR))
	notes = held.set("data", bytes.fromhex("00000016000000160806000001"))

	assert any("recomputed over 17 bytes" in note for note in notes), notes

	chunk  = bytes(held.buffer[8:8 + len(IHDR)])
	stored = int.from_bytes(chunk[-4:], "big")
	assert stored == zlib.crc32(chunk[4:-4]) & 0xFFFFFFFF, (
		f"stored {stored:#010x}, zlib says "
		f"{zlib.crc32(chunk[4:-4]) & 0xFFFFFFFF:#010x}")

	# And the rest of the file is untouched: an edit at an offset must not
	# be a rewrite of everything around it.
	assert bytes(held.buffer[:8]) == SIGNATURE
	assert bytes(held.buffer[8 + len(IHDR):]) == PNG[8 + len(IHDR):]


def test_a_nested_note_names_the_struct_and_an_offset(png: bytes) -> None:
	"""The note asks the reader to do something, so it says what to pass.

	ABSOLUTE, where the offset column is view-relative: that number goes
	on a command line. A document opened at 8 whose note said `--offset 8`
	for a member eight bytes in would send the reader back to the
	signature.
	"""
	image = _image_of(NESTED)

	# At zero, the note's offset is the member's own. Opened at 4, the
	# same member is four bytes further into the file and the note has to
	# say so -- which is the whole difference between the column and the
	# number a command takes.
	for base in (0, 4):
		held   = open_document(image, bytes(16), "outer", base)
		nested = [field for field in held.fields() if "a nested `" in field.note]

		assert nested, "no nested member to check the note on"
		for field in nested:
			assert field.offset is not None
			assert f"a nested `inner`" in field.note, field.note
			passed = int(field.note.split("--offset")[1].split()[0])
			assert passed == base + field.offset, (
				f"at base {base}, `{field.name}` is at {field.offset} and "
				f"the note says --offset {passed}")


@pytest.mark.parametrize("at,length,why", [
	(len(PNG) + 1, None, "an offset past the end"),
	(-1,           None, "a negative offset"),
	(8,            9999, "a length that reaches past the end"),
])
def test_a_window_outside_the_file_is_refused(
		png: bytes, at: int, length: int | None, why: str) -> None:
	"""Refused at open, naming the argument, rather than at the first read.

	Every answer below a bad window would be about bytes nobody has, and
	the reader who passed `--offset` is the one who can fix it.
	"""
	with pytest.raises(Refused):
		open_document(png, PNG, "chunk", at, length)


#: The ID3 tag `example/id3/id3.vectors` carries: 209 bytes of v2.4 out of
#: a real MP3, with nine frames and no padding. Read from the committed
#: vector rather than restated here, so the two cannot drift -- and it is a
#: fixture some other encoder wrote, which is what makes the chain below
#: evidence rather than this project's arithmetic about itself.
def _tag() -> bytes:
	for line in (ROOT / "example" / "id3" / "id3.vectors").read_text(
			encoding="ascii").splitlines():
		if line.startswith("id3_tag tag "):
			return bytes.fromhex(line.split(" ", 2)[2].replace(" ", ""))
	raise AssertionError("id3.vectors no longer carries an `id3_tag` vector")


def test_a_struct_reports_what_it_measures(png: bytes) -> None:
	"""The window is what the caller gave; the extent is what the struct is.

	A chunk opened at an offset with no length gets the rest of the file, so
	the two differ and both are worth having: 12 bytes of frame plus the
	length it declares is the record, and the rest is whatever follows it.
	"""
	held = open_document(png, PNG, "chunk", 8)

	assert held.extent == len(PNG) - 8, "the window is not the rest of the file"
	assert held.measured == len(IHDR), (
		f"the chunk measures {held.measured} where its own length field "
		f"makes it {len(IHDR)}")
	assert held.measured < held.extent


def test_the_header_names_both_only_when_they_differ(png: bytes) -> None:
	"""`M of N` where the window is bigger, `N` where it is not.

	The common case -- a document opened whole, or framed at exactly the
	length it occupies -- reads as it always did, so the second number
	appears when it says something and not otherwise.
	"""
	loose = render(open_document(png, PNG, "chunk", 8))[0]
	tight = render(open_document(png, PNG, "chunk", 8, len(IHDR)))[0]

	assert f"{len(IHDR)} of {len(PNG) - 8} bytes at 8" in loose, loose
	assert f"{len(IHDR)} bytes at 8" in tight, tight
	assert " of " not in tight, tight


def test_the_measured_extent_walks_a_run_of_records() -> None:
	"""Offset plus extent is the next record, nine times, landing exactly.

	The strong form of this feature, and the reason it is worth having: a
	reader stepping through a container needs the record's own length, and
	deriving it by hand means reimplementing the format's sizes -- which for
	ID3 is the synchsafe decode this corpus exists to describe.

	Nine frames, each offset taken ONLY from the previous frame's reported
	extent, arriving at the tag's own declared end. A wrong extent anywhere
	in the chain lands somewhere that is not 209, and the identifiers say
	which link broke rather than only that one did.
	"""
	image = _image(ROOT / "example" / "id3" / "id3.situ")
	tag   = _tag()

	# The tag's own size, which is the independent end-point: ten header
	# bytes plus four seven-bit groups.
	size = (tag[6] << 21) | (tag[7] << 14) | (tag[8] << 7) | tag[9]
	end  = 10 + size

	at    = 22				# past the header and the extended header
	found: list[tuple[int, str]] = []
	while at < end:
		held = open_document(image, tag, "id3_frame", at)
		reach = held.measured
		assert reach is not None, f"frame at {at} could not be measured"
		assert reach > 0, f"frame at {at} measures zero, so the walk cannot step"

		ident = bytes(int(field.value) for field in held.fields()
		              if field.name.startswith("identifier_")
		              and isinstance(field.value, int))
		found.append((at, ident.decode("latin-1")))
		at += reach

	assert at == end, (
		f"the chain of nine extents ends at {at}, where the tag's own size "
		f"makes it {end}: {found}")
	assert [name for _, name in found] == [
		"COMM", "COMM", "TIT2", "TALB", "TCON", "TPE1", "TYER", "TDRC",
		"TRCK"], found


def test_an_unmeasurable_struct_is_not_reported_as_zero(png: bytes) -> None:
	"""`None` and 0 are different answers and the header keeps them apart.

	Zero is real -- a DNS name whose first label does not fit holds no
	labels -- so a struct the walk cannot measure must not collapse into
	it. A two-byte window on a chunk cannot place the length field, which
	is the cheapest way to reach the branch.
	"""
	held = open_document(png, PNG, "chunk", 8, 2)

	assert held.measured is None, held.measured
	assert "extent unknown" in render(held)[0], render(held)[0]
