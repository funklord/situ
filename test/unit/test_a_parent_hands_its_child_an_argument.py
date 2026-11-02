"""A parent hands a nested member its arguments (0068 step 2).

    struct entry { parameter u16 block [stream]; u8 tag; u8 data[block]; }
    struct note  { parameter u8 mode; u8 v [max = mode]; }
    struct vol   { u16 block; entry(block = block) ent; note(mode = 3) k;
                   u8 after; }

`ent`'s extent follows `vol.block` and `k`'s rule follows a constant. Each
backend renders the argument in the parent and passes it wherever the
child is measured, validated or framed, so the four agree about where
`k` and `after` sit and what `validate` says.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import fourway
from test_walker_c import (COMPILER, _inline_image, c_verdict,
                           python_verdict, shape_named_text)
from situc.diagnostics import SituError
from situc.layout import solve
from situc.parser import parse_text

SCHEMA = """target buffer;
endian big;
struct entry { parameter u16 block [stream]; u8 tag; u8 data[block]; }
struct note { parameter u8 mode; u8 v [max = mode]; }
struct vol { u16 block; entry(block = block) ent; note(mode = 3) k; u8 after; }
"""

#: message -> (what `validate` says, `after`): 0 whole, 1 short, 2 a rule
#: broken.
CASES = {
	"000201aaaa0209": ("0", "9"),	# block 2: ent is 3 bytes, k = 2
	"000001020a":     ("0", "10"),	# block 0: ent is the tag alone
	"000201aaaa0709": ("2", "9"),	# k = 7 over mode 3
	"000301aaaa02":   ("1", "0"),	# block 3 swallows k: after is past
}


def _line(answers: str, name: str) -> str:
	section = answers[answers.index("-- vol\n"):].split("\n-- ")[0]
	return next(line.split()[1] for line in section.splitlines()
	            if line.startswith(name + " "))


@pytest.mark.skipif(not fourway.COMPLETE, reason="needs gcc, g++ and rustc")
def test_all_four_place_and_validate_alike(tmp_path: Path) -> None:
	schema = tmp_path / "unit.situ"
	schema.write_text(SCHEMA, encoding="ascii")
	command = fourway.build(tmp_path, schema)
	for message, (verdict, after) in CASES.items():
		given = {backend: fourway.answers(argv, bytes.fromhex(message),
		                                  tmp_path)
		         for backend, argv in command.items()}
		assert len(set(given.values())) == 1, (message, given)
		answer = next(iter(given.values()))
		assert _line(answer, "validate") == verdict, (message, answer)
		assert _line(answer, "after") == after, (message, answer)


#: The walkers' schema: `note`'s bound names a parameter, which the image
#: cannot encode yet, so `note` is not validatable there and a walk of it
#: declines. Here the argument SIZES the child, which is what has to reach
#: a walker for it to place anything after it.
WALKED = """target buffer;
endian big;
struct entry { parameter u16 block [stream]; u8 tag [max = 5]; u8 data[block]; }
struct vol { u16 block; entry(block = block) ent; entry(block = 1) one;
             u8 after; }
"""

WALKED_CASES = {
	"000201aaaa02bb09": "0",	# block 2, then `one` of two bytes
	"0000010209":       "1",	# block 0: `after` is past the frame
	"000207aaaa02bb09": "2",	# ent.tag 7 over its max
	"000201aaaa02bb":   "1",	# `after` cut off
}


@pytest.mark.skipif(not fourway.COMPLETE or COMPILER is None,
                    reason="needs gcc, g++ and rustc")
def test_both_walkers_take_the_argument_from_the_image(tmp_path: Path) -> None:
	"""The ARGUMENTS section: each walker evaluates the parent's program and
	hands the value to the child, and answers with the four backends."""
	schema = tmp_path / "unit.situ"
	schema.write_text(WALKED, encoding="ascii")
	command = fourway.build(tmp_path, schema)
	blob  = _inline_image(WALKED)
	shape = shape_named_text(WALKED, "vol")
	for message, verdict in WALKED_CASES.items():
		packet = bytes.fromhex(message)
		backends = {_line(fourway.answers(argv, packet, tmp_path), "validate")
		            for argv in command.values()}
		assert backends == {verdict}, (message, backends)
		assert python_verdict(blob, packet, shape) == verdict, message
		assert c_verdict(tmp_path, blob, packet, shape) == verdict, message


@pytest.mark.parametrize(("member", "words"), [
	# A value the parameter cannot hold: truncated by C, kept by Python.
	("u16 n; note(mode = n) k;", "may not hold this argument"),
	# Handing on the parent's own parameter is step 5's, with recursion.
	("parameter u8 m; note(mode = m) k;", "cannot be a member yet"),
	# A member inside a block is not built yet.
	("u8 k; positional { note(mode = 1) a; }", "cannot be a member yet"),
])
def test_what_step_2_does_not_build_is_refused(member: str,
		words: str) -> None:
	with pytest.raises(SituError) as refused:
		solve(parse_text("target buffer;\nendian big;\n"
		                 "struct note { parameter u8 mode; "
		                 "u8 v [max = mode]; }\n"
		                 f"struct s {{ {member} }}\n"))
	assert words in str(refused.value)
