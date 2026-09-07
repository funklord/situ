"""Characters, and the encodings that decide what they are worth.

A byte is written `0x2C` and means that number. A character is written `','`
and means whatever an encoding says -- which for the structural bytes of a
text format is the same number in ASCII, UTF-8 and every ISO-8859 part, and
is deliberately not the same in UTF-16.

The difference is not spelling. Writing the number asserts an agreement
nobody checked; writing the character asks the compiler to check it, and the
`encoding` directive is what it checks against. A schema that says
`encoding ascii | utf8 | latin1` has stated that its bytes may be any of the
three, and a literal is accepted only where that statement does not change
its value.
"""

from __future__ import annotations

import pytest

from situc.diagnostics import SituError
from situc.layout import solve
from situc.lexer import TokenKind, tokenize
from situc.diagnostics import Source
from situc.parser import parse_text
from situc.resolve import ResolvedSchema, resolve
from situc.types import (CODE_UNIT_BYTES, TEXT_ENCODINGS, EncodingError,
                         character_value)

PREAMBLE = "target buffer;\nendian big;\n"


def build(body: str) -> ResolvedSchema:
	schema = parse_text(PREAMBLE + body)
	return resolve(schema, solve(schema))


def test_a_character_is_one_character() -> None:
	"""`'ab'` is not a character and is not silently a string: the quote
	says which kind of literal this is, and a run of them has its own."""
	for text in ("''", "'ab'", "'a"):
		with pytest.raises(SituError):
			tokenize(Source("t", f"x == {text}"))


def test_the_escapes_are_the_string_ones_plus_the_quote() -> None:
	"""One escape table for both literals, so a reader who has met `\\xNN`
	in a delimiter has met it here."""
	for text, want in (("'\\n'", "\n"), ("'\\x7B'", "{"), ("'\\''", "'"),
	                   ("'\\\\'", "\\"), ("'\\0'", "\0")):
		found = [t for t in tokenize(Source("t", text))
		         if t.kind is TokenKind.CHAR]
		assert len(found) == 1, text
		assert found[0].text == want, text


def test_a_character_switches_a_variant() -> None:
	"""The case this exists for: `case '{'` rather than `case 0x7B`."""
	resolved = build("struct s { u8 t; variant v switch (t) {"
	                 " case '{': u8 a; default: u8 b; } }\n")
	arms = {arm.value for entry in resolved.structs["s"].entries
	        for arm in entry.placement.arm_cases}
	assert 0x7B in arms


def test_and_a_while_predicate() -> None:
	"""`while (sep == ',')`, which is how a run over separated records says
	what separates them."""
	build("struct e { u8 v; u8 sep; }\n"
	      "struct s { e runs[] while (sep == ','); }\n")


def test_nothing_declared_means_ascii() -> None:
	"""A default rather than a guess, and the one every text format in this
	repository agrees on for its structural bytes. Without it the absence
	would have to mean "whatever the compiler happens to do"."""
	build("struct s { u8 t; variant v switch (t) {"
	      " case 'A': u8 a; default: u8 b; } }\n")


def test_encodings_that_disagree_refuse_the_literal() -> None:
	"""The point of declaring several. `e` with an acute accent is one byte
	in Latin-1 and two in UTF-8, so a schema that says its bytes may be
	either cannot use it as a value -- and the diagnostic names the
	disagreement rather than picking a side, because there is no side to
	pick."""
	with pytest.raises(SituError, match="not one value"):
		build("encoding utf8 | latin1;\n"
		      "struct s { u8 t; variant v switch (t) {"
		      " case 'é': u8 a; default: u8 b; } }\n")

	# And the case where both encodings hold the character and hold it as
	# different bytes, which is the one nothing but a comparison catches.
	with pytest.raises(SituError, match="not one value"):
		build("encoding latin1 | iso8859_5;\n"
		      "struct s { u8 t; variant v switch (t) {"
		      " case '§': u8 a; default: u8 b; } }\n")


def test_and_one_encoding_accepts_what_two_refused() -> None:
	"""The control. Without it the test above would pass against a compiler
	that refused every non-ASCII character, which is a different rule."""
	build("encoding latin1;\n"
	      "struct s { u8 t; variant v switch (t) {"
	      " case 'é': u8 a; default: u8 b; } }\n")


def test_a_code_unit_is_the_encoding_s_own() -> None:
	"""`'A'` is 0x41 in ASCII and 0x0041 in UTF-16, and the second fits a
	`u16` rather than a `u8`. The literal is one code UNIT, not one byte,
	which is what lets a UTF-16 discriminant be written as a character."""
	assert character_value("A", ("ascii",)) == 0x41
	assert character_value("A", ("utf16be",)) == 0x0041
	assert CODE_UNIT_BYTES["utf16be"] == 2
	assert CODE_UNIT_BYTES["ascii"] == 1


