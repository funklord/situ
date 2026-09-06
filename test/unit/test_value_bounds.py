"""`[min]`/`[max]` exported as per-field constants (26.125).

The bound is stated once in the schema and enforced in `validate`, and was
reachable from nowhere else -- so hand-written code validating the same value
before it crosses the wire (a CLI flag that fills a field, a config key)
restated the number and drifted. The argv evaluation (26.124) is where the
gap was noticed; the constants are the single-source rule applied to the
value domain.

The C spelling lives here; the other three backends' spellings are asserted
in their own codegen suites, and the executable agreement test -- the
module's own constants driving its own `validate` at all four boundary
values -- is in `test_codegen_python.py`, where a generated module can be
imported without a compiler.
"""

from __future__ import annotations

import pytest

from situc.codegen.c import generate
from situc.diagnostics import SituError
from situc.layout import solve
from situc.parser import parse_text
from situc.resolve import resolve

PREAMBLE = "target buffer;\nendian big;\nbit_order msb_first;\n"

#: A field named `size` on purpose: its constants must not collide with the
#: struct's own `SIZE_MIN`/`SIZE_MAX`, which is why the spelling is
#: `VALUE_MIN`/`VALUE_MAX` -- the value's domain, not the field's byte size.
FIXTURE = ("const CAP = 9216;\n"
           "struct s { u16 mtu [min = 576, max = CAP];"
           " i8 bias [min = -20]; u16 size [max = 100]; }")


def header(body: str) -> str:
	schema   = parse_text(PREAMBLE + body)
	resolved = resolve(schema, solve(schema))
	return generate(schema, resolved, "unit").header


def source(body: str) -> str:
	schema   = parse_text(PREAMBLE + body)
	resolved = resolve(schema, solve(schema))
	return generate(schema, resolved, "unit").source


def test_value_bounds_are_exported_as_macros() -> None:
	emitted = header(FIXTURE)
	assert "#define SITU_S_MTU_VALUE_MIN 576u" in emitted
	assert "#define SITU_S_MTU_VALUE_MAX 9216u" in emitted
	assert "#define SITU_S_BIAS_VALUE_MIN -20" in emitted
	assert "#define SITU_S_SIZE_VALUE_MAX 100u" in emitted


def test_the_size_field_does_not_collide_with_the_struct_size() -> None:
	"""The reason for the VALUE spelling, held as a test."""
	emitted = header(FIXTURE)
	assert "#define SITU_S_SIZE_MIN   5u" in emitted
	assert emitted.count("SITU_S_SIZE_VALUE_MAX") >= 1


def test_a_bound_that_does_not_fold_is_skipped() -> None:
	"""`validate` still enforces it; a constant that cannot be computed at
	compile time is not a constant. Skipped rather than refused, and the
	absence is discoverable -- code using the macro fails to compile --
	rather than silently wrong."""
	emitted = header("struct s { u8 n; u8 v [max = n]; }")
	assert "VALUE_MAX" not in emitted


def test_a_converted_type_exports_its_bound_too() -> None:
	"""These were excluded, and the reason given was false.

	`declared_value_bounds` withheld them because "the getter's value is
	scaled or decoded, so exporting the raw bound would hand a caller a
	constant in the wrong domain". Nothing scales: 8.1 has a fixed-point
	getter return the stored integer and the caller scale with `_SCALE`, and
	every backend does. And BCD's bound is compared against the decoded
	value, in all four descriptions since 26.274 -- before that Rust compared
	it against the packed nibbles, which is the hazard the sentence was
	describing, in the one type it was true of.

	A bound is stated in the domain the getter returns. `validate` compares
	the schema's number against that same value, so the constant is the one
	a hand-written caller needs and no conversion is applied to it. Settled
	by the copyright holder 2026-09-06.

	The executable half -- the constant driving the module's own `validate`
	at both boundaries, for a type where the bytes and the value differ -- is
	`test_value_bounds_agree_with_validate_for_a_converted_type` in
	`test_codegen_python.py`. A plain `u16` cannot discriminate a
	right-domain constant from a wrong-domain one; these two can.
	"""
	emitted = header("struct s { q8_8 trim [max = 100]; bcd8 day [max = 49]; }")
	assert "#define SITU_S_TRIM_VALUE_MAX 100" in emitted
	assert "#define SITU_S_DAY_VALUE_MAX 49u" in emitted


