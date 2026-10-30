"""A `ratio` region expands its interior's fixed bytes too (26.773).

    coded body(doubling) { u8 content[n]; u16 x; }

The interior is `n + 2` bytes and `ratio_exact(2, 1)` makes the region
`2 * (n + 2)` on the wire. C++ and Rust rendered the product as
`2 + n * 2` -- the sum unparenthesised before `*` -- so the region was
two bytes short of what C and Python said and every member after it
moved. The corpus's `coded_run` has no fixed member in its interior, so
the constant was zero there and the four agreed.
"""

from __future__ import annotations

from pathlib import Path

import fourway

SCHEMA = """target buffer;
endian big;
codec doubling {
	expansion = ratio_exact(2, 1);
	granularity = byte;
	seekable = linear;
	invertible;
	deterministic;
}
impl doubling extern "my_doubling";
struct codedu {
	u8  n;
	coded body(doubling) {
		u8   content[n];
		u16  x;
	}
}
"""


def test_all_four_double_the_whole_interior(tmp_path: Path) -> None:
	schema = tmp_path / "unit.situ"
	schema.write_text(SCHEMA, encoding="ascii")
	command = fourway.build(tmp_path, schema)
	for n, wire in ((0, 4), (1, 6), (3, 10)):
		message = bytes([n]) + bytes(range(1, wire + 1))
		given = {name: fourway.answers(argv, message, tmp_path)
		         for name, argv in command.items()}
		first = next(iter(given.values()))
		assert all(one == first for one in given.values()), given
		assert any(line.startswith(f"body len={wire} ")
		           for line in first.splitlines()), (n, first)
