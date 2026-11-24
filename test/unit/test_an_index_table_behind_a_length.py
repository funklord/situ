"""An offset table behind a member the message sizes (26.910).

The table has two starts to name: entry N's offset, and where the region
begins. C called both `_offset`, so a variable header in front of a table
generated a header that did not compile, while the other three read it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import fourway
from situc.diagnostics import SituError, Source
from situc.layout import solve
from situc.parser import parse
from situc.resolve import resolve

SCHEMA = """target buffer;
endian big;
struct cell { u8 l; u8 d[l]; }
struct x {
	u8 a;
	u8 s[a];
	u8 n;
	u8 base;
	indexed(offset_type = u8, count = n, base = base) { cell cells[]; }
}
"""

#: Two bytes of name, then a table of two entries and the cells it reaches.
MESSAGES = (
	bytes.fromhex("02aabb 02 00 06 09 01 41 02 42 43"),
	bytes.fromhex("00 01 00 04 01 41"),
	bytes.fromhex("02aabb 02 00 06"),
	bytes.fromhex("02aabb"),
)


@pytest.mark.parametrize("message", MESSAGES, ids=lambda one: one.hex())
def test_the_four_read_a_table_behind_a_length(message: bytes,
		tmp_path: Path) -> None:
	source = tmp_path / "unit.situ"
	source.write_text(SCHEMA, encoding="ascii")
	commands = fourway.build(tmp_path, source)
	said = {target: fourway.answers(command, message, tmp_path)
	        for target, command in commands.items()}
	assert len(set(said.values())) == 1, said
	assert "validate" in said["c"]


def test_a_pad_arm_in_a_struct_holding_itself_is_refused() -> None:
	"""Named, rather than a header whose setters call what was declined."""
	text = ("target buffer;\nendian big;\nstruct nothing { }\n"
	        "struct x [depth = 4] {\n\tu8 k;\n\tvariant c switch (k) {\n"
	        "\t\tcase 1: x inner;\n\t\tdefault: nothing none;\n\t}\n"
	        "\tu8 f;\n\tvariant p switch (f) {\n\t\tcase 1: pad_to(4);\n"
	        "\t\tdefault: nothing no;\n\t}\n\tu8 l;\n}\n")
	schema = parse(Source("pad.situ", text))
	with pytest.raises(SituError, match="pads by its arm"):
		resolve(schema, solve(schema))


ONE_DOWN = """target buffer;
endian big;
struct rec { u8 len; u8 body[len]; }
struct inr {
	u8 n;
	indexed(offset_type = u16, count = n) {
		rec entries[];
	}
}
struct outer { u8 a; inr x; }
"""

#: (packet, verdict), worked by hand: `inr` is its count and a table of
#: `n` two-byte offsets, which is every backend's extent -- the entries
#: live past it.
ONE_DOWN_CASES = [("0200", 0), ("0201", 1), ("020100", 1)]


@pytest.mark.skipif(not fourway.COMPLETE, reason="needs all four toolchains")
@pytest.mark.parametrize(("packet", "verdict"), ONE_DOWN_CASES)
def test_a_struct_holding_an_index_is_measured_by_its_table(
		tmp_path: Path, packet: str, verdict: int) -> None:
	"""Both walkers measured an `indexed` region as one ELEMENT, the type
	its row names, so a count of zero read a `rec` that is not there and
	`outer` was refused BOUNDS one member down, where all four accept
	it; the C walker refused it and the Python walker `inr` alone too."""
	from test_walker_c import (c_verdict, packed_text, python_verdict,
	                           shape_named_text)
	source = tmp_path / "unit.situ"
	source.write_text(ONE_DOWN, encoding="ascii")
	command = fourway.build(tmp_path, source)
	data = bytes.fromhex(packet)
	for name, argv in command.items():
		held = fourway.answers(argv, data, tmp_path) \
			.split("-- outer\n")[1].split("\n-- ")[0]
		assert f"validate {verdict}" in held, (name, held)
	blob  = packed_text(ONE_DOWN)
	shape = shape_named_text(ONE_DOWN, "outer")
	assert python_verdict(blob, data, shape) == str(verdict)
	assert c_verdict(tmp_path, blob, data, shape) == str(verdict)
