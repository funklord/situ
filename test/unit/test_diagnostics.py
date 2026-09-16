"""Diagnostic rendering tests (project.md section 17).

Diagnostic quality is the product, so the exact text is snapshot-tested:
a regression in message quality is a real regression (section 22).
"""

from __future__ import annotations

import pytest

from situc.diagnostics import (Diagnostic, Label, Severity, SituError, Source,
                               Span, error)
from situc.layout import solve
from situc.parser import parse_text
from situc.resolve import resolve

SOURCE = Source("packet.situ", "struct Message {\n\tu8 opts[hdr.length];\n}\n")


def test_span_locates_line_and_column() -> None:
	span = Span(SOURCE, SOURCE.text.index("opts"), SOURCE.text.index("opts") + 4)
	assert (span.line, span.column) == (2, 5)


def test_span_text() -> None:
	span = Span(SOURCE, 0, 6)
	assert span.text() == "struct"


def test_span_join() -> None:
	first  = Span(SOURCE, 0, 6)
	second = Span(SOURCE, 7, 14)
	assert first.to(second) == Span(SOURCE, 0, 14)


def test_locate_first_character() -> None:
	assert SOURCE.locate(0) == (1, 1)


def test_line_text() -> None:
	assert SOURCE.line_text(2) == "\tu8 opts[hdr.length];"


def test_rendered_diagnostic_matches_section_17_shape() -> None:
	start = SOURCE.text.index("u8 opts[hdr.length];")
	span  = Span(SOURCE, start, start + len("u8 opts[hdr.length];"))

	diagnostic = Diagnostic(
		severity = Severity.ERROR,
		message  = "requirement not satisfied",
		primary  = Label(span, "dynamic size introduced here"),
		notes    = [
			"offset(Message.recs) is Dynamic, required AbsoluteStatic",
			"2 further members lost absolute addressing: recs, trailer",
		],
	)

	assert diagnostic.render() == (
		"error: requirement not satisfied\n"
		" --> packet.situ:2:2\n"
		"  |\n"
		"2 |  u8 opts[hdr.length];\n"
		"  |  ^^^^^^^^^^^^^^^^^^^^ dynamic size introduced here\n"
		"  |\n"
		"  = offset(Message.recs) is Dynamic, required AbsoluteStatic\n"
		"  = 2 further members lost absolute addressing: recs, trailer"
	)


def test_tabs_are_rendered_as_one_column() -> None:
	"""No tab width is prescribed in this project, so a quoted tab counts as a
	single column and the caret lands under the right token."""
	rendered = Diagnostic(
		severity = Severity.ERROR,
		message  = "x",
		primary  = Label(Span(SOURCE, SOURCE.text.index("opts"),
		                      SOURCE.text.index("opts") + 4)),
	).render()

	quoted, carets = rendered.splitlines()[3], rendered.splitlines()[4]
	assert quoted  == "2 |  u8 opts[hdr.length];"
	assert carets  == "  |     ^^^^"
	assert quoted.index("opts") == carets.index("^")


def test_gutter_widens_for_large_line_numbers() -> None:
	source = Source("big.situ", "\n" * 120 + "u8 a;\n")
	start  = source.text.index("u8 a;")
	span   = Span(source, start, start + 5)

	rendered = error("boom", span).diagnostic.render()

	assert "--> big.situ:121:1" in rendered
	assert "121 | u8 a;" in rendered


def test_notes_are_rendered() -> None:
	rendered = error("boom", Span(SOURCE, 0, 6), notes=["first", "second"]).diagnostic.render()
	assert "= first" in rendered
	assert "= second" in rendered


def test_severity_prefix() -> None:
	diagnostic = Diagnostic(Severity.WARNING, "careful", Label(Span(SOURCE, 0, 6)))
	assert diagnostic.render().startswith("warning: careful")


def test_zero_width_span_still_gets_a_caret() -> None:
	"""End-of-file diagnostics have nothing to underline but must still point."""
	rendered = error("boom", Span(SOURCE, len(SOURCE.text), len(SOURCE.text))).diagnostic.render()
	assert "^" in rendered


# -- diagnostics raised by real parses --------------------------------------


def test_parse_error_points_at_the_offending_token() -> None:
	with pytest.raises(Exception) as caught:
		parse_text("struct S {\n\tu8 a\n}\n", path="s.situ")

	rendered = caught.value.diagnostic.render()	# type: ignore[attr-defined]
	assert "--> s.situ:3:1" in rendered
	assert "expected `;`" in rendered


