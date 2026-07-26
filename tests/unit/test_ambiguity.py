"""The ambiguity table of project.md section 17.0, row by row.

Section 26.6 requires every row to be an error when unresolved and to compile
when resolved. Auditing them together rather than incidentally is the point:
each row is a place where situ refuses to guess, and a row that quietly stopped
being enforced would be invisible in the tests for the construct it governs.

The governing principle, restated because every row is an instance of it:
wherever an ambiguity exists the schema must resolve it explicitly, and where a
default does exist the *safe* option is silent and the *unsafe* option is loud.
"""

from __future__ import annotations

import pytest

from situc.capability import Axis, Value
from situc.diagnostics import SituError
from situc.layout import solve
from situc.parser import parse_text
from situc.resolve import ResolvedSchema, resolve

# Rows belonging to constructs that do not exist yet, with the phase that adds
# them. Listed so the gap is visible rather than forgotten.
DEFERRED_ROWS = {
	"overlapping non-nested tag coverage":            8,
	"layer order of codecs over the same region":     7,
	"a codec kernel contradicting declared properties": 7,
	"whether an unimplemented codec errors now or later": 7,
}


def analyse(body: str) -> ResolvedSchema:
	schema = parse_text(body)
	return resolve(schema, solve(schema))


def axis(resolved: ResolvedSchema, path: str, which: Axis) -> Value:
	entry = resolved.find(path)
	assert entry is not None, f"no entry for {path}"
	return entry.vector.get(which)


def rejected(body: str) -> str:
	with pytest.raises(SituError) as caught:
		analyse(body)
	return caught.value.diagnostic.render()


def accepted(body: str) -> ResolvedSchema:
	return analyse(body)


# -- row 1: endianness of a multi-byte scalar with no directive in scope ----


def test_missing_endian_is_an_error() -> None:
	report = rejected("struct S { u16 a; }")
	assert "no endianness in scope" in report
	assert "situ never guesses" in report


@pytest.mark.parametrize("resolution", [
	"endian big;\nstruct S { u16 a; }",
	"struct S [endian = little] { u16 a; }",
	"struct S { u16 a [endian = big]; }",
])
def test_endian_resolved_at_any_level_compiles(resolution: str) -> None:
	"""File, struct or field level, as the row says."""
	accepted(resolution)


def test_a_single_byte_scalar_needs_no_endian() -> None:
	"""The row is about multi-byte scalars; a byte has no byte order."""
	accepted("struct S { u8 a; byte b; }")


# -- row 2: bit order with any sub-byte field present -----------------------


def test_missing_bit_order_is_an_error() -> None:
	assert "no bit order in scope" in rejected("endian big;\nstruct S { u3 a; u5 b; }")


@pytest.mark.parametrize("resolution", [
	"endian big;\nbit_order msb_first;\nstruct S { u3 a; u5 b; }",
	"endian big;\nstruct S [bit_order = lsb_first] { u3 a; u5 b; }",
])
def test_bit_order_resolved_compiles(resolution: str) -> None:
	accepted(resolution)


def test_no_sub_byte_field_needs_no_bit_order() -> None:
	accepted("endian big;\nstruct S { u16 a; }")


# -- row 3: a bit field straddling a byte boundary --------------------------


PREAMBLE = "endian big;\nbit_order msb_first;\n"


def test_straddling_without_the_attribute_is_an_error() -> None:
	report = rejected(PREAMBLE + "struct S { u3 a; u3 b; u3 c; }")
	assert "straddles a byte boundary" in report
	assert "read-modify-write" in report


def test_straddling_with_the_attribute_compiles() -> None:
	accepted(PREAMBLE + "struct S [allow_straddle] { u3 a; u3 b; u3 c; }")


# -- row 5: variant arms of unequal size ------------------------------------


VARIANT = (
	PREAMBLE
	+ "enum K : u8 { a = 1, b = 2, }"
	+ "struct A { u16 x; } struct B { u32 y; u32 z; }"
)


def test_unequal_arms_are_accepted_and_reported() -> None:
	"""This row is not an error: the row says the consequence is reported."""
	resolved = analyse(
		VARIANT + "struct S { K k; variant v switch (k) "
		"{ case K.a: A p; case K.b: B q; } u16 tail; }")

	entry = resolved.find("S.v")
	assert entry is not None
	assert entry.vector.get(Axis.SIZE) == Value("Bounded", ("2", "8"))
	assert any("cost up to" in w.effect.because for w in entry.weakenings)


def test_equalize_pays_the_cost() -> None:
	resolved = analyse(
		VARIANT + "struct S { K k; variant v switch (k) [equalize] "
		"{ case K.a: A p; case K.b: B q; } u16 tail; }")

	entry = resolved.find("S.tail")
	assert entry is not None
	assert entry.vector.get(Axis.OFFSET).base == "AbsoluteStatic"


# -- row 6: unknown enum value, TLV tag, or version -------------------------


