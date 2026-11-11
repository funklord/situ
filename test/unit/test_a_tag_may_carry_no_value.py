"""A TLV item that is its tag alone, as DHCP's Pad and End are (26.958).

DHCP options are a tag, a length and a value -- except Pad (0) and End
(255), which are one byte. `case 0: 0` said so and was refused, on the
reason that "a walk over zero-extent values does not advance". It does:
every item has a tag, and the tag is what moves the walk. Item counts
here are worked out by hand, and all four backends and both walkers agree
with them.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import fourway
from test_walker_c import (COMPILER, c_verdict, packed_text, python_verdict,
                           shape_named_text)

SCHEMA = """target buffer;
endian big;
struct options {
	tlv opts (
		tag_type = u8,
		tag_decode = { code = tag },
		tag_identity = code,
		value_size = switch (code) {
			case 0: 0,
			case 255: 0,
			default: prefixed(u8)
		},
		duplicate_tags = allowed,
	);
}
"""

#: The bytes and how many items they are.
CASES = {
	"pads around an option": (bytes.fromhex("00 00 35 01 05 00".replace(" ", "")), 4),
	"an option then End":    (bytes.fromhex("35 01 05 ff".replace(" ", "")), 2),
	"End alone":             (bytes.fromhex("ff"), 1),
	"two options, no pad":   (bytes.fromhex("01 04 ff ff ff 00 03 04 0a 00 00 01".replace(" ", "")), 2),
}


def test_all_four_count_the_items(tmp_path: Path) -> None:
	schema = tmp_path / "unit.situ"
	schema.write_text(SCHEMA, encoding="ascii")
	command = fourway.build(tmp_path, schema)
	for label, (packet, items) in CASES.items():
		given = {name: fourway.answers(argv, packet, tmp_path)
		         for name, argv in command.items()}
		first = next(iter(given.values()))
		assert all(answer == first for answer in given.values()), \
			(label, given)
		lines = first.splitlines()
		assert f"opts count={items}" in lines, f"{label}: {lines!r}"
		assert "validate 0" in lines, f"{label}: {lines!r}"


@pytest.mark.skipif(COMPILER is None, reason="no C compiler")
def test_both_walkers_take_them(tmp_path: Path) -> None:
	blob  = packed_text(SCHEMA)
	shape = shape_named_text(SCHEMA, "options")
	for label, (packet, _items) in CASES.items():
		assert python_verdict(blob, packet, shape) == "0", label
		assert c_verdict(tmp_path, blob, packet, shape) == "0", label
