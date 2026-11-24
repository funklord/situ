"""Rules over a run's elements in a `when` (holder, 2026-11-24).

`any(r in s.rules: r.kind == 0)`, `all(...)`, `count(r in s.rules: ...)`
and `count(s.rules)`: each element asked over its own view, the run
walked once. fuzznet's remote permit holds "none stands alone" by hand --
an element meaning none, with others beside it -- which is the first rule
here, and "no more than two nines" the second, `count` with a predicate.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import fourway
from situc.diagnostics import SituError
from situc.layout import solve
from situc.parser import parse_text
from situc.resolve import resolve

SCHEMA = """target buffer;
endian big;
struct g { u8 kind; u8 n; u8 name[n]; }
struct s { u8 c [max = 8]; g gs[c]; }
when any(x in s.gs: x.kind == 0) && count(s.gs) > 1
	refuse none_alone "none stands alone";
when count(x in s.gs: x.kind == 9) > 2
	refuse too_many_nines "at most two nines";
when all(x in s.gs: x.n == 0) && count(s.gs) == 4
	refuse four_empty "four elements, none named";
"""


def element(kind: int, name: bytes = b"") -> bytes:
	return bytes([kind, len(name)]) + name


def message(*elements: bytes, count: int | None = None) -> bytes:
	return bytes([len(elements) if count is None else count]) + b"".join(elements)


#: (label, bytes, verdict), worked by hand. Member checks come first, so a
#: run the frame cuts short is BOUNDS whatever the rules say.
CASES = [
	("none alone", message(element(0)), "0"),
	("none beside another", message(element(0), element(1)), "2"),
	("none last of three", message(element(1), element(2, b"a"), element(0)),
	 "2"),
	("no none", message(element(1), element(2, b"ab")), "0"),
	("two nines", message(element(9), element(9)), "0"),
	("three nines", message(element(9), element(9, b"x"), element(9)), "2"),
	("four, one named", message(element(1), element(1), element(1),
	                            element(1, b"z")), "0"),
	("four, none named", message(*[element(1)] * 4), "2"),
	("three declared, two there", message(element(1), element(1), count=3),
	 "1"),
	("empty", message(), "0"),
]


@pytest.mark.skipif(not fourway.COMPLETE, reason="needs all four toolchains")
def test_all_four_hold_the_rules(tmp_path: Path) -> None:
	source = tmp_path / "unit.situ"
	source.write_text(SCHEMA, encoding="ascii")
	command = fourway.build(tmp_path, source)
	for label, data, verdict in CASES:
		for name, argv in command.items():
			held = fourway.answers(argv, data, tmp_path) \
				.split("-- s\n")[1].split("\n-- ")[0]
			line = next(one for one in held.splitlines()
			            if one.startswith("validate "))
			assert line.split()[1] == verdict, (label, name, held)


@pytest.mark.parametrize(("rule", "said"), [
	("any(x in s.gs: x.kind == s.c)", "which is not a member of `x`"),
	("any(x in s.c: x == 1)", "is not a run of structs"),
	("any(x in s.gs: x.nope == 1)", "has no field `nope`"),
	("any(x in s.gs: any(y in s.gs: y.kind == 1))",
	 "inside another rule over a run"),
])
def test_what_a_rule_cannot_read_is_refused(rule: str, said: str) -> None:
	text = ("target buffer;\nendian big;\n"
	        "struct g { u8 kind; u8 n; u8 name[n]; }\n"
	        "struct s { u8 c [max = 8]; g gs[c]; }\n"
	        f"when {rule} refuse r \"x\";\n")
	with pytest.raises(SituError) as caught:
		schema = parse_text(text)
		resolve(schema, solve(schema))
	assert said in caught.value.diagnostic.render(), \
		caught.value.diagnostic.render()


def test_a_rule_outside_a_when_is_refused() -> None:
	text = ("target buffer;\nendian big;\n"
	        "struct g { u8 kind; }\n"
	        "struct s { u8 c; g gs[c]; u8 t[any(x in s.gs: x.kind == 1)]; }\n")
	with pytest.raises(SituError) as caught:
		parse_text(text)
	assert "a rule is a `when`" in caught.value.diagnostic.message


UNIQUE = """target buffer;
endian big;
struct nm { u8 n; u8 text[n]; u8 tag; }
struct s { u8 c [max = 8]; nm names[c]; }
when !unique(x in s.names: x.text) refuse twice "a name twice";
when !unique(x in s.names: x.tag) refuse tag_twice "a tag twice";
"""


def named(text: bytes, tag: int) -> bytes:
	return bytes([len(text)]) + text + bytes([tag])


#: The group list's rule, `unique` by a run of bytes, and by a number.
UNIQUE_CASES = [
	("two names", message(named(b"a", 1), named(b"b", 2)), "0"),
	("a name twice", message(named(b"a", 1), named(b"a", 2)), "2"),
	("a tag twice", message(named(b"a", 1), named(b"b", 1)), "2"),
	("a name and its prefix", message(named(b"ab", 1), named(b"a", 2)), "0"),
	("first and third", message(named(b"a", 1), named(b"b", 2),
	                            named(b"a", 3)), "2"),
	("an empty name twice", message(named(b"", 1), named(b"", 2)), "2"),
	("none", message(), "0"),
]


@pytest.mark.skipif(not fourway.COMPLETE, reason="needs all four toolchains")
def test_all_four_hold_unique(tmp_path: Path) -> None:
	"""fuzznet's group list holds "no name twice" by hand: `unique`, every
	pair compared, by a run of bytes or by a number."""
	source = tmp_path / "unit.situ"
	source.write_text(UNIQUE, encoding="ascii")
	command = fourway.build(tmp_path, source)
	for label, data, verdict in UNIQUE_CASES:
		for name, argv in command.items():
			held = fourway.answers(argv, data, tmp_path) \
				.split("-- s\n")[1].split("\n-- ")[0]
			line = next(one for one in held.splitlines()
			            if one.startswith("validate "))
			assert line.split()[1] == verdict, (label, name, held)


@pytest.mark.parametrize(("schema", "said"), [
	(UNIQUE.replace("u8 c [max = 8]", "u8 c"), "elements, and `unique`"),
	(UNIQUE.replace("struct s { u8 c [max = 8]; nm names[c]; }",
	                "struct s { u16 c; nm names[c]; }"),
	 "elements, and `unique`"),
	(UNIQUE.replace("x.tag)", "x.tag + 1)"), "compares one member"),
])
def test_unique_is_bounded_and_over_one_member(schema: str,
                                               said: str) -> None:
	with pytest.raises(SituError) as caught:
		parsed = parse_text(schema)
		resolve(parsed, solve(parsed))
	assert said in caught.value.diagnostic.render(), \
		caught.value.diagnostic.render()


ENUMERATED = """target buffer;
endian big;
enum k : u8 { none = 0, read = 1, write = 2, }
struct grant { k kind; u8 n; u8 who[n]; }
struct p { u8 c [max = 8]; grant grants[c]; }
when any(g in p.grants: g.kind == k.none) && count(p.grants) > 1
	refuse none_alone "none stands alone";