def test_a_character_no_encoding_can_hold_is_refused() -> None:
	"""Three ways a literal fails, and each names which: not
	representable, more than one code unit, or the encodings disagree."""
	for char, encodings, why in (
			("é", ("ascii",),                  "cannot represent"),
			("é", ("utf8",),                   "needs 2 bytes"),
			# A real disagreement rather than a contrived one: the section
			# sign is 0xA7 in Latin-1 and 0xFD in ISO-8859-5, both a single
			# byte, so nothing but comparing the values catches it. Reaching
			# this branch took finding such a pair, which is itself worth
			# knowing -- most of the family agrees below 0xA0 and the ones
			# that differ mostly do so by one of them not having the
			# character at all.
			("§", ("latin1", "iso8859_5"),     "disagree")):
		with pytest.raises(EncodingError, match=why):
			character_value(char, encodings)


def test_every_encoding_names_a_codec_that_exists() -> None:
	"""The table is a claim about Python's codecs, so it is asked.

	A name that no codec answers to would fail only in a schema that used
	it -- and the ISO-8859 family is generated by a comprehension, which is
	exactly where a wrong part number hides.
	"""
	for name, codec in sorted(TEXT_ENCODINGS.items()):
		assert "A".encode(codec) is not None, name
		assert name in CODE_UNIT_BYTES


def test_the_iso_family_is_the_parts_that_exist() -> None:
	"""ISO-8859-12 was abandoned and there is no codec for it. Asserted
	rather than left to the comprehension above, because a range that
	included it would raise here and nowhere else."""
	assert "iso8859_12" not in TEXT_ENCODINGS
	assert "iso8859_11" in TEXT_ENCODINGS
	assert "iso8859_16" in TEXT_ENCODINGS


@pytest.mark.parametrize("body", [
	"encoding klingon;\nstruct s { u8 a; }\n",
	"encoding ascii | ascii;\nstruct s { u8 a; }\n",
	"encoding utf16;\nstruct s { u8 a; }\n",
])
def test_the_directive_is_held_to_what_it_can_mean(body: str) -> None:
	"""An unknown name, a repeat, and bare `utf16` -- which does not say the
	order and is refused for decision 0044's reason."""
	with pytest.raises(SituError):
		build(body)


def test_a_string_is_still_not_an_integer() -> None:
	"""And says what to write instead, which it did not before: the two
	literals now differ by the quote, so a reader who reached for the wrong
	one gets told which is which."""
	with pytest.raises(SituError, match="not an integer"):
		build("struct s { u8 t; variant v switch (t) {"
		      ' case "{": u8 a; default: u8 b; } }\n')


# -- `until "a" | "b"` ------------------------------------------------------
#
# A scalar in a text format usually ends at whichever of a set comes first --
# a JSON number at `,`, `]` or `}`; an HTTP header line at CRLF or, from most
# hand-written clients, a bare LF. One delimiter could not say that, so such
# a field was either refused or measured wrongly.

ALT = ("encoding ascii;\n"
       "struct field {\n"
       "\tu8  value[] until ',' | ']' | '}';\n"
       "\tu8  after;\n"
       "}\n")


def test_the_alternatives_are_all_carried() -> None:
	"""The field is a tuple, and the property that hands back the first
	says in its own docstring that a scan may not use it."""
	resolved = build(ALT)
	held = next(e.placement for e in resolved.structs["field"].entries
	            if e.placement.name == "value")
	assert held.delimiters == (b",", b"]", b"}")
	assert held.delimiter == b","


def test_the_same_alternative_twice_is_refused() -> None:
	"""A repeat changes nothing a scan does, so it is more likely a typo
	than a choice -- and 17.0 refuses a schema that states what the
	generated code does not enforce."""
	with pytest.raises(SituError, match="twice"):
		build("struct s { u8 v[] until ',' | ','; u8 a; }\n")


@pytest.mark.parametrize(("body", "why"), [
	# A record run checks its terminator only where an element would start,
	# so alternation there is a different walk from the one a byte run does.
	("struct r { u8 a; u8 b; }\n"
	 "struct s { r runs[] until \"\\x00\" | \"\\xFF\"; }\n", "run of records"),
	# `quoted`/`escape` carry state past the delimiter, and with several the
	# scan would have to carry it past each.
	("struct s { u8 v[] until ',' | ']' [quoted = \"\\\"\"]; u8 a; }\n",
	 "escaping form"),
])
def test_the_constructs_it_does_not_reach_are_refused(body: str,
		why: str) -> None:
	"""Named refusals rather than a half-converted emitter.

	Both are describable and neither is built: a schema that got silently
	wrong framing from one of them would be the failure this repository
	rates worst, and a schema that is refused by name is a reader's cue to
	ask for the construct.
	"""
	with pytest.raises(SituError, match=why):
		build(body)


