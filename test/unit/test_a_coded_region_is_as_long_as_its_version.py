"""A coded region holding `[since]` members is its version's length (26.774).

    coded body(doubling) { u8 content[n]; u16 x [since = 2]; }

`traverse.region_extent` counted every interior member, so a version-1
message -- which carries no `x` -- was given a body two interior bytes
longer than it holds, four on the wire, in all four backends and in the
walkers' size program. And the layout's minimum counted `x` too, so a
whole version-1 message with an empty body was refused as short.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import fourway

from test_walker_c import (COMPILER, _inline_image, c_widths, python_widths,
                           shape_named_text)

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
struct codedv [version = ver] {
	u8  ver;
	u8  n;
	coded body(doubling) {
		u8   content[n];
		u16  x [since = 2];
	}
}
"""

#: message -> the body's length on the wire.
CASES = {
	"0100":             0,
	"0101aabb":         2,
	"0200aabbccdd":     4,
	"0201aabbccddeeff": 6,
}


def test_all_four_size_the_body_by_its_version(tmp_path: Path) -> None:
	schema = tmp_path / "unit.situ"
	schema.write_text(SCHEMA, encoding="ascii")
	command = fourway.build(tmp_path, schema)
	for message, wire in CASES.items():
		given = {name: fourway.answers(argv, bytes.fromhex(message), tmp_path)
		         for name, argv in command.items()}
		first = next(iter(given.values()))
		assert all(one == first for one in given.values()), given
		assert any(line.startswith(f"body len={wire} ")
		           for line in first.splitlines()), (message, first)


@pytest.mark.skipif(COMPILER is None, reason="no C compiler")
@pytest.mark.parametrize("message", list(CASES))
def test_both_walkers_size_the_body_by_its_version(message: str,
		tmp_path: Path) -> None:
	blob  = _inline_image(SCHEMA)
	shape = shape_named_text(SCHEMA, "codedv")
	raw   = bytes.fromhex(message)
	in_python = python_widths(blob, raw, shape)[-1]
	in_c      = c_widths(tmp_path, blob, raw, shape)[-1]
	assert in_python == in_c == str(CASES[message]), (in_python, in_c)
