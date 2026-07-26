"""The capability lattice: domains, ordering and meet (project.md section 11).

The governing invariant is that nothing strengthens a capability. Most of these
tests exist to hold that line.
"""

from __future__ import annotations

import pytest

from situc.capability import (
	DOMAINS,
	Axis,
	Value,
	is_at_least,
	meet_all,
	meet_values,
	rank,
	strongest,
)


def test_every_axis_has_a_domain() -> None:
	"""Section 11.1 lists thirteen axes; a missing domain would make an axis
	silently unusable."""
	assert set(DOMAINS) == set(Axis)
	assert len(Axis) == 13


def test_domains_are_ordered_strongest_first() -> None:
	assert DOMAINS[Axis.OFFSET][0] == "AbsoluteStatic"
	assert DOMAINS[Axis.OFFSET][-1] == "Dynamic"
	assert DOMAINS[Axis.MUTATE][0] == "InPlaceFixed"
	assert DOMAINS[Axis.MUTATE][-1] == "Immutable"


def test_stage_is_ordered_toward_less_usable() -> None:
	"""The one axis that increases rather than weakens, treated uniformly."""
	assert DOMAINS[Axis.STAGE] == (
		"CompileTime", "ParseTime", "TransformTime", "VerifyGated")
	assert rank(Axis.STAGE, Value("CompileTime")) < rank(Axis.STAGE, Value("VerifyGated"))


def test_rank_rejects_a_foreign_value() -> None:
	with pytest.raises(ValueError, match="is not a value of axis"):
		rank(Axis.OFFSET, Value("Immutable"))


# -- ordering ---------------------------------------------------------------


def test_stronger_is_at_least_weaker() -> None:
	assert is_at_least(Axis.OFFSET, Value("AbsoluteStatic"), Value("Dynamic"))
	assert not is_at_least(Axis.OFFSET, Value("Dynamic"), Value("AbsoluteStatic"))


def test_equal_values_satisfy_each_other() -> None:
	assert is_at_least(Axis.MUTATE, Value("Shifting"), Value("Shifting"))


def test_numeric_parameters_carry_strength_on_align() -> None:
	assert is_at_least(Axis.ALIGN, Value("Aligned", ("8",)), Value("Aligned", ("4",)))
	assert not is_at_least(Axis.ALIGN, Value("Aligned", ("2",)), Value("Aligned", ("4",)))


def test_offset_parameters_are_identity_not_strength() -> None:
	"""0x04 is not stronger or weaker than 0x08, it is a different offset."""
	assert is_at_least(Axis.OFFSET,
	                   Value("AbsoluteStatic", ("0x04",)),
	                   Value("AbsoluteStatic", ("0x08",)))


# -- meet -------------------------------------------------------------------


def test_meet_takes_the_weaker() -> None:
	assert meet_values(Axis.OFFSET, Value("AbsoluteStatic"), Value("Dynamic")) \
	       == Value("Dynamic")
	assert meet_values(Axis.OFFSET, Value("Dynamic"), Value("AbsoluteStatic")) \
	       == Value("Dynamic")


def test_meet_of_alignments_takes_the_smaller() -> None:
	assert meet_values(Axis.ALIGN, Value("Aligned", ("8",)), Value("Aligned", ("2",))) \
	       == Value("Aligned", ("2",))


def test_meet_of_equals_is_the_value() -> None:
	assert meet_values(Axis.REPR, Value("ValueConverted"), Value("ValueConverted")) \
	       == Value("ValueConverted")


# -- vectors ----------------------------------------------------------------


def test_absent_axes_are_at_their_strongest() -> None:
	vector = strongest()
	for axis in Axis:
		assert vector.get(axis).base == DOMAINS[axis][0]


def test_setting_an_axis_weakens_it() -> None:
	vector = strongest().with_value(Axis.REPR, Value("ValueConverted"))
	assert vector.get(Axis.REPR) == Value("ValueConverted")
	assert vector.get(Axis.ATOMIC) == Value("AtomicWord")


def test_strengthening_an_axis_is_refused() -> None:
	"""Invariant 2 of section 26. An implementation that needs this has a wrong
	axis definition, so it fails loudly rather than quietly."""
	vector = strongest().with_value(Axis.OFFSET, Value("Dynamic"))
	with pytest.raises(ValueError, match="cannot strengthen"):
		vector.with_value(Axis.OFFSET, Value("AbsoluteStatic"))


def test_setting_the_same_strength_is_allowed() -> None:
	"""Two constructs can independently cost the same capability."""
	vector = strongest().with_value(Axis.ATOMIC, Value("NonAtomic"))
	assert vector.with_value(Axis.ATOMIC, Value("NonAtomic")).get(Axis.ATOMIC) \
	       == Value("NonAtomic")


def test_vector_meet_is_pointwise() -> None:
	left  = strongest().with_value(Axis.REPR, Value("ValueConverted"))
	right = strongest().with_value(Axis.ATOMIC, Value("NonAtomic"))
	met   = left.meet(right)

	assert met.get(Axis.REPR) == Value("ValueConverted")
	assert met.get(Axis.ATOMIC) == Value("NonAtomic")
	assert met.get(Axis.OFFSET) == Value("AbsoluteStatic")


def test_meet_all_of_nothing_is_the_strongest() -> None:
	assert meet_all([]).get(Axis.MUTATE) == Value("InPlaceFixed")


def test_vector_comparison() -> None:
	weak   = strongest().with_value(Axis.MUTATE, Value("Immutable"))
	strong = strongest()

	assert strong.is_at_least(weak)
	assert not weak.is_at_least(strong)


def test_incomparable_vectors_exist() -> None:
	"""A product lattice: the compiler never needs a total order."""
	left  = strongest().with_value(Axis.REPR, Value("ValueConverted"))
	right = strongest().with_value(Axis.ATOMIC, Value("NonAtomic"))

	assert not left.is_at_least(right)
	assert not right.is_at_least(left)


def test_value_rendering() -> None:
	assert Value("Dynamic").render() == "Dynamic"
	assert Value("Aligned", ("4",)).render() == "Aligned(4)"
	assert Value("Bounded", ("0", "1500")).render() == "Bounded(0, 1500)"
