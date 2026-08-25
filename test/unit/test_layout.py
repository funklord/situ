"""Layout solver tests (project.md section 26.2).

Offsets are bit-valued throughout. Every expected layout below is hand-computed
in the test, because a solver checked against its own output proves nothing.
"""

from __future__ import annotations

import pytest

from dataclasses import fields

from situc.diagnostics import SituError
from situc.layout import BITS_PER_BYTE, Placement, SchemaLayout, solve
from situc.parser import parse_text

PREAMBLE = "endian big;\nbit_order msb_first;\n"


def layout(body: str, preamble: str = PREAMBLE) -> SchemaLayout:
	return solve(parse_text(preamble + body))


def offsets(body: str, struct: str = "S", preamble: str = PREAMBLE) -> dict[str, int]:
	solved = layout(body, preamble)
	return {
		p.path.split(".", 1)[1]: p.offset_bits
		for p in solved.structs[struct].placements
		if p.offset_bits is not None
	}


def size_bits(body: str, preamble: str = PREAMBLE, struct: str = "S") -> int:
	return layout(body, preamble).structs[struct].size_bits


def rendered(body: str, preamble: str = PREAMBLE) -> str:
	with pytest.raises(SituError) as caught:
		layout(body, preamble)
	return caught.value.diagnostic.render()


# -- a pinned footprint, decision 0039 -------------------------------------


def test_a_pin_fixes_the_footprint_and_keeps_the_length() -> None:
	"""The whole point of the construct, in one struct.

	`body` occupies sixteen bytes whatever `used` says, so `trailer` after it
	keeps an absolute offset -- which `u8 body[used]` alone would have cost
	it. The length is untouched: `sized_by` still names `used`, which is why
	the accessors needed no special case.
	"""
	placed = {p.name: p for p in
	          layout("struct S { u8 used; u8 body[used] [size = 16]; u16 t; }")
	          .structs["S"].placements}

	assert placed["body"].size_bits == 16 * BITS_PER_BYTE
	assert placed["body"].size_max_bits == 16 * BITS_PER_BYTE
	assert placed["body"].pinned_bits == 16 * BITS_PER_BYTE
	assert placed["body"].sized_by == "used"
	# The member after it is the reason to pin at all.
	assert placed["t"].offset_bits == 17 * BITS_PER_BYTE


def test_without_the_pin_the_member_after_loses_its_offset() -> None:
	"""The control that says the assertion above is measuring the pin rather
	than something that was true anyway."""
	placed = {p.name: p for p in
	          layout("struct S { u8 used; u8 body[used]; u16 t; }")
	          .structs["S"].placements}

	assert placed["body"].pinned_bits is None
	assert placed["body"].size_bits != placed["body"].size_max_bits
	assert placed["t"].offset_bits is None


def test_a_pin_below_the_members_minimum_is_refused() -> None:
	"""A member whose expression cannot fit its pin describes a message that
	could never validate, so the two halves are refused rather than one of
	them silently winning."""
	text = rendered("struct S { u8 n [min = 40]; u8 a[n] [size = 8]; }")
	assert "pinned to 8 bytes" in text
	assert "needs at least 40" in text


def test_a_pin_must_be_a_positive_literal() -> None:
	"""A footprint the compiler cannot see is one no offset after the member
	can be computed from."""
	assert "positive literal byte count" in rendered(
		"struct S { u8 n; u8 a[n] [size = 0]; }")


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
	assert "may be negative" in rendered("const N = 0 - 1;\nstruct S { u8 a[N]; }")


def test_an_empty_array_needs_something_to_say_where_it_stops() -> None:
	"""`[]` says how many only where something else does: an `indexed` region
	supplies a count, and `until` supplies a delimiter. Alone it says nothing,
	and the diagnostic names both ways out rather than only the one that
	existed when it was written."""
	report = rendered("struct S { u8 a[]; }")
	assert "an array needs a size here" in report
	assert "`indexed` region" in report
	assert "`until`" in report


def test_array_sized_by_an_earlier_field() -> None:
	"""The size is a range, so the array is dynamic and the struct is a frame."""
	solved = layout("struct S { u8 n; u8 a[n]; }")
	struct = solved.structs["S"]

	assert struct.is_frame
	assert struct.size_bits == 8			# just `n` when the count is 0
	assert struct.size_max_bits == 8 + 255 * 8


def test_forward_reference_in_an_array_size_rejected() -> None:
	"""Section 10 forbids it, and the walk enforces it by construction: `n` is
	not in scope until it has been placed."""
	report = rendered("struct S { u8 a[n]; u8 n; }")
	assert "`n` is not in scope here" in report


def test_array_sized_by_a_pinned_field_is_static() -> None:
	"""An expression whose interval is a single point is a compile-time
	constant even though it names a field (section 10)."""
	solved = layout("struct S { u8 n [must_eq = 4]; u8 a[n]; }")
	assert solved.structs["S"].is_fixed_size
	assert solved.structs["S"].size_bytes == 5


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


