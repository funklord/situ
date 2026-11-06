"""`u32 board_id at 26;` is `MessageStatic(0x1A)` (26.667, 26.888).

A datasheet's list of offsets used to report `DataPlaced` and `Unstable`,
which say a number in the message decides the position. It does not. It is a
constant, measured from the message and not from the frame a reader holds, so
it is a value of its own: known, and still bounds-checked on every read.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import fourway
from situc.capability import DOMAINS, Axis, Value
from situc.layout import solve
from situc.parser import parse_text
from situc.resolve import ResolvedSchema, resolve
from walker import image as walker_image
from situc import pack

HEAD = "target buffer;\nendian big;\n"

BMIC = HEAD + """const BASE = 100;
struct bmic {
	u32 signature at 1;
	u32 board_id  at 26;
	u8  count     at BASE + 42;
	u16 where;
	u8  moved[2]  at where;
	u8  after;
}
"""


def resolved(text: str) -> ResolvedSchema:
	schema = parse_text(text, path="s.situ")
	return resolve(schema, solve(schema))


def axis(path: str, which: Axis) -> Value:
	entry = resolved(BMIC).find(path)
	assert entry is not None
	return entry.vector.get(which)


@pytest.mark.parametrize("path, shown", [
	("bmic.signature", "MessageStatic(0x01)"),
	("bmic.board_id",  "MessageStatic(0x1A)"),
	("bmic.count",     "MessageStatic(0x8E)"),
])
def test_the_number_is_reported(path: str, shown: str) -> None:
	assert axis(path, Axis.OFFSET).render() == shown


def test_a_literal_earns_no_instability() -> None:
	"""`Unstable` meant a pointer moves when the offset field is written, and
	a constant has no such field."""
	assert axis("bmic.board_id", Axis.ADDRESS).base == "Stable"


def test_the_alignment_is_the_constant_s() -> None:
	assert axis("bmic.board_id", Axis.ALIGN).render() == "Aligned(2)"
	assert axis("bmic.signature", Axis.ALIGN).render() == "Aligned(1)"


def test_an_offset_a_field_decides_is_still_data_placed() -> None:
	"""The control: the data does decide this one."""
	assert axis("bmic.moved", Axis.OFFSET).base == "DataPlaced"
	assert axis("bmic.moved", Axis.ADDRESS).base == "Unstable"


def test_a_member_behind_a_run_is_not_blamed_on_the_run() -> None:
	"""It owes nothing to what precedes it, so nothing variable moves it."""
	text = HEAD + "struct s { u8 n; u8 run[n]; u16 x at 8; }\n"
	entry = resolved(text).find("s.x")
	assert entry is not None
	assert entry.vector.get(Axis.OFFSET).render() == "MessageStatic(0x08)"
	assert entry.vector.get(Axis.ADDRESS).base == "Stable"


def test_the_axis_keeps_its_order() -> None:
	"""A view cannot resolve it, and no member has to be read to find it."""
	domain = [str(one) for one in DOMAINS[Axis.OFFSET]]
	assert domain.index("FrameStatic") < domain.index("MessageStatic") \
		< domain.index("Dynamic")


def test_the_image_format_moved_with_the_axis() -> None:
	"""The image stores an index, and an old walker reading a new one would
	call `Dynamic` something else, so the two refuse each other."""
	assert pack.FORMAT_VERSION == walker_image.FORMAT_VERSION >= 7
	assert walker_image.DOMAINS["offset"].index("MessageStatic") == 2


@pytest.mark.skipif(not fourway.COMPLETE, reason="needs all four toolchains")
def test_the_reads_are_unchanged(tmp_path: Path) -> None:
	"""The value is a statement about the map: the four read as they did."""
	schema = tmp_path / "unit.situ"
	schema.write_text(HEAD + "struct p { u8 a; u8 b[1] at 3; u8 c; }\n",
	                  encoding="ascii")
	command = fourway.build(tmp_path, schema)
	assert len(command) == 4
	for packet in (bytes([1, 2, 3, 4]), bytes([1, 2])):
		given = {name: fourway.answers(argv, packet, tmp_path)
		         for name, argv in command.items()}
		assert len(set(given.values())) == 1, given
