"""The interval rules behind every size the solver reports (section 10).

Nothing tested these directly. They are the decision procedure for "static or
dynamic", every `size=` in a capability map is read off them, and the only
coverage they had was whatever the worked examples happened to compute -- which
is `+`, `-` and `*` and nothing else (26.37). Four of the remaining operators
were unsound.

The test that matters here is exhaustive rather than exemplary: for small
ranges, every value the operator can actually produce has to lie inside the
interval the rule claims. A rule that is merely plausible passes an example and
fails this.
"""

from __future__ import annotations

import pytest

from situc.diagnostics import SituError
from situc.expr import BINARY_OPS, INTERVAL_RULES, Interval, build_env, interval_of
from situc.layout import solve
from situc.parser import parse_text

#: Small enough to enumerate every pair of ranges, wide enough to cross a
#: power of two, which is where the bitwise rules go wrong if they are going to.
LIMIT = 9

ARITHMETIC = ("+", "-", "*", "/", "%", "&", "|", "^", "<<", ">>")


def ranges() -> list[Interval]:
	return [Interval(lo, hi)
	        for lo in range(0, LIMIT)
	        for hi in range(lo, LIMIT)]


@pytest.mark.parametrize("op", ARITHMETIC)
def test_every_rule_contains_every_value_it_claims_to(op: str) -> None:
	"""Soundness, by enumeration.

	`(4 - n % 4) % 4` came out `[0, 1]` for an expression whose range is
	`0..3`, because corner enumeration was applied to operators that are not
	monotone. An understated size is the direction that grants a capability:
	the map said a member was at most one byte wide and the message could put
	three there.
	"""
	rule      = INTERVAL_RULES[op]
	operation = BINARY_OPS[op]

	for left in ranges():
		for right in ranges():
			if op in ("/", "%") and right.lo == 0:
				continue		# refused before the rule is asked
			claimed = rule(left, right)
			if not claimed.lo_known:
				continue		# claims nothing, so it cannot be wrong
			for a in range(left.lo, (left.hi or 0) + 1):
				for b in range(right.lo, (right.hi or 0) + 1):
					got = operation(a, b)
					assert claimed.lo <= got, \
						f"{a} {op} {b} = {got} is below {claimed.render()}"
					assert claimed.hi is None or got <= claimed.hi, \
						f"{a} {op} {b} = {got} is above {claimed.render()}"


def interval(source: str, fields: str = "") -> Interval:
	"""The interval of a size expression, read out of a solved schema."""
	schema = parse_text(f"target buffer;\nendian big;\n"
	                    f"struct s {{\n{fields}\tu8 body[{source}];\n}}\n")
	layout = solve(schema)
	member = layout.structs["s"].placements[-1]
	return Interval(member.size_bits // 8,
	                None if member.size_max_bits is None else member.size_max_bits // 8)


def test_the_padding_idiom_is_exact() -> None:
	"""`align_up(n, 4) - n` is 0..3 for every `n`, and interval arithmetic
	alone cannot see it: the two operands are the same value, and nothing in
	a range remembers that."""
	assert interval("align_up(n, 4) - n", "\tu16 n;\n") == Interval(0, 3)


def test_align_up_is_bounded_by_its_argument() -> None:
	assert interval("align_up(n, 4)", "\tu8 n [min = 1, max = 9];\n") \
		== Interval(4, 12)


def test_a_size_with_no_derivable_lower_bound_is_refused() -> None:
	"""It used to pass: the widening returned `[0, inf]`, and the zero was
	read by the one check that asks as "this cannot be negative"."""
	with pytest.raises(SituError) as caught:
		interval("align_up(n, 4) - n - 10", "\tu16 n;\n")
	assert "may be negative" in caught.value.diagnostic.render()


def test_a_modulo_narrower_than_its_dividend_is_the_identity() -> None:
	assert interval("n % 16", "\tu8 n [min = 2, max = 9];\n") == Interval(2, 9)


def test_a_comparison_is_zero_or_one() -> None:
	env = build_env(parse_text("const A = 1;"))
	assert INTERVAL_RULES["<"](Interval(0, 100), Interval(0, 100)) == Interval(0, 1)
	del env
