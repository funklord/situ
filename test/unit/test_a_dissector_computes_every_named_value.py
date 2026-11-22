"""The dissector shows every named value, in Wireshark's `UInt64` (26.1112).

26.1060 showed a `let` only where Lua's doubles are exact -- an `|` of
shifted fields -- and declined a subtraction, a division, a variable shift
or a wrapping product. Every Wireshark's Lua has `UInt64`, which computes
modulo 2^64 as the backends do, so those are shown too, with the guards
every reader uses: `/` and `%` by zero are 0, a shift by 64 or more is 0.

The values below are worked by hand rather than taken from another reader:
the walker comparison in `test_dissector` holds the stub to the walker, and
this holds both the stub and real tshark to arithmetic done on paper.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from situc.dissector import generate
from situc.parser import parse
from situc.cli import analyse
from test_dissector import LUA, read_back

SCHEMA = """target buffer;
endian big;

struct assembled {
	u16 high;
	u32 low;
	i8  a;
	u8  b;
	u8  zero;
	u8  big;
	let u64 offset   = (high << 32) | low;
	let i16 diff     = a - b;
	let u32 quotient = low / zero;
	let u32 rest     = low % b;
	let u64 shifted  = low << big;
	let u16 down     = high >> (big & 7);
	let u8  wrapped  = a * b;
	let u64 flipped  = ~low ^ 0xFFFF000000000000;
	let i32 negated  = -a;
	let u8  m        = b & 3;
}
"""

#: high 1, low 2, a -3, b 5, zero 0, big 70.
PACKET = bytes.fromhex("0001 00000002 fd 05 00 46")

#: Each value, worked by hand.
WANT = {
	"offset":   "4294967298",		# 1 << 32 | 2
	"diff":     "-8",			# -3 - 5, held to sixteen bits
	"quotient": "0",			# by zero
	"rest":     "2",			# 2 % 5
	"shifted":  "0",			# a shift by 70
	"down":     "0",			# 1 >> 6
	"wrapped":  "241",			# -15 held to eight bits
	"flipped":  "281474976710653",	# ~2 ^ 0xFFFF000000000000
	"negated":  "3",
	"m":        "1",			# 5 & 3
}


def _lua(tmp_path: Path) -> Path:
	schema = tmp_path / "assembled.situ"
	schema.write_text(SCHEMA, encoding="ascii")
	source, resolved, _ = analyse(schema)
	lua = tmp_path / "assembled.lua"
	lua.write_text(generate(parse(source), resolved, "assembled"),
	               encoding="ascii")
	return lua


@pytest.mark.skipif(LUA is None, reason="no Lua interpreter")
def test_every_named_value_is_shown_as_worked_by_hand(tmp_path: Path) -> None:
	lua = _lua(tmp_path)
	assert "not shown" not in lua.read_text(encoding="ascii")
	_, rows = read_back(lua, "assembled", PACKET)
	shown = {field.rpartition(".")[2]: value
	         for field, _, _, value in rows if field.startswith("assembled.")}
	assert {name: shown.get(name) for name in WANT} == WANT


TSHARK    = shutil.which("tshark")
TEXT2PCAP = shutil.which("text2pcap")


@pytest.mark.skipif(TSHARK is None or TEXT2PCAP is None,
                    reason="needs tshark and text2pcap")
def test_wireshark_computes_them_too(tmp_path: Path) -> None:
	"""The real consumer, whose `UInt64` the stub only stands in for."""
	script = _lua(tmp_path)
	script.write_text(script.read_text(encoding="ascii")
	                  + 'DissectorTable.get("wtap_encap"):add(wtap.USER0,'
	                  " assembled)\n", encoding="ascii")
	dump = tmp_path / "packet.hex"
	dump.write_text("0000 " + PACKET.hex(" ") + "\n", encoding="ascii")
	capture = tmp_path / "packet.pcap"
	subprocess.run([TEXT2PCAP or "text2pcap", "-q", "-l", "147",
	                str(dump), str(capture)],
	               check=True, capture_output=True)
	shown = subprocess.run([TSHARK or "tshark", "-r", str(capture), "-X",
	                        f"lua_script:{script}", "-T", "fields",
	                        *[arg for name in WANT
	                          for arg in ("-e", f"situ.assembled.{name}")]],
	                       capture_output=True, text=True, timeout=120)
	assert "Lua: Error" not in shown.stderr, shown.stderr
	assert shown.stdout.strip().split("\t") == list(WANT.values()), \
		shown.stdout


WIDE = """target buffer;
endian big;

struct widened {
	u64 x;
	u40 y;
	i64 s;
	let u64 next = x + 1;
	let u64 down = y >> 8;
	let u32 low  = y & 0xFFFFFFFF;
	let i64 less = s - 1;
	let u64 mix  = x ^ y;
}
"""

#: x 2^53 + 1, y 0x0100000005, s -2.
WIDE_PACKET = bytes.fromhex("0020000000000001 0100000005 fffffffffffffffe")

#: Worked by hand: x + 1 is the first sum a double gets wrong.
WIDE_WANT = {
	"next": "9007199254740994",
	"down": "16777216",
	"low":  "5",
	"less": "-3",
	"mix":  "9007203549708292",
}


def _wide_lua(tmp_path: Path) -> Path:
	schema = tmp_path / "widened.situ"
	schema.write_text(WIDE, encoding="ascii")
	source, resolved, _ = analyse(schema)
	lua = tmp_path / "widened.lua"
	lua.write_text(generate(parse(source), resolved, "widened"),
	               encoding="ascii")
	return lua


@pytest.mark.skipif(LUA is None, reason="no Lua interpreter")
def test_a_named_value_over_a_wide_field_is_exact(tmp_path: Path) -> None:
	"""A field wider than 32 bits read as a Lua number is a double, inexact
	past 2^53, so a `let` over one was declined; its two words are put
	together in a `UInt64` now (26.1151)."""
	lua = _wide_lua(tmp_path)
	assert "not shown" not in lua.read_text(encoding="ascii")
	_, rows = read_back(lua, "widened", WIDE_PACKET)
	shown = {field.rpartition(".")[2]: value
	         for field, _, _, value in rows if field.startswith("widened.")}
	assert {name: shown.get(name) for name in WIDE_WANT} == WIDE_WANT


@pytest.mark.skipif(TSHARK is None or TEXT2PCAP is None,
                    reason="needs tshark and text2pcap")
def test_wireshark_computes_the_wide_ones_too(tmp_path: Path) -> None:
	script = _wide_lua(tmp_path)
	script.write_text(script.read_text(encoding="ascii")
	                  + 'DissectorTable.get("wtap_encap"):add(wtap.USER0,'
	                  " widened)\n", encoding="ascii")
	dump = tmp_path / "packet.hex"
	dump.write_text("0000 " + WIDE_PACKET.hex(" ") + "\n", encoding="ascii")
	capture = tmp_path / "packet.pcap"
	subprocess.run([TEXT2PCAP or "text2pcap", "-q", "-l", "147",
	                str(dump), str(capture)],
	               check=True, capture_output=True)
	shown = subprocess.run([TSHARK or "tshark", "-r", str(capture), "-X",
	                        f"lua_script:{script}", "-T", "fields",
	                        *[arg for name in WIDE_WANT
	                          for arg in ("-e", f"situ.widened.{name}")]],
	                       capture_output=True, text=True, timeout=120)
	assert "Lua: Error" not in shown.stderr, shown.stderr
	assert shown.stdout.strip().split("\t") == list(WIDE_WANT.values()), \
		shown.stdout
