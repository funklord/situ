"""A value read at the frame's end, and a run that leaves what it sizes (26.905).

    peek u8 count at end;
    u8 payload[remaining];
    u8 padding[count];

RTP's padding count is the last byte of the packet and sizes the bytes before
it, and ESP's trailer is the same shape two bytes wide. A length has to be read
before it is used, so these could not be said at all: the value sits after what
it sizes. `peek` already reads without spending; this measures it from the end.
"""

from __future__ import annotations

import importlib
import shutil
import struct
import subprocess
import sys
from pathlib import Path

import pytest

import fourway
from test_walker_c import (COMPILER, _inline_image, c_verdict,
                           python_verdict, shape_named_text)
from situc.diagnostics import SituError
from situc.layout import solve
from situc.parser import parse_text

ROOT = Path(__file__).resolve().parents[2]
HEAD = "target buffer;\nendian big;\n"

SCHEMA = HEAD + """
struct rtp {
	peek u8 count at end [min = 1];
	u8 payload[remaining];
	u8 padding[count];
}

struct esp {
	peek u8 next_header at end;
	peek u8 pad_length at end - 1;
	u8 payload[remaining];
	u8 padding[pad_length];
	u8 pad_length_again [must_eq = pad_length];
	u8 next_header_again;
}

struct wide {
	peek u16 rest at end - 2;
	u8 head;
	u8 body[remaining];
	u8 tail[rest];
	u8 check[4];
}
"""

CASES = {
	"rtp": {
		"01":                  "0",
		"aabb01":              "0",
		"aabbcc0002":          "0",
		"aa0003":              "0",
		"03":                  "1",
		"aa04":                "1",
		"aabb00":              "2",
		"00":                  "2",
		"ff":                  "1",
	},
	"esp": {
		"0011":                "0",
		"aabb0011":            "0",
		"aaff01ff0111":        "0",
		"ff00ff":              "0",
		"0511":                "1",
		"0100":                "2",
	},
	"wide": {
		"aa" "00000000":       "0",
		"aa" "bbcc" "00000000": "0",
		"aa" "dd" "00010000":  "0",
		"aa" "0004" "0000":    "1",
	},
}


def _section(answers: str, name: str) -> str:
	return answers[answers.index(f"-- {name}\n"):].split("\n-- ")[0]


def _line(section: str, name: str) -> str:
	return next(line.split()[1] for line in section.splitlines()
	            if line.startswith(name + " "))


@pytest.mark.skipif(not fourway.COMPLETE, reason="needs gcc, g++ and rustc")
def test_all_four_read_it_alike(tmp_path: Path) -> None:
	schema = tmp_path / "unit.situ"
	schema.write_text(SCHEMA, encoding="ascii")
	command = fourway.build(tmp_path, schema)
	for name, cases in CASES.items():
		for message, verdict in cases.items():
			given = {backend: _section(fourway.answers(
			             argv, bytes.fromhex(message), tmp_path), name)
			         for backend, argv in command.items()}
			assert len(set(given.values())) == 1, (message, given)
			assert _line(next(iter(given.values())), "validate") == verdict, \
				(message, given)


@pytest.mark.skipif(COMPILER is None, reason="needs a C compiler")
@pytest.mark.parametrize("name", sorted(CASES))
def test_both_walkers_read_it_alike(name: str, tmp_path: Path) -> None:
	image = _inline_image(SCHEMA)
	shape = shape_named_text(SCHEMA, name)
	for message, verdict in CASES[name].items():
		data = bytes.fromhex(message)
		assert python_verdict(image, data, shape) == verdict, message
		assert c_verdict(tmp_path, image, data, shape) == verdict, message


REFUSED = {
	"a width past a byte":
		("bit_order msb_first;\nstruct s { peek u4 n at end; u8 d[remaining]; }",
		 "whole bytes"),
	"a length read after the run":
		("struct s { u8 d[remaining]; u8 n; u8 e[n]; }", "must be the last"),
	"a run of wider values left behind":
		("struct s { peek u8 n at end; u8 d[remaining]; u16 e[n]; }",
		 "must be the last"),
	"two runs that reach for the end":
		("struct s { u8 d[remaining]; u8 e[remaining]; }", "last member"),
}


@pytest.mark.parametrize("source, words", REFUSED.values(), ids=list(REFUSED))
def test_what_is_not_said_is_refused(source: str, words: str) -> None:
	with pytest.raises(SituError) as refused:
		solve(parse_text(HEAD + source))
	assert words in str(refused.value)


