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


#: openmlx4's VPD (inbox 20261122T1432Z): the RV keyword inside VPD-R's
#: keyword RUN, which sits in an arm of a variant that states its size.
VPD_RUN = """target buffer;
endian little;
bit_order msb_first;

codec checksum8 { kernel = byte_sum(width = 8, negate); }
impl checksum8 derived;

/* RV's data: the checksum byte, then reserved bytes to the length. */
struct vpd_rv {
	u8   length [min = 1];
	checksum u8 sum[1] covers(before) is checksum8;
	u8   reserved[length - 1];
}

struct vpd_value {
	u8   length;
	u8   data[length];
}

/* A keyword field of VPD-R. The two ASCII characters are read as one
 * little-endian u16, so "RV" is 0x5652. */
struct vpd_ro_field {
	u16  keyword;
	variant field switch (keyword) {
		case 0x5652: vpd_rv    rv;
		default:     vpd_value value;
	}
}

/* `name` is the large resource's seven-bit name: 0x02 the identifier,
 * 0x10 VPD-R, 0x11 VPD-W. */
struct vpd_large_body {
	parameter u8 name;
	u16  length;
	variant data[length] switch (name) {
		case 0x10: vpd_ro_field fields[remaining];
		default:   u8 bytes[remaining];
	}
}

struct vpd_resource [allow_straddle] {
	bit  is_large;
	u4   item_high;
	u3   item_low;
	variant body switch (is_large) {
		case 1:  vpd_large_body(name = item_high * 8 + item_low) large;
		default: u8 small_data[item_low];
	}
}

struct vpd {
	vpd_resource items[] while (is_large == 1 || item_high != 0x0F) max 32768;
}
"""

VPD_GOOD = ("82 0E 00 43 6F 6E 6E 65 63 74 58 2D 33 20 50 72 6F 90 12 00 50 4E"
            " 0B 4D 43 58 33 35 34 41 2D 46 43 42 52 56 01 EC 78")


def test_a_tag_in_a_run_inside_an_arm_is_checked(tmp_path: Path,
		capsys: pytest.CaptureFixture[str]) -> None:
	"""The Python reader had no accessor for a run of structs in an arm,
	so `verify` named RV unchecked and the flipped vector conformed; the
	run is walked now, as `validate` walks it (26.1155). The identifier
	resource before VPD-R selects the other arm, whose run is not there."""
	flipped = VPD_GOOD[:-5] + "ED 78"
	out = _verify(tmp_path, VPD_RUN,
	              f"vpd good {VPD_GOOD}\n"
	              f"vpd flipped {flipped}\n", capsys)
	assert "ChecksumError: vpd_rv.sum: stored 0xed, computed 0xec" in out, out
	assert "1 of 2 vectors do not conform" in out, out
	assert "not checked" not in out, out


@pytest.mark.parametrize("target", ["c", "cpp", "rust", "python"])
def test_no_parent_setter_writes_a_run_element_s_member(tmp_path: Path,
		target: str) -> None:
	"""`set_data_fields_keyword` wrote two bytes at offset 0 -- over
	`length` -- in Python, C++ and Rust: a member of a run's element has
	one place per element and none the parent can name (26.1155)."""
	from situc.cli import main as cli

	(tmp_path / "vpd.situ").write_text(VPD_RUN, encoding="ascii")
	assert cli(["build", "--target", target, "--layer", "edit", "--out",
	            str(tmp_path / "out"), str(tmp_path / "vpd.situ")]) in (0, None)
	text = "".join(path.read_text(encoding="ascii")
	               for path in (tmp_path / "out").iterdir() if path.is_file())
	assert "set_data_fields_keyword" not in text


FIXED_RUN = """target buffer;
endian big;

struct pair { u8 a; u8 b; }

struct fixed_arm {
	u8 size;
	u8 kind;
	variant body[size] switch (kind) {
		case 0:  pair items[remaining];
		default: u8   raw[remaining];
	}
}
"""


