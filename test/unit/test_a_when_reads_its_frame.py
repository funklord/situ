"""`remaining` in a `when` is the frame from the struct's start (26.980).

Both walkers evaluated it so already -- the C walker hands a program the
frame's length and `walk.py` measures from the view's start -- and the four
backends raised `UnknownName` out of the generator, so a schema could not say
"nothing after this message" in a description every reader holds. It is the
view's length in each now, and the dissector's buffer.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import fourway
from situc.layout import solve
from situc.parser import parse_text
from test_dissector import LUA, read_back_experts
from test_walker_c import (COMPILER, _inline_image, c_verdict, python_verdict,
                           shape_named_text)

SCHEMA = """target buffer;
endian big;
struct rec { u8 k; u8 v[k]; }
struct s { u16 len; rec body; u8 z; }
when 3 + size(s.body) != remaining refuse bytes_after "a byte after it";
"""

#: The struct exactly, one byte past it, and one short of it: the last is a
#: bounds refusal before the `when` is reached, which is the ordering 0051
#: fixes, and is here so the `when` is not credited with it.
MESSAGES = {
	"exact": (bytes([0, 3, 2, 9, 9, 7]), "0"),
	"after": (bytes([0, 3, 2, 9, 9, 7, 0]), "2"),
	"short": (bytes([0, 3, 2, 9, 9]), "1"),
}


@pytest.mark.skipif(not fourway.COMPLETE, reason = "needs gcc, g++ and rustc")
def test_all_four_read_the_frame(tmp_path: Path) -> None:
	source = tmp_path / "unit.situ"
	source.write_text(SCHEMA, encoding = "ascii")
	command = fourway.build(tmp_path, source)
	assert len(command) == 4
	for label, (data, verdict) in MESSAGES.items():
		for backend, argv in command.items():
			answer  = fourway.answers(argv, data, tmp_path)
			section = answer[answer.index("-- s\n"):].split("\n-- ")[0]
			line = next(one for one in section.splitlines()
			            if one.startswith("validate "))
			assert line.split()[1] == verdict, (label, backend, answer)


@pytest.mark.skipif(COMPILER is None, reason = "needs a C compiler")
def test_both_walkers_read_the_frame(tmp_path: Path) -> None:
	image = _inline_image(SCHEMA)
	shape = shape_named_text(SCHEMA, "s")
	for label, (data, verdict) in MESSAGES.items():
		assert python_verdict(image, data, shape) == verdict, label
		assert c_verdict(tmp_path, image, data, shape) == verdict, label


#: The dissector reads no `size()` in a `when` yet and says so, so its case
#: states the struct's length through the field that carries it.
DISSECTED = """target buffer;
endian big;
struct s { u8 n; u8 v[n]; }
when 1 + s.n != remaining refuse bytes_after "a byte after it";
"""


@pytest.mark.skipif(LUA is None, reason = "needs lua")
def test_the_dissector_reads_the_frame(tmp_path: Path) -> None:
	from situc.dissector import generate
	from situc.resolve import resolve
	schema = parse_text(DISSECTED, path = "s.situ")
	lua = tmp_path / "s.lua"
	lua.write_text(generate(schema, resolve(schema, solve(schema)), "s"),
	               encoding = "ascii")
	assert read_back_experts(lua, "s", bytes([2, 9, 9])) == []
	said = read_back_experts(lua, "s", bytes([2, 9, 9, 0]))
	assert [one[0] for one in said] == ["s.bytes_after"], said