def _pcap(packets: list[bytes]) -> bytes:
	"""UDP datagrams to port 5004, each carrying one packet, as a capture."""
	out = struct.pack("<IHHiIII", 0xa1b2c3d4, 2, 4, 0, 0, 65535, 1)
	for packet in packets:
		udp   = struct.pack(">HHHH", 5004, 5004, 8 + len(packet), 0) + packet
		ip    = struct.pack(">BBHHHBBH4s4s", 0x45, 0, 20 + len(udp), 0, 0, 64,
		                    17, 0, b"\x0a\0\0\1", b"\x0a\0\0\2") + udp
		frame = b"\2" * 6 + b"\4" * 6 + b"\x08\0" + ip
		out  += struct.pack("<IIII", 0, 0, len(frame), len(frame)) + frame
	return out


@pytest.mark.skipif(shutil.which("tshark") is None, reason="no tshark")
def test_rtp_reads_as_wireshark_reads_it(tmp_path: Path) -> None:
	"""Its own dissector, which situ had no hand in, over every vector."""
	vectors = [line.split() for line
	           in (ROOT / "example/rtp/rtp.vectors").read_text().splitlines()
	           if line.startswith("rtp ")]
	packets = [bytes.fromhex(line[2]) for line in vectors]
	(tmp_path / "rtp.pcap").write_bytes(_pcap(packets))
	ran = subprocess.run(
		["tshark", "-r", str(tmp_path / "rtp.pcap"), "-d", "udp.port==5004,rtp",
		 "-T", "fields", "-E", "separator=/t", "-e", "rtp.payload",
		 "-e", "rtp.padding.count"],
		check=True, capture_output=True, text=True, timeout=60)
	seen = [line.split("\t") for line in ran.stdout.splitlines()]
	assert len(seen) == len(packets)

	subprocess.run([sys.executable, "-m", "situc", "build", "--target",
	                "python", "--out", str(tmp_path),
	                str(ROOT / "example/rtp/rtp.situ")],
	               check=True, cwd=ROOT, capture_output=True)
	sys.path[:0] = [str(tmp_path), str(ROOT / "runtime/python")]
	try:
		rtp = importlib.import_module("rtp")
		for packet, (payload, count) in zip(packets, seen):
			data = bytearray(packet)
			view = rtp.rtp.at(rtp.Message(data), 0, len(data))
			view.validate()
			if view.padded_flag:
				assert view.last == int(count)
				assert len(view.padding) == int(count)
			else:
				assert not count
				assert not len(view.padding)
			assert bytes(view.payload).hex() == payload.replace(":", "")
	finally:
		del sys.path[:2]
		sys.modules.pop("rtp", None)


@pytest.mark.skipif(shutil.which("tshark") is None, reason="no tshark")
def test_the_generated_dissector_splits_it_as_the_vectors_do(
		tmp_path: Path) -> None:
	"""In Wireshark itself: a stub cannot refuse a file for its `&&`."""
	subprocess.run([sys.executable, "-m", "situc", "gen-dissector",
	                str(ROOT / "example/rtp/rtp.situ"), "--out", str(tmp_path)],
	               check=True, cwd=ROOT, capture_output=True)
	packets = [bytes.fromhex(line.split()[2]) for line
	           in (ROOT / "example/rtp/rtp.vectors").read_text().splitlines()
	           if line.startswith("rtp ")]
	capture = struct.pack("<IHHiIII", 0xa1b2c3d4, 2, 4, 0, 0, 65535, 147)
	for packet in packets:
		capture += struct.pack("<IIII", 0, 0, len(packet), len(packet)) + packet
	(tmp_path / "rtp.pcap").write_bytes(capture)
	ran = subprocess.run(
		["tshark", "-X", f"lua_script:{tmp_path / 'rtp.lua'}",
		 "-o", 'uat:user_dlts:"User 0 (DLT=147)","situ_rtp_rtp","0","","0",""',
		 "-r", str(tmp_path / "rtp.pcap"), "-T", "fields", "-E", "separator=/t",
		 "-e", "situ.rtp.payload", "-e", "situ.rtp.padding"],
		check=True, capture_output=True, text=True, timeout=60)
	shown = ran.stdout.replace(":", "").replace("<MISSING>", "")
	assert shown.splitlines() == [
		"deadbeef\t", "aabb\t", "cafe0000\t00000004", "cafe\t01", "feed\t0002"]
