"""`u32 size; u8 data[size];` is a run of `size` bytes (26.886).

Decision 0006 read a lone name in brackets as an attribute whenever the name
is in the language's vocabulary, so the commonest shape in binary formats was
refused and the author sent to `[(size)]`. A name that always takes a value
cannot mean anything bare, so reading it as a size still decides from the
vocabulary alone: the parse never looks at what the struct declares, which
is the whole of 0006's reason, and no schema that was accepted changes
meaning.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import fourway
from situc import ast
from situc.diagnostics import SituError
from situc.parser import (ATTRIBUTE_NAMES, BARE_ATTRIBUTE_NAMES,
                          VALUE_ATTRIBUTES, parse_text)

HEAD = "target buffer;\nendian big;\n"


def refusal(body: str) -> str:
	with pytest.raises(SituError) as caught:
		parse_text(HEAD + body, path="s.situ")
	return caught.value.diagnostic.render()


def sized_by(body: str, member: str) -> ast.Expr | None:
	schema = parse_text(HEAD + body, path="s.situ")
	held   = {one.name: one for one in schema.structs()[0].members
	          if isinstance(one, ast.Field)}
	array  = held[member].array
	return None if array is None else array.size


@pytest.mark.parametrize("name", sorted(VALUE_ATTRIBUTES))
def test_every_value_name_sizes_a_run(name: str) -> None:
	"""The size is the member, and no attribute was left on the run."""
	size = sized_by(f"struct p {{ u32 {name}; u8 data[{name}]; }}\n", "data")
	assert isinstance(size, ast.NameRef)
	assert size.name == name


@pytest.mark.parametrize("name", sorted(VALUE_ATTRIBUTES))
def test_a_constant_may_have_the_name_too(name: str) -> None:
	"""The collision 0006 refused a constant for is gone for these names."""
	size = sized_by(f"const {name} = 2;\nstruct p {{ u8 data[{name}]; }}\n",
	                "data")
	assert isinstance(size, ast.NameRef)


@pytest.mark.parametrize("name", sorted(VALUE_ATTRIBUTES))
def test_with_no_such_member_the_missing_value_is_named(name: str) -> None:
	"""A bound left without its number is still that, not an unknown name."""
	said = refusal(f"struct p {{ u8 x [{name}]; }}\n")
	assert f"`[{name}]` needs a value" in said
	assert f"[{name} = ...]" in said


def test_a_flag_beside_a_member_of_its_name_keeps_its_meaning() -> None:
	"""`[secret]` means something bare, so it stays an attribute."""
	assert sized_by("struct p { u8 secret; u8 k [secret]; }\n", "k") is None


def test_a_value_on_the_attribute_is_not_a_size() -> None:
	"""`[max = 3]` beside a member called `max` is simply a bound."""
	assert sized_by("struct p { u8 max; u8 x [max = 3]; }\n", "x") is None


def test_a_second_bracket_is_still_attributes() -> None:
	"""Only the first group can be a size, so this one stays refused."""
	assert "`[max]`" in refusal("struct p { u8 max; u8 x[max] [max]; }\n")


def test_a_call_of_a_builtin_is_still_a_call() -> None:
	"""`min` and `max` are builtins as well, and 0006's third rule holds."""
	size = sized_by("struct p { u8 a; u8 b; u8 x[min(a, b)]; }\n", "x")
	assert not isinstance(size, ast.NameRef)


def test_the_vocabulary_is_split_with_nothing_left_over() -> None:
	assert VALUE_ATTRIBUTES <= ATTRIBUTE_NAMES
	assert BARE_ATTRIBUTE_NAMES | VALUE_ATTRIBUTES == ATTRIBUTE_NAMES
	assert not BARE_ATTRIBUTE_NAMES & VALUE_ATTRIBUTES


@pytest.mark.skipif(not fourway.COMPLETE, reason="needs all four toolchains")
@pytest.mark.parametrize("spelled", ["size", "(size)"])
def test_the_four_read_one_length(tmp_path: Path, spelled: str) -> None:
	"""All four read the same length, on a message with two bytes of data and
	one that claims more than the frame holds, and the parenthesised form
	that was the only spelling goes on meaning the same."""
	schema = tmp_path / "unit.situ"
	schema.write_text(HEAD + "struct p { u8 size; "
	                  f"u8 data[{spelled}]; u16 tail; }}\n", encoding="ascii")
	try:
		command = fourway.build(tmp_path, schema)
	except fourway.BuildFailed as failed:
		pytest.fail(f"a backend did not build: {failed}")

	assert len(command) == 4
	for packet in (bytes([2, 9, 9, 0, 7]), bytes([200, 1])):
		given = {name: fourway.answers(argv, packet, tmp_path)
		         for name, argv in command.items()}
		assert len(set(given.values())) == 1, given
	assert "len=2" in fourway.answers(command["c"], bytes([2, 9, 9, 0, 7]),
	                                  tmp_path)
