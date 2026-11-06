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
