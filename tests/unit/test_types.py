"""Scalar type table tests (project.md section 8.1, decision 0005)."""

from __future__ import annotations

import dataclasses

import pytest

from situc.types import ScalarKind, WidthError, is_scalar_name, lookup


@pytest.mark.parametrize(("name", "kind", "bits"), [
	("u1",  ScalarKind.UINT,  1),
	("u8",  ScalarKind.UINT,  8),
	("u12", ScalarKind.UINT,  12),
	("u24", ScalarKind.UINT,  24),
	("u48", ScalarKind.UINT,  48),
	("u64", ScalarKind.UINT,  64),
	("i8",  ScalarKind.SINT,  8),
	("i64", ScalarKind.SINT,  64),
	("f16", ScalarKind.FLOAT, 16),
	("f32", ScalarKind.FLOAT, 32),
	("f64", ScalarKind.FLOAT, 64),
	("bit", ScalarKind.BIT,   1),
])
def test_widths_and_kinds(name: str, kind: ScalarKind, bits: int) -> None:
	scalar = lookup(name)
	assert scalar is not None
	assert (scalar.kind, scalar.bits) == (kind, bits)


def test_bool_is_one_bit_unsigned() -> None:
	scalar = lookup("bool")
	assert scalar is not None
	assert (scalar.kind, scalar.bits) == (ScalarKind.UINT, 1)


def test_byte_is_eight_bit_unsigned() -> None:
	scalar = lookup("byte")
	assert scalar is not None
	assert (scalar.kind, scalar.bits) == (ScalarKind.UINT, 8)


def test_signedness() -> None:
	unsigned = lookup("u16")
	signed   = lookup("i16")
	assert unsigned is not None and signed is not None
	assert not unsigned.signed
	assert signed.signed


@pytest.mark.parametrize(("name", "packed"), [
	("u1",  True),
	("u3",  True),
	("u7",  True),
	("bit", True),
	("u12", True),
	("u20", True),
	("u8",  False),
	("u16", False),
	("u24", False),
	("u32", False),
	("u48", False),
	("u64", False),
	("f32", False),
])
def test_whole_byte_widths_are_not_packed(name: str, packed: bool) -> None:
	"""Decision 0005: a width that is a whole number of bytes is byte-aligned.

	This is the sentence of section 8.1 that governs, against the one calling
	all non-power-of-two widths bit-packed.
	"""
	scalar = lookup(name)
	assert scalar is not None
	assert scalar.is_bit_packed is packed


@pytest.mark.parametrize(("name", "crosses"), [
	("u1",  False),
	("u7",  False),
	("bit", False),
	("u8",  False),
	("u12", True),
	("u20", True),
	("u48", False),
])
def test_packed_widths_above_eight_always_straddle(name: str, crosses: bool) -> None:
	scalar = lookup(name)
	assert scalar is not None
	assert scalar.crosses_byte_boundary is crosses


@pytest.mark.parametrize("name", ["u0", "i0", "u65", "u128", "i65"])
def test_out_of_range_widths_rejected(name: str) -> None:
	with pytest.raises(WidthError, match="out of range"):
		lookup(name)


@pytest.mark.parametrize("name", ["u08", "i012", "u0064"])
def test_leading_zero_widths_rejected(name: str) -> None:
	"""`u08` reads as though it might mean something other than `u8`."""
	with pytest.raises(WidthError, match="leading zero"):
		lookup(name)


@pytest.mark.parametrize("name", ["Header", "MsgType", "u", "uu8", "u8x", "f8", "f128"])
def test_non_scalars_return_none(name: str) -> None:
	assert lookup(name) is None


def test_is_scalar_name_reports_invalid_widths_as_scalars() -> None:
	"""`u65` is unmistakably a width form, so the parser must say "too wide"
	rather than "unknown type"."""
	assert is_scalar_name("u65")
	assert not is_scalar_name("Header")


# -- fixed point (section 8.1) ----------------------------------------------


def test_q_notation_splits_into_integer_and_fractional_bits() -> None:
	fixed = lookup("q16_16")

	assert fixed is not None
	assert fixed.bits == 32 and fixed.frac_bits == 16 and fixed.int_bits == 16
	assert fixed.signed
	assert fixed.scale == 65536


