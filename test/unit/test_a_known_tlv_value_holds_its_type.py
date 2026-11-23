"""A `known` tlv value is a whole number of its `type`, each holding the
type's own checks (26.1167).

openmlx4 (inbox 20261123T1138Z) measured an 8-byte struct verifying in a
3-byte value, `type = u32` verifying with 2 or 10 bytes, and a date whose
BCD year its struct refuses verifying inside an image-info region. `type
= T` is the element type filling the item's length -- protobuf's
`username` is `type = u8` over a string -- so 12 bytes of `u32` are three
and 16 bytes of a date are two. The verdicts are worked by hand.

The struct a value holds is declared after the region on purpose: C and
C++ have to name its validator before its definition.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import fourway

SCHEMA = """target buffer;
endian big;
struct image_info {
	tlv tags (
		tag_type = u8, length_type = u16,
		known = {
			1 : { name = version,    type = u8 },
			2 : { name = build_time, type = build_time },
			3 : { name = area,       type = u32 },
		},
		unknown = skip
	);
}
struct build_time { u32 time_of_day; bcd4 year; bcd2 month; bcd2 day; }
"""

DATE = "00000000 20170928"
BINARY_YEAR = "00000000 07e1091c"

#: (label, packet, valid)
CASES = [
	("a date", "02 0008" + DATE, True),
	("a binary year", "02 0008" + BINARY_YEAR, False),
	("a date in three bytes", "02 0003 000000", False),
	("two dates", "02 0010" + DATE + DATE, True),
	("a date and a binary year", "02 0010" + DATE + BINARY_YEAR, False),
	("a u32 in two bytes", "03 0002 0000", False),
	("a u32 in ten bytes", "03 000a" + "00" * 10, False),
	("three u32", "03 000c" + "00" * 12, True),
	("sixteen bytes of u8", "01 0010" + "41" * 16, True),
	("an unknown tag, skipped", "04 0003 aabbcc", True),
	("a good item, then a bad one", "01 0001 41 03 0003 000000", False),
]


@pytest.mark.skipif(not fourway.COMPLETE, reason="needs all four toolchains")
def test_the_four_hold_a_value_to_its_type(tmp_path: Path) -> None:
	source = tmp_path / "unit.situ"
	source.write_text(SCHEMA, encoding="ascii")
	command = fourway.build(tmp_path, source)
	assert len(command) == 4
	for label, packet, valid in CASES:
		data  = bytes.fromhex(packet.replace(" ", ""))
		given = {name: fourway.answers(argv, data, tmp_path)
		         for name, argv in command.items()}
		assert len(set(given.values())) == 1, (label, given)
		region = given["c"].split("-- image_info")[1].split("\n-- ")[0]
		assert ("validate 0" in region) == valid, (label, region)


@pytest.mark.skipif(not fourway.COMPLETE, reason="needs a C compiler")
def test_the_walkers_decline_rather_than_pass(tmp_path: Path) -> None:
	"""The image carries no element type, so both walkers say they cannot
	answer -- never `0` for the binary year the four refuse -- as they do
	for the region's other policies (26.738)."""
	from test_walker_c import (c_verdict, packed_text, python_verdict,
	                           shape_named_text)
	blob  = packed_text(SCHEMA)
	shape = shape_named_text(SCHEMA, "image_info")
	for label, packet, _ in CASES:
		data = bytes.fromhex(packet.replace(" ", ""))
		said = python_verdict(blob, data, shape)
		assert said == "cannot-say", (label, said)
		assert c_verdict(tmp_path, blob, data, shape) == said, label