def test_a_run_of_fixed_elements_in_a_sized_arm_is_walked(tmp_path: Path,
		capsys: pytest.CaptureFixture[str]) -> None:
	"""The arm-run walk of 26.1155 read each element's `_extent`, which a
	FIXED struct's view does not have: `verify` raised AttributeError on
	openmlx4's MFA TOC, a run of 24-byte entries in a sized arm, and its
	`make check` went red (inbox 20261122T1500Z)."""
	out = _verify(tmp_path, FIXED_RUN,
	              "fixed_arm one 02 00 01 02\n"
	              "fixed_arm other 02 07 aa bb\n", capsys)
	assert "2 vectors conform" in out, out


def test_a_sized_variant_frames_its_run_whole(tmp_path: Path,
		capsys: pytest.CaptureFixture[str]) -> None:
	"""Python's `required` framed `body[size]` through its arm: a run of
	pairs needed ONE pair, so two pairs were called surplus, and a default
	arm took the rest of what had arrived. C and Rust frame by the size,
	and so does Python now (26.1157, openmlx4 inbox 20261122T1445Z)."""
	out = _verify(tmp_path, FIXED_RUN,
	              "fixed_arm two 04 00 01 02 03 04\n"
	              "fixed_arm raw 02 07 aa bb\n", capsys)
	assert "2 vectors conform" in out, out
	out = _verify(tmp_path, FIXED_RUN, "fixed_arm long 02 07 aa bb cc\n",
	              capsys)
	assert "1 are surplus" in out, out


CODED = """target buffer;
endian little;

codec xz {
	expansion = unbounded;
	granularity = stream;
	not seekable;
	invertible;
}
impl xz extern "lzma";

struct rec { u8 n [max = 3]; u8 v[n]; }

struct packed { u8 kind; coded stream(xz) { rec records[remaining]; } }
struct opaque { u8 kind; coded stream(xz) { u8 data[remaining]; } }
"""


def test_a_coded_region_s_records_are_named_unread(tmp_path: Path,
		capsys: pytest.CaptureFixture[str]) -> None:
	"""A record inside `coded stream(xz)` is in decoded bytes, and a change
	to one conformed with no word said (openmlx4, inbox 20261122T1445Z).
	Where `verify` has no decoder -- an `extern` Python's library does not
	carry -- it names the region (26.1158); one holding bytes alone has
	nothing to read and is not named."""
	brotli = CODED.replace('extern "lzma"', 'extern "brotli"')
	out = _verify(tmp_path, brotli, "packed one 01 aa bb\n", capsys)
	assert "except inside arrays or coded regions" in out, out
	assert ("`packed.stream`: coded by `xz`, which `verify` does not "
	        "decode") in out, out
	out = _verify(tmp_path, brotli, "opaque one 01 aa bb\n", capsys)
	assert "coded by" not in out, out
	assert "1 vectors conform" in out, out


NESTED_CODED = CODED + """
struct xz_recs { coded stream(xz) { rec records[remaining]; } }

struct section {
	u8 flag;
	u8 size;
	variant payload[size] switch (flag) {
		case 0:  rec records[remaining];
		case 1:  xz_recs packed;
		default: error;
	}
}
"""


def test_an_lzma_region_is_decoded_and_its_records_read(tmp_path: Path,
		capsys: pytest.CaptureFixture[str]) -> None:
	"""`extern "lzma"` is a decoder Python carries, so `verify` decodes the
	region and reads each record in it (26.1160): a record breaking `[max
	= 3]` is refused, through a struct in a sized arm as openmlx4's MFA map
	has it, and bytes that are no xz stream are the vector's refusal."""
	import lzma

	good = lzma.compress(bytes([1, 0xAA, 2, 0xBB, 0xCC]))
	bad  = lzma.compress(bytes([5, 1, 2, 3, 4, 5]))
	out = _verify(tmp_path, NESTED_CODED,
	              f"packed good 01 {good.hex(' ')}\n"
	              f"packed bad 01 {bad.hex(' ')}\n"
	              "packed junk 01 aa bb\n"
	              f"section good 01 {len(good):02x} {good.hex(' ')}\n"
	              f"section bad 01 {len(bad):02x} {bad.hex(' ')}\n", capsys)
	assert out.count("ConstraintError: rec.n is 5, max 3") == 2, out
	assert "do not decode as `lzma`" in out, out
	assert "3 of 5 vectors do not conform" in out, out
	assert "coded by" not in out, out