def test_the_unsigned_form_is_uq() -> None:
	unsigned = lookup("uq8_8")

	assert unsigned is not None
	assert not unsigned.signed
	assert unsigned.bits == 16 and unsigned.scale == 256


def test_q15_is_sixteen_bits() -> None:
	"""The audio convention: one sign bit and fifteen fractional."""
	q15 = lookup("q1_15")

	assert q15 is not None
	assert q15.bits == 16 and q15.frac_bits == 15


def test_fixed_point_packs_by_the_same_rule_as_everything_else() -> None:
	"""`q4_4` is a byte and `q2_3` is not. Nothing about the type changes the
	rule in section 8.1: what decides is whether the width is whole bytes."""
	aligned = lookup("q4_4")
	packed  = lookup("q2_3")

	assert aligned is not None and not aligned.is_bit_packed
	assert packed is not None and packed.is_bit_packed


@pytest.mark.parametrize("name,reason", [
	("q0_16",  "no integer bits"),
	("q16_0",  "no fractional bits"),
	("q32_33", "65 bits"),
])
def test_a_malformed_fixed_point_name_says_why(name: str, reason: str) -> None:
	with pytest.raises(WidthError) as caught:
		lookup(name)

	assert reason in str(caught.value)


def test_q16_0_names_the_integer_type_it_should_have_been() -> None:
	"""A diagnostic that only says no is half a diagnostic."""
	with pytest.raises(WidthError) as caught:
		lookup("q16_0")

	assert "i16" in str(caught.value)


# -- BCD (section 8.1) -------------------------------------------------------


def test_bcd_counts_digits_not_bits() -> None:
	"""Which is what hardware counts: an RTC holds `bcd2` for seconds."""
	bcd = lookup("bcd8")

	assert bcd is not None
	assert bcd.digits == 8 and bcd.bits == 32
	assert bcd.decimal_max == 99999999
	assert bcd.is_bcd and not bcd.signed


def test_two_digits_are_one_byte() -> None:
	bcd = lookup("bcd2")

	assert bcd is not None
	assert bcd.bits == 8 and bcd.decimal_max == 99
	assert not bcd.is_bit_packed


def test_an_odd_digit_count_packs() -> None:
	"""Three digits are twelve bits, which is not a whole number of bytes."""
	bcd = lookup("bcd3")

	assert bcd is not None
	assert bcd.bits == 12 and bcd.is_bit_packed


@pytest.mark.parametrize(("bits", "expected"), [
	(8, 99),		# `bcd2` whole: both digits have four bits
	(7, 79),		# decision 0027's case: three bits for the tens digit
	(6, 39),		# `wall_clock.hours`, under two control bits
	(5, 19),		# `wall_clock.month`
])
def test_a_narrowed_bcd_field_stops_where_its_bits_do(bits: int,
		expected: int) -> None:
	"""Decision 0027 lets a control bit sit above packed decimal, and then the
	top digit is not a whole nibble.

	`decimal_max` answered "all nines" from the digit count alone, so a
	seven-bit `bcd2` reported 99 -- a `_MAX` macro naming a value the field
	cannot hold, a document repeating it, and a setter that writes 0x99 into
	seven bits and reads back 19. Every narrowed field in `examples/rtc` was
	wrong, and nothing had ever asked (26.43).
	"""
	bcd = lookup("bcd2")
	assert bcd is not None

	assert dataclasses.replace(bcd, bits=bits).decimal_max == expected


@pytest.mark.parametrize("name", ["bcd0", "bcd17"])
def test_a_bcd_width_out_of_range_is_refused(name: str) -> None:
	with pytest.raises(WidthError):
		lookup(name)


def test_the_new_types_do_not_shadow_ordinary_names() -> None:
	"""`q` and `bcd` are prefixes, not keywords: a struct may still be called
	`quality` and a field `bcdata`."""
	assert lookup("quality") is None
	assert lookup("bcdata") is None
	assert lookup("qos") is None
