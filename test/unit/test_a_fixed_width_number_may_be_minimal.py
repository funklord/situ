"""`[minimal]` on a fixed-width text number, in all four and both walkers.

C++ checked the spelling through `n_offset()`, an accessor no member at a
constant offset has, so `decimal u32 n[4] [minimal]` first in a struct
gave a header that did not compile (26.953). Found by building every text
number shape -- three radices, three forms, four kinds -- through every
generator and then through all six readers: this was the one disagreement.
No corpus schema has the shape: cpio's fixed numbers carry no `[minimal]`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import fourway
from test_walker_c import (COMPILER, c_verdict, packed_text, python_verdict,
                           shape_named_text)

SCHEMA = """target buffer;
endian big;
struct s {
	decimal u32  n[4] [minimal];
	u8           k;
}
"""

#: A fixed width has to be filled, so `[minimal]` there admits only the
#: numbers that need every digit: `1234` and never `0012`.
CASES = {
	"every digit needed": ("0", "n ok=1 value=1234", b"1234k"),
	"a leading zero":     ("2", "n ok=0 value=0",    b"0012k"),
}


def test_all_four_check_a_fixed_width_spelling(tmp_path: Path) -> None:
	schema = tmp_path / "unit.situ"
	schema.write_text(SCHEMA, encoding="ascii")
	command = fourway.build(tmp_path, schema)
	for label, (verdict, line, packet) in CASES.items():
		given = {name: fourway.answers(argv, packet, tmp_path)
		         for name, argv in command.items()}
		first = next(iter(given.values()))
		assert all(answer == first for answer in given.values()), \
			(label, given)
		lines = first.splitlines()
		assert f"validate {verdict}" in lines, f"{label}: {lines!r}"
		assert line in lines or verdict != "0", f"{label}: {lines!r}"


@pytest.mark.skipif(COMPILER is None, reason="no C compiler")
def test_both_walkers_check_a_fixed_width_spelling(tmp_path: Path) -> None:
	blob  = packed_text(SCHEMA)
	shape = shape_named_text(SCHEMA, "s")
	for label, (verdict, _line, packet) in CASES.items():
		assert python_verdict(blob, packet, shape) == verdict, label
		assert c_verdict(tmp_path, blob, packet, shape) == verdict, label
