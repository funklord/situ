"""A dissector checks the checksums a codec computes (26.1031).

The lens 26.1023 found in `situc verify` -- a consumer holding the whole
message that never asked the checksum's own check -- pointed at the
dissector as well: a generated dissector showed a CRC's bytes and nothing
else, where Wireshark's own dissectors mark one correct or say what it
should be. It now recomputes the tag over the span it covers, with the
backends' arithmetic, and adds a status row: `Good`, or `Bad, should be
0x...` with a CHECKSUM expert on it.

Held to values that are not situ's: PNG's IEND CRC `AE426082` (RFC 2083),
the IPv4 header checksum `b861` of the worked example most references
copy (Wikipedia's "Internet checksum"), and a FLAC frame header's CRC-8 from
the committed corpus, which a real encoder wrote.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from every_schema import ROOT
from situc.cli import analyse
from situc.dissector import generate
from situc.parser import parse
from test_dissector import LUA, read_back, read_back_experts

CASES = [
	# (schema, struct, the packet, the status row, whether it is bad)
	("png", "chunk", "0000000049454E44AE426082", "Good", False),
	("png", "chunk", "0000000049454E44AE426083",
	 "Bad, should be 0xae426082", True),
	("ipv4", "ipv4_header",
	 "45000073000040004011b861c0a80001c0a800c7",
	 "Good", False),
	("ipv4", "ipv4_header",
	 "45000073000040004011b862c0a80001c0a800c7",
	 "Bad, should be 0xb861", True),
	("flac", "frame_header", "fff86408000fce", "Good", False),
	("flac", "frame_header", "fff86408000fcf", "Bad, should be 0xce", True),
]


@pytest.mark.skipif(LUA is None, reason = "no Lua interpreter")
@pytest.mark.parametrize("stem,struct,packet,status,bad", CASES,
                         ids = [f"{one[0]}-{one[3][:3]}" for one in CASES])
def test_the_status_is_the_readers_verdict(tmp_path: Path, stem: str,
		struct: str, packet: str, status: str, bad: bool) -> None:
	path = ROOT / "example" / stem / f"{stem}.situ"
	source, resolved, _ = analyse(path)
	lua = tmp_path / f"{stem}.lua"
	lua.write_text(generate(parse(source), resolved, stem), encoding = "ascii")

	_, rows = read_back(lua, struct, bytes.fromhex(packet))
	shown = [text for field, _, _, text in rows if field.endswith(".status")]
	assert shown == [status], rows

	experts = read_back_experts(lua, struct, bytes.fromhex(packet))
	flagged = [abbrev for abbrev, _, _ in experts if abbrev.endswith(".bad")]
	assert bool(flagged) == bad, experts


TSHARK    = shutil.which("tshark")
TEXT2PCAP = shutil.which("text2pcap")


@pytest.mark.skipif(TSHARK is None or TEXT2PCAP is None,
                    reason = "needs tshark and text2pcap")
def test_wireshark_shows_the_verdict(tmp_path: Path) -> None:
	"""The real consumer, over PNG's IEND, correct and one bit off: its Lua
	is 5.4, where the bitwise operators are the ones loaded from a string."""
	path = ROOT / "example" / "png" / "png.situ"
	source, resolved, _ = analyse(path)
	script = tmp_path / "png.lua"
	script.write_text(generate(parse(source), resolved, "png")
	                  + 'DissectorTable.get("wtap_encap"):add(wtap.USER0,'
	                  " chunk)\n", encoding = "ascii")
	for crc, want in (("ae 42 60 82", "crc status: Good"),
	                  ("ae 42 60 83", "crc status: Bad, should be "
	                                  "0xae426082")):
		dump = tmp_path / "chunk.hex"
		dump.write_text(f"0000 00 00 00 00 49 45 4e 44 {crc}\n",
		                encoding = "ascii")
		capture = tmp_path / "chunk.pcap"
		subprocess.run([TEXT2PCAP or "text2pcap", "-q", "-l", "147",
		                str(dump), str(capture)],
		               check = True, capture_output = True)
		shown = subprocess.run([TSHARK or "tshark", "-r", str(capture), "-X",
		                        f"lua_script:{script}", "-V"],
		                       capture_output = True, text = True,
		                       timeout = 120)
		assert "Lua: Error" not in shown.stderr, shown.stderr
		assert want in shown.stdout, shown.stdout
		assert ("Expert Info (Error/Checksum)" in shown.stdout) \
			== ("Bad" in want), shown.stdout