def test_a_bound_may_name_a_field_declared_later() -> None:
	"""Whether a forward-referencing bound is legal is not this guard's
	question, and for a while it answered anyway.

	`interval_of` evaluates the whole expression, so it refuses a name it
	cannot resolve -- and running it over every bound turned a scope decision
	into an arithmetic refusal. fuzznet's `[max = chunks - 1]` reads a field
	two bytes further on, had compiled since their schema existed, and stopped
	at 74f3742; they bisected it against the committed schema and carried a
	red gate rather than working around it.

	No committed schema here has one, which is why the corpus could not show
	it: every example passed and the guard looked right. This fixture is the
	population situ does not otherwise have.
	"""
	header("struct s { u16 index [max = chunks - 1]; u16 chunks; }")
	header("struct s { u16 index [min = base]; u16 base; }")

	# And one that divides as well, which is the case that separates the two
	# conditions in the guard. Without the operator test the first two are
	# refused; without the name test this one is -- and with either alone the
	# other's sabotage stays green, which is how both came within a commit of
	# shipping undemonstrated.
	header("struct s { u16 index [max = chunks / 2]; u16 chunks; }")


def test_a_forward_reference_is_still_not_a_way_past_the_guard() -> None:
	"""The narrowing is `/` and `%` and resolvable names, so a bound that
	divides and reads a *sibling already declared* is still refused. Without
	this, "skip what does not resolve" would be a hole anybody could reach by
	reordering two fields."""
	with pytest.raises(SituError, match="left operand of `/` may be negative"):
		header("struct s { u16 base; u16 index [max = (0 - base) / 2]; }")


def test_a_float_bound_is_still_excluded() -> None:
	"""And for a reason the other two did not have: a bound folds to an
	integer here, so a float field has nothing to export rather than
	something in the wrong units."""
	emitted = header("struct s { f32 gain [max = 100]; }")
	assert "VALUE_MAX" not in emitted


def test_a_bound_the_descriptions_would_disagree_about_is_refused() -> None:
	"""`/` and `%` truncate toward zero in C, C++ and Rust and floor in
	Python and Lua, so a negative dividend is arithmetic the six descriptions
	do not share. Measured before the guard, on `03 7f`: the C description
	answered SITU_OK and the Python one raised, wanting 126.

	Refused in `solve` rather than in an emitter, so that `map` and `wire` --
	which call the solver and not codegen -- refuse it too. openmlx4 found
	those two exiting 0 where `build` exited 1, having been about to
	recommend `situc map --check` as their gate.
	"""
	with pytest.raises(SituError, match="left operand of `%` may be negative"):
		header("struct s { u8 b0; u8 b1 [must_eq = (0 - b0) % 256]; }")

	with pytest.raises(SituError, match="left operand of `/` may be negative"):
		header("struct s { u8 b0; u8 b1 [must_eq = (0 - b0) / 2 + 128]; }")


def test_a_bound_whose_dividend_cannot_go_negative_is_kept() -> None:
	"""The guard is about the sign, not about the operator: the same schema
	written so the subtraction cannot go below zero is the byte-sum check
	openmlx4 wanted, and all four backends render it alike -- C and Python
	agreed on 1260 byte quadruples, 12 of them accepted.

	`%` had also been missing from `invariant.OPERATORS`, which is what made
	`[must_eq = b0 % 256]` unrenderable and so refused by `build` while `map`
	published a row for it.
	"""
	emitted = source("struct s { u8 b0; u8 b1; u8 b2;"
	                 " u8 b3 [must_eq = (768 - b0 - b1 - b2) % 256]; }")
	assert "% 256" in emitted
	header("struct s { u8 b0; u8 b1 [must_eq = b0 % 256]; }")
	header("struct s { u8 b0; u8 b1 [must_eq = b0 / 2]; }")