# -- dynamic layout (phase 5) -----------------------------------------------


def test_a_struct_with_a_variable_member_is_a_frame() -> None:
	solved = layout("struct S { u8 n; u8 a[n]; u8 z; }")
	assert solved.structs["S"].is_frame


def test_members_before_the_dynamic_one_keep_static_offsets() -> None:
	"""The locality rule: a dynamic member weakens what follows it, and
	nothing else (section 11.3)."""
	solved = layout("struct S { u16 a; u8 n; u8 v[n]; u16 z; }")
	found  = {p.name: p for p in solved.structs["S"].placements}

	assert found["a"].offset_bits == 0
	assert found["n"].offset_bits == 16
	assert found["v"].offset_bits == 24
	assert found["z"].offset_bits is None


def test_size_bounds_come_from_the_driving_field() -> None:
	solved = layout("struct S { u16 n [max = 1500]; u8 v[n]; }")
	found  = {p.name: p for p in solved.structs["S"].placements}

	assert found["v"].size_bits == 0
	assert found["v"].size_max_bits == 1500 * 8


def test_remaining_is_unbounded() -> None:
	solved = layout("struct S { u8 a; u8 rest[remaining]; }")
	found  = {p.name: p for p in solved.structs["S"].placements}

	assert found["rest"].size_max_bits is None
	assert solved.structs["S"].size_max_bits is None


def test_remaining_must_be_last() -> None:
	report = rendered("struct S { u8 rest[remaining]; u8 z; }")
	assert "must be the last member of its frame" in report


def test_nothing_may_follow_remaining_even_indirectly() -> None:
	report = rendered("struct S { u8 a; u8 rest[remaining]; u16 z; }")
	assert "must be the last member" in report


def test_array_of_structs_records_its_elements_once() -> None:
	body   = "struct R { u32 id; u16 v; } struct S { u8 n; R rs[n]; }"
	solved = layout(body)
	paths  = [p.path for p in solved.structs["S"].placements]

	assert "S.rs[]" in paths
	assert "S.rs[].v" in paths
	assert "S.rs[0].v" not in paths


def test_element_offsets_are_frame_relative() -> None:
	body   = "struct R { u32 id; u16 v; } struct S { u8 n; R rs[n]; }"
	solved = layout(body)
	found  = {p.path: p for p in solved.structs["S"].placements}

	assert found["S.rs[].v"].offset_bits == 32
	assert found["S.rs[].v"].frame_relative

	# The count varies but the base does not: `rs` starts right after `n`, so
	# element k is at a computable 1 + k*6 and nothing can move it.
	assert not found["S.rs[].v"].frame_base_dynamic


def test_a_dynamically_placed_array_has_a_moving_base() -> None:
	body   = ("struct R { u32 id; u16 v; }"
	          "struct S { u8 n; u8 pad[n]; u8 m; R rs[m]; }")
	solved = layout(body)
	found  = {p.path: p for p in solved.structs["S"].placements}

	# `pad` is variable, so everything after it moves -- including the base the
	# elements are measured from.
	assert found["S.rs[].v"].frame_relative
	assert found["S.rs[].v"].frame_base_dynamic


def test_fixed_array_elements_have_a_static_base() -> None:
	"""Nothing can move a const-count array at a known offset."""
	body   = "struct R { u32 id; u16 v; } struct S { R rs[4]; }"
	solved = layout(body)
	found  = {p.path: p for p in solved.structs["S"].placements}

	assert found["S.rs[].v"].frame_relative
	assert not found["S.rs[].v"].frame_base_dynamic


def test_positional_block_refuses_a_dynamic_member() -> None:
	"""Section 9.2: the block exists so the compiler defends a static region."""
	report = rendered("struct S { u8 n; positional { u8 v[n]; } }")
	assert "cannot contain a dynamic member" in report
	assert "staticness is asserted here" in report


def test_pin_on_a_dynamically_placed_field_rejected() -> None:
	report = rendered("struct S { u8 n; u8 v[n]; u32 z @ 0x08; }")
	assert "no static offset, so the pin cannot hold" in report
	assert "dynamically sized member precedes it" in report


def test_size_builtin_refuses_a_frame() -> None:
	"""`size(X)` is a single number; a frame does not have one."""
	solved = layout("struct S { u8 n; u8 v[n]; }")
	assert solved.lookup("size", "S") is None


