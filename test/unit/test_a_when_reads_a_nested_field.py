"""A `when` may read a field of a struct its own struct nests (26.915).

A size could already say `h.len`, and a rule relating a nested header to the
body it sizes could not: `when` stopped at the struct's own members.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import fourway
from situc.diagnostics import SituError
from situc.layout import solve
from situc.parser import parse_text
from test_walker_c import (COMPILER, _inline_image, c_verdict,
                           python_verdict, shape_named_text)

SCHEMA = """target buffer;
endian big;
struct hdr { u8 kind; u16 len; }
struct inner { hdr h; u8 mark; }
struct s { hdr h; u8 body[h.len]; u8 tail; }
struct deep { inner i; u8 last; }
struct behind { u8 n; u8 d[n]; hdr h; }
when s.h.kind == 2 && s.h.len == 0 refuse empty_data "kind 2 carries data";
when s.tail != s.h.kind refuse tail_mismatch "the tail repeats the kind";
when deep.i.h.kind != deep.last refuse last_mismatch "two levels down";
when behind.h.len != behind.n refuse behind_mismatch "at a dynamic offset";
"""

#: struct -> message -> what `validate` says: 0 whole, 1 short, 2 a rule.
CASES = {
	"s": {
		"01" "0001" "aa" "01": "0",
		"02" "0001" "aa" "02": "0",
		"02" "0000" "02":      "2",		# the first rule, both halves
		"01" "0000" "01":      "0",		# ...and with one half false
		"01" "0001" "aa" "09": "2",		# the second, across the body
		"01" "0005" "aa" "01": "1",		# short still wins
	},
	"deep": {
		"07" "0000" "ff" "07": "0",
		"07" "0000" "ff" "08": "2",
	},
	"behind": {
		"01" "aa" "07" "0001": "0",
		"01" "aa" "07" "0002": "2",
	},
}


def _section(answers: str, struct: str) -> str:
	return answers[answers.index(f"-- {struct}\n"):].split("\n-- ")[0]


def _line(section: str, name: str) -> str:
	return next(line.split()[1] for line in section.splitlines()
	            if line.startswith(name + " "))


@pytest.mark.skipif(not fourway.COMPLETE, reason = "needs gcc, g++ and rustc")
def test_all_four_hold_the_rule(tmp_path: Path) -> None:
	schema = tmp_path / "unit.situ"
	schema.write_text(SCHEMA, encoding = "ascii")
	command = fourway.build(tmp_path, schema)
	for struct, cases in CASES.items():
		for message, verdict in cases.items():
			given = {backend: _section(fourway.answers(
			             argv, bytes.fromhex(message), tmp_path), struct)
			         for backend, argv in command.items()}
			lines = {backend: _line(text, "validate")
			         for backend, text in given.items()}
			assert set(lines.values()) == {verdict}, (struct, message, lines)


@pytest.mark.skipif(COMPILER is None, reason = "needs a C compiler")
@pytest.mark.parametrize("struct", sorted(CASES))
def test_both_walkers_hold_the_rule(struct: str, tmp_path: Path) -> None:
	image = _inline_image(SCHEMA)
	shape = shape_named_text(SCHEMA, struct)
	for message, verdict in CASES[struct].items():
		data = bytes.fromhex(message)
		# `h` behind a length has no constant place in the image, and a walker
		# saying so is an answer: the backends are what read it (26.916).
		allowed = {verdict, "cannot-say"} if struct == "behind" else {verdict}
		assert python_verdict(image, data, shape) in allowed, message
		assert c_verdict(tmp_path, image, data, shape) in allowed, message


REFUSED = "target buffer;\nendian big;\n" \
	"struct arms { u8 kind; variant v switch (kind) { case 1: u8 a;" \
	" default: u16 b; } }\n" \
	"struct plain { u8 k; }\n"


@pytest.mark.parametrize("text, wanted", [
	("struct s { plain h; u8 t; }\nwhen s.h.nope == 1 refuse x \"m\";",
	 "`plain` has no field `nope`"),
	("struct s { plain h; u8 t; }\nwhen s.g.k == 1 refuse x \"m\";",
	 "`s` has no field `g`"),
	("struct s { u8 h; u8 t; }\nwhen s.h.kind == 1 refuse x \"m\";",
	 "`h` is not one nested struct"),
	("struct s { u8 n; plain h[n]; }\nwhen s.h.k == 1 refuse x \"m\";",
	 "`h` is not one nested struct"),
	("struct s { arms h; u8 t; }\nwhen s.h.a == 1 refuse x \"m\";",
	 "which is inside"),
	("struct s { u8 n; variant w switch (n) { case 1: plain h;"
	 " default: error; } }\nwhen s.h.k == 1 refuse x \"m\";",
	 "which is inside"),
], ids = ["no-last", "no-first", "scalar", "run", "child-arm", "own-arm"])
def test_what_a_path_cannot_step_through(text: str, wanted: str) -> None:
	"""Each was a traceback or, through a scalar, a rule reading nothing."""
	with pytest.raises(SituError) as caught:
		solve(parse_text(REFUSED + text))
	assert wanted in str(caught.value)
