"""An argument may be computed from `remaining` (26.993).

hull's pack index cannot say "each frame lies inside the data": an entry can
only know the data's length if the pack hands it, and the length is what
`remaining` leaves after the index. An argument's range must fit its
parameter, and `remaining` was unbounded above. Set by the copyright holder:
bound it by the 32-bit view every runtime has, in argument range checks only,
so no member's capability moves.

In an argument `remaining` means what it means in a size -- the bytes from
the member's start to the frame's end -- and is signed, so room that runs
out is negative rather than wrapped.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import fourway
from situc.layout import solve
from situc.parser import parse_text
from test_walker_c import (COMPILER, _inline_image, c_verdict, python_verdict,
                           shape_named_text)

SCHEMA = """target buffer;
endian big;
struct entry {
	parameter i64 room;
	u32 offset;
	u32 frame;
}
when entry.offset + entry.frame > entry.room
	refuse outside "a frame lies past the end of the data";
struct pack {
	u8 count;
	entry(room = remaining - count * 8) index[count];
	u8 data[remaining];
}
"""


def pack(entries: list[tuple[int, int]], data: int) -> bytes:
	body = b"".join(offset.to_bytes(4, "big") + frame.to_bytes(4, "big")
	                for offset, frame in entries)
	return bytes([len(entries)]) + body + bytes(data)


#: `validate`'s code over each: 0 holds, 2 a frame past the data.
CASES = [
	("inside", pack([(0, 4), (4, 6)], 10), "0"),
	("at the end", pack([(6, 4)], 10), "0"),
	("past it", pack([(0, 4), (8, 4)], 10), "2"),
	("no data", pack([(0, 0)], 0), "0"),
]


def test_capabilities_do_not_move() -> None:
	"""The bound is the argument check's: a run to the frame's end is still
	unbounded where a capability is measured."""
	from situc.resolve import resolve
	schema = parse_text(SCHEMA, path = "p.situ")
	placed = resolve(schema, solve(schema)).find("pack.data")
	assert placed is not None
	assert placed.placement.size_max_bits is None


@pytest.mark.skipif(not fourway.COMPLETE, reason = "needs gcc, g++ and rustc")
def test_all_four_hold_frames_inside_the_data(tmp_path: Path) -> None:
	source = tmp_path / "p.situ"
	source.write_text(SCHEMA, encoding = "ascii")
	command = fourway.build(tmp_path, source)
	assert len(command) == 4
	for label, data, verdict in CASES:
		said = {name: fourway.answers(argv, data, tmp_path)
		        for name, argv in command.items()}
		section = {name: one[one.index("-- pack\n"):].split("\n-- ")[0]
		           for name, one in said.items()}
		lines = {name: next(line for line in one.splitlines()
		                    if line.startswith("validate "))
		         for name, one in section.items()}
		assert set(lines.values()) == {f"validate {verdict}"}, (label, lines)


@pytest.mark.skipif(COMPILER is None, reason = "needs a C compiler")
def test_both_walkers_hold_them(tmp_path: Path) -> None:
	image = _inline_image(SCHEMA)
	shape = shape_named_text(SCHEMA, "pack")
	for label, data, verdict in CASES:
		assert python_verdict(image, data, shape) == verdict, label
		assert c_verdict(tmp_path, image, data, shape) == verdict, label
