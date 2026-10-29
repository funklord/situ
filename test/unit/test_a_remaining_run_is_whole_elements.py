"""`validate` refuses a `[remaining]` run the frame does not divide (26.736).

The frame decides how many bytes a bare `x[remaining]` holds, and every
reader turned those bytes into elements and dropped what was left:

- `u16 v[remaining]` over three bytes was one element and a byte nobody
  read -- the count divides and the remainder vanishes;
- a run of records whose last element runs past the frame was walked to
  that element and no further -- mqtt's `filters[remaining]` with a string
  claiming five bytes and holding two had `count = 0`.

Both validated clean in all four backends and both walkers. As with 26.735
they agreed, so this asserts the verdict and not only agreement.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import fourway
from test_walker_c import (COMPILER, c_verdict, packed_text, python_verdict,
                           shape_named_text)

#: mqtt's shape, and a fixed record so that the walk's fixed-element case
#: is asked too: `pair` is three bytes, which no remainder of one or two
#: can hold.
SCHEMA = """target buffer;
endian little;
struct text {
	u16  length;
	u8   value[length];
}
struct pair {
	u8   a;
	u16  b;
}
struct words {
	u8   lead;
	u16  v[remaining];
}
struct strings {
	u16   id;
	text  filters[remaining];
}
struct pairs {
	u8    lead;
	pair  p[remaining];
}
"""

#: struct, whether valid, bytes.
CASES = {
	"words, whole":                     ("words", True, bytes([1, 1, 0, 2, 0])),
	"words, a byte over":               ("words", False, bytes([1, 1, 0, 2])),
	"strings, whole":                   ("strings", True,
	                                     bytes([0, 1, 2, 0, 0x61, 0x62])),
	"strings, the last claims more":    ("strings", False,
	                                     bytes([0, 1, 5, 0, 0x61, 0x62])),
	"strings, a length cut in half":    ("strings", False,
	                                     bytes([0, 1, 1, 0, 0x61, 9])),
	"pairs, whole":                     ("pairs", True, bytes([0, 1, 2, 3])),
	"pairs, two bytes over":            ("pairs", False,
	                                     bytes([0, 1, 2, 3, 4, 5])),
}


def _verdict(answer: str, struct: str) -> str:
	"""The `validate` line under `-- struct` in a driver's answer."""
	lines = answer.splitlines()
	start = lines.index(f"-- {struct}")
	return next(line for line in lines[start + 1:]
	            if line.startswith("validate "))


def test_a_ragged_remaining_run_is_refused_by_all_four(tmp_path: Path) -> None:
	schema = tmp_path / "unit.situ"
	schema.write_text(SCHEMA, encoding="ascii")

	try:
		command = fourway.build(tmp_path, schema)
	except fourway.BuildFailed as failed:
		pytest.skip(f"a backend could not be built here: {failed}")

	for label, (struct, valid, packet) in CASES.items():
		given = {name: _verdict(fourway.answers(argv, packet, tmp_path),
		                        struct)
		         for name, argv in command.items()}
		first = next(iter(given.values()))
		assert all(answer == first for answer in given.values()), (
			f"{label}: the four backends disagree: {given}")
		assert (first == "validate 0") == valid, f"{label}: {first!r}"


@pytest.mark.skipif(COMPILER is None, reason="no C compiler")
def test_a_ragged_remaining_run_is_refused_by_both_walkers(
		tmp_path: Path) -> None:
	blob = packed_text(SCHEMA)
	for label, (struct, valid, packet) in CASES.items():
		shape    = shape_named_text(SCHEMA, struct)
		expected = "0" if valid else "1"
		assert python_verdict(blob, packet, shape) == expected, label
		assert c_verdict(tmp_path, blob, packet, shape) == expected, label
