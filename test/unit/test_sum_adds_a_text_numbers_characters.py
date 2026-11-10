"""`sum()` over a text number adds its characters (26.949).

A text number's width is digits, not elements (8.6.2), and every backend
summed it as elements of its scalar anyway: `sum(n)` over `decimal u16
n[3]` read three `u16`s, six bytes from a three-byte field. A size cannot
name a text number in `sum` -- 26.896 refuses it -- but a `when` can, and
ustar's header checksum is a sum over the header's bytes, most of them
digits. Held in all four backends and both walkers.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import fourway
from test_walker_c import (COMPILER, c_verdict, packed_text, python_verdict,
                           shape_named_text)

#: `k` must be the sum of `n`'s three characters; the two bytes after it
#: are what a six-byte read would have added.
SCHEMA = """target buffer;
endian big;
struct s {
	decimal u16  n[3];
	u16          k;
	u8           tail[2];
}
when s.k != sum(s.n) refuse wrong_sum "`k` is not the sum of `n`'s digits";
"""

#: '1' + '2' + '3' is 0x96. A six-byte read of `n` as `u16`s would add
#: 0x3132 + 0x3300 + 0x9600 and refuse the first.
CASES = {
	"the characters' sum":  ("0", b"123\x00\x96\x00\x00"),
	"one more than it":     ("2", b"123\x00\x97\x00\x00"),
	"the elements' sum":    ("2", b"123" + (0x3132 + 0x3300 + 0x0096)
	                        .to_bytes(2, "big") + b"\x96\x00"),
}


def test_all_four_add_characters(tmp_path: Path) -> None:
	schema = tmp_path / "unit.situ"
	schema.write_text(SCHEMA, encoding="ascii")
	command = fourway.build(tmp_path, schema)
	for label, (verdict, packet) in CASES.items():
		given = {name: fourway.answers(argv, packet, tmp_path)
		         for name, argv in command.items()}
		first = next(iter(given.values()))
		assert all(answer == first for answer in given.values()), \
			(label, given)
		assert f"validate {verdict}" in first.splitlines(), \
			f"{label}: {first!r}"


@pytest.mark.skipif(COMPILER is None, reason="no C compiler")
def test_both_walkers_add_characters(tmp_path: Path) -> None:
	blob  = packed_text(SCHEMA)
	shape = shape_named_text(SCHEMA, "s")
	for label, (verdict, packet) in CASES.items():
		assert python_verdict(blob, packet, shape) == verdict, label
		assert c_verdict(tmp_path, blob, packet, shape) == verdict, label