"""

#: An enum member in the body, and the element's own member at a static
#: offset -- which C++ loads from the view rather than through a getter, so
#: a body rendered over the enclosing struct read ITS first byte, the count,
#: and called "none beside another" clean (found by hand, 2026-11-24).
ENUMERATED_CASES = [
	("none beside another", bytes.fromhex("0200000100"), "2"),
	("another, then none", bytes.fromhex("020100000000"), "2"),
	("none alone", bytes.fromhex("010000"), "0"),
	("no none", bytes.fromhex("0201000200"), "0"),
]


@pytest.mark.skipif(not fourway.COMPLETE, reason="needs all four toolchains")
def test_an_enum_member_reads_each_element(tmp_path: Path) -> None:
	source = tmp_path / "unit.situ"
	source.write_text(ENUMERATED, encoding="ascii")
	command = fourway.build(tmp_path, source)
	for label, data, verdict in ENUMERATED_CASES:
		for name, argv in command.items():
			held = fourway.answers(argv, data, tmp_path) \
				.split("-- p\n")[1].split("\n-- ")[0]
			line = next(one for one in held.splitlines()
			            if one.startswith("validate "))
			assert line.split()[1] == verdict, (label, name, held)


#: The run is the struct's first member, so it sits at a static offset and
#: has no `_offset()` -- which C++ and Rust called, and did not compile
#: (found 2026-11-24 framing fuzznet's permit shape). A `while` run to the
#: frame's end, as a permit is.
FIRST = """target buffer;
endian big;
struct grant { u8 kind; u8 n; u8 who[n]; }
struct p { grant grants[] while (1) | end; }
when any(g in p.grants: g.kind == 0) && count(p.grants) > 1
	refuse none_alone "none stands alone";
