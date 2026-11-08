"""A length read past a variable member, in the dissector (26.934).

A field a later length reads, standing where only the walk knows -- DNS's
`rdlength`, after an owner name -- was read by the dissector's body and
not by an extent function, which has no cursor of its own. So `resource`
had no extent, a count of resources was declined, and a reply showed its
question and none of its answers. The extent captures it at the running
`n` now, as the body does at `at`, and a member's arguments are among
what is captured.

A variant's arms were measured from the struct's start inside an extent,
where every other member is measured from where it starts. DNS's arms are
sized by argument, so nothing there showed it; `late` below measures an
arm from its own bytes after a variable member.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from struct import pack

import pytest

from test_dissector import LUA, compare, dissect

ROOT = Path(__file__).resolve().parents[2]

SCHEMA = """target buffer;
endian big;
struct inner { u8 n [max = 6]; u8 b[n]; }
struct late {
	u8 k;
	u8 m [max = 6];
	u8 pre[m];
	variant v switch (k) { case 1: inner one; default: u8 raw; }
	u8 after;
}
struct outer { u8 count [max = 3]; late items[count]; u8 tail; }
"""


@pytest.mark.skipif(LUA is None, reason="no Lua interpreter")
def test_an_arm_after_a_variable_member_agrees_with_the_walker(
		tmp_path: Path) -> None:
	schema = tmp_path / "late.situ"
	schema.write_text(SCHEMA, encoding="ascii")
	result = compare(schema)
	assert not result.differ, "\n".join(result.differ[:5])
	assert result.compared > 0


@pytest.mark.skipif(LUA is None, reason="no Lua interpreter")
def test_an_arm_is_measured_where_it_starts(tmp_path: Path) -> None:
	"""Two `late` records, the first selecting `inner` behind two bytes of
	`pre`: its length byte is 3 where the record's first byte is 1, so an
	arm measured from the record's start is two bytes where it is four.
	Measured that way the dissector consumed 9 bytes of these 15. Offsets
	worked by hand."""
	schema = tmp_path / "late.situ"
	schema.write_text(SCHEMA, encoding="ascii")
	packet = bytes.fromhex("02" "0102aabb" "03112233" "44"
	                       "0100" "00" "55" "99")
	consumed, rows = dissect(tmp_path, schema, "outer", packet)

	assert consumed == 15
	assert ("outer.tail", 14, 1, "153") in rows
	assert [row for row in rows if row[0] == "late.after"] == [
		("late.after", 9, 1, "68"), ("late.after", 13, 1, "85")]


@pytest.mark.skipif(shutil.which("tshark") is None, reason="no tshark")
def test_a_reply_reads_as_wiresharks_own_dns(tmp_path: Path) -> None:
	"""`every_arm` from `dnsname.vectors`, five resources of five types,
	beside Wireshark's DNS dissector reading the same bytes over UDP."""
	subprocess.run([sys.executable, "-m", "situc", "gen-dissector",
	                str(ROOT / "example/dnsname/dnsname.situ"), "--out",
	                str(tmp_path)], check=True, cwd=ROOT, capture_output=True)
	line = next(one for one in (ROOT / "example/dnsname/dnsname.vectors")
	            .read_text(encoding="ascii").splitlines()
	            if one.startswith("message"))
	message = bytes.fromhex("".join(line.split()[2:]))
	udp = pack(">HHHH", 53, 40000, 8 + len(message), 0) + message
	ip  = pack(">BBHHHBBH4s4s", 0x45, 0, 20 + len(udp), 0, 0, 64, 17, 0,
	           bytes([127, 0, 0, 1]), bytes([127, 0, 0, 1])) + udp
	for name, link, frame in (("situ", 147, message), ("own", 101, ip)):
		(tmp_path / f"{name}.pcap").write_bytes(
			pack("<IHHiIII", 0xa1b2c3d4, 2, 4, 0, 0, 65535, link)
			+ pack("<IIII", 0, 0, len(frame), len(frame)) + frame)

	def fields(command: list[str], names: list[str]) -> list[str]:
		ran = subprocess.run(
			[*command, "-T", "fields",
			 *(arg for name in names for arg in ("-e", name))],
			check=True, capture_output=True, text=True, timeout=120)
		return ran.stdout.strip().split("\t")

	ours = fields(["tshark", "-X", f"lua_script:{tmp_path / 'dnsname.lua'}",
	               "-o", 'uat:user_dlts:"User 0 (DLT=147)",'
	               '"situ_dnsname_message","0","","0",""',
	               "-r", str(tmp_path / "situ.pcap")],
	              ["situ.resource.rtype", "situ.resource.ttl",
	               "situ.resource.rdlength", "situ.mail_exchange.preference",
	               "situ.ipv4_address.octets"])
	theirs = fields(["tshark", "-r", str(tmp_path / "own.pcap")],
	                ["dns.resp.type", "dns.resp.ttl", "dns.resp.len",
	                 "dns.mx.preference", "dns.a"])

	assert ours[:4] == theirs[:4]
	assert ours[0] == "5,1,28,15,16"
	# situ shows the address's four bytes; Wireshark, the dotted quad.
	assert ".".join(str(byte) for byte in bytes.fromhex(ours[4])) \
		== theirs[4] == "192.0.2.1"