def test_a_bit_packed_field_cannot_follow_a_dynamic_member() -> None:
	"""The rule every backend leans on without saying so.

	A bit phase across a dynamic boundary is not something the solver computes,
	and a wrong bit offset is undetectable at run time -- so the construct is
	refused here rather than guessed at anywhere downstream. Four backends
	assert this rather than handle it; if it is ever relaxed, they fire.
	"""
	with pytest.raises(SituError) as caught:
		layout("struct h { u8 v; u16 n; }\n"
		       "struct s { h hdr; u8 opts[hdr.n]; u4 a; u4 b; }\n")

	rendered = caught.value.diagnostic.render()
	assert "bit-packed and cannot follow a dynamically sized member" in rendered
	assert "move this field before the dynamic member" in rendered


def test_the_same_field_is_fine_before_the_dynamic_member() -> None:
	"""Which is the remedy the diagnostic names, so it has to work."""
	solved = layout("struct h { u8 v; u16 n; }\n"
	                "struct s { u4 a; u4 b; h hdr; u8 opts[hdr.n]; }\n")

	packed = [p for p in solved.structs["s"].placements
	          if p.scalar is not None and p.scalar.is_bit_packed]

	assert packed
	assert all(p.offset_bits is not None for p in packed)


# -- a member seen from its parent ------------------------------------------

RUN_IN_A_STRUCT = """
struct label { u2 form; u6 rest; u8 text[rest]; }
struct name { label labels[] while (form == 0) max 8; }
struct question { name qname; }
"""


def placement_at(body: str, path: str) -> Placement:
	schema = parse_text(PREAMBLE + body)
	found  = solve(schema)
	return next(held
	            for layout in found.structs.values()
	            for held in layout.placements
	            if held.path == path)


def test_a_while_run_claims_no_element_count() -> None:
	"""How many there are is whichever one first fails the condition, which is
	not a number the layout knows. It recorded 1 -- the same lie `until`
	carried, with a different construct in front of it (invariant 25)."""
	assert placement_at(RUN_IN_A_STRUCT, "name.labels").array_count is None
	assert placement_at(RUN_IN_A_STRUCT, "name.labels").repeat_while is not None


def test_the_copy_in_the_parent_is_the_same_member() -> None:
	"""The nested copy carried a hand-written list of fields and had fallen
	six behind, `repeat_while` among them -- so a run inside a nested struct
	stopped being a run when the parent looked at it. It read `Sequential`
	anyway, because the false `array_count` above made the generic
	variable-element row fire and reach the same answer by accident. Removing
	one without the other is how the accident showed.
	"""
	own    = placement_at(RUN_IN_A_STRUCT, "name.labels")
	seen   = placement_at(RUN_IN_A_STRUCT, "question.qname.labels")
	differ = {"path", "offset_bits", "frame_relative", "frame_base_dynamic",
	          "dynamic_cause", "dynamic_cause_span", "dynamic_cause_size"}

	for slot in fields(Placement):
		if slot.name not in differ:
			assert getattr(own, slot.name) == getattr(seen, slot.name), slot.name


# -- a packed-decimal field narrower than its digits (decision 0027) --------

CLOCK = ("struct S { u1 halt; bcd2 seconds [bits = 7];"
	" u2 mode; bcd2 hours [bits = 6]; }")


def test_a_bcd_field_may_declare_a_width() -> None:
	"""What a register holding a control bit above the decimal is: a DS1307
	spends the top bit of its seconds register on Clock Halt and two bits of
	its hours register on 12/24 and PM, leaving seven and six bits of packed
	decimal. `bcd2` at eight bits could not describe either (26.35)."""
	assert offsets(CLOCK) == {"halt": 0, "seconds": 1, "mode": 8, "hours": 10}
	assert size_bits(CLOCK) == 16


def test_the_narrowed_field_stays_packed_decimal() -> None:
	"""It is the *top* digit that gives up bits; everything below it stays a
	whole nibble, which is what the hardware does."""
	held = next(p for p in layout(CLOCK).structs["S"].placements
	            if p.path == "S.seconds")

	assert held.scalar is not None
	assert held.scalar.is_bcd and held.scalar.digits == 2
	assert held.scalar.bits == 7


def test_a_width_wider_than_the_type_is_refused() -> None:
	text = rendered("struct S { bcd2 x [bits = 9]; }")

	assert "wider than the type" in text


def test_a_width_that_loses_a_digit_is_refused() -> None:
	"""Four bits cannot hold two digits: the lower one is a whole nibble, so
	the top would have none at all."""
	text = rendered("struct S { bcd2 x [bits = 4]; }")

	assert "leaves no room for 2 digits" in text


def test_a_width_on_anything_else_is_refused() -> None:
	"""Every other type carries its width in its name. Accepting `[bits]` on
	one and ignoring it is invariant 9's silent default."""
	text = rendered("struct S { u8 x [bits = 7]; }")

	assert "not a `bcd` type" in text


def test_a_width_with_no_value_is_refused() -> None:
	text = rendered("struct S { bcd2 x [bits]; }")

	assert "`[bits]` needs a width" in text