def test_the_scan_takes_the_longest_match_at_the_earliest_offset() -> None:
	"""Both halves, and the second is the one that is easy to get wrong.

	`"\\r"` and `"\\r\\n"` match in the same place. The span includes the
	delimiter, so taking the shorter leaves the newline as the next
	member's first byte -- which is a wrong offset for every conforming
	message rather than an unusual one.
	"""
	import sys
	from pathlib import Path

	sys.path.insert(0, str(Path(__file__).resolve().parents[2]
	                       / "runtime" / "python"))
	try:
		from situ_runtime import scan_any  # type: ignore[import-not-found]
	finally:
		sys.path.pop(0)

	assert scan_any(b"ab\r\ncd", 6, (b"\r", b"\r\n")) == (2, 2)
	assert scan_any(b"ab\rcd", 5, (b"\r", b"\r\n")) == (2, 1)
	assert scan_any(b"abcd", 4, (b"\r", b"\r\n")) == (4, 0)
	assert scan_any(b"12,x", 4, (b",", b"]", b"}")) == (2, 1)


def test_a_character_is_a_delimiter_too() -> None:
	"""`until ','` rather than `until ","`, for the reason the whole file
	is about: the byte is the encoding's answer rather than a number
	somebody looked up."""
	resolved = build("encoding ascii | utf8 | latin1;\n"
	                 "struct s { u8 v[] until ','; u8 a; }\n")
	held = next(e.placement for e in resolved.structs["s"].entries
	            if e.placement.name == "v")
	assert held.delimiters == (b",",)


def test_the_directive_comes_before_the_structs() -> None:
	"""A character is resolved where it is written, so a directive below a
	struct would give the literals above it a different meaning from the
	ones below -- from a line that reads like a statement about the file."""
	with pytest.raises(SituError, match="before the structs"):
		build("struct s { u8 a; }\nencoding ascii;\n")


# -- `before` ---------------------------------------------------------------
#
# `until` is a TERMINATOR: the delimiter belongs to the member, which is what
# a CRLF is to the line it ends. `before` is a SEPARATOR: it belongs to
# neither side, which is what a comma is between two JSON members. One word
# could not say both, and every text format has both.

BEFORE = ("encoding ascii;\n"
          "struct field {\n"
          "\tu8  value[] before ',' | ']' | '}';\n"
          "\tu8  sep;\n"
          "}\n")


def test_before_leaves_the_delimiter_for_the_next_member() -> None:
	"""The whole difference, and the reason the member after it can read
	the delimiter at all."""
	resolved = build(BEFORE)
	held = next(e.placement for e in resolved.structs["field"].entries
	            if e.placement.name == "value")
	assert held.delimiters == (b",", b"]", b"}")
	assert held.delimiter_consumed is False


def test_until_still_takes_it() -> None:
	"""The control. Without it the test above would pass against a compiler
	that had stopped consuming delimiters altogether."""
	resolved = build("struct s { u8 v[] until ','; u8 a; }\n")
	held = next(e.placement for e in resolved.structs["s"].entries
	            if e.placement.name == "v")
	assert held.delimiter_consumed is True


def test_a_before_member_need_not_be_terminated() -> None:
	"""`validate` requires an `until` delimiter to be there -- a member
	without it was cut short. It must not require a `before` one: the
	delimiter belongs to whatever comes next, and whether there is a next
	is the enclosing structure's business.

	A JSON number at the end of a document has no separator after it, and a
	check that refused it would call every such document malformed. That is
	what happened, for about ten minutes, and the schema that showed it was
	the one the construct was built for.
	"""
	from situc.traverse import must_be_terminated

	resolved = build(BEFORE)
	held = next(e.placement for e in resolved.structs["field"].entries
	            if e.placement.name == "value")
	assert not must_be_terminated(held)

	consumed = build("struct s { u8 v[] until ','; u8 a; }\n")
	other = next(e.placement for e in consumed.structs["s"].entries
	             if e.placement.name == "v")
	assert must_be_terminated(other)


def test_before_round_trips_as_before() -> None:
	"""`situc dump-ast` and the unparser print the keyword that was
	written: a schema printed back as `until` would be a different schema,
	and the two differ by exactly one byte of framing."""
	from situc.unparse import unparse

	printed = unparse(parse_text(PREAMBLE + BEFORE))
	assert "before ',' | ']' | '}'" in printed, printed
	assert " until " not in printed, printed
