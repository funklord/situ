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


def test_a_dividend_that_may_be_negative_is_refused() -> None:
	"""`/` and `%` are C's here, and two of the four backends are not C's.

	`BINARY_OPS` says why the language chose truncation toward zero: "these
	expressions describe layouts a C backend will reproduce". C, C++ and Rust
	reproduce it. Python's `//` and `%` floor, and Lua's do too, and the two
	answers differ exactly when the operands have opposite signs.

	Measured, and run rather than argued: `u8 a[(n - 10) / 3 + 5]` compiled,
	and at n = 5 the generated C computed a length of 4 and the generated
	Python computed 3.

	Refused rather than corrected, because the expression reaches a host
	compiler as *text* and making two of the four spell a call instead of an
	operator needs the tree that `layout` has already rendered to source.
	Where the dividend is provably non-negative all four agree, which is both
	of the divisions any committed schema performs.
	"""
	with pytest.raises(SituError) as caught:
		interval("(n - 10) / 3", "\tu8 n [min = 1, max = 100];\n")
	rendered = caught.value.diagnostic.render()
	assert "may be negative" in rendered
	assert "truncate toward zero" in rendered


#: A shift amount and whether the rule can prove it below 64. The remedy the
#: diagnostic names is in here as a case, because a refusal that suggests a
#: fix nothing accepts is worse than one that suggests nothing (0049).
SHIFT_AMOUNTS = [
	("a literal",                 "code >> 2",  "\tu8 code;\n",                  True),
	("a literal at the width",    "code >> 64", "\tu8 code;\n",                  False),
	("a negative literal",        "code >> -1", "\tu8 code;\n",                  False),
	("an unbounded `u8`",         "code >> n",  "\tu8 code;\n\tu8 n;\n",         False),
	("a `u8` bounded to 63",      "code >> n",  "\tu8 code;\n\tu8 n [max = 63];\n", True),
	("a `u8` bounded to 64",      "code >> n",  "\tu8 code;\n\tu8 n [max = 64];\n", False),
	("a `u16` bounded to 63",     "code >> n",  "\tu8 code;\n\tu16 n [max = 63];\n", True),
]


@pytest.mark.parametrize(("label", "source", "fields", "allowed"),
                         SHIFT_AMOUNTS,
                         ids=[label for label, _s, _f, _a in SHIFT_AMOUNTS])
def test_a_shift_amount_must_be_provably_below_the_width(
		label: str, source: str, fields: str, allowed: bool) -> None:
	"""0049, built. A shift by 64 or more is undefined in C, refused
	outright by rustc -- `deny(arithmetic_overflow)` -- a panic in a debug
	build, and an ordinary answer in Python. Three behaviours for one schema,
	and the schema says none of them.

	64 and not the field's own width, because the generated C widens before
	it shifts: `u8 body[(code >> 2) + 1]` emits
	`situ_leaf_u64(situ_s_code_get(view)) >> 2`. A `u5` amount is provable on
	its type alone; a `u8` is not, and 64 is the boundary rather than a
	number over it.

	**The `[max]` case is the one that earns the diagnostic.** The refusal
	tells an author to bound the field, and `layout.constrain` folds `[max]`
	into the interval that reaches this rule, so the advice works -- checked
	here rather than assumed, because a remedy nothing accepts sends the
	reader in a circle.
	"""
	if allowed:
		interval(source, fields)
		return

	with pytest.raises(SituError) as caught:
		interval(source, fields)
	assert "not provably below 64" in caught.value.diagnostic.render()


def test_a_modulo_whose_dividend_may_be_negative_is_refused_too() -> None:
	"""`%` diverges further than `/` does: Python's result takes the sign of
	the *divisor* and C's takes the sign of the dividend, so they disagree on
	the sign and not only on the magnitude."""
	with pytest.raises(SituError) as caught:
		interval("(n - 10) % 3", "\tu8 n [min = 1, max = 100];\n")
	# The wording, not just "may be negative": the size check says that too,
	# and it catches this expression a moment later for its own reasons. A
	# looser assertion passed with `%` taken out of the rule entirely.
	assert "truncate toward zero" in caught.value.diagnostic.render()


def test_a_dividend_with_no_known_lower_bound_is_refused() -> None:
	"""`lo_known` is the other half of the condition and was untested: an
	interval that has widened to "no lower bound at all" is not the same as
	one whose lower bound is negative, and only the second was exercised.
	Dropping `not left.lo_known` left every test here green."""
	with pytest.raises(SituError) as caught:
		# `_and_interval` widens to unknown for a possibly-negative operand,
		# which is sound and deliberately imprecise -- and is the only way to
		# reach a dividend whose lower bound is not merely negative but
		# absent.
		interval("(n & -1) / 3", "\tu16 n;\n")
	assert "truncate toward zero" in caught.value.diagnostic.render()


def test_a_bounded_dividend_is_accepted() -> None:
	"""The refusal is about the sign, not about the operator: bound the field
	so the subtraction cannot go below zero and the four backends agree."""
	assert interval("(n - 10) / 3", "\tu8 n [min = 10, max = 100];\n") \
		== Interval(0, 30)


def test_a_modulo_narrower_than_its_dividend_is_the_identity() -> None:
	assert interval("n % 16", "\tu8 n [min = 2, max = 9];\n") == Interval(2, 9)


def test_a_comparison_is_zero_or_one() -> None:
	env = build_env(parse_text("const A = 1;"))
	assert INTERVAL_RULES["<"](Interval(0, 100), Interval(0, 100)) == Interval(0, 1)
	del env
