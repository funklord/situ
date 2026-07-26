"""Layout solver tests (project.md section 26.2).

Offsets are bit-valued throughout. Every expected layout below is hand-computed
in the test, because a solver checked against its own output proves nothing.
"""

from __future__ import annotations

import pytest

from situc.diagnostics import SituError
from situc.layout import BITS_PER_BYTE, SchemaLayout, solve
from situc.parser import parse_text

PREAMBLE = "endian big;\nbit_order msb_first;\n"


def layout(body: str, preamble: str = PREAMBLE) -> SchemaLayout:
	return solve(parse_text(preamble + body))


def offsets(body: str, struct: str = "S", preamble: str = PREAMBLE) -> dict[str, int]:
	solved = layout(body, preamble)
	return {
		p.path.split(".", 1)[1]: p.offset_bits
		for p in solved.structs[struct].placements
	}


def size_bits(body: str, preamble: str = PREAMBLE, struct: str = "S") -> int:
	return layout(body, preamble).structs[struct].size_bits


def rendered(body: str, preamble: str = PREAMBLE) -> str:
	with pytest.raises(SituError) as caught:
		layout(body, preamble)
	return caught.value.diagnostic.render()


# -- byte-aligned scalars ---------------------------------------------------


def test_scalars_pack_end_to_end_with_no_padding() -> None:
	"""Section 8.4: situ inserts no implicit padding, so a u32 may sit at 3."""
	assert offsets("struct S { u8 a; u16 b; u32 c; }") == {
		"a": 0,
		"b": 1 * BITS_PER_BYTE,
		"c": 3 * BITS_PER_BYTE,
	}
	assert size_bits("struct S { u8 a; u16 b; u32 c; }") == 7 * BITS_PER_BYTE


def test_arrays_multiply_the_element_width() -> None:
	assert offsets("struct S { u8 a[6]; u16 b; }") == {"a": 0, "b": 6 * BITS_PER_BYTE}


def test_array_sized_by_a_constant() -> None:
	solved = layout("const N = 4;\nstruct S { u8 a[N]; u8 b; }")
	assert solved.structs["S"].size_bytes == 5


def test_nested_struct_contributes_its_whole_size() -> None:
	body = "struct Inner { u16 x; u16 y; } struct S { u8 a; Inner inner; u8 b; }"
	assert offsets(body) == {
		"a":       0,
		"inner":   1 * BITS_PER_BYTE,
		"inner.x": 1 * BITS_PER_BYTE,
		"inner.y": 3 * BITS_PER_BYTE,
		"b":       5 * BITS_PER_BYTE,
	}


def test_enum_lays_out_as_its_backing_type() -> None:
	body = "enum E : u16 { a = 1, } struct S { u8 x; E kind; u8 y; }"
	assert offsets(body) == {"x": 0, "kind": 8, "y": 24}


def test_positional_block_does_not_open_a_frame() -> None:
	body = "struct S { u8 a; positional { u16 b; u8 c; } u8 d; }"
	assert offsets(body) == {"a": 0, "b": 8, "c": 24, "d": 32}


# -- bit packing ------------------------------------------------------------


def test_bit_fields_pack_within_a_byte() -> None:
	assert offsets("struct S { bit a; bit b; u3 c; u3 d; }") == {
		"a": 0, "b": 1, "c": 2, "d": 5,
	}
	assert size_bits("struct S { bit a; bit b; u3 c; u3 d; }") == 8


def test_four_plus_four_closes_a_byte() -> None:
	assert offsets("struct S { u4 a; u4 b; u8 c; }") == {"a": 0, "b": 4, "c": 8}


def test_enum_with_a_sub_byte_backing_type_packs() -> None:
	"""An enum is its backing type for layout: `enum E : u4` must pack exactly
	as a bare u4 does, not demand a byte boundary it can never have."""
	body = "enum E : u4 { a = 1, } struct S { bit x; u3 y; E z; }"
	assert offsets(body) == {"x": 0, "y": 1, "z": 4}
	assert size_bits(body) == 8


def test_bit_packing_continues_across_bytes() -> None:
	body = "struct S [allow_straddle] { u3 a; u3 b; u3 c; u3 d; }"
	assert offsets(body) == {"a": 0, "b": 3, "c": 6, "d": 9}
	assert size_bits(body) == 12


