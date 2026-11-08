"""The dissector follows a struct that contains itself, to its depth (26.936).

A struct in a recursion cycle had no extent here, "this file carries no
depth", so CBOR showed the first array's head and stopped, and so did JSON
and the s-expressions. The cycle's extent functions are declared ahead of
each other now, and each counts how deep it is and measures nothing past
its struct's `[limit]` or `[depth]`, which ends the walk as any element of
no extent does.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from struct import pack

import pytest

from test_dissector import LUA, dissect

ROOT  = Path(__file__).resolve().parents[2]
CBOR  = ROOT / "example" / "cbor" / "cbor.situ"


@pytest.mark.skipif(LUA is None, reason="no Lua interpreter")
def test_nesting_is_followed_to_the_depth_and_no_further(
		tmp_path: Path) -> None:
	"""`item` is `[depth = 64]`: sixty arrays around a 7 are shown to the
	7, and a hundred stop at the sixty-fourth array, with no error."""
	_, rows = dissect(tmp_path, CBOR, "item", bytes([0x81]) * 60 + b"\x07")
	assert sum(row[0] == "any_array.lead" for row in rows) == 60
	assert ("unsigned_integer.value", 60, 1, "7") in rows

	_, rows = dissect(tmp_path, CBOR, "item", bytes([0x81]) * 100 + b"\x07")
	assert sum(row[0] == "any_array.lead" for row in rows) == 64
	assert not any(row[0] == "unsigned_integer.value" for row in rows)


@pytest.mark.skipif(shutil.which("tshark") is None, reason="no tshark")
def test_wireshark_shows_a_nested_array_and_a_map(tmp_path: Path) -> None:
	"""`nested_array` and `mixed_map` from `cbor.vectors`: [1, [2, 3],
	[4, 5]] and {"a": 1, "b": [2, 3]}, every value and key shown."""
	subprocess.run([sys.executable, "-m", "situc", "gen-dissector",
	                str(CBOR), "--out", str(tmp_path)],
	               check=True, cwd=ROOT, capture_output=True)
	frames = [bytes.fromhex("8301820203820405"),
	          bytes.fromhex("a26161016162820203")]
	(tmp_path / "cbor.pcap").write_bytes(
		pack("<IHHiIII", 0xa1b2c3d4, 2, 4, 0, 0, 65535, 147)
		+ b"".join(pack("<IIII", 0, 0, len(one), len(one)) + one
		           for one in frames))
	ran = subprocess.run(
		["tshark", "-X", f"lua_script:{tmp_path / 'cbor.lua'}", "-o",
		 'uat:user_dlts:"User 0 (DLT=147)","situ_cbor_item","0","","0",""',
		 "-r", str(tmp_path / "cbor.pcap"), "-T", "fields",
		 "-e", "situ.unsigned_integer.value", "-e", "situ.text_string.text"],
		check=True, capture_output=True, text=True, timeout=120)
	# The numbers, not their bytes: a varint is shown as what it spells
	# (26.941), and Wireshark takes the 64-bit value the dissector builds.
	assert ran.stdout.splitlines() == ["1,2,3,4,5\t", "1,2,3\t61,62"]
