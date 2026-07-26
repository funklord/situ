"""Capability axes and the map (project.md sections 11.1, 18.1, 26.2)."""

from __future__ import annotations

from pathlib import Path

import pytest

from situc import capmap, requirements
from situc.capability import alignment_of, derive, meet, render_offset, render_size
from situc.diagnostics import Source, SituError
from situc.layout import solve
from situc.parser import parse, parse_text

SCHEMAS  = Path(__file__).resolve().parents[1] / "schemas"
PREAMBLE = "endian big;\nbit_order msb_first;\n"


def vectors(body: str, struct: str = "S") -> dict[str, dict[str, str]]:
	schema = parse_text(PREAMBLE + body)
	layout = solve(schema)
	text   = capmap.render(schema, layout, "<test>")
	found: dict[str, dict[str, str]] = {}
	for line in text.splitlines():
		if line.startswith("  "):
			parts = line.split()
			found[parts[0]] = dict(item.split("=", 1) for item in parts[1:])
	return found


# -- offset and size rendering ----------------------------------------------


@pytest.mark.parametrize(("bits", "expected"), [
	(0,   "0x00"),
	(8,   "0x01"),
	(96,  "0x0C"),
	(1,   "0x00:1"),
	(19,  "0x02:3"),
])
def test_offsets_never_truncate_the_bit_part(bits: int, expected: str) -> None:
	"""Section 26.2 asks specifically that a sub-byte offset be reported as one
	rather than silently rounded to a byte."""
	assert render_offset(bits) == expected


@pytest.mark.parametrize(("bits", "expected"), [
	(8,  "1"),
	(32, "4"),
	(3,  "3bit"),
	(13, "13bit"),
])
def test_size_rendering(bits: int, expected: str) -> None:
	assert render_size(bits) == expected


@pytest.mark.parametrize(("offset_bits", "expected"), [
	(0,        8),
	(8,        1),
	(16,       2),
	(24,       1),
	(32,       4),
	(64,       8),
	(128,      8),
	(8 * 12,   4),
	(3,        0),
])
def test_alignment_of_an_offset(offset_bits: int, expected: int) -> None:
	assert alignment_of(offset_bits) == expected


# -- meet -------------------------------------------------------------------


def test_meet_takes_the_weakest() -> None:
	order = ("Strong", "Middle", "Weak")
	assert meet(["Strong", "Weak"], order) == "Weak"
	assert meet(["Strong", "Middle"], order) == "Middle"
	assert meet(["Strong", "Strong"], order) == "Strong"


def test_meet_of_nothing_is_the_weakest() -> None:
	order = ("Strong", "Weak")
	assert meet([], order) == "Weak"


# -- repr -------------------------------------------------------------------


def test_single_byte_scalars_are_the_bytes() -> None:
	found = vectors("struct S { u8 a; i8 b; byte c; }")
	for name in ("S.a", "S.b", "S.c"):
		assert found[name]["repr"] == "MemoryIdentical"


def test_multi_byte_scalars_convert() -> None:
	found = vectors("struct S { u16 a; u32 b; f64 c; }")
	for name in ("S.a", "S.b", "S.c"):
		assert found[name]["repr"] == "ValueConverted"


def test_native_endian_does_not_convert() -> None:
	found = vectors("struct S [endian = native] { u32 a; }")
	assert found["S.a"]["repr"] == "MemoryIdentical"


def test_bit_fields_always_convert() -> None:
	found = vectors("struct S { u3 a; u5 b; }")
	assert found["S.a"]["repr"] == "ValueConverted"


def test_enum_reports_its_backing_representation() -> None:
	"""An enum is its backing type: `E : u8` must read like a plain u8."""
	found = vectors("enum E : u8 { a = 1, } struct S { E kind; }")
	assert found["S.kind"]["repr"] == "MemoryIdentical"


def test_byte_array_stays_addressable() -> None:
	found = vectors("struct S { u8 mac[6]; }")
	assert found["S.mac"]["repr"] == "MemoryIdentical"


