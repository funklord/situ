"""Scalar type table tests (project.md section 8.1, decision 0005)."""

from __future__ import annotations

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