@pytest.mark.parametrize(("order", "expected"), [
	# msb_first fills from the most significant bit down, so the first field
	# takes the top bits and its shift is what remains below it.
	("msb_first", {"a": 5, "b": 3, "c": 0}),
	# lsb_first fills upward, so a field's shift is its own bit offset.
	("lsb_first", {"a": 0, "b": 3, "c": 5}),
])
def test_bit_order_decides_the_shift(order: str, expected: dict[str, int]) -> None:
	solved = layout("struct S { u3 a; u2 b; u3 c; }",
	                preamble=f"endian big;\nbit_order {order};\n")

	shifts = {}
	for placement in solved.structs["S"].placements:
		assert placement.bit_position is not None
		shifts[placement.name] = placement.bit_position.shift

	assert shifts == expected


def test_bit_order_does_not_change_linear_offsets() -> None:
	"""Only the physical bit within the byte changes; the layout does not."""
	body = "struct S { u3 a; u2 b; u3 c; }"
	msb  = layout(body, "endian big;\nbit_order msb_first;\n")
	lsb  = layout(body, "endian big;\nbit_order lsb_first;\n")

	assert ([p.offset_bits for p in msb.structs["S"].placements]
	        == [p.offset_bits for p in lsb.structs["S"].placements])


# -- straddling -------------------------------------------------------------


def test_straddling_bit_field_rejected() -> None:
	report = rendered("struct S { u3 a; u3 b; u3 c; }")
	assert "straddles a byte boundary" in report
	assert "read-modify-write" in report
	assert "add `[allow_straddle]`" in report


def test_straddling_permitted_with_the_attribute() -> None:
	assert size_bits("struct S [allow_straddle] { u3 a; u3 b; u3 c; }") == 9


def test_wide_packed_field_always_straddles() -> None:
	"""13 bits cannot fit in a byte at any offset."""
	assert "straddles" in rendered("struct S { u13 a; u3 b; }")


def test_packed_field_ending_exactly_on_a_byte_does_not_straddle() -> None:
	assert size_bits("struct S { u4 a; u4 b; }") == 8


# -- byte-aligned types after bit fields ------------------------------------


def test_byte_aligned_type_after_an_unclosed_byte_rejected() -> None:
	"""No implicit padding, so this cannot be silently nudged into place."""
	report = rendered("struct S { u3 a; u8 b; }")
	assert "must start on a byte boundary" in report
	assert "situ inserts no implicit padding" in report
	assert "reserved u5" in report


def test_closing_the_byte_makes_it_legal() -> None:
	assert offsets("struct S { u3 a; reserved u5; u8 b; }") == {
		"a": 0, "<reserved0>": 3, "b": 8,
	}


def test_nested_struct_after_an_unclosed_byte_rejected() -> None:
	assert "must start on a byte boundary" in rendered(
		"struct Inner { u8 x; } struct S { u3 a; Inner b; }")


# -- missing directives (section 17.0) --------------------------------------


def test_multi_byte_scalar_without_endian_rejected() -> None:
	report = rendered("struct S { u16 a; }", preamble="")
	assert "no endianness in scope" in report
	assert "situ never guesses" in report


def test_single_byte_scalar_needs_no_endian() -> None:
	assert size_bits("struct S { u8 a; byte b; }", "") == 16


def test_sub_byte_field_without_bit_order_rejected() -> None:
	report = rendered("struct S { u3 a; u5 b; }", preamble="endian big;\n")
	assert "no bit order in scope" in report


def test_struct_level_endian_satisfies_the_requirement() -> None:
	assert size_bits("struct S [endian = little] { u16 a; }", "") == 16


def test_field_level_endian_overrides_the_file() -> None:
	solved = layout("struct S { u16 a; u16 b [endian = little]; }")
	placements = {p.name: p for p in solved.structs["S"].placements}
	assert placements["a"].endian is not None
	assert placements["b"].endian is not None
	assert placements["a"].endian != placements["b"].endian


def test_unknown_marker_is_rejected() -> None:
	report = rendered("struct S [endian = from(nope)] { endian_marker nope; }", "")
	assert "unknown endian marker `nope`" in report


