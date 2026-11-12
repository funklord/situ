"""A scalar placed by `at` reads as the number its type declares (26.959).

`u8 c at 3;` was accepted and exposed by every backend as one byte of the
message -- C `_view`, C++ and Rust a byte slice, Python a `memoryview` --
never as a number. The four agreed, so no differential saw it, and the
differential's C driver asked for a `_get` nobody emits and did not
compile. Each backend has `<name>_value` beside the bytes now, reading
through them so the message-relative bounds check stays in one place.

Values here are worked out by hand from the bytes.
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
	u8   off;
	u8   pad;
	u8   c   at 3;
	u16  w   at off;
	i16  le  at 6 [endian = little];
}
"""

#: The message, and the three values: `c` at 3, `w` at `off` big-endian,
#: `le` at 6 little-endian and signed.
CASES = {
	"in range": (bytes.fromhex("04 00 00 7f 12 34 fe ff".replace(" ", "")),
	             ("c_value ok=1 value=127", "w_value ok=1 value=4660",
	              "le_value ok=1 value=-2")),
	"w past the end": (bytes.fromhex("07 00 00 01 00 00 01 00".replace(" ", "")),
	                   ("c_value ok=1 value=1", "w_value ok=0 value=0",
	                    "le_value ok=1 value=1")),
}


def test_all_four_read_the_value(tmp_path: Path) -> None:
	schema = tmp_path / "unit.situ"
	schema.write_text(SCHEMA, encoding="ascii")
	command = fourway.build(tmp_path, schema)
	for label, (packet, lines) in CASES.items():
		given = {name: fourway.answers(argv, packet, tmp_path)
		         for name, argv in command.items()}
		first = next(iter(given.values()))
		assert all(answer == first for answer in given.values()), \
			(label, given)
		for line in lines:
			assert line in first.splitlines(), f"{label}: {first!r}"


@pytest.mark.skipif(COMPILER is None, reason="no C compiler")
def test_both_walkers_take_the_message(tmp_path: Path) -> None:
	blob  = packed_text(SCHEMA)
	shape = shape_named_text(SCHEMA, "s")
	for label, (packet, _lines) in CASES.items():
		assert python_verdict(blob, packet, shape) \
			== c_verdict(tmp_path, blob, packet, shape), label