"""

FIRST_CASES = [
	("none beside another", bytes.fromhex("00000100"), "2"),
	("another, then none", bytes.fromhex("01000000"), "2"),
	("none alone", bytes.fromhex("0000"), "0"),
	("no none", bytes.fromhex("01000200"), "0"),
]


@pytest.mark.skipif(not fourway.COMPLETE, reason="needs all four toolchains")
def test_a_run_at_a_static_offset_is_walked(tmp_path: Path) -> None:
	source = tmp_path / "unit.situ"
	source.write_text(FIRST, encoding="ascii")
	command = fourway.build(tmp_path, source)
	for label, data, verdict in FIRST_CASES:
		for name, argv in command.items():
			held = fourway.answers(argv, data, tmp_path) \
				.split("-- p\n")[1].split("\n-- ")[0]
			line = next(one for one in held.splitlines()
			            if one.startswith("validate "))
			assert line.split()[1] == verdict, (label, name, held)

#: A fixed-size element, which has no `extent` of its own: C measured it by
#: its size constant, and C++, Rust and Python called the `extent` it does
#: not have -- found asking the rule against a bit-field element and a lone
#: `u16` (2026-11-24). `unique` walks the run the same way, twice.
FIXED = """target buffer;
endian big;
bit_order msb_first;
struct g { u4 kind; u4 flag; }
struct s { u8 c [max = 8]; g gs[c]; }
when any(x in s.gs: x.kind == 0) && count(s.gs) > 1
	refuse none_alone "none stands alone";
when !unique(x in s.gs: x.kind) refuse twice "a kind twice";
"""

FIXED_CASES = [
	("none beside another", bytes([2, 0x05, 0x12]), "2"),
	("a kind twice", bytes([2, 0x15, 0x12]), "2"),
	("two kinds", bytes([2, 0x15, 0x23]), "0"),
	("one", bytes([1, 0x10]), "0"),
	("three kinds", bytes([3, 0x15, 0x23, 0x31]), "0"),
	("first and third alike", bytes([3, 0x15, 0x23, 0x14]), "2"),
]


@pytest.mark.skipif(not fourway.COMPLETE, reason="needs all four toolchains")
def test_a_fixed_size_element_is_measured_by_its_size(tmp_path: Path) -> None:
	source = tmp_path / "unit.situ"
	source.write_text(FIXED, encoding="ascii")
	command = fourway.build(tmp_path, source)
	for label, data, verdict in FIXED_CASES:
		for name, argv in command.items():
			held = fourway.answers(argv, data, tmp_path) \
				.split("-- s\n")[1].split("\n-- ")[0]
			line = next(one for one in held.splitlines()
			            if one.startswith("validate "))
			assert line.split()[1] == verdict, (label, name, held)


@pytest.mark.skipif(not fourway.COMPLETE, reason="needs a C compiler")
@pytest.mark.parametrize(("schema", "cases", "shape_name"), [
	(SCHEMA, CASES, "s"), (UNIQUE, UNIQUE_CASES, "s"),
	(ENUMERATED, ENUMERATED_CASES, "p"), (FIRST, FIRST_CASES, "p"),
	(FIXED, FIXED_CASES, "s"),
])
def test_both_walkers_hold_the_rules(tmp_path: Path, schema: str,
                                     cases: list[tuple[str, bytes, str]],
                                     shape_name: str) -> None:
	"""The image carries each rule -- its run, its element, its body as a
	program over the element, `unique`'s key -- and `RULE` where it stood,
	so both walkers give the four backends' verdict rather than declining.
	A logical `!` is a comparison with zero, which is how `!unique(...)`
	reaches an image at all."""
	from test_walker_c import (c_verdict, packed_text, python_verdict,
	                           shape_named_text)
	blob  = packed_text(schema)
	shape = shape_named_text(schema, shape_name)
	for label, data, verdict in cases:
		assert python_verdict(blob, data, shape) == verdict, label
		assert c_verdict(tmp_path, blob, data, shape) == verdict, label