def test_not_yet_implemented_names_its_phase() -> None:
	with pytest.raises(Exception) as caught:
		parse_text("namespace a {\n\tnamespace b { struct s { u8 x; } }\n}\n",
		           path="s.situ")

	rendered = caught.value.diagnostic.render()	# type: ignore[attr-defined]
	assert "a nested `namespace` is not yet implemented" in rendered
	assert "planned for phase 12" in rendered
	assert "--> s.situ:2:2" in rendered


def test_recursion_diagnostic_shows_the_cycle() -> None:
	with pytest.raises(Exception) as caught:
		parse_text("struct A { B b; }\nstruct B { A a; }\n", path="s.situ")

	rendered = caught.value.diagnostic.render()	# type: ignore[attr-defined]
	assert "cycle: A -> B -> A" in rendered
	assert "recursion needs a bound" in rendered


def test_width_error_points_at_the_type() -> None:
	with pytest.raises(Exception) as caught:
		parse_text("struct S { u65 wide; }", path="s.situ")

	rendered = caught.value.diagnostic.render()	# type: ignore[attr-defined]
	assert "u65" in rendered
	assert "widths run from 1 to 64" in rendered


# -- one refusal, two doors --------------------------------------------------


def refused_label(source: str) -> str:
	"""The text under the caret, which is where a diagnostic says what is
	wrong with what the author wrote."""
	with pytest.raises(SituError) as caught:
		schema = parse_text(source, path="s.situ")
		resolve(schema, solve(schema))

	rendered = caught.value.diagnostic.render()
	carets   = next(line for line in rendered.splitlines() if "^" in line)
	return carets.split("^")[-1].strip()


VARINT   = "endian big;\nvarint_type v {{ encoding = leb128; {0} }}\n"
VERSION  = "endian big;\nstruct s [version = v] {{ u8 v; u16 a [since = {0}]; }}\n"
KERNEL   = "endian big;\ncodec c {{ kernel = polynomial(field = {0}, n = 3, k = 2); }}\n"
INDEXED  = ("endian big;\nstruct r {{ u8 a; }}\n"
            "struct s {{ u16 n; indexed({0}count = n) {{ r entries[]; }} }}\n")

#: A refusal reached by two different mistakes, and what each must say.
#:
#: Every one is a condition written `A or B` where the label described A and
#: the author had done B -- `[since = 0]` answering "expected a literal" to
#: somebody who wrote one, `field = 2` answering "not a power of two" about a
#: power of two. The message line stayed true throughout; what was wrong is
#: the line under the caret, which is the one a reader checks their own text
#: against (26.366).
TWO_DOORS = [
	("an enum's element count",
	 "endian big;\nenum e : u8 [0] { A = 1; }\n",     "a count of 0",
	 "endian big;\nenum e : u8 [wide] { A = 1; }\n",  "not a literal number"),
	("`max_bytes`",
	 VARINT.format("max_bits = 32; max_bytes = 99;"), "out of range",
	 VARINT.format("max_bits = 32; max_bytes = n;"),  "not a literal"),
	("`max_bits`",
	 VARINT.format("max_bits = 99;"), "out of range",
	 VARINT.format("max_bits = n;"),  "not a literal"),
	("`since`",
	 VERSION.format("0"), "version 0 is before the first",
	 VERSION.format("n"), "not a literal version number"),
	("a Reed-Solomon field",
	 KERNEL.format("3"), "not a power of two",
	 KERNEL.format("2"), "a power of two, but GF(2) is a bit, not a symbol"),
	("`offset_type`",
	 INDEXED.format("offset_type = 16, "), "expected a type name such as `u16`",
	 INDEXED.format(""),                   "expected `offset_type = u16` or similar"),
	("an offset type that is not a type",
	 INDEXED.format("offset_type = nope, "), "no type of this name",
	 INDEXED.format("offset_type = u4, "),   "invalid offset type"),
]


@pytest.mark.parametrize(("named", "first", "says_first", "second", "says_second"),
                         TWO_DOORS, ids=[row[0] for row in TWO_DOORS])
def test_each_door_into_a_refusal_says_which_one_was_taken(
		named: str, first: str, says_first: str,
		second: str, says_second: str) -> None:
	"""Both halves, because pinning one leaves the other free to describe it."""
	assert refused_label(first)  == says_first,  named
	assert refused_label(second) == says_second, named
