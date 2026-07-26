"""The capability map, requirement discharge and blame chains.

Section 26 invariant 3: every diagnostic has a blame chain, and one without is a
bug. These tests hold that line for the predicates phase 3 can decide.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from situc import capmap, requirements
from situc.capability import Axis
from situc.diagnostics import Source, SituError
from situc.layout import solve
from situc.parser import parse, parse_text
from situc.propagate import render_offset, render_size
from situc.resolve import ResolvedSchema, resolve

SCHEMAS  = Path(__file__).resolve().parents[1] / "schemas"
PREAMBLE = "endian big;\nbit_order msb_first;\n"


def analysed(body: str, preamble: str = PREAMBLE) -> tuple[object, ResolvedSchema]:
	schema = parse_text(preamble + body)
	return schema, resolve(schema, solve(schema))


def discharge(body: str, preamble: str = PREAMBLE) -> list[requirements.Outcome]:
	schema, resolved = analysed(body, preamble)
	return requirements.discharge(schema, resolved)			# type: ignore[arg-type]


def failure(body: str, preamble: str = PREAMBLE) -> str:
	with pytest.raises(SituError) as caught:
		discharge(body, preamble)
	return caught.value.diagnostic.render()


def rendered_map(body: str, preamble: str = PREAMBLE) -> str:
	schema, resolved = analysed(body, preamble)
	return capmap.render(schema, resolved, "x.situ")		# type: ignore[arg-type]


# -- rendering helpers ------------------------------------------------------


@pytest.mark.parametrize(("bits", "expected"), [
	(0, "0x00"), (8, "0x01"), (96, "0x0C"), (1, "0x00:1"), (19, "0x02:3"),
])
def test_offsets_never_truncate_the_bit_part(bits: int, expected: str) -> None:
	assert render_offset(bits) == expected


@pytest.mark.parametrize(("bits", "expected"), [
	(8, "1"), (32, "4"), (3, "3bit"), (13, "13bit"),
])
def test_size_rendering(bits: int, expected: str) -> None:
	assert render_size(bits) == expected


# -- map format -------------------------------------------------------------


def test_map_is_stable_across_runs() -> None:
	body = "struct S { u8 a; u16 b; }"
	assert rendered_map(body) == rendered_map(body)


def test_map_orders_structs_by_name() -> None:
	text = rendered_map("struct Zed { u8 a; } struct Alpha { u8 b; }")
	assert text.index("struct Alpha") < text.index("struct Zed")


def test_map_records_only_the_file_name() -> None:
	"""A committed map must be byte-identical however it was generated."""
	schema, resolved = analysed("struct S { u8 a; }")
	text = capmap.render(schema, resolved, "path/to/x.situ")	# type: ignore[arg-type]
	assert "# schema: x.situ" in text
	assert "path/to" not in text


def test_core_axes_appear_in_a_fixed_order() -> None:
	line = next(l for l in rendered_map("struct S { u8 a; }").splitlines() if "S.a" in l)
	assert line.split()[1:] == [
		"offset=AbsoluteStatic(0x00)", "size=Fixed(1)", "align=Aligned(8)",
		"repr=MemoryIdentical", "atomic=AtomicWord",
	]


def test_non_core_axes_appear_only_when_weakened() -> None:
	assert "canonical=" not in rendered_map("struct S { u8 a; }")
	assert "canonical=NonCanonical" in rendered_map(
		"struct S { reserved u8 [unknown]; }")


def test_reserved_entries_get_a_distinct_path() -> None:
	assert "S.<reserved0>" in rendered_map("struct S { u3 a; reserved u5; }")


def test_multiple_reserved_regions_are_numbered() -> None:
	text = rendered_map("struct S { reserved u8; reserved u8; }")
	assert "S.<reserved0>" in text and "S.<reserved1>" in text


def test_struct_line_drops_offset_and_alignment() -> None:
	"""A type is not placed anywhere, so those axes describe a field of the
	type rather than the type itself."""
	line = next(l for l in rendered_map("struct S { u16 a; }").splitlines()
	            if l.startswith("struct S"))
	assert "offset=" not in line
	assert "align=" not in line
	assert "size=2" in line
	assert "repr=ValueConverted" in line


def test_summary_lists_weakened_axes() -> None:
	_, resolved = analysed("struct S { u16 a; }")
	text = capmap.summary(resolved)
	assert "S: 2 bytes, 1 entries" in text
	assert "weakened: repr" in text


# -- arithmetic requirements ------------------------------------------------


def test_size_requirement_passes() -> None:
	assert [o.satisfied for o in discharge(
		"struct S { u8 a; u16 b; }\nrequire size(S) == 3;")] == [True]


def test_failed_size_requirement_is_an_error() -> None:
	report = failure("struct S { u8 a; }\nrequire size(S) == 4;")
	assert "requirement not satisfied" in report
	assert "size(S) is 1, == 4 required" in report


def test_failed_assert_is_only_a_warning() -> None:
	warnings = requirements.warnings(
		discharge("struct S { u8 a; }\nassert size(S) == 4;"))
	assert len(warnings) == 1
	assert warnings[0].severity.value == "warning"


def test_offset_requirement_discharges_now() -> None:
	outcome = discharge("struct S { u8 a; u16 b; }\nrequire offset(S.b) == 1;")[0]
	assert outcome.satisfied and outcome.deferred is None


# -- capability predicates --------------------------------------------------


def test_absolute_static_passes_in_the_static_subset() -> None:
	assert discharge("struct S { u8 a; }\nrequire absolute_static(S.a);")[0].satisfied


def test_canonical_passes_by_default() -> None:
	assert discharge("struct S { u8 a; }\nrequire canonical(S);")[0].satisfied


def test_canonical_fails_and_names_its_cause() -> None:
	report = failure("struct S { reserved u8 [unknown]; }\nrequire canonical(S);")
	assert "canonical(S) is NonCanonical, required Canonical" in report
	assert "caused by: `reserved [unknown]`" in report
	assert "malleability surface" in report
	assert "remedy:" in report


def test_atomic_failure_blames_the_alignment() -> None:
	report = failure("struct S { u8 pad; u32 c; }\nrequire atomic(S.c);")
	assert "atomic(S.c) is NonAtomic, required AtomicWord" in report
	assert "caused by: a multi-byte scalar at an offset below its natural alignment" in report
	assert "remedy: reorder the preceding fields" in report


def test_atomic_failure_on_a_bit_field_blames_the_packing() -> None:
	report = failure("struct S { bit a; u7 b; }\nrequire atomic(S.a);")
	assert "caused by: a bit-packed field" in report
	assert "read-modify-write of the containing byte" in report


def test_aligned_takes_a_second_argument() -> None:
	assert discharge("struct S { u32 a; }\nrequire aligned(S.a, 4);")[0].satisfied


def test_aligned_fails_below_the_requested_boundary() -> None:
	report = failure("struct S { u8 pad; u32 a; }\nrequire aligned(S.a, 4);")
	assert "align(S.a) is Aligned(1), required Aligned(4)" in report


def test_aligned_needs_a_literal() -> None:
	with pytest.raises(SituError, match="needs a literal second argument"):
		discharge("const N = 4;\nstruct S { u32 a; }\nrequire aligned(S.a, N);")


def test_predicate_on_an_unknown_path_is_rejected() -> None:
	with pytest.raises(SituError) as caught:
		discharge("struct S { u8 a; }\nrequire canonical(S.nope);")
	assert "unknown path `S.nope`" in caught.value.diagnostic.render()


def test_predicate_over_a_struct_uses_the_struct_vector() -> None:
	outcome = discharge(
		"enum E : u8 { a = 1, default = pass, }"
		"struct S { E kind; }\nassert canonical(S);")[0]
	assert not outcome.satisfied


# -- blame chains -----------------------------------------------------------


def test_every_capability_failure_carries_a_blame_chain() -> None:
	"""Invariant 3 of section 26."""
	cases = [
		"struct S { reserved u8 [unknown]; }\nrequire canonical(S);",
		"struct S { u8 pad; u32 c; }\nrequire atomic(S.c);",
		"struct S { bit a; u7 b; }\nrequire atomic(S.a);",
		"enum E : u8 { a = 1, default = pass, }"
		"struct S { E k; }\nrequire canonical(S.k);",
	]
	for body in cases:
		assert "caused by:" in failure(body), body


def test_blame_points_at_the_offending_source_line() -> None:
	report = failure("struct S {\n\tu8  pad;\n\tu32 c;\n}\nrequire atomic(S.c);")
	assert "unaligned-multi-byte-scalar applies here" in report
	assert ":5:" in report


def test_blast_radius_is_reported() -> None:
	"""Section 17 asks how far a weakening spread, not just that it happened."""
	report = failure("struct S { u8 pad; u32 a; u32 b; u32 c; }\nrequire atomic(S.a);")
	assert "other field(s) share this weakness" in report
	assert "S.b" in report


def test_no_blast_radius_when_the_failure_is_alone() -> None:
	report = failure("struct S { u8 pad; u32 a; }\nrequire atomic(S.a);")
	assert "share this weakness" not in report


def test_failure_with_no_upstream_cause_says_so() -> None:
	report = failure("struct S { u8 a; }\nrequire immutable(S.a);")
	assert "caused by: the declaration itself" in report


# -- deferral ---------------------------------------------------------------


def test_later_phase_predicates_are_deferred_not_passed() -> None:
	outcome = discharge("struct S { u8 a; }\nrequire verify_gated(S);")[0]
	assert outcome.deferred == 8
	assert not outcome.is_error


def test_deferrals_are_grouped_by_phase() -> None:
	outcomes = discharge(
		"struct S { u8 a; }\n"
		"require no_alloc(S);\n"
		"require verify_gated(S);\n")
	report = "\n".join(note.render() for note in requirements.deferrals(outcomes))
	assert "needs phase 4" in report
	assert "needs phase 8" in report


def test_a_requirement_reports_the_latest_phase_it_needs() -> None:
	outcome = discharge("struct S { u8 a; }\nrequire no_alloc(S) && verify_gated(S);")[0]
	assert outcome.deferred == 8


# -- JSON diagnostics -------------------------------------------------------


def test_diagnostic_to_dict_shape() -> None:
	with pytest.raises(SituError) as caught:
		discharge("struct S {\n\tu8  pad;\n\tu32 c;\n}\nrequire atomic(S.c);")

	payload = caught.value.diagnostic.to_dict()
	assert set(payload) == {"severity", "message", "primary", "labels", "notes"}
	assert payload["severity"] == "error"
	assert payload["message"] == "requirement not satisfied"

	primary = payload["primary"]
	assert isinstance(primary, dict)
	assert set(primary) == {
		"file", "line", "column", "end_line", "end_column", "text", "message"}


def test_diagnostic_json_is_serialisable() -> None:
	with pytest.raises(SituError) as caught:
		discharge("struct S { u8 a; }\nrequire size(S) == 4;")

	text = json.dumps({"diagnostics": [caught.value.diagnostic.to_dict()]})
	assert json.loads(text)["diagnostics"][0]["severity"] == "error"


def test_labels_are_included_in_json() -> None:
	with pytest.raises(SituError) as caught:
		discharge("struct S {\n\tu8  pad;\n\tu32 c;\n}\nrequire atomic(S.c);")

	labels = caught.value.diagnostic.to_dict()["labels"]
	assert isinstance(labels, list)
	assert len(labels) == 1
	assert labels[0]["message"] == "unaligned-multi-byte-scalar applies here"


# -- example 5.1 ------------------------------------------------------------


def example() -> tuple[object, ResolvedSchema]:
	path   = SCHEMAS / "header.situ"
	source = Source(str(path), path.read_text(encoding="ascii"))
	schema = parse(source)
	return schema, resolve(schema, solve(schema))


def test_example_5_1_map_is_exact() -> None:
	schema, resolved = example()
	text = capmap.render(schema, resolved, "header.situ")	# type: ignore[arg-type]

	assert "struct Header size=9" in text

	# Hand-computed: 1 + 1 + 1 + 2 = 5, so seq lands at 0x05 and the header is
	# 9 bytes. Reaching project.md's 0x06 and 10 would need a padding byte, and
	# section 8.4 inserts none.
	seq = next(line for line in text.splitlines() if "Header.seq" in line)
	assert "offset=AbsoluteStatic(0x05)" in seq
	assert "size=Fixed(4)" in seq
	# A u32 at an odd offset is neither naturally aligned nor atomic.
	assert "align=Aligned(1)" in seq
	assert "atomic=NonAtomic" in seq


def test_example_5_1_requirements_discharge() -> None:
	schema, resolved = example()
	outcomes = requirements.discharge(schema, resolved)	# type: ignore[arg-type]
	assert all(o.satisfied for o in outcomes), [(o.detail, o.satisfied) for o in outcomes]


def test_example_5_1_axes() -> None:
	_, resolved = example()
	seq = resolved.find("Header.seq")
	assert seq is not None
	assert seq.vector.get(Axis.OFFSET).params == ("0x05",)
	assert seq.vector.get(Axis.REPR).base == "ValueConverted"
	assert seq.vector.get(Axis.ATOMIC).base == "NonAtomic"


# -- dynamic layout (phase 5) -----------------------------------------------


def test_frame_static_passes_for_an_array_element() -> None:
	body = ("struct R { u32 id; u16 v; }"
	        "struct S { u8 n; u8 pad[n]; u8 m; R rs[m]; }\n"
	        "require frame_static(S.rs[].v);")
	assert discharge(body)[0].satisfied


def test_absolute_static_fails_and_blames_the_dynamic_member() -> None:
	"""Section 17's worked example: name the root cause, not the victim."""
	report = failure(
		"struct S {\n\tu16 n [max = 1500];\n\tu8 opts[n];\n\tu32 z;\n}\n"
		"require absolute_static(S.z);")

	assert "offset(S.z) is Dynamic, required AbsoluteStatic" in report
	assert "`opts` has size Bounded(0, 1500)" in report
	assert "remedy: move the variable-length member after this one" in report
	# The blame points at the line `opts` is declared on, not at `z`. The
	# preamble is two lines, so `opts` is line 5.
	assert "dynamic-predecessor applies here" in report
	assert ":5:" in report


