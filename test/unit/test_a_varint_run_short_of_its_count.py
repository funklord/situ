"""`validate` refuses a varint run the frame holds only part of (26.735).

`u8 n; uvar v[n];` over `{ 3, 0x01, 0x01 }` declares three varints and holds
two, and all four backends called it valid. Their check held the frame
against `_span`, which is how far the walk REACHED -- and the walk stops at
the frame, so the comparison could never fail. 26.676 is the same fault in a
run of records, which gained `_fits` for it; the varint run arrived seven
entries later and did not.

**Agreement was not the property that failed**, so this asserts the verdict
as well. The four agreed throughout; they agreed on OK -- and so did both
walkers, whose `counted_run_fits` answered yes for any run without an
element struct.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import fourway
from test_walker_c import (COMPILER, c_verdict, packed_text, python_verdict,
                           shape_named_text)

SCHEMA = """target buffer;
endian little;
varint_type uvar { encoding = leb128; max_bits = 32; }
struct r {
	u8    n;
	uvar  v[n];
}
"""

#: Whether each is a valid message, and the bytes.
CASES = {
	"two of three declared elements":       (False, bytes([3, 0x01, 0x01])),
	"the last element truncated":           (False, bytes([2, 0x01, 0x81])),
	"three elements, one of them two bytes": (True, bytes([3, 0x01, 0x81, 0x01, 0x02])),
}


def test_a_short_varint_run_is_refused_by_all_four(tmp_path: Path) -> None:
	schema = tmp_path / "unit.situ"
	schema.write_text(SCHEMA, encoding="ascii")

	try:
		command = fourway.build(tmp_path, schema)
	except fourway.BuildFailed as failed:
		pytest.skip(f"a backend could not be built here: {failed}")

	for label, (valid, packet) in CASES.items():
		given = {name: fourway.answers(argv, packet, tmp_path)
		         for name, argv in command.items()}
		first = next(iter(given.values()))
		assert all(answer == first for answer in given.values()), (
			f"{label}: the four backends disagree\n"
			+ "\n".join(f"  {name}: {answer!r}"
			            for name, answer in given.items()))
		assert ("validate 0" in first) == valid, f"{label}: {first!r}"


@pytest.mark.skipif(COMPILER is None, reason="no C compiler")
def test_a_short_varint_run_is_refused_by_both_walkers(tmp_path: Path) -> None:
	"""The fifth and sixth readers, which have a validator of their own."""
	blob  = packed_text(SCHEMA)
	shape = shape_named_text(SCHEMA, "r")
	for label, (valid, packet) in CASES.items():
		expected = "0" if valid else "1"
		assert python_verdict(blob, packet, shape) == expected, label
		assert c_verdict(tmp_path, blob, packet, shape) == expected, label
