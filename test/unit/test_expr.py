"""Constant expression evaluation (project.md section 10)."""

from __future__ import annotations

import pytest

from situc.diagnostics import SituError
from situc.expr import build_env, evaluate
from situc.parser import parse_text


def value(source: str) -> int:
	schema = parse_text(f"const RESULT = {source};")
	return build_env(schema).consts["RESULT"]


def rejected(source: str) -> str:
	with pytest.raises(SituError) as caught:
		build_env(parse_text(source))
	return caught.value.diagnostic.render()


@pytest.mark.parametrize(("source", "expected"), [
	("42",            42),
	("0x2A",          42),
	("0b101010",      42),
	("1_000",         1000),
	("2 + 3 * 4",     14),
	("(2 + 3) * 4",   20),
	("10 - 3 - 2",    5),
	("100 / 7",       14),
	("100 % 7",       2),
	("1 << 8",        256),
	("256 >> 4",      16),
	("0xF0 | 0x0F",   255),
	("0xFF & 0x0F",   15),
	("0xFF ^ 0x0F",   240),
	("-5",            -5),
	("~0 & 0xFF",     255),
	("!0",            1),
	("!7",            0),
	("1 == 1",        1),
	("1 != 1",        0),
	("3 < 4",         1),
	("min(3, 7)",     3),
	("max(3, 7)",     7),
	("align_up(5, 4)", 8),
	("align_up(8, 4)", 8),
])
def test_folding(source: str, expected: int) -> None:
	assert value(source) == expected


def test_constants_may_reference_earlier_constants() -> None:
	schema = parse_text("const A = 4; const B = A * 2; const C = A + B;")
	env    = build_env(schema)
	assert (env.consts["A"], env.consts["B"], env.consts["C"]) == (4, 8, 12)


def test_forward_reference_between_constants_rejected() -> None:
	"""Section 10 forbids forward references, which keeps this a single pass."""
	assert "not a compile-time constant" in rejected("const A = B; const B = 1;")


def test_enum_member_reference() -> None:
	schema = parse_text("enum E : u8 { hello = 7, } const N = E.hello;")
	assert build_env(schema).consts["N"] == 7


def test_enum_may_reference_an_earlier_constant() -> None:
	schema = parse_text("const BASE = 10; enum E : u8 { a = BASE + 1, }")
	assert build_env(schema).enums["E"]["a"] == 11


def test_constant_may_reference_an_earlier_enum() -> None:
	"""Constants and enums resolve in one interleaved pass, not two."""
	schema = parse_text("enum E : u8 { a = 3, } const N = E.a * 2;")
	assert build_env(schema).consts["N"] == 6


def test_unknown_enum_member_rejected() -> None:
	report = rejected("enum E : u8 { a = 1, } const N = E.b;")
	assert "has no member `b`" in report
	assert "members: a" in report


def test_bare_enum_name_suggests_the_member_syntax() -> None:
	report = rejected("enum E : u8 { a = 1, } const N = E;")
	assert "write `E.<member>`" in report


def test_division_by_zero_rejected() -> None:
	assert "division by zero" in rejected("const N = 1 / 0;")


def test_modulo_by_zero_rejected() -> None:
	assert "division by zero" in rejected("const N = 1 % 0;")


def test_negative_shift_rejected() -> None:
	assert "negative shift" in rejected("const N = 1 << (0 - 1);")


def test_oversized_shift_rejected() -> None:
	assert "exceeds the widest scalar" in rejected("const N = 1 << 65;")


def test_zero_alignment_rejected() -> None:
	assert "alignment must be positive" in rejected("const N = align_up(4, 0);")


def test_unknown_function_rejected() -> None:
	report = rejected("const N = sqrt(4);")
	assert "unknown function `sqrt`" in report
	assert "no user-defined functions" in report


def test_wrong_arity_rejected() -> None:
	assert "takes 2 arguments, found 1" in rejected("const N = min(4);")


def test_remaining_is_not_constant() -> None:
	report = rejected("const N = remaining;")
	assert "not a compile-time constant" in report
	assert "phase 5" in report


def test_layout_builtin_without_a_layout_rejected() -> None:
	"""A struct's own layout cannot depend on its size."""
	assert "the layout is not resolved yet" in rejected("const N = size(S);")


def test_failed_constant_names_itself() -> None:
	assert "while evaluating the constant `N`" in rejected("const N = 1 / 0;")


def test_integer_division_truncates_toward_zero() -> None:
	"""C semantics, not Python's floor, because a C backend reproduces these."""
	schema = parse_text("const A = 0 - 7; const B = A / 2; const C = A % 2;")
	env    = build_env(schema)
	assert (env.consts["B"], env.consts["C"]) == (-3, -1)