def test_max_size_passes_within_the_bound() -> None:
	body = ("struct S { u16 n [max = 100]; u8 v[n]; }\n"
	        "require max_size(S.v, 100);")
	assert discharge(body)[0].satisfied


def test_max_size_fails_above_the_bound() -> None:
	report = failure("struct S { u16 n [max = 100]; u8 v[n]; }\n"
	                 "require max_size(S.v, 50);")
	assert "size(S.v) is Bounded(0, 100), required Bounded(50)" in report


def test_max_size_fails_for_an_unbounded_region() -> None:
	"""A region with no upper bound cannot be statically allocated at any N."""
	report = failure("struct S { u8 a; u8 rest[remaining]; }\n"
	                 "require max_size(S.rest, 4096);")
	assert "Unbounded" in report
	assert "caused by:" in report


def test_bounded_size_weakens_mutation_to_shifting() -> None:
	body = ("struct S { u16 n [max = 100]; u8 v[n]; u32 z; }\n"
	        "assert in_place(S.v);")
	outcome = discharge(body)[0]
	assert not outcome.satisfied
	assert "Shifting" in outcome.detail


def test_a_pinned_count_keeps_everything_static() -> None:
	"""An interval that is a single point is a constant, however written."""
	body = ("struct S { u8 n [must_eq = 4]; u8 v[n]; u32 z; }\n"
	        "require absolute_static(S.z);\n"
	        "require in_place(S.v);")
	assert all(outcome.satisfied for outcome in discharge(body))


def test_map_shows_dynamic_and_frame_offsets() -> None:
	text = rendered_map("struct R { u32 id; }"
	                    "struct S { u8 n; u8 pad[n]; u8 m; R rs[m]; }")
	assert "offset=Dynamic" in text
	assert "offset=FrameStatic(0x00)" in text
	assert "size=Unbounded" not in text
