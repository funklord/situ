"""`max N` on a `while` run takes N elements in every backend (26.784).

    struct top { u8 kind; pt recs[] while (x != 0) max 2; u16 after; }

Python's walk capped with `len(starts) <= N`, `starts` being the elements
read so far, so it took N + 1. C, C++, Rust and both walkers take N. So
Python read `after` from the bytes of a third element, and where those
were the frame's last two it found no `after` and refused the message.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import fourway

SCHEMA = """target buffer;
endian big;
struct pt { u8 x; u8 y; }
struct top { u8 kind; pt recs[] while (x != 0) max 2; u16 after; }
"""

#: Each runs two elements to the cap with the condition still true; the
#: second has a third element's worth of bytes after the cap.
MESSAGES = ("0001010202aabb", "00010102020303aabb", "000101000faabb")


@pytest.mark.skipif(not fourway.COMPLETE, reason="needs gcc, g++ and rustc")
def test_all_four_stop_at_the_cap(tmp_path: Path) -> None:
	schema = tmp_path / "unit.situ"
	schema.write_text(SCHEMA, encoding="ascii")
	command = fourway.build(tmp_path, schema)
	for message in MESSAGES:
		given = {name: fourway.answers(argv, bytes.fromhex(message), tmp_path)
		         for name, argv in command.items()}
		assert len(set(given.values())) == 1, (message, given)
		top = given["python"][given["python"].index("-- top"):]
		assert "recs count=2" in top, (message, top)
