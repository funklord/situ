"""A counted run of signed varints, through the four drivers (26.954).

The differential drivers declared a varint run's first element as an
unsigned 64-bit integer whatever the run held. C++'s `_at` takes a
`std::int64_t &` for an `sleb128` run, so its driver did not compile, and
C's passed a `uint64_t *` and printed -1 as 18446744073709551615 where
Python printed -1. No corpus schema has a signed run -- wasm's `sleb128`
values are single -- so the differential had never built one. Found by
putting every varint encoding in every position and asking all six
readers.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import fourway
from test_walker_c import (COMPILER, c_verdict, packed_text, python_verdict,
                           shape_named_text)

SCHEMA = """target buffer;
endian big;
varint_type v { encoding = sleb128; max_bits = 32; }
struct s {
	u8  c;
	v   xs[c];
	u8  k;
}
"""

#: The driver's line for the run, and the bytes: -1, -63 and -128 first.
CASES = {
	"minus one":            ("xs count=2 span=2 [0]=-1",
	                         bytes([2, 0x7F, 0x01, 0x05])),
	"minus sixty-three":    ("xs count=1 span=1 [0]=-63",
	                         bytes([1, 0x41, 0x00])),
	"two bytes, negative":  ("xs count=3 span=4 [0]=-128",
	                         bytes([3, 0x80, 0x7F, 0x01, 0x02, 9])),
}


def test_all_four_print_a_signed_run_alike(tmp_path: Path) -> None:
	schema = tmp_path / "unit.situ"
	schema.write_text(SCHEMA, encoding="ascii")
	command = fourway.build(tmp_path, schema)
	for label, (line, packet) in CASES.items():
		given = {name: fourway.answers(argv, packet, tmp_path)
		         for name, argv in command.items()}
		first = next(iter(given.values()))
		assert all(answer == first for answer in given.values()), \
			(label, given)
		assert line in first.splitlines(), f"{label}: {first!r}"


@pytest.mark.skipif(COMPILER is None, reason="no C compiler")
def test_both_walkers_take_a_signed_run(tmp_path: Path) -> None:
	blob  = packed_text(SCHEMA)
	shape = shape_named_text(SCHEMA, "s")
	for label, (_line, packet) in CASES.items():
		assert python_verdict(blob, packet, shape) \
			== c_verdict(tmp_path, blob, packet, shape), label