def test_struct_field_meets_its_members() -> None:
	"""A struct of bytes stays MemoryIdentical; one holding a u16 does not."""
	bytes_only = vectors("struct Inner { u8 x; u8 y; } struct S { Inner i; }")
	converted  = vectors("struct Inner { u16 x; } struct S { Inner i; }")

	assert bytes_only["S.i"]["repr"] == "MemoryIdentical"
	assert converted["S.i"]["repr"] == "ValueConverted"


# -- atomic -----------------------------------------------------------------


def test_aligned_word_is_atomic() -> None:
	found = vectors("struct S { u32 a; u32 b; }")
	assert found["S.a"]["atomic"] == "AtomicWord"
	assert found["S.b"]["atomic"] == "AtomicWord"


def test_misaligned_word_is_not_atomic() -> None:
	found = vectors("struct S { u8 pad; u32 a; }")
	assert found["S.a"]["atomic"] == "NonAtomic"


def test_bit_field_is_never_atomic() -> None:
	"""Section 11.1 is categorical: writing one is a read-modify-write."""
	found = vectors("struct S { bit a; u7 b; }")
	assert found["S.a"]["atomic"] == "NonAtomic"


def test_array_is_not_atomic() -> None:
	found = vectors("struct S { u32 a[2]; }")
	assert found["S.a"]["atomic"] == "NonAtomic"


def test_struct_is_never_atomic() -> None:
	"""A multi-field update is never atomic in v0, whatever the members say."""
	found = vectors("struct Inner { u32 x; } struct S { Inner i; }")
	assert found["S.i"]["atomic"] == "NonAtomic"


def test_odd_width_is_not_atomic() -> None:
	found = vectors("struct S { u24 a; }")
	assert found["S.a"]["atomic"] == "NonAtomic"


# -- align ------------------------------------------------------------------


def test_bit_fields_are_uniformly_unaligned() -> None:
	"""Their address is not a byte address, so there is no aligned access."""
	found = vectors("struct S { bit a; u7 b; }")
	assert found["S.a"]["align"] == "Unaligned"
	assert found["S.b"]["align"] == "Unaligned"


def test_alignment_is_reported_for_byte_fields() -> None:
	found = vectors("struct S { u32 a; u8 b; u8 c; u16 d; }")
	assert found["S.a"]["align"] == "Aligned(8)"
	assert found["S.b"]["align"] == "Aligned(4)"
	assert found["S.c"]["align"] == "Aligned(1)"
	assert found["S.d"]["align"] == "Aligned(2)"


# -- map format -------------------------------------------------------------


def test_map_is_stable_across_runs() -> None:
	schema = parse_text(PREAMBLE + "struct S { u8 a; u16 b; }")
	layout = solve(schema)
	first  = capmap.render(schema, layout, "x.situ")
	second = capmap.render(schema, layout, "x.situ")
	assert first == second


def test_map_orders_structs_by_name() -> None:
	schema = parse_text(PREAMBLE + "struct Zed { u8 a; } struct Alpha { u8 b; }")
	text   = capmap.render(schema, solve(schema), "x.situ")
	assert text.index("struct Alpha") < text.index("struct Zed")


def test_map_names_the_schema_and_axes() -> None:
	schema = parse_text(PREAMBLE + "struct S { u8 a; }")
	text   = capmap.render(schema, solve(schema), "path/to/x.situ")
	assert "# axes:   offset size align repr atomic" in text
	# The file name only: a committed map must be byte-identical however it was
	# generated, so the caller's working directory cannot leak into it.
	assert "# schema: x.situ" in text
	assert "path/to" not in text


def test_reserved_entries_get_a_distinct_path() -> None:
	"""An unnamed region still has to be nameable, and must not collide with
	the struct's own path."""
	found = vectors("struct S { u3 a; reserved u5; }")
	assert "S.<reserved0>" in found


def test_multiple_reserved_regions_are_numbered() -> None:
	found = vectors("struct S { reserved u8; reserved u8; }")
	assert "S.<reserved0>" in found
	assert "S.<reserved1>" in found


