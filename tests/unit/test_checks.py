"""`situc gen-checks`: tests that the generated tests are worth running.

The suite this emits holds the generated accessors to what the capability map
claims. That only means something if the checks would actually fail when the
claim is false, so the important test in this file is
`test_a_wrong_offset_is_caught`: it corrupts an accessor and requires the
generated check to notice.

Everything here derives from the schema alone. A user runs one command and gets
a suite with no vectors to write and nothing to keep in step by hand, which is
what makes it get run.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from situc.codegen.c import checks, generate
from situc.layout import solve
from situc.parser import parse_text
from situc.resolve import resolve

ROOT    = Path(__file__).resolve().parents[2]
RUNTIME = ROOT / "runtime" / "c"
HOST_CC = shutil.which("gcc") or shutil.which("cc")

WARNINGS = ["-std=c11", "-O1", "-Wall", "-Wextra", "-Werror",
	"-Wconversion", "-Wsign-conversion"]

PREAMBLE = "endian big;\nbit_order msb_first;\n"

SIMPLE = """struct s {
	u8  version [must_eq = 1];
	u16 length;
	u32 seq;
}
"""


def emit(body: str, preamble: str = PREAMBLE) -> str:
	schema   = parse_text(preamble + body)
	resolved = resolve(schema, solve(schema))
	return checks.generate(schema, resolved, "unit")


def build(tmp_path: Path, body: str, preamble: str = PREAMBLE,
		corrupt: tuple[str, str] | None = None) -> subprocess.CompletedProcess[str]:
	"""Generate accessors and checks, optionally breaking one, and run them."""
	schema    = parse_text(preamble + body)
	resolved  = resolve(schema, solve(schema))
	generated = generate(schema, resolved, "unit")

	header = generated.header
	source = generated.source

	if corrupt is not None:
		before, after = corrupt
		if before in header:
			header = header.replace(before, after, 1)
		elif before in source:
			source = source.replace(before, after, 1)
		else:
			raise AssertionError(f"nothing to corrupt: {before}")

	(tmp_path / "unit.h").write_text(header, encoding="ascii")
	(tmp_path / "unit.c").write_text(source, encoding="ascii")
	(tmp_path / "unit_checks.c").write_text(
		checks.generate(schema, resolved, "unit"), encoding="ascii")

	subprocess.run(
		[HOST_CC or "cc", *WARNINGS, f"-I{RUNTIME}", f"-I{tmp_path}",
		 str(tmp_path / "unit.c"), str(tmp_path / "unit_checks.c"),
		 str(ROOT / "build" / "host" / "runtime" / "libsitu.a"),
		 "-lcmocka", "-o", str(tmp_path / "run")],
		check=True, capture_output=True, text=True, cwd=tmp_path)

	return subprocess.run([str(tmp_path / "run")], capture_output=True, text=True)


# -- what it emits ----------------------------------------------------------


def test_a_field_gets_a_check_of_the_bytes_it_claims() -> None:
	emitted = emit(SIMPLE)
	assert "check_s_length_occupies_its_claimed_bytes" in emitted
	assert "The map claims bytes 1..2" in emitted


def test_a_short_buffer_check_is_emitted() -> None:
	assert "check_s_refuses_a_short_buffer" in emit(SIMPLE)
	assert "SITU_ERR_BOUNDS" in emit(SIMPLE)


def test_a_constraint_gets_a_check_that_breaks_it() -> None:
	emitted = emit(SIMPLE)
	assert "check_s_version_must_eq_is_enforced" in emitted
	assert "The schema demands 1" in emitted
	assert "SITU_ERR_CONSTRAINT" in emitted


def test_boundary_values_round_trip_and_touch_nothing_else() -> None:
	emitted = emit(SIMPLE)
	assert "check_s_seq_round_trips_in_place" in emitted
	assert "(uint32_t)0xFFFFFFFFu" in emitted
	assert "InPlaceFixed: this field's bytes, and nothing else" in emitted


def test_a_constrained_field_is_not_round_tripped_over_its_whole_range() -> None:
	"""`must_eq = 1` means 0 and 255 are not values it can take."""
	assert "check_s_version_round_trips_in_place" not in emit(SIMPLE)


def test_no_comparison_is_vacuous() -> None:
	"""`i >= 0u` is always true, and -Wtype-limits says so.

	Generated code a user compiles with the project's own flags has to build,
	so the range test is written differently when the range starts at zero.
	"""
	assert "i >= 0u" not in emit(SIMPLE)


def test_what_is_skipped_is_said_out_loud() -> None:
	emitted = emit("varint_type v { encoding = leb128; max_bits = 32; minimal; }\n"
		"struct s { u16 a; v n; }\n")
	assert "Not checked here, and why:" in emitted
	assert "a varint has no fixed extent to check" in emitted


def test_a_schema_with_nothing_checkable_still_emits_a_suite() -> None:
	"""cmocka refuses an empty group, so an empty one has to be deliberate."""
	emitted = emit("struct s { tlv opts (tag_type = u8); }")
	assert "test_nothing_to_check" in emitted
	assert "purpose rather than by accident" in emitted


def test_crypto_behaviour_is_checked() -> None:
	emitted = emit("""codec aead {
		length_preserving; seekable = linear; granularity = byte;
		authenticated; invertible; deterministic;
	}
	struct s {
		authenticated { u32 seq; }
		sealed(aead) { u32 inner; }
		tag u8[16];
	}
	""")
	assert "check_s_covered_write_marks_tag" in emitted
	assert "situ_msg_transmittable" in emitted
	assert "check_s_sealed_refuses_before_verification" in emitted


def test_registers_are_checked_against_memory() -> None:
	"""A user cannot issue a bus transaction in CI; the arithmetic is testable."""
	emitted = emit("""register ctrl @ 0x00 {
		width = 32; access_width = 32; no_rmw;
		bit  enable [rw];
		bit  start  [wo, on_write = trigger];
		bit  error  [w1c];
		reserved u29;
	}
	""", preamble="target mmio;\nendian little;\nbit_order lsb_first;\n")

	assert "check_ctrl_enable_composes_and_decodes" in emitted
	assert "check_ctrl_error_clear_writes_the_bit" in emitted
	assert "check_ctrl_start_trigger_writes_only_itself" in emitted


# -- and whether they would catch anything ----------------------------------


@pytest.mark.skipif(HOST_CC is None, reason="no host compiler")
def test_the_generated_checks_pass_on_correct_output(tmp_path: Path) -> None:
	result = build(tmp_path, SIMPLE)
	assert result.returncode == 0, result.stdout


@pytest.mark.skipif(HOST_CC is None, reason="no host compiler")
def test_a_wrong_offset_is_caught(tmp_path: Path) -> None:
	"""The test that makes the rest of this file worth having.

	A conformance check that cannot fail proves nothing, so an accessor is
	corrupted -- `seq` is moved one byte from where the map says it is -- and
	the generated check has to notice. It works from the outside, poking bytes
	and watching which ones the getter reacts to, so it catches this without
	knowing anything about how the accessor is written.
	"""
	result = build(tmp_path, SIMPLE,
		corrupt=("situ_get_be32(view.base + 3u)",
		         "situ_get_be32(view.base + 4u)"))

	assert result.returncode != 0, "a field moved off its claimed offset and no check noticed"
	assert "check_s_seq_occupies_its_claimed_bytes" in result.stdout


@pytest.mark.skipif(HOST_CC is None, reason="no host compiler")
def test_a_setter_that_touches_a_neighbour_is_caught(tmp_path: Path) -> None:
	"""`mutate = InPlaceFixed` is a promise that a write moves nothing else.

	Nothing checked it before, and it is the promise the whole language exists
	to make.
	"""
	result = build(tmp_path, SIMPLE,
		corrupt=("situ_put_be32(view.base + 3u, (uint32_t)value);",
		         "situ_put_be32(view.base + 3u, (uint32_t)value);"
		         " (view.base)[0] = 0;"))

	assert result.returncode != 0, "a setter scribbled outside its field unnoticed"
	assert "round_trips_in_place" in result.stdout


@pytest.mark.skipif(HOST_CC is None, reason="no host compiler")
def test_a_constraint_that_stops_being_enforced_is_caught(tmp_path: Path) -> None:
	# Loosened rather than deleted: a validator that still reads the field but
	# accepts what the schema forbids is the shape this actually goes wrong in.
	result = build(tmp_path, SIMPLE,
		corrupt=("if (situ_s_version_get(view) != 1) {",
		         "if (situ_s_version_get(view) > 200) {"))

	assert result.returncode != 0, "a dropped constraint went unnoticed"
	assert "must_eq_is_enforced" in result.stdout


@pytest.mark.skipif(HOST_CC is None, reason="no host compiler")
def test_a_nested_struct_constraint_is_enforced(tmp_path: Path) -> None:
	"""The bug gen-checks found on its first run.

	An enclosing struct's validator ignored the constraints of a struct-typed
	member entirely, so a message whose header was wrong parsed clean.
	"""
	result = build(tmp_path, """struct inner { u8 version [must_eq = 1]; }
	struct outer { inner hdr; u16 rest; }
	""")
	assert result.returncode == 0, result.stdout

	emitted = emit("""struct inner { u8 version [must_eq = 1]; }
	struct outer { inner hdr; u16 rest; }
	""")
	assert "check_outer_hdr_version_must_eq_is_enforced" in emitted


def test_an_array_of_structs_says_it_is_not_walked() -> None:
	"""Validating every element on every parse is a cost worth stating."""
	schema   = parse_text(PREAMBLE + "struct r { u8 v [must_eq = 1]; }\n"
	                                 "struct s { u8 n; r recs[4]; }\n")
	resolved = resolve(schema, solve(schema))
	source   = generate(schema, resolved, "unit").source

	assert "is an array of `r`" in source
	assert "not validated here" in source
	assert re.search(r"situ_r_validate\(\) on each", source)


@pytest.mark.skipif(HOST_CC is None, reason="no host compiler")
def test_a_swapped_byte_order_is_caught(tmp_path: Path) -> None:
	"""The hole mutation testing found, and the reason `_encoding_check` exists.

	Every other generated check is symmetric. `occupies_its_claimed_bytes` asks
	which bytes a field reaches, and a byte-swapped accessor reaches the same
	ones. `round_trips_in_place` asks whether the setter and the getter agree
	with each other, and a swap in both keeps them agreeing. A generator
	emitting little-endian loads for big-endian fields passed the entire
	generated suite, which is a poor showing for a language whose whole subject
	is byte order.
	"""
	result = build(tmp_path, SIMPLE,
		corrupt=("situ_get_be32(view.base + 3u)",
		         "situ_get_le32(view.base + 3u)"))

	assert result.returncode != 0, "a byte-swapped getter went unnoticed"
	assert "decodes_a_known_encoding" in result.stdout


@pytest.mark.skipif(HOST_CC is None, reason="no host compiler")
def test_a_nested_struct_placed_wrong_is_caught(tmp_path: Path) -> None:
	"""A nested struct's interior is checked against its own base, not its parent's.

	So every field inside it can agree with every other and the whole thing
	still sit at the wrong offset. Only the parent's placement falsifies that.
	"""
	result = build(tmp_path, """struct inner { u16 a; u16 b; }
	struct outer { u8 tag; inner nested; }
	""", corrupt=("situ_view_sub(view, 1u", "situ_view_sub(view, 2u"))

	assert result.returncode != 0, "a nested struct moved and no check noticed"
	assert "starts_where_the_map_says" in result.stdout


def test_an_element_past_the_first_is_checked() -> None:
	"""A drifting stride leaves element zero right and the rest quietly wrong."""
	source = emit("struct s { u8 n; u16 xs[4]; }\n")

	assert "check_s_xs_element_lands_on_its_stride" in source
	# The last element, because it is the furthest a wrong stride moves it.
	assert "situ_s_xs_get(view, 3u)" in source


def test_an_index_past_the_end_is_refused() -> None:
	"""`situ_view_sub` bounds against the view, which is the weaker claim.

	An array that stops before its struct does has bytes after it that are
	inside the view and are not elements, so the count has to be checked too.
	"""
	schema   = parse_text(PREAMBLE + "struct r { u8 v; }\n"
	                                 "struct s { r recs[4]; u32 trailer; }\n")
	resolved = resolve(schema, solve(schema))
	source   = generate(schema, resolved, "unit").header

	assert "if (index >= SITU_S_RECS_COUNT) {" in source
	assert "return SITU_ERR_BOUNDS;" in source


def test_an_unbounded_struct_gets_one_concrete_instance() -> None:
	"""No buffer fits every instance, but nothing forces the general case.

	The offset functions are the whole reason such a struct is unbounded, and
	before this they were checked by nothing: `gen-checks` sized no buffer, so
	it emitted no checks at all for the struct that needed them most.
	"""
	source = emit("""struct h { u8 v; u16 n; }
	struct r { u32 a; u16 b; }
	struct s { h hdr; u8 opts[hdr.n]; r recs[hdr.n]; u8 rest[remaining]; }
	""")

	assert "check_s_places_its_members_in_one_instance" in source
	# hdr 0..3, opts 3..5 (two of them), recs 5..17 (two six-byte records).
	assert "situ_s_hdr_view(view, &inner_hdr)" in source
	assert "assert_int_equal((uint32_t)(situ_s_opts_ptr(view) - view.base), 3u);" in source
	assert "assert_int_equal((uint32_t)(first_recs.base - view.base), 5u);" in source
	assert "assert_int_equal((uint32_t)(situ_s_rest_ptr(view) - view.base), 17u);" in source


def test_an_instance_takes_the_view_again_after_sizing_it() -> None:
	"""A shifting setter bumps the generation, so the first view goes stale.

	A check that kept using it would fail in a SITU_CHECKED build for a reason
	that has nothing to do with what it is testing.
	"""
	source = emit("""struct h { u8 v; u16 n; }
	struct s { h hdr; u8 opts[hdr.n]; u8 rest[remaining]; }
	""")

	body   = source[source.index("check_s_places_its_members_in_one_instance"):]
	setter = body.index("situ_s_hdr_n_set(&msg, view, 2);")
	assert "situ_s_view(&msg, 0, " in body[setter:], \
		"the view must be taken again after a shifting setter"


def test_a_struct_with_nothing_to_place_says_so() -> None:
	"""An unbounded member with no offset accessor leaves nothing to assert."""
	source = emit("struct s { u8 head; u8 rest[remaining]; }\n",
	              preamble=PREAMBLE)

	# `rest` does expose a pointer, so this one is placed rather than skipped.
	assert "check_s_places_its_members_in_one_instance" in source


@pytest.mark.skipif(HOST_CC is None, reason="no host compiler")
def test_a_covered_span_that_collapsed_is_caught(tmp_path: Path) -> None:
	"""The span is bounded rather than recomputed, which is enough to falsify it.

	A region's extent is its interior through a codec's expansion, so checking
	it exactly would mean a second solver. Checking that every member the map
	calls covered is inside the span needs no solver at all.
	"""
	schema = """struct h { u8 version; u16 length; }
	struct s {
		u8   hop;
		authenticated {
			h    hdr;
			u8   nonce[12]  [nonce];
		}
		tag  u8[16];
	}
	"""
	result = build(tmp_path, schema, corrupt=("*len    = ", "*len    = 0 * "))

	assert result.returncode != 0, "a collapsed coverage span went unnoticed"
	assert "covers_what_it_claims" in result.stdout


@pytest.mark.skipif(HOST_CC is None, reason="no host compiler")
def test_a_reserved_field_that_stops_being_enforced_is_caught(tmp_path: Path) -> None:
	"""Section 8.8: reserved bits are malleability control, not pedantry. A
	receiver that ignores them lets a sender vary bytes the format calls fixed,
	which is a bypass primitive in anything authenticated -- and nothing
	generated checked them at all."""
	# Loosened rather than deleted, and in a way that still compiles cleanly
	# under the project's own flags: the validator reads the field and accepts
	# what the schema forbids, which is the shape this goes wrong in.
	result = build(tmp_path, "struct s { u8 v; reserved u8 [must_be_zero]; }",
		corrupt=("(uint8_t)(view.base)[1u] != 0",
		         "(uint8_t)(view.base)[1u] != 0 && view.limit == 0u"))

	assert result.returncode != 0, "a dropped reserved check went unnoticed"
	assert "must_be_zero_is_enforced" in result.stdout


def test_the_baseline_satisfies_every_constraint_first() -> None:
	"""Without it the check is vacuous for `must_be_one`: a zeroed buffer
	already breaks that policy, so asserting a wrong value is refused would pass
	against a validator that did nothing. Establishing that the right value is
	accepted is what gives the refusal meaning."""
	source = emit("""struct s {
		u8        version  [must_eq = 4];
		u4        ihl      [min = 5];
		reserved  u4       [must_be_one];
	}
	""")

	body = source[source.index("check_s_reserved0_must_be_one_is_enforced"):]
	assert "SITU_OK" in body[:body.index("SITU_ERR_CONSTRAINT")], \
		"the baseline must be shown valid before it is broken"


def test_a_reserved_array_is_validated() -> None:
	"""`reserved u8[3]` was skipped entirely, in the one example where it
	matters most: those bytes sit inside an authenticated region."""
	schema   = parse_text(PREAMBLE + "struct s { u8 v; reserved u8[3]; }\n")
	resolved = resolve(schema, solve(schema))
	emitted  = generate(schema, resolved, "unit").source

	assert "reserved u8[3] [must_be_zero]" in emitted
	assert "SITU_ERR_CONSTRAINT" in emitted


def test_the_dirty_mask_is_checked_against_its_obligations() -> None:
	"""It is a constant for callers, like SIZE_FIXED, and nothing generated
	consumes it -- so an off-by-one in it went unnoticed until it was tested as
	the API it is."""
	source = emit("""struct s {
		u8 hop;
		authenticated inner { u8 a; }
		tag u8[16] covers(inner);
		authenticated outer { u8 b; }
		checksum u8[2] covers(outer);
	}
	""")

	assert "check_s_dirty_mask_names_every_obligation" in source
	assert "SITU_S_DIRTY_MASK & SITU_S_TAG_DIRTY" in source
	assert "SITU_S_DIRTY_MASK & SITU_S_CHECKSUM_DIRTY" in source


def test_the_dirty_mask_covers_invariants_too() -> None:
	"""Tags and invariants share the dirty word (section 16.1). A mask over the
	tags alone leaves a struct able to be stale in a way its own mask cannot
	express, which is the kind of gap a caller finds by trusting it."""
	source = emit("""struct s {
		u16 total;
		u8  a;
		authenticated inner { u8 b; }
		tag u8[16] covers(inner);
	}
	invariant s.total == size(s.a);
	""")

	assert "SITU_S_DIRTY_MASK & SITU_S_TAG_DIRTY" in source
	assert "SITU_S_DIRTY_MASK & SITU_S_TOTAL_STALE" in source


def test_each_obligation_gets_its_own_coverage_check() -> None:
	"""Pairing the first covered field with the first tag is right only while a
	struct carries one obligation. With a tag and an invariant side by side it
	wrote through a field the invariant covers and asserted the tag went dirty,
	which passes for the wrong reason or fails for a confusing one."""
	source = emit("""struct s {
		u16 total;
		u8  a;
		authenticated inner { u8 b; }
		tag u8[16] covers(inner);
	}
	invariant s.total == size(s.a);
	""")

	assert "check_s_covered_write_marks_tag" in source
	assert "check_s_covered_write_marks_total" in source
	# The discharge differs: a tag is finalized, a derived field recomputed.
	assert "situ_s_tag_finalize(&msg);" in source
	assert "situ_s_total_recompute(&msg, view);" in source


# -- a run whose length the instance cannot choose --------------------------

WHILE_RUN = """
struct link { u8 kind; u8 len; u8 data[len]; }
struct chain { link items[] while (kind == 0x11) max 6; u8 tail[remaining]; }
"""


def test_a_while_run_stops_the_offset_walk_rather_than_the_check() -> None:
	"""Where the run starts is known; where anything after it starts is not.

	The instance is zeroed, so whether the run ends at the first element is a
	question about the predicate, which this does not evaluate. It used to
	answer "one element" -- via a false `array_count`, and then via
	`size_bits`, which is the minimum and means the same thing -- and place
	the members after it at an offset the run walks straight past. That check
	asserted 8 while the accessor said 16.
	"""
	text = emit(WHILE_RUN)

	assert "chain_items_at(view, 0u, &first_items)" in text
	assert "Assertions stop at `chain.items`" in text
	assert "chain_tail_ptr" not in text


def test_a_run_gets_no_sub_view_check() -> None:
	"""It has `_at`, not `_view`: there is no one instance to take a view of.
	The nested-struct check excluded it for the wrong reason -- a false
	`array_count` -- and called an accessor that was never emitted the moment
	that went."""
	text = emit(WHILE_RUN)

	assert "chain_items_view" not in text
