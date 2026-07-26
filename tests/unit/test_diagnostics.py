"""Diagnostic rendering tests (project.md section 17).

Diagnostic quality is the product, so the exact text is snapshot-tested:
a regression in message quality is a real regression (section 22).
"""

from __future__ import annotations

import pytest

from situc.diagnostics import Diagnostic, Label, Severity, Source, Span, error
from situc.parser import parse_text

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
		parse_text("struct S {\n\tsealed(aes) { u8 a; }\n}\n", path="s.situ")

	rendered = caught.value.diagnostic.render()	# type: ignore[attr-defined]
	assert "`sealed` is not yet implemented" in rendered
	assert "planned for phase 8" in rendered
	assert "--> s.situ:2:2" in rendered


def test_recursion_diagnostic_shows_the_cycle() -> None:
	with pytest.raises(Exception) as caught:
		parse_text("struct A { B b; }\nstruct B { A a; }\n", path="s.situ")

	rendered = caught.value.diagnostic.render()	# type: ignore[attr-defined]
	assert "cycle: A -> B -> A" in rendered
	assert "non-terminating" in rendered


def test_width_error_points_at_the_type() -> None:
	with pytest.raises(Exception) as caught:
		parse_text("struct S { u65 wide; }", path="s.situ")

	rendered = caught.value.diagnostic.render()	# type: ignore[attr-defined]
	assert "u65" in rendered
	assert "widths run from 1 to 64" in rendered
