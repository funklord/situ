"""`invariant s.len == size(s.body)` over a member the data sizes (26.897).

The corpus's one invariant sums scalars, so `size` only ever folded to a number
and no backend had been asked for a run-time extent there. Rust named
`body_extent` on its `Mut`, which has none, and did not compile.

And an invariant was only ever written. `validate` passed a `len` contradicting
the body, which is fuzznet's report: a length prefix could not be tied to a
nested object's size. It is a refusal now, which is what brings it to all four
and both walkers.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import fourway
from situc import ast, wire
from situc.diagnostics import SituError
from situc.layout import solve
from situc.parser import parse_text
from situc.resolve import resolve
from situc.unparse import unparse

SCHEMA = """target buffer;
endian big;
struct rec { u8 k; u8 v[k]; }
struct s { u16 len; rec body; u8 z; }
invariant s.len == size(s.body);
"""


@pytest.mark.skipif(not fourway.COMPLETE, reason="needs all four toolchains")
def test_the_four_build_and_recompute_alike(tmp_path: Path) -> None:
	source = tmp_path / "unit.situ"
	source.write_text(SCHEMA, encoding="ascii")
	command = fourway.build(tmp_path, source)
	assert len(command) == 4
	packet = bytes([0, 3, 2, 9, 9, 7])
	given = {name: fourway.answers(argv, packet, tmp_path)
	         for name, argv in command.items()}
	assert len(set(given.values())) == 1, given


GOOD  = bytes([0, 3, 2, 9, 9, 7])
LYING = bytes([0, 9, 2, 9, 9, 7])


def test_an_invariant_implies_a_refusal() -> None:
	schema = parse_text(SCHEMA, path="s.situ")
	refused = [one for one in schema.whens()
	           if one.severity is ast.Severity.REFUSE]
	assert [one.name for one in refused] == ["s_len_contradicted"]


def test_the_refusal_is_in_the_signature() -> None:
	schema = parse_text(SCHEMA, path="s.situ")
	said = wire.render(schema, resolve(schema, solve(schema)), "s.situ")
	assert "s_len_contradicted" in said


def test_the_unparser_writes_only_what_the_author_did() -> None:
	once = unparse(parse_text(SCHEMA, path="s.situ"))
	assert "invariant s.len" in once
	assert "contradicted" not in once


def test_an_invariant_no_reader_can_evaluate_implies_nothing() -> None:
	"""16.1: no recompute and no setter, so there is nothing to refuse over."""
	text = SCHEMA.replace("size(s.body)", "offset(s.z)")
	assert parse_text(text, path="s.situ").whens() == []


@pytest.mark.parametrize("call", ["offset(s.z)", "count(s.body)"])
def test_a_when_cannot_ask_what_no_reader_evaluates(call: str) -> None:
	text = SCHEMA.replace("invariant s.len == size(s.body);",
	                      f'when s.len != {call} refuse bad "no";')
	with pytest.raises(SituError) as caught:
		parse_text(text, path="s.situ")
	assert "cannot be read in a `when`" in caught.value.diagnostic.render()


@pytest.mark.skipif(not fourway.COMPLETE, reason="needs all four toolchains")
@pytest.mark.parametrize("text", [
	SCHEMA,
	SCHEMA.replace("invariant s.len == size(s.body);",
	               'when s.len != size(s.body) refuse bad "no";'),
], ids=["invariant", "when"])
def test_the_four_refuse_a_contradicted_length(tmp_path: Path, text: str) -> None:
	source = tmp_path / "unit.situ"
	source.write_text(text, encoding="ascii")
	command = fourway.build(tmp_path, source)
	said = {}
	for packet in (GOOD, LYING):
		given = {name: fourway.answers(argv, packet, tmp_path)
		         for name, argv in command.items()}
		assert len(set(given.values())) == 1, given
		said[packet] = given["c"].split("-- s", 1)[1]
	assert "validate 0" in said[GOOD]
	assert "validate 0" not in said[LYING]


@pytest.mark.parametrize("statement, said", [
	('when s != 0 refuse bad "no";', "a `when` reads its members"),
	("invariant s.len == size(s);", "an invariant derives from its fields"),
	("invariant s.len == s;", "an invariant derives from its fields"),
], ids=["when-bare", "invariant-size", "invariant-bare"])
def test_a_struct_s_own_name_is_refused_where_a_member_is_read(
		statement: str, said: str) -> None:
	"""A path with no member stepped through nothing: `s` in a `when`
	reached all four backends as a name none defines, and they raised
	`UnknownName`; `invariant s.len == s` was taken and maintained nothing.
	`size(s)` in a `when` measures the struct since 26.981."""
	text = SCHEMA.replace("invariant s.len == size(s.body);", statement)
	with pytest.raises(SituError) as caught:
		parse_text(text, path="s.situ")
	assert said in caught.value.diagnostic.render()
