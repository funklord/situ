"""The dissector reads a marker-governed member in the marker's order (26.963).

Every member an `endian_marker` governs was shown big-endian whatever the
marker said: a little-endian TIFF -- most of them -- showed `magic` as
10752 where the file says 42, and a little-endian pcap file's
`version_major` as 512. The four backends and both walkers have followed
the marker since 26.576; the dissector was the one reader left.

It reads the marker as every reader does, big-endian, and compares it
against the `little` literal; a struct handed its order (26.961) compares
the argument. Values are worked out by hand from the bytes.
"""

from __future__ import annotations

import shutil
import struct
import subprocess
import sys
from pathlib import Path

import pytest

from test_a_struct_takes_its_byte_order import FILES, SCHEMA as PCAP
from situc.cli import analyse
from situc.dissector import generate
from situc.parser import parse
from test_dissector import LUA, read_back

ROOT = Path(__file__).resolve().parents[2]

TIFF = {"little": struct.pack("<2sHI", b"II", 42, 20),
        "big":    struct.pack(">2sHI", b"MM", 42, 20)}


def _lua(tmp_path: Path, schema: Path, stem: str) -> Path:
	source, resolved, _ = analyse(schema)
	script = tmp_path / f"{stem}.lua"
	script.write_text(generate(parse(source), resolved, stem),
	                  encoding="ascii")
	return script


@pytest.mark.skipif(LUA is None, reason="no Lua interpreter")
def test_a_tiff_header_reads_in_its_marker_s_order(tmp_path: Path) -> None:
	script = _lua(tmp_path, ROOT / "example/tiff/tiff.situ", "tiff")
	for order, packet in TIFF.items():
		_, rows = read_back(script, "tiff_header", packet)
		shown = {name: value for name, _, _, value in rows}
		assert shown["tiff_header.magic"] == "42", (order, rows)
		assert shown["tiff_header.ifd_offset"] == "20", (order, rows)


@pytest.mark.skipif(LUA is None, reason="no Lua interpreter")
def test_a_pcap_file_reads_in_its_magic_s_order(tmp_path: Path) -> None:
	schema = tmp_path / "pcap.situ"
	schema.write_text(PCAP, encoding="ascii")
	script = _lua(tmp_path, schema, "pcap")
	for order, packet in FILES.items():
		_, rows = read_back(script, "pcap", packet)
		shown = [(name, value) for name, _, _, value in rows]
		assert ("pcap.version_major", "2") in shown, (order, rows)
		assert ("pcap.snaplen", "262144") in shown, (order, rows)
		# Both records, in the order the header states: the handed one.
		assert [value for name, value in shown
		        if name == "record.incl_len"] == ["14", "4"], (order, rows)
		assert [value for name, value in shown
		        if name == "record.ts_usec"] == ["1", "2"], (order, rows)


@pytest.mark.skipif(shutil.which("tshark") is None, reason="no tshark")
def test_wireshark_reads_a_little_endian_pcap_inside_a_capture(
		tmp_path: Path) -> None:
	"""In tshark: the handed argument reaches the record's dissector there
	too, which a harness that calls each function itself cannot say."""
	schema = tmp_path / "pcap.situ"
	schema.write_text(PCAP, encoding="ascii")
	subprocess.run([sys.executable, "-m", "situc", "gen-dissector",
	                str(schema), "--out", str(tmp_path)],
	               check=True, cwd=ROOT, capture_output=True)
	packet  = FILES["little"]
	capture = struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 147) \
		+ struct.pack("<IIII", 0, 0, len(packet), len(packet)) + packet
	(tmp_path / "outer.pcap").write_bytes(capture)
	ran = subprocess.run(
		["tshark", "-X", f"lua_script:{tmp_path / 'pcap.lua'}", "-o",
		 'uat:user_dlts:"User 0 (DLT=147)","situ_pcap_pcap","0","","0",""',
		 "-r", str(tmp_path / "outer.pcap"), "-T", "fields",
		 "-e", "situ.pcap.version_major", "-e", "situ.record.incl_len"],
		check=True, capture_output=True, text=True, timeout=60)
	assert ran.stdout.split() == ["2", "14,4"], ran.stdout + ran.stderr
