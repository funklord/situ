"""The phase 1 acceptance property: dump-ast round-trips example 5.1 exactly.

"Round-trips" is read as: parsing, rendering back to source, and reparsing
reaches the same tree. The structural dump is the comparison surface because it
excludes spans, which necessarily differ between the two parses.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from situc import ast
from situc.diagnostics import Source
from situc.dump import dump
from situc.parser import parse_decls, parse_text
from situc.unparse import unparse

SCHEMAS = Path(__file__).resolve().parents[1] / "schema"

EXAMPLE_5_1 = (SCHEMAS / "header.situ").read_text(encoding="ascii")

EXPECTED_DUMP = """\
schema
	target buffer
	endian big
	bit_order msb_first
	const MAX_PAYLOAD = 1500
	enum msg_type : u8 default=error
		hello = 0x01
		data = 0x02
		close = 0x03
	struct flags
		field urgent : bit
		field ack : bit
		field priority : u3
		reserved u3
			attr must_be_zero
	struct header
		field version : u8
			attr must_eq = 1
		field type : msg_type
		field flags : flags
		field length : u16
			attr max = MAX_PAYLOAD
		field seq : u32
			pin 0x05
	require size(header) == 9
	require absolute_static(header)
	require in_place(header.seq)
"""


def roundtrip(source: str) -> None:
	first = parse_text(source)
	again = parse_text(unparse(first))
	assert dump(again) == dump(first)


def roundtrip_decls(source: str) -> None:
	"""Round-trip without expanding imports (17.0a).

	`parse` resolves an `import` now, and a schema parsed from a string has
	no directory to resolve one against. What these cases test is that the
	*directive* survives being printed and read back, which is a question
	about the parser and `unparse` rather than about the file system.
	"""
	first = parse_decls(Source("s.situ", source))
	again = parse_decls(Source("s.situ", unparse(first)))
	assert dump(again) == dump(first)


def test_example_5_1_dump_is_exact() -> None:
	assert dump(parse_text(EXAMPLE_5_1)) == EXPECTED_DUMP


def test_example_5_1_round_trips() -> None:
	roundtrip(EXAMPLE_5_1)


def test_example_5_1_is_stable_under_repeated_unparsing() -> None:
	"""Unparsing must reach a fixed point, or `situc doc` would never settle."""
	once  = unparse(parse_text(EXAMPLE_5_1))
	twice = unparse(parse_text(once))
	assert once == twice


def test_integer_bases_survive_a_round_trip() -> None:
	"""The author chose the base; rewriting 0x06 as 6 loses their intent."""
	source = unparse(parse_text("const A = 0x06; const B = 0b1010; const C = 1_000;"))
	assert "0x06" in source
	assert "0b1010" in source
	assert "1_000" in source


@pytest.mark.parametrize("source", [
	"target buffer;",
	"endian little;",
	"bit_order lsb_first;",
	"const N = 1 + 2 * 3;",
	"const N = (1 + 2) * 3;",
	"const N = a - (b - c);",
	"const N = ~a & 0xFF;",
	"const N = 1 << 4 | 3;",
	"enum E : u8 { a = 1, b = 2, }",
	"enum E : u8 { a = 1, default = pass, }",
	"struct S { }",
	"struct S { u8 a; u16 b; }",
	"struct S { u8 buf[12]; }",
	"struct S { u8 buf[]; }",
	"struct S { u32 seq @ 0x06; }",
	"struct S { u8 a [must_eq = 1]; }",
	"target mmio; register S @ 0x00 { width = 32; access_width = 32; bit x [rw]; }",
	"struct S { u8 a [must_eq = 1, max = 4]; }",
	"struct S [allow_straddle] { u12 wide; }",
	"struct S { reserved u3 [must_be_zero]; }",
	"struct S { reserved u8; }",
	"struct S { positional { u16 a; u16 b; } }",
	"struct S { positional { positional { u8 a; } } }",
	"require size(H) == 10;",
	"assert in_place(H.seq);",
	"require in_place(M.recs[].value);",
	"require aligned(H.seq, 4);",
	"require align_up(size(H), 4) == 12;",
])
def test_constructs_round_trip(source: str) -> None:
	roundtrip(source)


@pytest.mark.parametrize("source", [
	'import "std/codecs.situ";',
	r'import "a\tb\\c";',
])
def test_an_import_directive_round_trips(source: str) -> None:
	"""Including its escapes: a path is a string literal, and the printer has
	to give back the bytes the lexer read."""
	roundtrip_decls(source)


SLIP_FRAMED = r"""target buffer;
endian big;
bit_order msb_first;
codec slip {
	kernel = stuffing(worst_case = 2, per = 1, unit = byte, code = slip);
}
impl slip derived;
struct frame {
	coded body(slip) until "\xC0" { u8 payload[remaining]; }
}
"""


def test_a_binary_delimiter_survives_a_round_trip() -> None:
	"""The half of `\\xNN` that is easy to forget, and breaks quietly.

	Reading the escape is only useful if writing it back produces source that
	reads the same way. Before `_escape` learned it, a 0xC0 delimiter was
	printed as the raw byte -- which the lexer would have accepted, since
	non-ASCII is refused outside a string literal and allowed inside one, and
	which still puts a byte in a `.situ` file that this project's ASCII rule
	forbids and its ascii-codec readers cannot open.
	"""
	roundtrip(SLIP_FRAMED)

	printed = unparse(parse_text(SLIP_FRAMED))
	assert r"\xC0" in printed, printed
	assert printed.isascii(), "unparse emitted a byte no situ source may carry"


def test_the_delimiter_is_the_byte_and_not_its_spelling() -> None:
	"""`\\xC0` has to reach the AST as one byte 0xC0.

	The spelling is four ASCII characters and the delimiter is one byte, and
	nothing downstream should be able to tell which way it was written -- a
	scan comparing four bytes against a frame would never terminate.
	"""
	schema = parse_text(SLIP_FRAMED)
	found = [member.until.delimiter
	         for struct in schema.structs()
	         for member in struct.members
	         if isinstance(member, ast.Coded) and member.until is not None]

	assert found == [b"\xc0"], found


def test_precedence_survives_a_round_trip() -> None:
	"""Parenthesisation must be regenerated where it changes meaning."""
	source = unparse(parse_text("const N = (1 + 2) * 3;"))
	assert "(1 + 2) * 3" in source


def test_redundant_parentheses_are_dropped() -> None:
	source = unparse(parse_text("const N = (1 * 2) + 3;"))
	assert "1 * 2 + 3" in source


def test_left_associativity_survives_a_round_trip() -> None:
	source = unparse(parse_text("const N = a - (b - c);"))
	assert "a - (b - c)" in source
