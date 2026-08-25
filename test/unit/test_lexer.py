"""Lexer tests (project.md section 6)."""

from __future__ import annotations

import pytest

from situc.diagnostics import Source, SituError
from situc.lexer import TokenKind, tokenize


def kinds(text: str) -> list[TokenKind]:
	return [token.kind for token in tokenize(Source("<test>", text))]


def texts(text: str) -> list[str]:
	return [token.text for token in tokenize(Source("<test>", text))[:-1]]


def values(text: str) -> list[int]:
	return [token.value for token in tokenize(Source("<test>", text))
	        if token.kind is TokenKind.INT]


def test_empty_source_is_just_eof() -> None:
	assert kinds("") == [TokenKind.EOF]


def test_identifiers_and_symbols() -> None:
	assert texts("u8 seq;") == ["u8", "seq", ";"]


def test_underscore_leading_identifier() -> None:
	assert texts("_private") == ["_private"]


@pytest.mark.parametrize(("source", "expected"), [
	("0", 0),
	("1500", 1500),
	("0x06", 6),
	("0xFF", 255),
	("0xff", 255),
	("0b1010", 10),
	("1_000", 1000),
	("0xDE_AD", 0xDEAD),
	("0b1010_1010", 0b10101010),
])
def test_integer_literals(source: str, expected: int) -> None:
	assert values(source) == [expected]


def test_integer_literal_keeps_its_written_form() -> None:
	"""Unparsing must not turn 0x06 into 6; the schema author chose the base."""
	assert texts("0x06") == ["0x06"]


@pytest.mark.parametrize("source", ["0x", "0b", "0xg", "0b2", "0x1g", "12ab"])
def test_malformed_integer_literals_rejected(source: str) -> None:
	with pytest.raises(SituError):
		tokenize(Source("<test>", source))


def test_line_comment_runs_to_end_of_line() -> None:
	assert texts("u8 a; // comment ; u16 b;\nu8 c;") == ["u8", "a", ";", "u8", "c", ";"]


def test_line_comment_at_end_of_file() -> None:
	assert texts("u8 a; // trailing") == ["u8", "a", ";"]


def test_block_comment() -> None:
	assert texts("u8 /* skip me */ a;") == ["u8", "a", ";"]


def test_block_comments_nest() -> None:
	assert texts("u8 /* outer /* inner */ still outer */ a;") == ["u8", "a", ";"]


def test_unterminated_block_comment_rejected() -> None:
	with pytest.raises(SituError, match="unterminated block comment"):
		tokenize(Source("<test>", "u8 /* never closed"))


def test_unterminated_nested_block_comment_rejected() -> None:
	with pytest.raises(SituError, match="unterminated block comment"):
		tokenize(Source("<test>", "u8 /* outer /* inner */ a;"))


def test_multi_character_symbols_scan_longest_first() -> None:
	assert texts("a << b >> c == d != e <= f >= g") == [
		"a", "<<", "b", ">>", "c", "==", "d", "!=", "e", "<=", "f", ">=", "g",
	]


def test_single_character_symbols_still_scan() -> None:
	assert texts("a < b > c = d") == ["a", "<", "b", ">", "c", "=", "d"]


def test_string_literal() -> None:
	tokens = tokenize(Source("<test>", 'import "std/codecs.situ";'))
	assert tokens[1].kind is TokenKind.STRING
	assert tokens[1].text == "std/codecs.situ"


def test_string_escapes() -> None:
	tokens = tokenize(Source("<test>", r'"a\tb\nc\\d\"e"'))
	assert tokens[0].text == 'a\tb\nc\\d"e'


def test_unknown_escape_rejected() -> None:
	with pytest.raises(SituError, match="unknown escape sequence"):
		tokenize(Source("<test>", r'"a\qb"'))


def test_unterminated_string_rejected() -> None:
	with pytest.raises(SituError, match="unterminated string literal"):
		tokenize(Source("<test>", '"no closing quote'))


def test_string_may_not_span_lines() -> None:
	with pytest.raises(SituError, match="unterminated string literal"):
		tokenize(Source("<test>", '"first\nsecond"'))


# Spelled with escapes so this file stays ASCII and passes its own convention
# check (doc/decision/0003-source-formatting.md).
def test_non_ascii_outside_string_rejected() -> None:
	with pytest.raises(SituError, match="non-ASCII"):
		tokenize(Source("<test>", "u8 caf\u00e9;"))


def test_non_ascii_inside_string_allowed() -> None:
	"""Section 6 confines the ASCII rule to source outside string literals."""
	tokens = tokenize(Source("<test>", '"caf\u00e9"'))
	assert tokens[0].text == "caf\u00e9"


def test_unexpected_character_rejected() -> None:
	with pytest.raises(SituError, match="unexpected character"):
		tokenize(Source("<test>", "u8 a $ b;"))


def test_token_spans_point_at_source() -> None:
	tokens = tokenize(Source("<test>", "u8 seq;"))
	assert tokens[1].span.text() == "seq"
	assert tokens[1].span.column == 4


def test_spans_survive_newlines() -> None:
	tokens = tokenize(Source("<test>", "u8 a;\nu16 b;"))
	line, column = tokens[3].span.source.locate(tokens[3].span.start)
	assert (tokens[3].text, line, column) == ("u16", 2, 1)