def test_enum_unknown_policy_defaults_to_the_safe_option() -> None:
	"""Silent because it is safe; the unsafe one has to be written."""
	safe   = analyse(PREAMBLE + "enum E : u8 { a = 1, } struct S { E k; }")
	loud   = analyse(PREAMBLE + "enum E : u8 { a = 1, default = pass, } struct S { E k; }")

	assert axis(safe, "S.k", Axis.CANONICAL) == Value("Canonical")
	assert axis(loud, "S.k", Axis.CANONICAL) == Value("NonCanonical")


def test_tlv_unknown_policy_defaults_to_the_safe_option() -> None:
	safe = analyse(PREAMBLE + "struct S { tlv o (tag_type = u8, ordering = ascending); }")
	loud = analyse(PREAMBLE + "struct S { tlv o (tag_type = u8, ordering = ascending, "
	               "unknown = preserve); }")

	assert axis(safe, "S.o", Axis.CANONICAL) == Value("Canonical")
	assert axis(loud, "S.o", Axis.CANONICAL) == Value("NonCanonical")


def test_an_unknown_version_needs_a_default_arm() -> None:
	"""Section 19: never silently accept an unknown version."""
	assert "does not cover every value" in rejected(
		PREAMBLE + "enum V : u8 { one = 1, two = 2, } struct A { u8 x; }"
		"struct S { V v; variant body switch (v) { case V.one: A a; } }")


# -- row 7: non-minimal varint acceptance -----------------------------------


def test_minimal_is_never_defaulted() -> None:
	"""Present or absent, and the difference shows in the map."""
	strict = analyse(PREAMBLE + "varint_type v { encoding = leb128; max_bits = 64; "
	                 "minimal; } struct S { v a; }")
	loose  = analyse(PREAMBLE + "varint_type v { encoding = leb128; max_bits = 64; } "
	                 "struct S { v a; }")

	assert axis(strict, "S.a", Axis.CANONICAL) == Value("Canonical")
	assert axis(loose, "S.a", Axis.CANONICAL) == Value("NonCanonical")


# -- row 8: duplicate TLV tags ----------------------------------------------


def test_duplicate_tags_default_to_the_safe_option() -> None:
	resolved = analyse(PREAMBLE + "struct S { tlv o (tag_type = u8, "
	                   "ordering = ascending); }")
	assert axis(resolved, "S.o", Axis.CANONICAL) == Value("Canonical")


def test_duplicates_allowed_without_ordering_is_not_canonical() -> None:
	"""The row asks for an ordering rule alongside `allowed`; without one the
	same content has several encodings, and the map says so."""
	resolved = analyse(PREAMBLE + "struct S { tlv o (tag_type = u8, "
	                   "duplicate_tags = allowed); }")
	entry = resolved.find("S.o")
	assert entry is not None
	assert entry.vector.get(Axis.CANONICAL) == Value("NonCanonical")
	assert "tlv-unordered-duplicates" in {w.rule.name for w in entry.weakenings}


def test_duplicates_allowed_with_ordering_is_canonical() -> None:
	resolved = analyse(PREAMBLE + "struct S { tlv o (tag_type = u8, "
	                   "duplicate_tags = allowed, ordering = ascending); }")
	assert axis(resolved, "S.o", Axis.CANONICAL) == Value("Canonical")


def test_an_unknown_duplicate_policy_is_rejected() -> None:
	assert "unknown `duplicate_tags` policy" in rejected(
		PREAMBLE + "struct S { tlv o (tag_type = u8, duplicate_tags = sometimes); }")


# -- row 11: a field's alignment where the target may fault -----------------


def test_require_aligned_refuses_a_misaligned_field() -> None:
	report = rejected(PREAMBLE + "struct S { u8 p; u32 a [require_aligned]; }")
	assert "lands at 1-byte alignment" in report
	assert "needs 4-byte alignment" in report
	assert "faults on some targets" in report


def test_require_aligned_accepts_a_naturally_aligned_field() -> None:
	accepted(PREAMBLE + "struct S { u32 a [require_aligned]; }")


def test_omitting_the_attribute_is_the_explicit_acceptance() -> None:
	"""The row offers `[require_aligned]` *or* explicit acceptance. Absence is
	the acceptance, and the align axis records what was accepted."""
	resolved = analyse(PREAMBLE + "struct S { u8 p; u32 a; }")
	assert axis(resolved, "S.a", Axis.ALIGN) == Value("Aligned", ("1",))


def test_require_aligned_on_a_bit_field_is_rejected() -> None:
	report = rejected(PREAMBLE + "struct S { u3 a [require_aligned]; u5 b; }")
	assert "not a whole-byte scalar" in report


def test_require_aligned_on_a_dynamic_offset_is_rejected() -> None:
	"""Alignment cannot be promised for a field whose position depends on the
	data."""
	report = rejected(PREAMBLE + "struct S { u8 n; u8 v[n]; u32 a [require_aligned]; }")
	assert "has no static offset" in report


# -- the rows that belong to later phases -----------------------------------


def test_deferred_rows_are_recorded() -> None:
	"""Four rows govern constructs that do not exist yet. Naming them here
	keeps the audit honest about what it does not cover."""
	assert set(DEFERRED_ROWS.values()) <= {7, 8}
	assert len(DEFERRED_ROWS) == 4
