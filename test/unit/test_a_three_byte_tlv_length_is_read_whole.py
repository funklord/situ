"""A `tlv` region's `u24` length or tag is three bytes in every reader.

C and C++ read a length prefix or a tag of a width with no `get_` helper
of its own -- three bytes -- as its first byte alone, so `00 00 08` was a
length of zero there and eight in Rust and Python. openmlx4's FS2 image
info region is `length_type = u24` (inbox 20261123T1138Z), and C read its
every item wrong. The verdicts are worked by hand, in both byte orders.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import fourway

REGION = """target buffer;
endian ORDER;
struct info {
	tlv items (tag_type = TAG, length_type = u24);
}
"""

#: (order, tag type, packet, valid): one item of two bytes, a length
#: whose high byte alone would read as zero or as huge, and a value cut.
CASES = [
	("big", "u8", "01 000002 aabb", True),
	("big", "u8", "01 000102" + "cc" * 258, True),
	("big", "u8", "01 000003 aabb", False),
	("big", "u8", "01 000002 aabb 02 000000", True),
	("little", "u8", "01 020000 aabb", True),
	("little", "u8", "01 020100" + "cc" * 258, True),
	("little", "u8", "01 030000 aabb", False),
	("big", "u24", "000001 000002 aabb", True),
	("big", "u24", "000001 000003 aabb", False),
]


@pytest.mark.skipif(not fourway.COMPLETE, reason="needs all four toolchains")
@pytest.mark.parametrize(("order", "tag"), [("big", "u8"), ("little", "u8"),
                                            ("big", "u24")])
def test_the_four_read_three_bytes(tmp_path: Path, order: str,
                                   tag: str) -> None:
	source = tmp_path / "unit.situ"
	source.write_text(REGION.replace("ORDER", order).replace("TAG", tag),
	                  encoding="ascii")
	command = fourway.build(tmp_path, source)
	assert len(command) == 4
	for held_order, held_tag, packet, valid in CASES:
		if (held_order, held_tag) != (order, tag):
			continue
		data  = bytes.fromhex(packet.replace(" ", ""))
		given = {name: fourway.answers(argv, data, tmp_path)
		         for name, argv in command.items()}
		assert len(set(given.values())) == 1, (packet, given)
		assert ("validate 0" in given["c"]) == valid, (packet, given["c"])


@pytest.mark.skipif(not fourway.COMPLETE, reason="needs a C compiler")
@pytest.mark.parametrize(("order", "tag"), [("big", "u8"), ("little", "u8"),
                                            ("big", "u24")])
def test_both_walkers_read_three_bytes(tmp_path: Path, order: str,
                                       tag: str) -> None:
	"""The walkers agree with each other, and where they answer, with the
	verdict: the image describes no three-byte width, so today they say
	`cannot-say` rather than read one byte as C did."""
	from test_walker_c import (c_verdict, packed_text, python_verdict,
	                           shape_named_text)
	text  = REGION.replace("ORDER", order).replace("TAG", tag)
	blob  = packed_text(text)
	shape = shape_named_text(text, "info")
	for held_order, held_tag, packet, valid in CASES:
		if (held_order, held_tag) != (order, tag):
			continue
		data = bytes.fromhex(packet.replace(" ", ""))
		said = python_verdict(blob, data, shape)
		assert said == "cannot-say" or (said == "0") == valid, (packet, said)
		assert c_verdict(tmp_path, blob, data, shape) == said, packet
