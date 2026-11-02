"""A nested member the frame cuts short is BOUNDS, whatever it holds (26.800).

    struct entry { u8 tag [max = 5]; u8 n; u8 data[n]; }
    struct vol   { entry ent; entry one; u8 after; }

`0100bb05aaaa`: `one` declares five bytes of data and the frame holds two,
and its `tag` breaks its rule. Every backend acquires `one` with
`situ_view_sub(view, base, extent, out)`, which refuses the extent, so
`validate` says BOUNDS and never reaches the tag. Both walkers clamped the
inner frame to what was there, walked in, and said CONSTRAINT.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import fourway
from test_walker_c import (COMPILER, _inline_image, c_verdict,
                           python_verdict, shape_named_text)

SCHEMA = """target buffer;
endian big;
struct entry { u8 tag [max = 5]; u8 n; u8 data[n]; }
struct vol { entry ent; entry one; u8 after; }
"""

CASES = {
	"0100bb05aaaa": "1",	# `one` cut short, its tag broken
	"0100bb0102":   "2",	# `one` whole, its tag broken
	"0100010102ff": "0",
}


def _validate(answers: str) -> str:
	section = answers[answers.index("-- vol\n"):].split("\n-- ")[0]
	return next(line.split()[1] for line in section.splitlines()
	            if line.startswith("validate"))


@pytest.mark.skipif(not fourway.COMPLETE or COMPILER is None,
                    reason="needs gcc, g++ and rustc")
def test_six_readers_agree(tmp_path: Path) -> None:
	schema = tmp_path / "unit.situ"
	schema.write_text(SCHEMA, encoding="ascii")
	command = fourway.build(tmp_path, schema)
	blob  = _inline_image(SCHEMA)
	shape = shape_named_text(SCHEMA, "vol")
	for message, verdict in CASES.items():
		packet = bytes.fromhex(message)
		backends = {_validate(fourway.answers(argv, packet, tmp_path))
		            for argv in command.values()}
		assert backends == {verdict}, (message, backends)
		assert python_verdict(blob, packet, shape) == verdict, message
		assert c_verdict(tmp_path, blob, packet, shape) == verdict, message
