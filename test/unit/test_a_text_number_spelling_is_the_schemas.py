"""Which spellings of a text number a reader accepts is the schema's choice.

The holder, asked through fuzznet (inbox 20261123T1050Z) whether leading
zeros are allowed: "Shouldn't this be programmable in situ?" It is:
without `[minimal]` every form accepts `007` and the map calls the field
NonCanonical; with it, every form refuses a leading zero, and a
hexadecimal one an upper-case digit as well. These are the verdicts,
worked by hand and held in all four backends, one form at a time:
delimited (`until`), to the end of the frame (`[remaining]`, fuzznet's
settings value), a fixed width, hexadecimal and signed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import fourway

FORMS = """target buffer;
endian big;
struct s {
	decimal u32 a until "," MIN;
	hex u32     b until "," MIN;
	decimal i32 c until "," MIN;
	decimal u16 d[3] MIN;
	decimal u64 e[remaining] MIN;
}
"""

#: One field spelled otherwise at a time; the rest are each value's one
#: spelling, so a refusal names the field under test.
PLAIN = {"a": b"1", "b": b"1", "c": b"1", "d": b"100", "e": b"1"}


def _packet(field: str, spelling: bytes) -> bytes:
	parts = dict(PLAIN, **{field: spelling})
	return (parts["a"] + b"," + parts["b"] + b"," + parts["c"] + b","
	        + parts["d"] + parts["e"])


#: (field, spelling, accepted without [minimal], accepted with it)
CASES = [
	("a", b"007", True, False),
	("a", b"0", True, True),
	("b", b"0ff", True, False),
	("b", b"FF", True, False),
	("b", b"ff", True, True),
	("c", b"-007", True, False),
	("c", b"-7", True, True),
	# A sign is one optional `-`: `+7` and `-0` are second spellings of
	# values that have one, refused whatever the schema says (8.6.2).
	("c", b"+7", False, False),
	("c", b"-0", False, False),
	("d", b"007", True, False),
	("e", b"007", True, False),
	("e", b"0", True, True),
]


@pytest.mark.skipif(not fourway.COMPLETE, reason="needs all four toolchains")
@pytest.mark.parametrize("minimal", [False, True], ids=["loose", "minimal"])
def test_every_form_takes_the_schemas_word(tmp_path: Path,
                                           minimal: bool) -> None:
	source = tmp_path / "unit.situ"
	source.write_text(FORMS.replace("MIN", "[minimal]" if minimal else ""),
	                  encoding="ascii")
	command = fourway.build(tmp_path, source)
	assert len(command) == 4
	assert "validate 0" in fourway.answers(
		command["c"], _packet("a", PLAIN["a"]), tmp_path)
	for field, spelling, loose, strict in CASES:
		packet = _packet(field, spelling)
		given  = {name: fourway.answers(argv, packet, tmp_path)
		          for name, argv in command.items()}
		assert len(set(given.values())) == 1, (field, spelling, given)
		accepted = "validate 0" in given["c"]
		assert accepted == (strict if minimal else loose), (field, spelling)


# -- 26.1166: the rules `[minimal]` tied together, one at a time ------------

RULES = """target buffer;
endian big;
struct s {
	decimal i32 a until "," [plus_sign, negative_zero];
	decimal u32 b until "," [plus_sign];
	hex u32     c until "," [hex_case = upper];
	hex u32     d until "," [leading_zeros = refused];
	hex u32     e until "," [minimal, hex_case = upper];
	decimal i32 f until ",";
	u8          r[remaining];
}
"""

RULED = {"a": b"1", "b": b"1", "c": b"1", "d": b"1", "e": b"1", "f": b"1"}

#: (field, spelling, accepted), worked by hand.
RULE_CASES = [
	("a", b"+7", True), ("a", b"-0", True), ("a", b"-00", True),
	("a", b"+-7", False), ("a", b"+", False), ("a", b"-7", True),
	("b", b"+7", True), ("b", b"+", False), ("b", b"-7", False),
	("c", b"FF", True), ("c", b"ff", False), ("c", b"0FF", True),
	("d", b"0ff", False), ("d", b"ff", True), ("d", b"FF", True),
	("e", b"FF", True), ("e", b"ff", False), ("e", b"0F", False),
	# The control: a signed number that says nothing refuses both, as
	# before.
	("f", b"+7", False), ("f", b"-0", False), ("f", b"-7", True),
]


def _ruled(field: str, spelling: bytes) -> bytes:
	parts = dict(RULED, **{field: spelling})
	return b",".join(parts[name] for name in "abcdef") + b","


@pytest.mark.skipif(not fourway.COMPLETE, reason="needs all four toolchains")
def test_each_rule_is_the_schemas(tmp_path: Path) -> None:
	source = tmp_path / "unit.situ"
	source.write_text(RULES, encoding="ascii")
	command = fourway.build(tmp_path, source)
	assert len(command) == 4
	assert "validate 0" in fourway.answers(command["c"], _ruled("a", b"1"),
	                                       tmp_path)
	for field, spelling, accepted in RULE_CASES:
		packet = _ruled(field, spelling)
		given  = {name: fourway.answers(argv, packet, tmp_path)
		          for name, argv in command.items()}
		assert len(set(given.values())) == 1, (field, spelling, given)
		assert ("validate 0" in given["c"]) == accepted, (field, spelling)


@pytest.mark.parametrize(("member", "said"), [
	('decimal u32 n until "," [hex_case = upper];', "have no case"),
	('decimal u32 n until "," [negative_zero];', "unsigned"),
	("decimal u32 n[3] [plus_sign];", "delimited number"),
	('decimal i32 n until "," [minimal, plus_sign];', "contradict"),
	('decimal i32 n until "," [minimal, negative_zero];', "contradict"),
	('decimal u32 n until "," [minimal, leading_zeros = allowed];',
	 "contradict"),
	('hex u32 n until "," [hex_case = title];', "`lower` or `upper`"),
	('decimal u32 n until "," [leading_zeros = sometimes];',
	 "`refused` or `allowed`"),
	("u32 n [plus_sign];", "means nothing here"),
])
def test_a_rule_that_cannot_hold_is_refused(member: str, said: str) -> None:
	from situc.diagnostics import SituError
	from situc.parser import parse_text
	from situc.wellformed import check
	text = ("target buffer;\nendian big;\nstruct s {\n\t" + member
	        + "\n\tu8 r[remaining];\n}\n")
	with pytest.raises(SituError) as caught:
		check(parse_text(text, path="s.situ"))
	assert said in str(caught.value)


@pytest.mark.parametrize(("member", "canonical"), [
	('decimal u32 n until "," [minimal];', True),
	('decimal u32 n until "," [leading_zeros = refused];', True),
	('hex u32 n until "," [leading_zeros = refused];', False),
	('hex u32 n until "," [leading_zeros = refused, hex_case = upper];', True),
	('decimal i32 n until "," [leading_zeros = refused, plus_sign];', False),
	('decimal u32 n until ",";', False),
	# A width fixes the zeros and not the case: `ff`, `FF` and `fF` are
	# one value in every reader, so only a stated case is one spelling --
	# and a field too narrow for a digit past nine has no case at all.
	("hex u8 n[2];", False),
	("hex u8 n[2] [hex_case = upper];", True),
	("hex u2 n[1];", True),
	("decimal u16 n[3];", True),
])
def test_the_map_follows_the_rules(member: str, canonical: bool) -> None:
	"""One spelling per value takes every rule; any one left loose is a
	second spelling, which is what `canonical` reports."""
	from situc.capability import Axis
	from situc.layout import solve
	from situc.parser import parse_text
	from situc.resolve import resolve
	text = ("target buffer;\nendian big;\nbit_order msb_first;\n"
	        "struct s {\n\t" + member
	        + "\n\tu8 r[remaining];\n}\n")
	schema   = parse_text(text, path="s.situ")
	resolved = resolve(schema, solve(schema))
	entry    = next(one for one in resolved.structs["s"].entries
	                if one.placement.path == "s.n")
	said = entry.vector.get(Axis.CANONICAL).base == "Canonical"
	assert said == canonical, member


CASED = """target buffer;
endian big;
struct s {
	hex u32 c until "," [hex_case = upper];
	hex u32 d until "," [leading_zeros = refused];
	hex u32 e until "," [minimal, hex_case = upper];
	decimal i32 f until "," [minimal];
	u8      r[remaining];
}
"""


@pytest.mark.skipif(not fourway.COMPLETE, reason="needs a C compiler")
def test_both_walkers_read_the_rules(tmp_path: Path) -> None:
	"""The image's spelling row carries the rules above the radix, and
	both walkers read it; `+07` under `[minimal]` names the spelling as C
	does, skipping only the `-` there."""
	from test_walker_c import (c_verdict, packed_text, python_verdict,
	                           shape_named_text)
	blob  = packed_text(CASED)
	shape = shape_named_text(CASED, "s")
	plain = {"c": b"1", "d": b"1", "e": b"1", "f": b"1"}
	cases = [("c", b"FF", True), ("c", b"ff", False), ("d", b"0ff", False),
	         ("d", b"Ff", True), ("e", b"FF", True), ("e", b"ff", False),
	         ("e", b"0F", False), ("f", b"-07", False), ("f", b"+7", False),
	         ("f", b"-7", True)]
	for field, spelling, accepted in cases:
		parts  = dict(plain, **{field: spelling})
		packet = b",".join(parts[name] for name in "cdef") + b","
		said   = python_verdict(blob, packet, shape)
		assert (said == "0") == accepted, (field, spelling, said)
		assert c_verdict(tmp_path, blob, packet, shape) == said, \
			(field, spelling)


def test_the_dissector_reads_a_plus_signed_length(tmp_path: Path) -> None:
	"""The seventh reader: a length written `+3` under `[plus_sign]` places
	the body as the four backends do. The dissector's own digit read
	refused any byte that is not a digit, so the length read 0 there and
	3 everywhere else. Offsets worked by hand."""
	from test_dissector import LUA, dissect
	if LUA is None:
		pytest.skip("no Lua interpreter")
	schema = tmp_path / "plus.situ"
	schema.write_text("target buffer;\nendian big;\nstruct s {\n"
	                  '\tdecimal u16 n until "," [plus_sign];\n'
	                  "\tu8 body[n];\n\tu8 tail;\n}\n", encoding="ascii")
	consumed, rows = dissect(tmp_path, schema, "s", b"+3,abcZ")
	assert consumed == 7, rows
	assert ("s.tail", 6, 1, "90") in rows, rows


TOKEN_ARM = """target buffer;
endian big;
tokens word { a = "any", default = error, }
struct pick {
	peek u8 kind;
	variant body switch (kind) {
		case 'a': word w until " " | end;
		default: error;
	}
}
"""


@pytest.mark.skipif(not fourway.COMPLETE, reason="needs all four toolchains")
def test_a_token_in_an_arm_is_checked_and_compiles(tmp_path: Path) -> None:
	"""A delimited token member in a variant arm: C's check called the
	member form of `_ptr`, and an arm's takes the pointer and length out
	through parameters, so the header did not compile (fuzznet, inbox
	20261123T1616Z). Verdicts by hand: `any` and `any ` are the token,
	`axe` is no spelling of it, `b` selects the error arm."""
	source = tmp_path / "unit.situ"
	source.write_text(TOKEN_ARM, encoding="ascii")
	command = fourway.build(tmp_path, source)
	assert len(command) == 4
	for packet, valid in ((b"any", True), (b"any ", True), (b"axe", False),
	                      (b"b", False)):
		given = {name: fourway.answers(argv, packet, tmp_path)
		         for name, argv in command.items()}
		assert len(set(given.values())) == 1, (packet, given)
		assert ("validate 0" in given["c"]) == valid, (packet, given["c"])