# -- requirements -----------------------------------------------------------


def test_size_requirement_passes() -> None:
	schema = parse_text(PREAMBLE + "struct S { u8 a; u16 b; }\nrequire size(S) == 3;")
	outcomes = requirements.discharge(schema, solve(schema))
	assert [outcome.satisfied for outcome in outcomes] == [True]


def test_failed_size_requirement_is_an_error() -> None:
	schema = parse_text(PREAMBLE + "struct S { u8 a; }\nrequire size(S) == 4;")
	with pytest.raises(SituError) as caught:
		requirements.discharge(schema, solve(schema))

	rendered = caught.value.diagnostic.render()
	assert "requirement not satisfied" in rendered
	assert "size(S) is 1, == 4 required" in rendered


def test_failed_assert_is_only_a_warning() -> None:
	schema = parse_text(PREAMBLE + "struct S { u8 a; }\nassert size(S) == 4;")
	outcomes = requirements.discharge(schema, solve(schema))

	assert [outcome.satisfied for outcome in outcomes] == [False]
	warnings = requirements.warnings(outcomes)
	assert len(warnings) == 1
	assert "assertion not satisfied" in warnings[0].render()


def test_capability_predicates_are_deferred_not_passed() -> None:
	"""A requirement that quietly does nothing is worse than none at all."""
	schema = parse_text(PREAMBLE + "struct S { u8 a; }\nrequire absolute_static(S);")
	outcomes = requirements.discharge(schema, solve(schema))

	assert outcomes[0].deferred == 3
	assert not outcomes[0].satisfied
	assert not outcomes[0].is_error


def test_deferrals_are_reported_and_grouped_by_phase() -> None:
	schema = parse_text(
		PREAMBLE
		+ "struct S { u8 a; }\n"
		+ "require absolute_static(S);\n"
		+ "require in_place(S.a);\n"
		+ "require verify_gated(S);\n"
	)
	notes = requirements.deferrals(requirements.discharge(schema, solve(schema)))

	rendered = "\n".join(note.render() for note in notes)
	assert "2 requirements not checked by this build; needs phase 3" in rendered
	assert "1 requirement not checked by this build; needs phase 8" in rendered


def test_a_requirement_reports_the_latest_phase_it_needs() -> None:
	schema = parse_text(
		PREAMBLE + "struct S { u8 a; }\nrequire canonical(S) && verify_gated(S);")
	outcomes = requirements.discharge(schema, solve(schema))
	assert outcomes[0].deferred == 8


def test_offset_requirement_discharges_now() -> None:
	schema = parse_text(PREAMBLE + "struct S { u8 a; u16 b; }\nrequire offset(S.b) == 1;")
	outcomes = requirements.discharge(schema, solve(schema))
	assert outcomes[0].satisfied
	assert outcomes[0].deferred is None


# -- example 5.1 ------------------------------------------------------------


def test_example_5_1_map_is_exact() -> None:
	path   = SCHEMAS / "header.situ"
	source = Source(str(path), path.read_text(encoding="ascii"))
	schema = parse(source)
	text   = capmap.render(schema, solve(schema), "header.situ")

	assert "struct Flags size=1" in text
	assert "struct Header size=9" in text

	# Hand-computed: 1 + 1 + 1 + 2 = 5, so seq lands at 0x05 and the header is
	# 9 bytes. Reaching project.md's 0x06 and 10 would need a padding byte, and
	# section 8.4 inserts none.
	assert "Header.version" in text and "AbsoluteStatic(0x00)" in text
	assert "Header.seq" in text
	seq = next(line for line in text.splitlines() if "Header.seq" in line)
	assert "offset=AbsoluteStatic(0x05)" in seq
	assert "size=Fixed(4)" in seq
	# A u32 at an odd offset is neither naturally aligned nor atomic.
	assert "align=Aligned(1)" in seq
	assert "atomic=NonAtomic" in seq
