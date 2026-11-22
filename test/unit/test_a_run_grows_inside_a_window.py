"""A run written at a new length inside a window opened from its parent
(26.1141).

26.1115 refused it: a window's parent has lengths that would go stale.
A window opened from its parent knows the chain, and each level is
measured as `_resize` measures one -- so a PNG `tEXt` chunk grown inside
the whole file moves the chunk's `length`, recomputes its CRC, and leaves
a file zlib agrees with; and a run of a run's elements is reachable, which
`open_nested` on `chunks` (the first chunk only) never made it.
"""
from __future__ import annotations

import struct
import sys
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import pytest							# noqa: E402

from editor.document import open_document			# noqa: E402
from situc.layout import solve					# noqa: E402
from situc.pack import pack					# noqa: E402
from situc.parser import parse_text				# noqa: E402
from situc.resolve import resolve				# noqa: E402
from walker.walk import Refused					# noqa: E402

SIGNATURE = bytes.fromhex("89504e470d0a1a0a")


def _image(text: str) -> bytes:
	schema = parse_text(text)
	return pack(schema, resolve(schema, solve(schema)), metadata=True)[0]


def _chunk(kind: bytes, data: bytes) -> bytes:
	return struct.pack(">I", len(data)) + kind + data \
		+ struct.pack(">I", zlib.crc32(kind + data))


def _png(text: bytes) -> bytes:
	return SIGNATURE + _chunk(b"IHDR", bytes(13)) \
		+ _chunk(b"tEXt", text) + _chunk(b"IEND", b"")


def test_a_text_chunk_grows_inside_the_whole_file() -> None:
	image = _image((ROOT / "example" / "png" / "png.situ")
	               .read_text(encoding="utf-8"))
	document = open_document(image, bytearray(_png(b"Comment\x00hi")),
	                         "png_file")
	chunk = document.open_element(1, 1)
	assert {f.name: f.value for f in chunk.fields()}["kind"] == b"tEXt"
	notes = chunk.set("data", b"Comment\x00hello world")
	assert bytes(document.buffer) == _png(b"Comment\x00hello world")
	assert any("`chunk.length` moved by +9" in note for note in notes), notes
	assert document.verdict is not None and document.verdict[0] == "ok"
	# And back down, through the same window, whose frame followed it.
	chunk.set("data", b"Comment\x00x")
	assert bytes(document.buffer) == _png(b"Comment\x00x")


NESTED = """target buffer;
endian big;
struct inner { u8 n; u8 name[n]; u8 flag; }
struct plain { u8 kind; inner body; u8 end; }
struct framed { u8 kind; u8 total; inner body; u8 end; }
invariant framed.total == size(framed.body);
"""


def test_a_nested_run_grows_and_its_sizer_follows() -> None:
	document = open_document(_image(NESTED),
	                         bytearray(bytes.fromhex("07026869" "01ee")),
	                         "plain")
	inner = document.open_nested(1)
	inner.set("name", b"hello")
	assert bytes(document.buffer) == bytes.fromhex("0705" "68656c6c6f" "01ee")


def test_a_derived_frame_that_cannot_follow_refuses_the_write() -> None:
	held = bytes.fromhex("0704026869" "01ee")
	document = open_document(_image(NESTED), bytearray(held), "framed")
	inner = document.open_nested(2)
	with pytest.raises(Refused, match="needs what frames it to move"):
		inner.set("name", b"hello")
	assert bytes(document.buffer) == held
