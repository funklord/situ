"""`covers(after)` and `covers(message)`: `before`'s twins (26.1117).

A checksum kept in a header covering the bytes its PARENT places -- U-Boot's
data CRC over the payload after the header, FS2's image CRC over the whole
image with its own sixteen bits read as 0xFFFF. `before` already took its
span from the message rather than from members; these take the end of the
tag's own struct to the message's end, and the whole message.

Every sum below is zlib's, computed here, not another reader's: six
readers agreeing is one witness if they share a mistake (26.1109).
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

import pytest

import fourway
from editor.document import open_document
from situc.diagnostics import SituError
from situc.layout import solve
from situc.pack import pack
from situc.parser import parse_text
from situc.resolve import resolve
from situc.unparse import unparse
from test_a_walker_checks_a_checksum import SUMS, python_sums
from test_walker_c import _build_driver, _run_driver, image_for, shape_named

CRC32 = """codec crc32 {
	kernel = polynomial(width = 32, poly = 0x04C11DB7, init = 0xFFFFFFFF,
	                    xorout = 0xFFFFFFFF, reflect);
}
impl crc32 derived;
"""

UBOOT = f"""target buffer;
endian big;

{CRC32}
struct uheader {{
	u32 magic [must_eq = 0x27051956];
	u32 size;
	checksum u8 dcrc[4] covers(after) is crc32;
}}
struct uimage {{
	uheader h;
	u8 data[h.size];
}}
"""

FS = f"""target buffer;
endian big;

{CRC32}
struct fsheader {{
	u32 magic;
	checksum u8 crc[4] covers(message) is crc32 [self_as = 0xFF];
	u32 count;
}}
struct fsimage {{
	fsheader h;
	u8 sections[remaining];
}}
"""

PAYLOAD = b"hello, uboot"
UB_MESSAGE = (struct.pack(">II", 0x27051956, len(PAYLOAD))
              + struct.pack(">I", zlib.crc32(PAYLOAD)) + PAYLOAD)

SECTIONS = b"section-bytes-here"
FS_HEADER = struct.pack(">I", 0x5A5A5A5A) + b"\xff" * 4 + struct.pack(">I", 3)
FS_CRC = zlib.crc32(FS_HEADER + SECTIONS)
FS_MESSAGE = FS_HEADER[:4] + struct.pack(">I", FS_CRC) + FS_HEADER[8:] \
	+ SECTIONS


def _packed(source: str) -> bytes:
	schema = parse_text(source)
	return pack(schema, resolve(schema, solve(schema)), metadata=True)[0]


def test_the_clauses_round_trip() -> None:
	for source in (UBOOT, FS):
		assert unparse(parse_text(unparse(parse_text(source)))) \
			== unparse(parse_text(source))
	assert "covers(after)" in unparse(parse_text(UBOOT))
	assert "covers(message)" in unparse(parse_text(FS))


@pytest.mark.parametrize("schema,refusal", [
	# The whole message holds the tag, so its bytes must read as something.
	(FS.replace(" [self_as = 0xFF]", ""), "inside the region it covers"),
	# `after` starts where its struct ends, which a variable one does not say.
	(UBOOT.replace("u32 size;", "u8 n; u8 pad[n]; u32 size;"),
	 "which has no fixed size"),
	# A span of the message is not a region, so it does not share a list.
	(UBOOT.replace("covers(after)", "covers(after, before)"),
	 "cannot be listed with regions or with another span"),
])
def test_what_cannot_be_said_is_refused(schema: str, refusal: str) -> None:
	with pytest.raises(SituError, match=refusal):
		_packed(schema)


@pytest.mark.parametrize("source,message,name,want", [
	(UBOOT, UB_MESSAGE, "uheader", zlib.crc32(PAYLOAD)),
	(FS, FS_MESSAGE, "fsheader", FS_CRC),
])
def test_both_walkers_sum_what_zlib_sums(tmp_path: Path, source: str,
		message: bytes, name: str, want: int) -> None:
	schema = tmp_path / "s.situ"
	schema.write_text(source, encoding="ascii")
	blob  = image_for(schema)
	shape = shape_named(schema, name)
	drive = _build_driver(tmp_path, blob, SUMS)
	c_said = _run_driver(drive, tmp_path, message, shape, by_line=True)
	assert f"sum={want}" in c_said, c_said
	assert python_sums(blob, message, shape) == c_said


def test_four_backends_and_the_walkers_agree_with_zlib(
		tmp_path: Path) -> None:
	"""The differential's own probes, `covered` and `sum`, from every
	backend -- each line held to the value worked here."""
	for source, message, name, tag, want, lo in (
			(UBOOT, UB_MESSAGE, "uheader", "dcrc", zlib.crc32(PAYLOAD), 12),
			(FS, FS_MESSAGE, "fsheader", "crc", FS_CRC, 0)):
		where = tmp_path / name
		where.mkdir()
		schema = where / "s.situ"
		schema.write_text(source, encoding="ascii")
		for backend, run in fourway.build(where, schema).items():
			said = fourway.answers(run, message, where)
			assert f"{tag}.covered {lo}+{len(message) - lo}" in said, \
				(backend, said)
			assert f"{tag}.sum {want}" in said, (backend, said)


def test_verify_refuses_a_flipped_byte(tmp_path: Path,
		capsys: pytest.CaptureFixture[str]) -> None:
	"""`situc verify` runs the generated Python's `_check`: a byte past the
	header is covered by `after`, and any byte at all by `message`."""
	from situc.cli import main

	# The PARENT is what a vector is: the header alone leaves the payload
	# surplus, and `verify` descends into the header to check its tag
	# (26.1111).
	for source, message, name in ((UBOOT, UB_MESSAGE, "uimage"),
	                              (FS, FS_MESSAGE, "fsimage")):
		flipped = bytearray(message)
		flipped[-1] ^= 1
		(tmp_path / "s.situ").write_text(source, encoding="ascii")
		(tmp_path / "v.vectors").write_text(
			f"{name} good {message.hex(' ')}\n"
			f"{name} bad {bytes(flipped).hex(' ')}\n", encoding="ascii")
		main(["verify", str(tmp_path / "s.situ"), str(tmp_path / "v.vectors")])
		out = capsys.readouterr().out
		assert f"{name} `bad` does not conform" in out, out
		assert "1 of 2 vectors do not conform" in out, out


def test_the_editor_brings_both_up_to_date() -> None:
	"""A write in the PARENT's bytes stales a checksum in the header it
	holds; the editor recomputes it, as zlib says it should be."""
	document = open_document(_packed(UBOOT), bytearray(UB_MESSAGE), "uimage")
	notes = document.set("data", b"HELLO, uboot")
	assert any("dcrc" in note and "recomputed" in note for note in notes)
	assert bytes(document.buffer[8:12]) \
		== struct.pack(">I", zlib.crc32(b"HELLO, uboot"))

	document = open_document(_packed(FS), bytearray(FS_MESSAGE), "fsimage")
	document.set("sections", b"SECTION-bytes-here")
	held = bytearray(document.buffer)
	stored = struct.unpack(">I", bytes(held[4:8]))[0]
	held[4:8] = b"\xff" * 4
	assert stored == zlib.crc32(bytes(held))
