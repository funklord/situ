"""`u64 x[remaining / 8]` is a run, not one `u64` (26.986).

A size written as arithmetic over `remaining` and constants names no field,
and the solver's test for a CONSTANT size was "names no field" -- so the
size was dropped, and C, C++, Rust and Python each read one element where
the schema states a run, with nothing said. The walkers read `remaining`
from the member's start, as section 10 means it, and were right alone.

git's pack index has the shape: its large-offset table runs to the two
20-byte trailers, and how many entries it holds is stored nowhere.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import fourway
from test_walker_c import (COMPILER, _inline_image, c_verdict, python_verdict,
                           shape_named_text)

SCHEMA = """target buffer;
endian big;
struct s {
	u8  n;
	u64 x[max(remaining, 2) / 8 - 0];
	u8  t[2];
}
"""

#: The bytes after `n`, how many `u64` `remaining / 8` makes of them, and
#: `validate`'s code. The two-byte trailer fits in what the division leaves
#: over -- except at 25, where three `u64` leave one byte for it: a bounds
#: refusal, which every reader has to agree on as well.
FRAMES = [(2, 0, "0"), (10, 1, "0"), (18, 2, "0"), (26, 3, "0"),
          (25, 3, "1")]


def packet(after: int) -> bytes:
	return bytes([1]) + bytes(range(7, 7 + after))


@pytest.mark.skipif(not fourway.COMPLETE, reason = "needs gcc, g++ and rustc")
def test_all_four_read_a_run(tmp_path: Path) -> None:
	source = tmp_path / "unit.situ"
	source.write_text(SCHEMA, encoding = "ascii")
	command = fourway.build(tmp_path, source)
	assert len(command) == 4
	for after, count, verdict in FRAMES:
		said = {backend: fourway.answers(argv, packet(after), tmp_path)
		        for backend, argv in command.items()}
		assert len(set(said.values())) == 1, (after, said)
		lines = said["c"].splitlines()
		assert next(one for one in lines if one.startswith("x ")) \
			.startswith(f"x count={count}"), (after, lines)
		assert f"validate {verdict}" in lines, (after, lines)


@pytest.mark.skipif(COMPILER is None, reason = "needs a C compiler")
def test_the_walkers_agree_on_the_verdict(tmp_path: Path) -> None:
	image = _inline_image(SCHEMA)
	shape = shape_named_text(SCHEMA, "s")
	for after, _, verdict in FRAMES:
		assert python_verdict(image, packet(after), shape) == verdict, after
		assert c_verdict(tmp_path, image, packet(after), shape) == verdict, \
			after
