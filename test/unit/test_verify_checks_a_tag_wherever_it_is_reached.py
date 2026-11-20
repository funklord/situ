"""`verify` checks a tag through nested structs, arms and chain links.

openmlx4 (2026-11-19) found two halves. A checksum one struct down was
reported "not checked (inside ..., which this does not descend into)"; one
reached through a variant ARM was neither checked nor named, because the
walk over a struct's own members leaves a variant's arms out -- a VPD
keyword's RV byte flipped conformed in silence. And a chain of sections,
each with its own CRC, had every link's CRC unchecked: FS2's whole section
list. `verify` now opens a nested struct, the arm the discriminant selects
and a link placed by `at` through their own accessors, over the same
message, and checks their tags there.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from situc.cli import main

VPD = """target buffer;
endian little;

codec vpd_sum { kernel = byte_sum(width = 8, negate); }
impl vpd_sum derived;

struct vpd_rv {
	u8 length [min = 1];
	checksum u8 sum[1] covers(before) is vpd_sum;
	u8 reserved[length - 1];
}
struct vpd_value { u8 length; u8 data[length]; }
struct vpd_keyword {
	u16 keyword;
	variant field switch (keyword) {
		case 0x5652: vpd_rv rv;
		default:     vpd_value value;
	}
}
struct t_direct { u8 a; vpd_keyword k; }
struct t_rv { u8 a; vpd_rv r; }
"""

CHAIN = """target buffer;
endian big;

codec s8 { kernel = byte_sum(width = 8, negate); }
impl s8 derived;

struct end { }
struct sect {
	authenticated body { u8 next; u8 v; }
	checksum u8 sum[1] covers(body) is s8;
}
struct link [limit = 8] {
	sect s;
	variant rest switch (s.next) {
		case 0:  end none;
		default: link following at s.next;
	}
}
"""


def _link(after: int, value: int) -> bytes:
	return bytes([after, value, -(after + value) & 0xFF])


#: Links at 0, 3 and 6, the last ending the chain.
GOOD_CHAIN = _link(3, 1) + _link(6, 2) + _link(0, 3)


def _verify(tmp_path: Path, schema: str, vectors: str,
		capsys: pytest.CaptureFixture[str]) -> str:
	(tmp_path / "s.situ").write_text(schema, encoding="ascii")
	(tmp_path / "v.vectors").write_text(vectors, encoding="ascii")
	main(["verify", str(tmp_path / "s.situ"), str(tmp_path / "v.vectors")])
	return capsys.readouterr().out


def test_a_tag_behind_an_arm_is_checked(tmp_path: Path,
		capsys: pytest.CaptureFixture[str]) -> None:
	"""`covers(before)` from the message's start: 05 52 56 01 sums to
	0xae, so the RV byte is 0x52 -- openmlx4's own figure."""
	out = _verify(tmp_path, VPD,
	              "t_direct good 05 52 56 01 52\n"
	              "t_direct bad 05 52 56 01 53\n"
	              "vpd_keyword good 52 56 01 57\n"
	              "vpd_keyword bad 52 56 01 58\n"
	              "vpd_keyword other 41 42 01 99\n", capsys)
	assert "ChecksumError: vpd_rv.sum: stored 0x53, computed 0x52" in out, out
	assert "ChecksumError: vpd_rv.sum: stored 0x58, computed 0x57" in out, out
	assert "2 of 5 vectors do not conform" in out, out
	assert "not checked" not in out, out


def test_a_tag_one_struct_down_is_checked(tmp_path: Path,
		capsys: pytest.CaptureFixture[str]) -> None:
	out = _verify(tmp_path, VPD, "t_rv bad 05 01 00\n", capsys)
	assert "ChecksumError: vpd_rv.sum: stored 0x0, computed 0xfa" in out, out


def test_every_link_of_a_chain_has_its_tag_checked(tmp_path: Path,
		capsys: pytest.CaptureFixture[str]) -> None:
	"""The third link's sum flipped: only reached by following two links
	placed by `at`."""
	bad = bytearray(GOOD_CHAIN)
	bad[8] ^= 1
	out = _verify(tmp_path, CHAIN,
	              f"link good {GOOD_CHAIN.hex(' ')}\n"
	              f"link bad_third {bytes(bad).hex(' ')}\n", capsys)
	assert "link `bad_third` does not conform" in out, out
	assert "ChecksumError: sect.sum" in out, out
	assert "1 of 2 vectors do not conform" in out, out
	assert "not checked" not in out, out
