"""`[fills_frame]`, and `size(s)` of the struct a `when` reads (26.981).

fuzznet's `node/live.c` refused a call message with bytes after a fixed body
from a hand-kept table of each arm's size -- a sixth description of the
layout. `struct message [fills_frame]` states it: the message's own extent,
measured as every reader measures it, against the frame (26.980). The shape
here is fuzznet's: a fixed head, then a variant whose arms are fixed but for
one that runs to the frame's end.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import fourway
from situc.diagnostics import SituError
from situc.layout import solve
from situc.parser import parse_text
from situc.resolve import resolve
from test_dissector import LUA, read_back, read_back_experts
from test_walker_c import (COMPILER, _inline_image, c_verdict, python_verdict,
                           shape_named_text)

SCHEMA = """target buffer;
endian big;
struct nothing { }
struct pair { u8 a; u8 b; }
struct tail { u8 n; u8 rest[remaining]; }
struct m [fills_frame] {
	u8 kind;
	variant body switch (kind) {
		case 1: nothing none;
		case 2: pair two;
		case 3: tail open;
		default: error;
	}
}
"""

#: `validate`'s code: 0 well formed, 1 out of bounds, 2 a constraint.
MESSAGES = {
	"none exact":  (bytes([1]), "0"),
	"none after":  (bytes([1, 0]), "2"),
	"pair exact":  (bytes([2, 7, 8]), "0"),
	"pair after":  (bytes([2, 7, 8, 9]), "2"),
	"pair short":  (bytes([2, 7]), "1"),
	"open any":    (bytes([3, 1, 2, 3, 4]), "0"),
	"open empty":  (bytes([3, 1]), "0"),
}


@pytest.mark.skipif(not fourway.COMPLETE, reason = "needs gcc, g++ and rustc")
def test_all_four_refuse_bytes_after_it(tmp_path: Path) -> None:
	source = tmp_path / "unit.situ"
	source.write_text(SCHEMA, encoding = "ascii")
	command = fourway.build(tmp_path, source)
	assert len(command) == 4
	for label, (data, verdict) in MESSAGES.items():
		for backend, argv in command.items():
			answer  = fourway.answers(argv, data, tmp_path)
			section = answer[answer.index("-- m\n"):].split("\n-- ")[0]
			line = next(one for one in section.splitlines()
			            if one.startswith("validate "))
			assert line.split()[1] == verdict, (label, backend, answer)


@pytest.mark.skipif(COMPILER is None, reason = "needs a C compiler")
def test_both_walkers_refuse_bytes_after_it(tmp_path: Path) -> None:
	image = _inline_image(SCHEMA)
	shape = shape_named_text(SCHEMA, "m")
	for label, (data, verdict) in MESSAGES.items():
		assert python_verdict(image, data, shape) == verdict, label
		assert c_verdict(tmp_path, image, data, shape) == verdict, label


@pytest.mark.skipif(LUA is None, reason = "needs lua")
def test_the_dissector_says_bytes_follow(tmp_path: Path) -> None:
	"""Its expert info where `validate` refuses for this alone, and nowhere
	else: a short frame is cut off, not followed by anything."""
	from situc.dissector import generate
	schema = parse_text(SCHEMA, path = "m.situ")
	lua = tmp_path / "m.lua"
	lua.write_text(generate(schema, resolve(schema, solve(schema)), "m"),
	               encoding = "ascii")
	for label, (data, verdict) in MESSAGES.items():
		said = [one[0] for one in read_back_experts(lua, "m", data)]
		assert said == (["m.m_bytes_after"] if verdict == "2" else []), label


@pytest.mark.skipif(LUA is None, reason = "needs lua")
def test_the_dissector_shows_the_arm_that_runs_to_the_end(
		tmp_path: Path) -> None:
	"""An arm whose length is the rest of the frame, where nothing follows
	the variant: the whole variant was declined for it, and fuzznet's call
	messages showed their kind and nothing of their body."""
	from situc.dissector import generate
	schema = parse_text(SCHEMA, path = "m.situ")
	lua = tmp_path / "m.lua"
	lua.write_text(generate(schema, resolve(schema, solve(schema)), "m"),
	               encoding = "ascii")
	consumed, rows = read_back(lua, "m", bytes([3, 1, 2, 3, 4]))
	assert consumed == 5
	assert rows == [("m.kind", 0, 1, "3"), ("tail.n", 1, 1, "1"),
	                ("tail.rest", 2, 3, "020304")]


def test_it_is_a_refusal_in_the_signature() -> None:
	from situc import wire
	schema = parse_text(SCHEMA, path = "m.situ")
	said = wire.render(schema, resolve(schema, solve(schema)), "m.situ")
	assert "m_bytes_after" in said


def _refused(text: str) -> str:
	with pytest.raises(SituError) as caught:
		schema = parse_text(text, path = "s.situ")
		resolve(schema, solve(schema))
	return caught.value.diagnostic.render()


def test_a_struct_no_reader_can_measure_is_refused() -> None:
	said = _refused("target buffer;\nendian big;\n"
	                "bit_order msb_first;\nstruct s { u3 a; u2 b; }\n"
	                'when size(s) != 1 refuse odd "x";\n')
	assert "`size(s)` cannot be measured" in said


def test_a_member_named_for_its_struct_is_ambiguous() -> None:
	said = _refused("target buffer;\nendian big;\n"
	                "struct s { u8 s; }\n"
	                'when size(s) != 1 refuse odd "x";\n')
	assert "could be the struct or its member" in said
