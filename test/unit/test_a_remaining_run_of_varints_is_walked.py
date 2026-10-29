"""`uvar v[remaining]` is walked to the frame's end (26.742).

The frame says how many BYTES such a run holds and nothing says how many
varints, so no backend could count it: C answered the constant `0u` until
26.733, and after it all four declined the member in words. The count is
the whole varints from the run's start to the end of the view, which is a
walk -- the cost a Sequential run already names.

An element the frame cuts short is not counted, and `validate` refuses
that frame: the bytes after the last whole varint are a varint nobody can
read, which is 26.735 and 26.736's question asked of this run.

Asserted as values as well as agreement, because the four have agreed
wrongly before -- 26.683 was six readers measuring one varint where the run
held several.
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
	u8    lead;
	uvar  v[remaining];
}
"""

#: Whether each is a valid message, the run line every backend prints for
#: it, and the bytes.
CASES = {
	"three one-byte varints":
		(True, "v count=3 span=3 [0]=1", bytes([9, 0x01, 0x02, 0x03])),
	"a two-byte varint first":
		(True, "v count=2 span=3 [0]=129", bytes([9, 0x81, 0x01, 0x05])),
	"the last varint cut short":
		(False, "v count=1 span=1 [0]=1", bytes([9, 0x01, 0x81])),
	"an empty run":
		(True, "v count=0 span=0 [0]=0", bytes([9])),
}


def test_all_four_walk_the_run_to_the_frame_end(tmp_path: Path) -> None:
	schema = tmp_path / "unit.situ"
	schema.write_text(SCHEMA, encoding="ascii")

	try:
		command = fourway.build(tmp_path, schema)
	except fourway.BuildFailed as failed:
		pytest.skip(f"a backend could not be built here: {failed}")

	for label, (valid, line, packet) in CASES.items():
		given = {name: fourway.answers(argv, packet, tmp_path)
		         for name, argv in command.items()}
		first = next(iter(given.values()))
		assert all(answer == first for answer in given.values()), (
			f"{label}: the four backends disagree\n"
			+ "\n".join(f"  {name}: {answer!r}"
			            for name, answer in given.items()))
		assert line in first.splitlines(), f"{label}: {first!r}"
		assert ("validate 0" in first) == valid, f"{label}: {first!r}"


@pytest.mark.skipif(COMPILER is None, reason="no C compiler")
def test_both_walkers_refuse_a_run_cut_short(tmp_path: Path) -> None:
	"""The fifth and sixth readers, which have a validator of their own."""
	blob  = packed_text(SCHEMA)
	shape = shape_named_text(SCHEMA, "r")
	for label, (valid, _line, packet) in CASES.items():
		expected = "0" if valid else "1"
		assert python_verdict(blob, packet, shape) == expected, label
		assert c_verdict(tmp_path, blob, packet, shape) == expected, label
