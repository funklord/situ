"""`until "\\n" | end`: the last line need not end in a newline (26.895).

A text format's final line, and a comment at the end of a file, stop at the
delimiter or at the frame's end. `until` alone calls the second a truncated
member, which is right for a protocol and wrong for these.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import fourway
from situc import wire
from situc.layout import solve
from situc.parser import parse_text
from situc.resolve import resolve
from situc.traverse import must_be_terminated
from situc.unparse import unparse

HEAD = "target buffer;\nendian big;\n"
LINE = HEAD + 'struct p { u8 line[] until "\\n" | end; }\n'


def placement(text: str, name: str = "line") -> object:
	solved = solve(parse_text(text, path="s.situ"))
	return next(one for layout in solved.structs.values()
	            for one in layout.placements if one.name == name)


def test_the_delimiter_is_not_required() -> None:
	assert not must_be_terminated(placement(LINE))  # type: ignore[arg-type]


def test_plain_until_still_requires_it() -> None:
	"""The control: `| end` is what changes it."""
	text = LINE.replace(" | end", "")
	assert must_be_terminated(placement(text))  # type: ignore[arg-type]


def test_it_comes_after_the_delimiters() -> None:
	text = HEAD + "struct p { u8 line[] until \"\\n\" | \"\\r\" | end max 4; }\n"
	held = placement(text)
	assert getattr(held, "delimiters") == (b"\n", b"\r")
	assert getattr(held, "delimiter_cap") == 4


def test_the_word_is_soft() -> None:
	"""`end` stays a name everywhere it was one."""
	parse_text(HEAD + 'struct p { u8 end; u8 x[] until "\\n"; }\n', path="s.situ")


def test_it_round_trips() -> None:
	once = unparse(parse_text(LINE, path="s.situ"))
	assert "| end" in once
	assert once == unparse(parse_text(once, path="s.situ"))


def test_dropping_it_changes_the_contract() -> None:
	def signature(text: str) -> str:
		schema = parse_text(text, path="s.situ")
		return wire.render(schema, resolve(schema, solve(schema)), "s.situ")

	assert signature(LINE) != signature(LINE.replace(" | end", ""))


@pytest.mark.skipif(not fourway.COMPLETE, reason="needs all four toolchains")
def test_the_four_accept_a_last_line_with_no_newline(tmp_path: Path) -> None:
	source = tmp_path / "unit.situ"
	source.write_text(LINE, encoding="ascii")
	command = fourway.build(tmp_path, source)
	assert len(command) == 4
	for packet in (b"abc\n", b"abc", b"", b"\n", b"ab\ncd"):
		given = {name: fourway.answers(argv, packet, tmp_path)
		         for name, argv in command.items()}
		assert len(set(given.values())) == 1, (packet, given)
	assert "validate 0" in fourway.answers(command["c"], b"abc", tmp_path)


@pytest.mark.skipif(not fourway.COMPLETE, reason="needs all four toolchains")
def test_plain_until_refuses_it_in_all_four(tmp_path: Path) -> None:
	source = tmp_path / "unit.situ"
	source.write_text(LINE.replace(" | end", ""), encoding="ascii")
	command = fourway.build(tmp_path, source)
	said = {name: fourway.answers(argv, b"abc", tmp_path)
	        for name, argv in command.items()}
	assert len(set(said.values())) == 1
	assert "validate 0" not in said["c"]
