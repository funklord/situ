"""`u32 size; u8 data[size];` is refused, and says how to write it.

`[size]` is an attribute by decision 0006, which reads the language's closed
vocabulary and never a schema's names, so the parse of a struct cannot change
with what the struct declares. The refusal stands. What was wrong is what it
said: "`[size]` means nothing here" is true of the attribute and tells an
author who wrote a length nothing, in the commonest shape binary formats have.

`[(size)]` is an expression and so a size, under 0006 as written. It has to be
shown to work, since the diagnostic now sends people to it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import fourway
from situc.diagnostics import SituError
from situc.parser import ATTRIBUTE_NAMES, parse_text
from situc.wellformed import VALUE_ATTRIBUTES


def refusal(body: str) -> str:
	with pytest.raises(SituError) as caught:
		parse_text("target buffer;\nendian big;\n" + body, path="s.situ")
	return caught.value.diagnostic.render()


@pytest.mark.parametrize("name", sorted(VALUE_ATTRIBUTES))
def test_the_refusal_names_the_spelling_that_works(name: str) -> None:
	said = refusal(f"struct p {{ u32 {name}; u8 data[{name}]; }}\n")
	assert f"`[{name}]` is read as an attribute" in said
	assert f"`[({name})]`" in said


def test_a_member_declared_after_the_bracket_is_named_too() -> None:
	"""Later members are named as well: a size may not read them, but the
	author is still asking for the member and still wants the reason."""
	assert "read as an attribute" in refusal(
		"struct p { u8 data[size]; u32 size; }\n")


def test_with_no_such_member_the_attribute_s_own_refusal_stands() -> None:
	"""The control: nothing here looks like a length, so `[max]` is what it
	looks like, and the message for it is not disturbed."""
	assert "`[max]` needs a value" in refusal("struct p { u8 x [max]; }\n")


def test_a_flag_beside_a_member_of_its_name_keeps_its_meaning() -> None:
	"""`[secret]` means something bare, so this is not a mistake to report."""
	parse_text("target buffer;\nendian big;\n"
	           "struct p { u8 secret; u8 k [secret]; }\n", path="s.situ")


def test_a_value_on_the_attribute_is_not_a_size() -> None:
	"""`[max = 3]` beside a member called `max` is simply a bound."""
	parse_text("target buffer;\nendian big;\n"
	           "struct p { u8 max; u8 x [max = 3]; }\n", path="s.situ")


def test_every_name_listed_is_an_attribute() -> None:
	assert VALUE_ATTRIBUTES <= ATTRIBUTE_NAMES


@pytest.mark.skipif(not fourway.COMPLETE, reason="needs all four toolchains")
def test_the_spelling_it_recommends_sizes_the_run(tmp_path: Path) -> None:
	"""All four read the same length from `[(size)]`, on a message with two
	bytes of data and one that claims more than the frame holds."""
	schema = tmp_path / "unit.situ"
	schema.write_text("target buffer;\nendian big;\n"
	                  "struct p { u8 size; u8 data[(size)]; u16 tail; }\n",
	                  encoding="ascii")
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
