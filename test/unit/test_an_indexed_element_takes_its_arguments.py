"""An `indexed` region hands each element its arguments (26.859).

    struct cell { parameter u8 block [stream]; u8 tag [max = 5]; u8 data[block]; }
    struct page { u8 block; u8 n;
                  indexed(offset_type = u8, count = n) {
                      cell(block = block) cells[]; } }

The region found each cell through its table and then measured it with no
argument to measure with, so it was refused. It takes them as a run does
now: the value is the parent's, one for every element, and each cell is
measured and validated with it. A parent's own parameter handed on stays
refused, since C's table accessors do not carry the parent's tail.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import fourway
from situc.diagnostics import SituError
from situc.parser import parse_text
from test_dissector import LUA, compare
from test_walker_c import (COMPILER, _inline_image, c_verdict,
                           python_verdict, shape_named_text)

SCHEMA = """target buffer;
endian big;
struct cell { parameter u8 block [stream]; u8 tag [max = 5]; u8 data[block]; }
struct page { u8 block; u8 n;
              indexed(offset_type = u8, count = n) {
                  cell(block = block) cells[];
              } }
"""

#: `02` block, `02` cells, a table of two offsets from the region's start,
#: then the cells. 0 whole, 1 a cell the frame does not hold, 2 a rule.
CASES = {
	"0202020501aabb03ccdd": "0",
	"0202020501aabb07ccdd": "2",	# the second cell's tag
	"0202020507aabb03ccdd": "2",	# the first
	"0202020901aabb03ccdd": "1",	# an entry past the frame
	"0102020401aa03cc":     "0",	# block 1: the same table, shorter cells
}


def _validate(answers: str) -> str:
	section = answers[answers.index("-- page\n"):].split("\n-- ")[0]
	return next(line.split()[1] for line in section.splitlines()
	            if line.startswith("validate"))


@pytest.mark.skipif(not fourway.COMPLETE or COMPILER is None,
                    reason="needs gcc, g++ and rustc")
def test_six_readers_agree(tmp_path: Path) -> None:
	schema = tmp_path / "unit.situ"
	schema.write_text(SCHEMA, encoding="ascii")
	command = fourway.build(tmp_path, schema)
	blob  = _inline_image(SCHEMA)
	shape = shape_named_text(SCHEMA, "page")
	for message, verdict in CASES.items():
		packet = bytes.fromhex(message)
		given = {backend: _validate(fourway.answers(argv, packet, tmp_path))
		         for backend, argv in command.items()}
		assert set(given.values()) == {verdict}, (message, given)
		assert python_verdict(blob, packet, shape) == verdict, message
		assert c_verdict(tmp_path, blob, packet, shape) == verdict, message


@pytest.mark.skipif(LUA is None, reason="no Lua interpreter")
def test_the_dissector_agrees(tmp_path: Path) -> None:
	schema = tmp_path / "unit.situ"
	schema.write_text(SCHEMA, encoding="ascii")
	assert not compare(schema).differ


def test_a_parent_s_own_parameter_is_still_refused() -> None:
	with pytest.raises(SituError) as refused:
		parse_text("target buffer;\nendian big;\n"
		           "struct cell { parameter u8 block [stream]; u8 t; "
		           "u8 d[block]; }\n"
		           "struct page { parameter u8 b [stream]; u8 n; "
		           "indexed(offset_type = u8, count = n) "
		           "{ cell(block = b) cells[]; } }\n")
	assert "cannot be a member yet" in str(refused.value)
