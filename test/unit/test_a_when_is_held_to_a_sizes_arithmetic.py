"""A `when` rule's arithmetic is held to what a size's is (26.956).

`interval_of` refuses `/`, `%` and `>>` over a left operand that may be
negative -- C truncates and shifts at the compiler's discretion where
Python floors -- but it ran on layout expressions only. A `when` rule over
an `i8 a` saying `s.a / 2 == -1` refused a = -3 in C, C++ and Rust and
accepted it in Python: four readers, two answers.
"""

from __future__ import annotations

import pytest

from situc.diagnostics import Source, SituError
from situc.layout import solve
from situc.parser import parse
from situc.resolve import resolve


def _resolved(rule: str, field: str = "i8 a;") -> object:
	text = ("target buffer;\nendian big;\n"
	        f"struct s {{\n\t{field}\n\tu8 k;\n}}\n{rule}\n")
	parsed = parse(Source("unit.situ", text))
	return resolve(parsed, solve(parsed))


@pytest.mark.parametrize(("rule", "op"), [
	('when (s.a / 2) == -1 refuse odd "x";', "/"),
	('when (s.a % 2) == 1 refuse odd "x";', "%"),
	('when (s.a >> 1) == -64 refuse odd "x";', ">>"),
])
def test_a_possibly_negative_operand_is_refused(rule: str, op: str) -> None:
	with pytest.raises(SituError, match=f"left operand of `{op}`"):
		_resolved(rule)


@pytest.mark.parametrize("rule", [
	'when (s.a / 2) == 1 refuse odd "x";',
	'when (s.a >> 1) == 1 refuse odd "x";',
])
def test_a_bounded_operand_is_accepted(rule: str) -> None:
	"""The same rule over an unsigned field, or one `[min = 0]` holds."""
	_resolved(rule, "u8 a;")
	_resolved(rule, "i8 a [min = 0];")
