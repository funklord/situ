"""The dissector walks a tlv region's items (26.937).

A tlv region fell to the generic "a length this dissector cannot render"
and stopped the cursor, so CoAP's options, protobuf's fields, QUIC's
transport parameters and HTTP/3's frames showed nothing. It is walked as
`walk._tlv_walk` walks it, each item a row, and the tag's bit fields are
arithmetic rather than bit operators (decision 0021).
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from struct import pack

import pytest

from test_dissector import LUA, dissect

ROOT     = Path(__file__).resolve().parents[2]
PROTOBUF = ROOT / "example" / "protobuf" / "protobuf.situ"


@pytest.mark.skipif(LUA is None, reason="no Lua interpreter")
def test_protobuf_fields_are_items(tmp_path: Path) -> None:
	"""Field 1 = 150 (`08 96 01`, the encoding guide's own) and field 2 =
	"hi" (`12 02 68 69`): one varint value and one prefixed, worked by
	hand."""
	_, rows = dissect(tmp_path, PROTOBUF, "proto_message",
	                  bytes.fromhex("089601" "12026869"))
	assert rows == [("proto_message.fields", 0, 3, "089601"),
	                ("proto_message.fields", 3, 4, "12026869")]


@pytest.mark.skipif(LUA is None, reason="no Lua interpreter")
def test_a_wide_tag_keeps_its_low_bits(tmp_path: Path) -> None:
	"""An eight-byte tag whose low three bits say wire type 4, a group,
	which the schema refuses: the walk stops after the first item. The
	value saturated for a length read 0 there, so another item was found
	where the walker stops, and only the exact value's bits say 4."""
	_, rows = dissect(tmp_path, PROTOBUF, "proto_message",
	                  bytes.fromhex("0801" "ac9db5ce80b3b15d" "3001"))
	assert rows == [("proto_message.fields", 0, 2, "0801")]


@pytest.mark.skipif(shutil.which("tshark") is None, reason="no tshark")
def test_coap_options_are_wiresharks_options(tmp_path: Path) -> None:
	"""Every vector in `coap.vectors`, beside Wireshark's CoAP dissector
	over UDP: as many items as it finds options, each one header byte and
	the value length it reads."""
	subprocess.run([sys.executable, "-m", "situc", "gen-dissector",
	                str(ROOT / "example/coap/coap.situ"), "--out",
	                str(tmp_path)], check=True, cwd=ROOT, capture_output=True)
	packets = [bytes.fromhex("".join(line.split()[2:])) for line in
	           (ROOT / "example/coap/coap.vectors")
	           .read_text(encoding="ascii").splitlines()
	           if line.startswith("coap ")]
	frames = []
	for one in packets:
		udp = pack(">HHHH", 40000, 5683, 8 + len(one), 0) + one
		frames.append(pack(">BBHHHBBH4s4s", 0x45, 0, 20 + len(udp), 0, 0,
		                   64, 17, 0, bytes([127, 0, 0, 1]),
		                   bytes([127, 0, 0, 1])) + udp)
	for name, link, held in (("situ", 147, packets), ("own", 101, frames)):
		(tmp_path / f"{name}.pcap").write_bytes(
			pack("<IHHiIII", 0xa1b2c3d4, 2, 4, 0, 0, 65535, link)
			+ b"".join(pack("<IIII", 0, 0, len(one), len(one)) + one
			           for one in held))

	ours = subprocess.run(
		["tshark", "-X", f"lua_script:{tmp_path / 'coap.lua'}", "-o",
		 'uat:user_dlts:"User 0 (DLT=147)","situ_coap_coap","0","","0",""',
		 "-r", str(tmp_path / "situ.pcap"), "-T", "fields",
		 "-e", "situ.coap.options"],
		check=True, capture_output=True, text=True, timeout=120)
	theirs = subprocess.run(
		["tshark", "-r", str(tmp_path / "own.pcap"), "-T", "fields",
		 "-e", "coap.opt.length"],
		check=True, capture_output=True, text=True, timeout=120)

	shown  = [[len(bytes.fromhex(item)) - 1 for item in line.split(",")]
	          if line else [] for line in ours.stdout.split("\n")[:-1]]
	lengths = [[int(one) for one in line.split(",")] if line else []
	           for line in theirs.stdout.split("\n")[:-1]]
	assert shown == lengths == [[4], [1], [4, 3], [], []]
