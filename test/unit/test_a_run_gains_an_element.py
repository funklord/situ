"""An element put into, or taken out of, a run of structs (26.1147).

A byte run could be written at a new length (26.1115) and an element of a
run of structs opened and edited (26.1144), but the run itself could not
grow by an element: a PNG could not gain a chunk, nor a TLV list an
option. The run's bytes are spliced and written as any run at a new
length is -- measured by the walk, the field that sizes it moved by the
bytes or by one element -- and the new element's own tags recomputed, as
a write to a covered member recomputes them.
"""
from __future__ import annotations

import struct
import subprocess
import sys
import zlib
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import pytest							# noqa: E402

from editor.document import Document, open_document		# noqa: E402
from situc.layout import solve					# noqa: E402
from situc.pack import pack					# noqa: E402
from situc.parser import parse_text				# noqa: E402
from situc.resolve import resolve				# noqa: E402
from walker.walk import Refused					# noqa: E402

PNG = ROOT / "example" / "png" / "png.situ"


def _image(text: str) -> bytes:
	schema = parse_text(text)
	return pack(schema, resolve(schema, solve(schema)), metadata=True)[0]


def _chunk(kind: bytes, data: bytes, crc: int | None = None) -> bytes:
	return struct.pack(">I", len(data)) + kind + data + struct.pack(
		">I", zlib.crc32(kind + data) if crc is None else crc)


def _png(*middle: bytes) -> bytes:
	return bytes.fromhex("89504e470d0a1a0a") + _chunk(b"IHDR", bytes(13)) \
		+ b"".join(middle) + _chunk(b"IEND", b"")


def test_a_png_gains_a_chunk_and_its_crc_is_computed() -> None:
	"""The chunk arrives with a wrong CRC, which nobody may write by
	hand: the editor computes it, as it does when a chunk's data grows."""
	document = open_document(_image(PNG.read_text(encoding="utf-8")),
	                         bytearray(_png()), "png_file")
	notes = document.insert("chunks", 1,
	                        _chunk(b"tEXt", b"A\x00b", crc=0xdeadbeef))
	assert bytes(document.buffer) == _png(_chunk(b"tEXt", b"A\x00b"))
	assert any("crc` recomputed" in note for note in notes), notes
	assert document.verdict is not None and document.verdict[0] == "ok"


def test_a_png_loses_a_chunk() -> None:
	document = open_document(_image(PNG.read_text(encoding="utf-8")),
	                         bytearray(_png(_chunk(b"tEXt", b"A\x00b"))),
	                         "png_file")
	document.remove("chunks", 1)
	assert bytes(document.buffer) == _png()


COUNTED = """target buffer;
endian big;
struct option { u8 len; u8 value[len]; }
struct options { u8 kind; u8 n; option list[n]; u8 end; }
struct framed { u8 size; u8 body[size] [holds = options]; u8 tail; }
"""


def test_a_counted_run_moves_its_count_by_one_element() -> None:
	"""`n` counts elements of no one size: inserting five bytes moves it by
	one, which no step taken from the bytes alone could find."""
	document = open_document(_image(COUNTED),
	                         bytearray(bytes.fromhex("07" "01" "0161" "ee")),
	                         "options")
	notes = document.insert("list", 1, bytes.fromhex("0462636465"))
	assert bytes(document.buffer) \
		== bytes.fromhex("07" "02" "0161" "0462636465" "ee")
	assert any("`n` moved by +1" in note for note in notes), notes
	document.remove("list", 0)
	assert bytes(document.buffer) == bytes.fromhex("07" "01" "0462636465" "ee")
	document.insert("list", 1, bytes.fromhex("00"))
	assert bytes(document.buffer) \
		== bytes.fromhex("07" "02" "0462636465" "00" "ee")


def test_an_element_goes_in_inside_a_window() -> None:
	"""The run inside a struct opened from its parent: the count moves by
	one, and the parent's frame by the bytes (26.1141's chain)."""
	document = open_document(_image(COUNTED),
	                         bytearray(bytes.fromhex("05" "07010161ee" "ff")),
	                         "framed")
	inner = document.open_nested(1)
	inner.insert("list", 0, bytes.fromhex("0178"))
	assert bytes(document.buffer) \
		== bytes.fromhex("07" "070201780161ee" "ff")


@pytest.mark.parametrize("call, said", [
	(lambda d: d.insert("list", 3, b"\x00"), "element goes in at 0 to 1"),
	(lambda d: d.remove("list", 1), r"has 1 element\(s\)"),
	(lambda d: d.insert("n", 0, b"\x00"), "is not a run of structs"),
	(lambda d: d.insert("nothing", 0, b"\x00"), "no member named"),
])
def test_what_is_not_an_element_is_refused(
		call: Callable[[Document], object], said: str) -> None:
	document = open_document(_image(COUNTED),
	                         bytearray(bytes.fromhex("07" "01" "0161" "ee")),
	                         "options")
	with pytest.raises(Refused, match=said):
		call(document)
	assert bytes(document.buffer) == bytes.fromhex("07" "01" "0161" "ee")


def test_the_cli_inserts_and_removes(tmp_path: Path) -> None:
	(tmp_path / "a.png").write_bytes(_png())
	run = subprocess.run(
		[sys.executable, str(ROOT / "bin" / "situ-edit"), "--struct",
		 "png_file", "--insert",
		 "chunks:1=" + _chunk(b"tEXt", b"A\x00b", crc=0).hex(),
		 "--out", str(tmp_path / "b.png"), str(PNG), str(tmp_path / "a.png")],
		capture_output=True, text=True, timeout=120, cwd=ROOT)
	assert run.returncode == 0, run.stderr
	assert (tmp_path / "b.png").read_bytes() \
		== _png(_chunk(b"tEXt", b"A\x00b"))
	run = subprocess.run(
		[sys.executable, str(ROOT / "bin" / "situ-edit"), "--struct",
		 "png_file", "--remove", "chunks:1", "--out", str(tmp_path / "c.png"),
		 str(PNG), str(tmp_path / "b.png")],
		capture_output=True, text=True, timeout=120, cwd=ROOT)
	assert run.returncode == 0, run.stderr
	assert (tmp_path / "c.png").read_bytes() == _png()


def test_the_tui_splices_as_the_cli_does(tmp_path: Path) -> None:
	"""0034: nothing the TUI does is absent from the CLI, and the two give
	the same bytes; `i` and `x` on its screen are these two flags asked
	for interactively, through the same `apply_splices`."""
	(tmp_path / "a.png").write_bytes(_png(_chunk(b"tEXt", b"A\x00b")))
	edits = ("--insert", "chunks:2=" + _chunk(b"tEXt", b"C\x00d", crc=0).hex(),
	         "--remove", "chunks:1")
	for tool in ("situ-edit", "situ-edit-tui"):
		run = subprocess.run(
			[sys.executable, str(ROOT / "bin" / tool),
			 *(("--no-ui",) if tool.endswith("tui") else ()),
			 "--struct", "png_file", *edits,
			 "--out", str(tmp_path / f"{tool}.png"), str(PNG),
			 str(tmp_path / "a.png")],
			capture_output=True, text=True, timeout=120, cwd=ROOT)
		assert run.returncode == 0, run.stderr
		assert (tmp_path / f"{tool}.png").read_bytes() \
			== _png(_chunk(b"tEXt", b"C\x00d")), tool
