"""A region whose codec adds a fixed count has a length (26.962).

    codec c { expansion = +4; ... }
    struct s { u8 n; coded body(c) { u16 x; u8 data[n]; } u8 k; }

The region is `2 + n + 4` bytes. `traverse.region_extent` compared the
expansion's spelling with "fixed_add" and the enum spells it "add", so
from d633a094 on every `+N` region had no closed form in any backend:
no accessor for the region, and -- where the interior is sized by the
data -- none for `k` after it either. The "add" branch each backend
carries had never run. Found by a sweep of coded regions over seven
expansions, where every `+4` shape failed to build its differential.

The lengths and `k` here are worked out by hand.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import fourway
from test_walker_c import (COMPILER, c_verdict, packed_text, python_verdict,
                           shape_named_text)

SCHEMA = """target buffer;
endian big;
codec c {
	expansion = +4;
	granularity = byte;
	invertible;
	deterministic;
}
impl c extern "my_c";
struct s {
	u8 n;
	coded body(c) {
		u16 x;
		u8  data[n];
	}
	u8 k;
}
"""


def test_all_four_measure_the_region(tmp_path: Path) -> None:
	schema = tmp_path / "unit.situ"
	schema.write_text(SCHEMA, encoding="ascii")
	command = fourway.build(tmp_path, schema)
	for n in (0, 1, 3):
		wire    = 2 + n + 4
		message = bytes([n]) + bytes(range(1, wire + 1)) + b"\xEE"
		given = {name: fourway.answers(argv, message, tmp_path)
		         for name, argv in command.items()}
		first = next(iter(given.values()))
		assert all(one == first for one in given.values()), given
		lines = first.splitlines()
		assert f"body len={wire} first=1" in lines, (n, lines)
		assert "k 238" in lines, (n, lines)


@pytest.mark.skipif(COMPILER is None, reason="no C compiler")
def test_both_walkers_place_what_follows(tmp_path: Path) -> None:
	blob  = packed_text(SCHEMA)
	shape = shape_named_text(SCHEMA, "s")
	for n in (0, 2):
		whole = bytes([n]) + bytes(2 + n + 4) + b"\x01"
		assert python_verdict(blob, whole, shape) \
			== c_verdict(tmp_path, blob, whole, shape) == "0", n
		# One byte short of `k`.
		assert python_verdict(blob, whole[:-1], shape) \
			== c_verdict(tmp_path, blob, whole[:-1], shape) != "0", n
