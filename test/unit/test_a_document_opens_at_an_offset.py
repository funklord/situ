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