def test_marker_satisfies_the_byte_order_requirement() -> None:
	"""A marked struct needs no `endian` directive: the order is known, it just
	is not known until parse time."""
	body = ("endian_marker bo : u16 { little = 0x4949, big = 0x4D4D, }"
	        "struct S [endian = from(bo)] { endian_marker bo; u32 a; }")
	assert size_bits(body, "") == 48


def test_marker_field_lays_out_as_its_backing_type() -> None:
	body = ("endian_marker bo : u16 { little = 0x4949, big = 0x4D4D, }"
	        "struct S [endian = from(bo)] { endian_marker bo; u16 a; }")
	assert offsets(body, preamble="") == {"bo": 0, "a": 16}


def test_marker_must_start_on_a_byte_boundary() -> None:
	body = ("endian_marker bo : u16 { little = 0x4949, big = 0x4D4D, }"
	        "struct S [endian = from(bo), allow_straddle] "
	        "{ u3 x; endian_marker bo; }")
	assert "must start on a byte boundary" in rendered(body)


# -- pins -------------------------------------------------------------------


def test_correct_pin_passes() -> None:
	assert size_bits("struct S { u8 a; u16 b; u32 c @ 0x03; }") == 7 * BITS_PER_BYTE


def test_wrong_pin_rejected_with_both_values() -> None:
	report = rendered("struct S { u8 a; u16 b; u32 c @ 0x08; }")
	assert "does not match the computed layout" in report
	assert "pinned to 0x08, solved to 0x03" in report
	assert "5 bytes earlier" in report
	assert "it does not place the field" in report


def test_pin_drift_direction_is_reported() -> None:
	report = rendered("struct S { u8 a; u16 b; u32 c @ 0x01; }")
	assert "2 bytes later" in report


def test_pin_off_by_one_uses_the_singular() -> None:
	report = rendered("struct S { u8 a; u16 b; u32 c @ 0x04; }")
	assert "1 byte earlier" in report


def test_pin_on_a_sub_byte_field_rejected() -> None:
	report = rendered("struct S [allow_straddle] { u3 a; u13 b @ 0x00; }")
	assert "not at a byte offset" in report
	assert "byte 0, bit 3" in report


def test_pin_may_reference_a_constant() -> None:
	assert size_bits("const AT = 3;\nstruct S { u8 a; u16 b; u32 c @ AT; }") == 56


# -- arrays -----------------------------------------------------------------


def test_zero_length_array_rejected() -> None:
	report = rendered("struct S { u8 a[0]; }")
	assert "array length is zero" in report
	assert "remove it instead" in report


def test_negative_length_array_rejected() -> None:
	assert "is negative" in rendered("const N = 0 - 1;\nstruct S { u8 a[N]; }")


def test_empty_array_form_deferred_to_phase_six() -> None:
	report = rendered("struct S { u8 a[]; }")
	assert "an array needs a size here" in report
	assert "phase 6" in report


def test_field_reference_in_an_array_size_rejected() -> None:
	"""Phase 1 accepts the syntax; constness is the solver's to enforce."""
	report = rendered("struct S { u8 n; u8 a[n]; }")
	assert "not a compile-time constant" in report


# -- size and offset builtins -----------------------------------------------


def test_size_builtin_answers_in_bytes() -> None:
	solved = layout("struct S { u8 a; u16 b; }")
	assert solved.lookup("size", "S") == 3
	assert solved.lookup("size", "S.b") == 2


def test_offset_builtin_answers_in_bytes() -> None:
	solved = layout("struct S { u8 a; u16 b; }")
	assert solved.lookup("offset", "S.b") == 1


def test_size_of_a_sub_byte_field_is_not_rounded() -> None:
	"""Refused rather than reported as 0 or 1 bytes."""
	solved = layout("struct S { u3 a; u5 b; }")
	assert solved.lookup("size", "S.a") is None


def test_count_builtin_answers_the_array_length() -> None:
	solved = layout("struct S { u8 a[6]; }")
	assert solved.lookup("count", "S.a") == 6


def test_unknown_path_is_not_found() -> None:
	solved = layout("struct S { u8 a; }")
	assert solved.lookup("size", "S.nope") is None
	assert solved.lookup("size", "Nope") is None
