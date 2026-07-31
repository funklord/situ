"""The C backend (project.md sections 20.1, 20.2, 26.4).

Two kinds of test here. The structural ones assert what the generator emits;
the compile ones actually build it, because generated code that reads well and
does not compile is worth nothing. Both matter, and the second kind is what
catches a -Wconversion regression.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from situc.codegen.c import generate
from situc.diagnostics import Source, SituError
from situc.layout import solve
from situc.parser import parse, parse_text
from situc.resolve import resolve

from every_schema import ROOT, SCHEMAS, ids

RUNTIME  = ROOT / "runtime" / "c"

PREAMBLE = "endian big;\nbit_order msb_first;\n"

WARNINGS = [
	"-std=c11", "-O2", "-Wall", "-Wextra", "-Werror",
	"-Wconversion", "-Wsign-conversion",
]

HOST_CC  = shutil.which("gcc") or shutil.which("cc")
CROSS_CC = shutil.which("aarch64-linux-gnu-gcc")
OBJDUMP  = shutil.which("objdump")


def emit(body: str, preamble: str = PREAMBLE, name: str = "unit") -> tuple[str, str]:
	schema   = parse_text(preamble + body)
	resolved = resolve(schema, solve(schema))
	generated = generate(schema, resolved, name)
	return generated.header, generated.source


def compile_generated(tmp_path: Path, body: str, preamble: str = PREAMBLE,
		compiler: str | None = None, extra: str = "") -> None:
	"""Write the generated pair plus an optional probe and compile them."""
	header, source = emit(body, preamble)
	(tmp_path / "unit.h").write_text(header, encoding="ascii")
	(tmp_path / "unit.c").write_text(source, encoding="ascii")

	sources = [str(tmp_path / "unit.c")]
	if extra:
		(tmp_path / "probe.c").write_text(extra, encoding="ascii")
		sources.append(str(tmp_path / "probe.c"))

	command = [compiler or HOST_CC or "cc", *WARNINGS,
	           f"-I{RUNTIME}", f"-I{tmp_path}", "-c", *sources]
	result = subprocess.run(command, cwd=tmp_path, capture_output=True, text=True)
	assert result.returncode == 0, result.stderr


# -- structure --------------------------------------------------------------


def test_header_has_an_include_guard() -> None:
	header, _ = emit("struct S { u8 a; }")
	assert "#ifndef SITU_UNIT_H" in header
	assert header.rstrip().endswith("#endif /* SITU_UNIT_H */")


def test_size_constants_are_emitted() -> None:
	"""So callers can size static buffers without running the compiler."""
	header, _ = emit("struct S { u8 a; u32 b; }")
	assert "#define SITU_S_SIZE_FIXED 5u" in header
	assert "#define SITU_S_SIZE_MIN   5u" in header
	assert "#define SITU_S_SIZE_MAX   5u" in header


def test_accessors_are_static_inline() -> None:
	"""Section 20.2: field access must compile to base + K."""
	header, _ = emit("struct S { u32 a; }")
	assert "static inline uint32_t situ_S_a_get(situ_view_t view)" in header
	assert "static inline void situ_S_a_set(situ_view_t view, uint32_t value)" in header


def test_big_endian_field_goes_through_the_swap_helper() -> None:
	header, _ = emit("struct S { u32 a; }")
	assert "situ_get_be32(view.base + 0u)" in header
	assert "situ_put_be32(view.base + 0u" in header


def test_little_endian_field_uses_the_other_helper() -> None:
	header, _ = emit("struct S { u32 a; }", preamble="endian little;\n")
	assert "situ_get_le32(view.base + 0u)" in header


def test_converted_field_gets_no_pointer_accessor() -> None:
	"""A pointer into a byte-swapped field is a bug waiting to happen, so none
	is offered (section 20.2)."""
	header, _ = emit("struct S { u32 a; }")
	assert "situ_S_a_get" in header
	assert "situ_S_a_ptr" not in header


def test_memory_identical_field_gets_a_pointer_accessor() -> None:
	header, _ = emit("struct S { u8 a; }")
	assert "situ_S_a_ptr" in header


def test_byte_array_gets_a_pointer_and_a_count() -> None:
	header, _ = emit("struct S { u8 mac[6]; }")
	assert "#define SITU_S_MAC_COUNT 6u" in header
	assert "static inline uint8_t *situ_S_mac_ptr(situ_view_t view)" in header


def test_converted_array_gets_an_indexed_getter_not_a_pointer() -> None:
	header, _ = emit("struct S { u32 xs[4]; }")
	assert "situ_S_xs_get(situ_view_t view, uint32_t index)" in header
	assert "situ_S_xs_ptr" not in header


def test_bit_field_goes_through_the_bit_helpers() -> None:
	header, _ = emit("struct S { u3 a; u5 b; }")
	assert "situ_bits_get_msb(view.base, 0u, 3u)" in header
	assert "situ_bits_set_msb(view.base, 0u, 3u" in header


def test_bit_order_selects_the_helper() -> None:
	header, _ = emit("struct S { u3 a; u5 b; }",
	                 preamble="endian big;\nbit_order lsb_first;\n")
	assert "situ_bits_get_lsb" in header


def test_enum_field_exposes_its_typedef() -> None:
	"""The backing type is mandatory so the layout is fixed, not so that
	callers have to remember which width it was."""
	header, _ = emit("enum E : u8 { a = 1, } struct S { E kind; }")
	assert "typedef enum situ_E {" in header
	assert "SITU_E_A = 1," in header
	assert "static inline situ_E_t situ_S_kind_get(situ_view_t view)" in header


def test_nested_struct_gets_a_sub_view() -> None:
	header, _ = emit("struct Inner { u16 x; } struct S { u8 a; Inner i; }")
	assert "situ_S_i_view(situ_view_t view, situ_view_t *out)" in header
	assert "situ_view_sub(view, 1u, SITU_INNER_SIZE_FIXED, out)" in header


def test_nested_members_are_not_duplicated_in_the_parent() -> None:
	header, _ = emit("struct Inner { u16 x; } struct S { Inner i; }")
	assert header.count("situ_Inner_x_get") == 1
	assert "situ_S_i_x_get" not in header


def test_reserved_regions_get_no_accessor() -> None:
	header, _ = emit("struct S { u3 a; reserved u5; }")
	assert "reserved, no accessor" in header
	assert "reserved0_get" not in header


def test_reserved_note_appears_once_for_a_nested_region() -> None:
	header, _ = emit("struct Inner { u3 a; reserved u5; } struct S { Inner i; }")
	assert header.count("reserved, no accessor") == 1


def test_field_comment_carries_the_capability_vector() -> None:
	"""The header is where a user looks; the map should not be the only copy."""
	header, _ = emit("struct S { u8 pad; u32 a; }")
	assert "repr=ValueConverted" in header
	assert "atomic=NonAtomic" in header


# -- validation -------------------------------------------------------------


def test_must_eq_becomes_a_check() -> None:
	_, source = emit("struct S { u8 v [must_eq = 1]; }")
	assert "if (situ_S_v_get(view) != 1) {" in source
	assert "return SITU_ERR_CONSTRAINT;" in source


def test_max_and_min_become_checks() -> None:
	_, source = emit("struct S { u16 a [max = 1500]; u16 b [min = 4]; }")
	assert "situ_S_a_get(view) > 1500" in source
	assert "situ_S_b_get(view) < 4" in source


def test_reserved_must_be_zero_is_checked() -> None:
	_, source = emit("struct S { u3 a; reserved u5 [must_be_zero]; }")
	assert "!= 0" in source


def test_reserved_defaults_to_must_be_zero() -> None:
	"""Section 8.8: every ignored bit is a malleability surface."""
	_, source = emit("struct S { u3 a; reserved u5; }")
	assert "must_be_zero" in source


def test_reserved_preserve_is_not_checked() -> None:
	_, source = emit("struct S { u3 a; reserved u5 [preserve]; }")
	assert "SITU_ERR_CONSTRAINT" not in source


def test_a_struct_with_no_constraints_still_validates() -> None:
	_, source = emit("struct S { u8 a; }")
	assert "situ_err_t situ_S_validate(situ_view_t view)" in source
	assert "(void)view;" in source


# -- compilation ------------------------------------------------------------


@pytest.mark.skipif(HOST_CC is None, reason="no host compiler")
@pytest.mark.parametrize("body", [
	"struct S { u8 a; }",
	"struct S { u16 a; u32 b; u64 c; }",
	"struct S { i8 a; i16 b; i32 c; i64 d; }",
	"struct S { u3 a; u5 b; }",
	"struct S [allow_straddle] { u3 a; u13 b; }",
	"struct S { u24 a; u48 b; }",
	"struct S { u8 mac[6]; u32 xs[4]; }",
	"struct S { f32 a; f64 b; }",
	"enum E : u8 { a = 1, } struct S { E kind; }",
	"enum E : u4 { a = 1, } struct S { E kind; u4 rest; }",
	"struct Inner { u16 x; } struct S { u8 a; Inner i; }",
	"struct S { u8 v [must_eq = 1]; u16 n [max = 1500]; }",
	"struct S { u3 a; reserved u5 [must_be_zero]; }",
])
def test_generated_code_compiles_warning_clean(tmp_path: Path, body: str) -> None:
	compile_generated(tmp_path, body)


@pytest.mark.skipif(HOST_CC is None, reason="no host compiler")
def test_little_endian_schema_compiles(tmp_path: Path) -> None:
	compile_generated(tmp_path, "struct S { u32 a; u16 b; }",
	                  preamble="endian little;\n")


@pytest.mark.skipif(HOST_CC is None, reason="no host compiler")
def test_lsb_first_schema_compiles(tmp_path: Path) -> None:
	compile_generated(tmp_path, "struct S { u3 a; u5 b; }",
	                  preamble="endian big;\nbit_order lsb_first;\n")


@pytest.mark.skipif(CROSS_CC is None, reason="no aarch64 cross compiler")
def test_generated_code_compiles_for_aarch64(tmp_path: Path) -> None:
	"""Section 24 requires the generated code to build clean for a Cortex-A55.

	This is the compile-only half of decision 0004: the -Wconversion findings
	that differ between the two targets are compile-time findings, and
	compiling is enough to surface them.
	"""
	compile_generated(tmp_path, "struct S { u16 a; u32 b; u3 c; u5 d; u8 e[6]; }",
	                  compiler=CROSS_CC)


@pytest.mark.skipif(HOST_CC is None, reason="no host compiler")
@pytest.mark.parametrize("path", SCHEMAS, ids=ids(SCHEMAS))
def test_every_schema_generates_and_compiles(path: Path, tmp_path: Path) -> None:
	"""Every schema in the repository, not every example.

	This was the examples alone for four phases, and so were the other three
	backends' versions of it -- which is how `tests/schemas/edges.situ` came to
	not compile in C++ at all. That file exists to carry the constructs the
	worked examples do not have, so it is the *last* one a compile check should
	skip (26.31).
	"""
	if "STATUS: needs phase" in path.read_text(encoding="ascii"):
		pytest.skip("declares itself unbuildable")

	source    = Source(str(path), path.read_text(encoding="ascii"))
	schema    = parse(source)
	resolved  = resolve(schema, solve(schema))
	generated = generate(schema, resolved, path.stem)

	for name, text in generated.files().items():
		(tmp_path / name).write_text(text, encoding="ascii")

	result = subprocess.run(
		[HOST_CC or "cc", *WARNINGS, f"-I{RUNTIME}", f"-I{tmp_path}",
		 "-c", str(tmp_path / f"{path.stem}.c"), "-o", str(tmp_path / "out.o")],
		capture_output=True, text=True)
	assert result.returncode == 0, f"{path.parent.name}:\n{result.stderr}"


# -- constant-offset access -------------------------------------------------


@pytest.mark.skipif(HOST_CC is None or OBJDUMP is None, reason="no toolchain")
def test_field_access_compiles_to_a_constant_offset(tmp_path: Path) -> None:
	"""Section 26.4 asks for this specifically, and section 20.2 says to verify
	it by inspecting the generated code rather than trusting the shape.

	A view is a value, so its base arrives in a register; the field access must
	then be one load at a literal displacement, with no address arithmetic and
	no branch.
	"""
	probe = (
		'#include "unit.h"\n'
		"uint32_t probe(situ_view_t v) { return situ_S_b_get(v); }\n"
	)
	compile_generated(tmp_path, "struct S { u8 a; u32 b; }", extra=probe)

	disassembly = subprocess.run(
		[OBJDUMP or "objdump", "-d", "--no-show-raw-insn", str(tmp_path / "probe.o")],
		capture_output=True, text=True, check=True).stdout

	body = disassembly[disassembly.index("<probe>:"):]
	body = body[: body.index("ret")]

	# One load at the literal offset the solver computed, and nothing that
	# recomputes an address.
	assert "0x1(" in body, f"expected a constant displacement of 1:\n{body}"
	assert "call" not in body
	assert body.count("\n") <= 6, f"more instructions than a load and a swap:\n{body}"


def test_nested_struct_is_emitted_before_its_container() -> None:
	"""A sub-view accessor refers to the nested struct's SIZE_FIXED macro, so
	alphabetical order would emit a forward reference C cannot resolve."""
	header, _ = emit("struct Outer { u8 a; Zebra z; } struct Zebra { u16 x; }")
	assert header.index("SITU_ZEBRA_SIZE_FIXED 2u") < header.index("situ_Outer_z_view")


# -- gen-fuzz ---------------------------------------------------------------


def fuzz_source(body: str, preamble: str = PREAMBLE, name: str = "unit") -> str:
	from situc.codegen.c import fuzz

	schema   = parse_text(preamble + body)
	resolved = resolve(schema, solve(schema))
	return fuzz.generate(schema, resolved, name)


def test_fuzz_harness_has_the_libfuzzer_entry_point() -> None:
	text = fuzz_source("struct S { u32 a; }")
	assert "int LLVMFuzzerTestOneInput(const uint8_t *data, size_t size)" in text


def test_fuzz_harness_covers_every_struct() -> None:
	"""A harness with a hand-maintained list is a harness that goes stale."""
	text = fuzz_source("struct A { u8 x; } struct B { u16 y; }")
	assert "fuzz_A(" in text
	assert "fuzz_B(" in text
	assert "data[0] % 2u" in text


def test_fuzz_harness_reads_every_accessor() -> None:
	text = fuzz_source("struct S { u32 a; u8 b; u3 c; u5 d; }")
	for field in ("a", "b", "c", "d"):
		assert f"situ_S_{field}_get(view)" in text


def test_fuzz_harness_calls_validate() -> None:
	"""Validation is the first thing a parser runs on attacker-controlled bytes."""
	assert "situ_S_validate(view)" in fuzz_source("struct S { u8 a [must_eq = 1]; }")


def test_fuzz_harness_refuses_short_input() -> None:
	text = fuzz_source("struct S { u32 a; }")
	assert "if (size < SITU_S_SIZE_FIXED) {" in text


def test_fuzz_sink_is_declared_before_use() -> None:
	"""C is one-pass; the sink has to precede the harnesses that call it."""
	text = fuzz_source("struct S { u32 a; }")
	assert text.index("static void situ_fuzz_sink") < text.index("situ_fuzz_sink((uint64_t)")


@pytest.mark.skipif(HOST_CC is None, reason="no host compiler")
def test_fuzz_harness_compiles_and_runs(tmp_path: Path) -> None:
	"""Built standalone, so a harness nobody can compile cannot go unnoticed."""
	body = "struct S { u32 a; u8 b[4]; u3 c; u5 d; }"
	header, source = emit(body)
	(tmp_path / "unit.h").write_text(header, encoding="ascii")
	(tmp_path / "unit.c").write_text(source, encoding="ascii")
	(tmp_path / "unit_fuzz.c").write_text(fuzz_source(body), encoding="ascii")

	binary = tmp_path / "fuzz"
	build = subprocess.run(
		[HOST_CC or "cc", *WARNINGS, "-DSITU_FUZZ_STANDALONE",
		 f"-I{RUNTIME}", f"-I{tmp_path}",
		 str(tmp_path / "unit_fuzz.c"), str(tmp_path / "unit.c"),
		 str(RUNTIME / "situ.c"), "-o", str(binary)],
		capture_output=True, text=True)
	assert build.returncode == 0, build.stderr

	for payload in (b"", b"\x00", b"\x00" * 64, bytes(range(64))):
		run = subprocess.run([str(binary)], input=payload, capture_output=True)
		assert run.returncode == 0, run.stderr


# -- gen-tests --------------------------------------------------------------


def vector_source(body: str, vectors: str, preamble: str = PREAMBLE) -> str:
	from situc.codegen.c import vectors as vec

	schema   = parse_text(preamble + body)
	resolved = resolve(schema, solve(schema))
	cases    = vec.parse_vectors(Source("<vectors>", vectors))
	return vec.generate(schema, resolved, cases, "unit")


def test_vectors_parse() -> None:
	from situc.codegen.c import vectors as vec

	cases = vec.parse_vectors(Source("<v>", "S basic 01 02\n\ta = 1\n"))
	assert len(cases) == 1
	assert cases[0].struct == "S"
	assert cases[0].data == b"\x01\x02"
	assert cases[0].expectations == [("a", "1")]


def test_vectors_accept_unspaced_hex() -> None:
	from situc.codegen.c import vectors as vec

	assert vec.parse_vectors(Source("<v>", "S basic 0102\n"))[0].data == b"\x01\x02"


def test_vectors_reject_odd_hex() -> None:
	from situc.codegen.c import vectors as vec

	with pytest.raises(SituError, match="odd number of digits"):
		vec.parse_vectors(Source("<v>", "S basic 010\n"))


def test_vectors_reject_an_orphan_expectation() -> None:
	from situc.codegen.c import vectors as vec

	with pytest.raises(SituError, match="expectation before any vector"):
		vec.parse_vectors(Source("<v>", "\ta = 1\n"))


def test_vectors_reject_a_wrong_length() -> None:
	"""A vector of the wrong size is the commonest way one goes stale."""
	with pytest.raises(ValueError, match="is 1 bytes, but `S` is 4"):
		vector_source("struct S { u32 a; }", "S basic 01\n")


def test_vectors_reject_an_unknown_struct() -> None:
	with pytest.raises(ValueError, match="unknown struct `Nope`"):
		vector_source("struct S { u8 a; }", "Nope basic 01\n")


def test_vectors_reject_an_unknown_field() -> None:
	with pytest.raises(ValueError, match="unknown field `S.nope`"):
		vector_source("struct S { u8 a; }", "S basic 01\n\tnope = 1\n")


def test_generated_vector_test_asserts_the_expectations() -> None:
	text = vector_source("struct S { u32 a; }", "S basic 00 00 00 2A\n\ta = 42\n")
	assert "assert_int_equal(situ_S_a_get(view), 42);" in text
	assert "situ_S_validate(view)" in text


def test_generated_vector_test_round_trips() -> None:
	text = vector_source("struct S { u32 a; }", "S basic 00 00 00 2A\n")
	assert "situ_S_a_set(view, situ_S_a_get(view));" in text
	assert "assert_memory_equal(buf, vector_basic, sizeof(buf));" in text


@pytest.mark.skipif(HOST_CC is None, reason="no host compiler")
def test_generated_vector_test_compiles(tmp_path: Path) -> None:
	body = "struct S { u32 a; u16 b; }"
	header, source = emit(body)
	(tmp_path / "unit.h").write_text(header, encoding="ascii")
	(tmp_path / "unit.c").write_text(source, encoding="ascii")
	(tmp_path / "unit_vectors.c").write_text(
		vector_source(body, "S basic 00 00 00 2A 01 F4\n\ta = 42\n\tb = 500\n"),
		encoding="ascii")

	build = subprocess.run(
		[HOST_CC or "cc", *WARNINGS, f"-I{RUNTIME}", f"-I{tmp_path}",
		 "-c", str(tmp_path / "unit_vectors.c"), "-o", str(tmp_path / "v.o")],
		capture_output=True, text=True)
	assert build.returncode == 0, build.stderr


# -- view invalidation documentation (section 12.3) -------------------------


def test_a_fixed_struct_says_nothing_invalidates_it() -> None:
	header, _ = emit("struct S { u32 a; u16 b; }")
	assert "INVALIDATION: nothing invalidates a S view" in header
	assert "no write can move another" in header


def test_a_frame_names_the_fields_that_invalidate_it() -> None:
	"""Section 12.3: the header documents, per view type, exactly which
	operations invalidate it. The C type system cannot enforce this, so the
	only other record of the rule would be in the compiler's head."""
	header, _ = emit("struct S { u16 n [max = 100]; u8 v[n]; u32 z; }")
	assert "INVALIDATION: a S view" in header
	assert "invalidated by writing `n`" in header
	assert "Re-acquire the view after any such write" in header


def test_a_frame_emits_generation_bumping_setters() -> None:
	header, _ = emit("struct S { u16 n [max = 100]; u8 v[n]; u32 z; }")
	assert ("static inline void situ_S_n_set(situ_msg_t *msg, situ_view_t view, "
	        "uint16_t value)") in header
	assert "situ_msg_touch(msg);" in header


def test_a_frame_view_takes_a_length() -> None:
	"""A frame's extent depends on the data, so the caller supplies what they
	have and the bounds check is made against that."""
	header, _ = emit("struct S { u16 n [max = 100]; u8 v[n]; }")
	assert "situ_S_view(const situ_msg_t *msg, uint32_t offset, uint32_t length," in header
	assert "if (length < SITU_S_SIZE_MIN) {" in header


def test_a_frame_has_no_size_fixed_constant() -> None:
	"""Emitting one would hand a caller a number that is wrong for every
	message but the shortest."""
	header, _ = emit("struct S { u16 n [max = 100]; u8 v[n]; }")
	assert "SITU_S_SIZE_FIXED" not in header
	assert "#define SITU_S_SIZE_MIN   2u" in header
	assert "#define SITU_S_SIZE_MAX   102u" in header


def test_an_unbounded_frame_says_why_it_has_no_maximum() -> None:
	header, _ = emit("struct S { u8 a; u8 rest[remaining]; }")
	assert "No SITU_S_SIZE_MAX" in header
	assert "Give the driving length field a `[max = N]`" in header


# -- dynamic accessors ------------------------------------------------------


def test_dynamic_offset_resolves_from_the_driving_field() -> None:
	header, _ = emit("struct S { u16 n [max = 100]; u8 v[n]; u32 z; }")
	assert "static inline uint32_t situ_S_z_offset(situ_view_t view)" in header
	# The driving field is at a static offset, so reading it is a constant load.
	assert "situ_get_be16(view.base + 0u)" in header


def test_remaining_measures_to_the_end_of_the_view() -> None:
	header, _ = emit("struct S { u8 a; u8 rest[remaining]; }")
	assert "situ_S_rest_len(situ_view_t view)" in header
	# Saturating. It was `view.limit - 1u`, which wraps when the members
	# before it claim more than the view holds -- and a `[remaining]` member
	# then reports about four billion bytes with a pointer past the end.
	assert "situ_remaining_u32(view.limit, 1u)" in header


def test_array_of_structs_gets_an_indexed_element_view() -> None:
	"""Acquiring the element view is the bounds check; the fields inside it are
	then constant offsets from its base (section 12.2)."""
	header, _ = emit("struct R { u32 id; u16 v; } struct S { u8 n; R rs[n]; }")
	assert "situ_S_rs_at(situ_view_t view, uint32_t index, situ_view_t *out)" in header
	assert "const uint32_t stride = SITU_R_SIZE_FIXED;" in header
	assert "situ_view_sub(view, base + index * stride, stride, out)" in header


def test_dynamic_array_gets_a_runtime_count() -> None:
	header, _ = emit("struct R { u32 id; } struct S { u8 n; R rs[n]; }")
	assert "static inline uint32_t situ_S_rs_count(situ_view_t view)" in header
	assert "SITU_S_RS_COUNT" not in header


def test_fixed_array_keeps_a_compile_time_count() -> None:
	header, _ = emit("struct R { u32 id; } struct S { R rs[4]; }")
	assert "#define SITU_S_RS_COUNT 4u" in header
	assert "situ_S_rs_count(situ_view_t view)" not in header


@pytest.mark.skipif(HOST_CC is None, reason="no host compiler")
@pytest.mark.parametrize("body", [
	"struct S { u16 n [max = 100]; u8 v[n]; u32 z; }",
	"struct S { u8 a; u8 rest[remaining]; }",
	"struct R { u32 id; u16 v; } struct S { u8 n; R rs[n]; u8 tail[remaining]; }",
	"struct S { u8 n [must_eq = 4]; u8 v[n]; u32 z; }",
	"struct R { u32 id; } struct S { u8 n; u8 pad[n]; u8 m; R rs[m]; }",
])
def test_dynamic_code_compiles_warning_clean(tmp_path: Path, body: str) -> None:
	compile_generated(tmp_path, body)


# -- phase 6 constructs -----------------------------------------------------


def test_opaque_gets_bytes_and_a_length() -> None:
	"""Treat-as-bytes is the whole of what an opaque region supports."""
	header, _ = emit("struct S { u16 n; opaque payload [n]; }")
	assert "situ_S_payload_len(situ_view_t view)" in header
	assert "situ_S_payload_ptr(situ_view_t view)" in header
	assert "no interior access" in header


def test_opaque_length_is_a_byte_count_not_an_element_count() -> None:
	header, _ = emit("struct S { u16 n; opaque payload [n]; }")
	assert "return (uint32_t)(situ_get_be16(view.base + 0u));" in header


VARINT = "varint_type v { encoding = leb128; max_bits = 64; minimal; }"


def test_a_varint_field_decodes() -> None:
	"""It got no accessor at all, on the argument that emitting one would
	pretend the width is fixed. The width is in the bytes, so the answer was
	two accessors rather than none -- invariant 11 again, an assertion of
	absence with a shelf life."""
	header, _ = emit(VARINT + "struct S { u16 a; v n; }")

	assert "situ_S_n_get(situ_view_t view, uint64_t *out)" in header
	assert "situ_S_n_len(situ_view_t view)" in header


def test_a_member_after_a_varint_is_placed_past_it() -> None:
	"""The bug the accessor was hiding. `_length_expression` had no case for a
	varint, so it fell to the array branch and returned zero: every member
	after one was placed as though it occupied nothing, and read the varint's
	own bytes. Silently, through an accessor that looked like any other."""
	header, _ = emit(VARINT + "struct S { u8 kind; v n; u16 after; }")

	assert "offset = offset + (situ_S_n_len(view));" in header


def test_a_varint_may_size_an_array() -> None:
	"""What a varint is usually for. `u8 payload[n]` was refused outright --
	a varint member never entered the field scope, so the error read "no
	fields are in scope at this point"."""
	header, _ = emit(VARINT + "struct S { v n; u8 payload[n]; }")

	assert "situ_S_payload_size_value" in header or \
	       "situ_S_n_value(view)" in header


def test_a_minimal_varint_refuses_a_padded_encoding() -> None:
	"""`minimal` is a canonicality claim, and one nothing enforced would be a
	comment."""
	header, _ = emit(VARINT + "struct S { v n; }")

	assert "if (used != situ_varint_len(raw)) {" in header
	assert "return SITU_ERR_CONSTRAINT;" in header


def test_a_non_minimal_varint_makes_no_such_check() -> None:
	header, _ = emit("varint_type w { encoding = leb128; max_bits = 64; }"
	                 "struct S { w n; }")

	assert "situ_varint_len" not in header


def test_a_zigzag_varint_decodes_signed() -> None:
	header, _ = emit("varint_type z { encoding = leb128; max_bits = 64;"
	                 " transform = zigzag; }struct S { z n; }")

	assert "situ_S_n_get(situ_view_t view, int64_t *out)" in header
	assert "situ_zigzag_decode(raw)" in header


@pytest.mark.skipif(HOST_CC is None, reason="no host compiler")
def test_a_varint_reads_the_bytes_after_it(tmp_path: Path) -> None:
	"""The whole point, and the regression that matters: `after` used to come
	back as the varint's own bytes."""
	header, source = emit(VARINT + "struct S { u8 kind; v n; u16 after; }")
	(tmp_path / "unit.h").write_text(header, encoding="ascii")
	(tmp_path / "unit.c").write_text(source, encoding="ascii")
	(tmp_path / "probe.c").write_text("""
#include "unit.h"

int main(void)
{
	/* kind = 1, n = 300 (leb128 AC 02), after = 0xBEEF */
	uint8_t buf[] = { 0x01, 0xAC, 0x02, 0xBE, 0xEF };
	situ_msg_t msg;
	situ_view_t view;
	uint64_t n = 0;

	situ_msg_init(&msg, buf, sizeof(buf));
	if (situ_S_view(&msg, 0, sizeof(buf), &view) != SITU_OK) return 1;

	if (situ_S_n_get(view, &n) != SITU_OK || n != 300u) return 2;
	if (situ_S_n_len(view) != 2u) return 3;
	if (situ_S_after_offset(view) != 3u) return 4;
	if (situ_S_after_get(view) != 0xBEEFu) return 5;

	return 0;
}
""", encoding="ascii")

	binary = tmp_path / "probe"
	build = subprocess.run(
		[HOST_CC or "cc", *WARNINGS, f"-I{RUNTIME}", f"-I{tmp_path}",
		 str(tmp_path / "probe.c"), str(tmp_path / "unit.c"),
		 str(RUNTIME / "situ.c"), "-o", str(binary)],
		capture_output=True, text=True)
	assert build.returncode == 0, build.stderr

	assert subprocess.run([str(binary)]).returncode == 0


def test_a_variant_gets_no_single_accessor() -> None:
	"""There is no one thing to hand back -- but its arms' members are
	reachable now, each behind the discriminant that selects it.

	A struct-typed arm gets a sub-view over it, guarded the same way; its
	own members belong to its type and are emitted there.
	"""
	header, _ = emit("enum K : u8 { a = 1, b = 2, }"
	                 "struct A { u16 x; } struct B { u32 y; }"
	                 "struct S { K k; variant v switch (k) "
	                 "{ case K.a: A p; case K.b: B q; } }")
	assert "exactly one of" in header
	assert "S.v.p, present when the discriminant selects `K.a`" in header
	assert "situ_S_v_p_view(situ_view_t view, situ_view_t *out)" in header


INDEXED = ("struct R { u32 id; u16 kind; }"
	"struct S { u16 n; indexed(offset_type = u16, count = n)"
	" { R entries[]; } }")


def test_an_indexed_region_gets_its_table_walked() -> None:
	"""It was the last construct no backend reached into: the header said the
	table was not walked yet and stopped there. Invariant 11 -- this used to
	assert the absence, and the absence had a shelf life."""
	header, _ = emit(INDEXED)

	assert "situ_S_entries_count(situ_view_t view)" in header
	assert "situ_S_entries_offset(situ_view_t view, uint32_t index," in header
	assert "situ_S_entries_at(situ_view_t view, uint32_t index," in header


def test_an_indexed_region_still_says_insertion_is_not_an_operation() -> None:
	"""The one thing the old note said that is still true, and the reason has
	not changed: every offset after the insertion point would have to move."""
	header, _ = emit(INDEXED)

	assert "Insertion is not an operation here" in header


def test_an_index_entry_is_read_in_the_region_s_byte_order() -> None:
	"""The placement recorded no endian, so a backend asking had nothing to ask
	and defaulted -- which reads a big-endian table little end first and hands
	back a plausible offset."""
	header, _ = emit(INDEXED)

	assert "situ_get_be16(view.base + at)" in header


def test_an_index_over_variable_elements_measures_one() -> None:
	"""The construct exists for elements that are not the same size, so the
	element has to be measured rather than assumed. `_is_run_element` gated the
	extent function on runs and nested members, so an indexed region asked for
	an extent that was simply never emitted and reported it could not compute
	one."""
	header, _ = emit("struct V { u16 len; u8 body[len]; }"
	                 "struct T { u16 n; indexed(offset_type = u16, count = n)"
	                 " { V varying[]; } }")

	assert "situ_V_extent(probe)" in header
	assert "situ_T_varying_at" in header


@pytest.mark.skipif(HOST_CC is None, reason="no host compiler")
@pytest.mark.parametrize("body", [
	"struct S { u16 n; opaque payload [n]; }",
	"struct S { u16 n; opaque payload [n]; u32 z; }",
	"varint_type v { encoding = leb128; max_bits = 64; minimal; }"
	"struct S { u16 a; v n; }",
	"enum K : u8 { a = 1, b = 2, }struct A { u16 x; } struct B { u32 y; }"
	"struct S { K k; variant v switch (k) { case K.a: A p; case K.b: B q; } }",
	"struct R { u32 id; }"
	"struct S { u16 n; indexed(offset_type = u16, count = n) { R entries[]; } }",
])
def test_phase_six_constructs_compile_warning_clean(tmp_path: Path, body: str) -> None:
	compile_generated(tmp_path, body)


# -- the cryptographic model (section 14) -----------------------------------

CRYPTO = PREAMBLE + """codec aead {
	length_preserving;
	seekable = linear;
	granularity = byte;
	authenticated;
	invertible;
	deterministic;
}
"""

SEALED = """struct S {
	u8  hop;
	authenticated { u32 seq; }
	sealed(aead) { u32 inner; }
	tag u8[16];
}
"""


def test_the_sealed_interior_takes_a_gated_view_type() -> None:
	header, _ = emit(SEALED, preamble=CRYPTO)

	assert "typedef struct situ_S_sealed_t {" in header
	assert "situ_S_sealed_open(situ_view_t view, int verified, situ_S_sealed_t *out)" in header
	assert "situ_S_sealed_inner_get(situ_S_sealed_t gate)" in header


def test_a_covered_field_gets_a_setter_that_takes_the_message() -> None:
	header, _ = emit(SEALED, preamble=CRYPTO)

	assert "situ_S_seq_set(situ_msg_t *msg, situ_view_t view, uint32_t value)" in header
	assert "situ_msg_mark_dirty(msg, SITU_S_TAG_DIRTY);" in header
	# And no plain one, or the obligation could be sidestepped by accident.
	assert "situ_S_seq_set(situ_view_t view" not in header


def test_an_uncovered_field_keeps_its_plain_setter() -> None:
	header, _ = emit(SEALED, preamble=CRYPTO)
	assert "situ_S_hop_set(situ_view_t view, uint8_t value)" in header


def test_a_tag_carries_its_covered_span_and_its_dirty_bit() -> None:
	header, _ = emit(SEALED, preamble=CRYPTO)

	assert "#define SITU_S_TAG_DIRTY 0x1u" in header
	assert "situ_S_tag_covered(situ_view_t view, uint32_t *offset, uint32_t *len)" in header
	assert "situ_S_tag_is_dirty(const situ_msg_t *msg)" in header
	assert "situ_S_tag_finalize(situ_msg_t *msg)" in header


def test_allow_unverified_read_generates_no_gate_and_says_so() -> None:
	header, _ = emit(SEALED.replace("sealed(aead) {",
	                                "sealed(aead) [allow_unverified_read] {"),
	                 preamble=CRYPTO)

	assert "typedef struct situ_S_sealed_t" not in header
	assert "bytes nobody has authenticated" in header
	assert "situ_S_sealed_inner_get(situ_view_t view)" in header


def test_a_region_whose_codec_expands_without_bound_stops_addressing() -> None:
	"""Nothing after it has an offset, and the header says why.

	Emitting arithmetic that ignored the expansion would put every later
	accessor on the wrong bytes, which is worse than no accessor at all.
	"""
	header, _ = emit("""struct S {
		coded body(squash) { u32 inner; }
		u32 trailer;
	}
	""", preamble=PREAMBLE + "codec squash { expansion = unbounded; not seekable; "
	                         "invertible; deterministic; }\n")

	assert "No accessor for `trailer`" in header
	assert "not known until it has been decoded" in header


@pytest.mark.skipif(HOST_CC is None, reason="no host compiler")
def test_crypto_constructs_compile_warning_clean(tmp_path: Path) -> None:
	compile_generated(tmp_path, SEALED, preamble=CRYPTO)


@pytest.mark.skipif(HOST_CC is None, reason="no host compiler")
def test_the_stage_gate_is_a_compile_error_not_a_convention(tmp_path: Path) -> None:
	"""The other half of 14.3, which cannot be tested at run time.

	A caller holding an ordinary view cannot reach a single field of the sealed
	interior -- not by discipline, but because the program does not build. That
	is what makes "parse attacker-controlled plaintext before authenticating it"
	unrepresentable rather than discouraged, so it is asserted here by compiling
	the attempt and requiring it to fail.
	"""
	header, source = emit(SEALED, preamble=CRYPTO)
	(tmp_path / "unit.h").write_text(header, encoding="ascii")
	(tmp_path / "unit.c").write_text(source, encoding="ascii")
	(tmp_path / "probe.c").write_text(
		'#include "unit.h"\n'
		"uint32_t peek(situ_view_t view);\n"
		"uint32_t peek(situ_view_t view)\n"
		"{\n"
		"\treturn situ_S_inner_get(view);\n"
		"}\n", encoding="ascii")

	result = subprocess.run(
		[HOST_CC or "cc", *WARNINGS, f"-I{RUNTIME}", f"-I{tmp_path}", "-c",
		 str(tmp_path / "probe.c")],
		cwd=tmp_path, capture_output=True, text=True)

	assert result.returncode != 0, (
		"an unverified read of the sealed interior compiled; the stage gate of "
		"section 14.3 is not holding")


@pytest.mark.skipif(HOST_CC is None, reason="no host compiler")
def test_passing_a_plain_view_to_a_gated_accessor_is_refused(tmp_path: Path) -> None:
	"""The same gate, approached by the name that does exist.

	`situ_S_sealed_inner_get` is a real function; what it will not accept is a
	view nobody verified. C's struct types are not interchangeable, so the
	refusal is the type system's rather than a check that could be skipped.
	"""
	header, source = emit(SEALED, preamble=CRYPTO)
	(tmp_path / "unit.h").write_text(header, encoding="ascii")
	(tmp_path / "unit.c").write_text(source, encoding="ascii")
	(tmp_path / "probe.c").write_text(
		'#include "unit.h"\n'
		"uint32_t peek(situ_view_t view);\n"
		"uint32_t peek(situ_view_t view)\n"
		"{\n"
		"\treturn situ_S_sealed_inner_get(view);\n"
		"}\n", encoding="ascii")

	result = subprocess.run(
		[HOST_CC or "cc", *WARNINGS, f"-I{RUNTIME}", f"-I{tmp_path}", "-c",
		 str(tmp_path / "probe.c")],
		cwd=tmp_path, capture_output=True, text=True)

	assert result.returncode != 0, (
		"a plain view was accepted where a verified one is required")


@pytest.mark.skipif(HOST_CC is None, reason="no host compiler")
def test_a_namespaced_schema_compiles_warning_clean(tmp_path: Path) -> None:
	"""Two types of the same name in one file, and one header holding both."""
	compile_generated(tmp_path, """namespace outer {
		struct header { u8 version; u16 length; }
	}
	namespace inner {
		struct header { u32 seq; }
	}
	struct packet {
		outer::header out;
		inner::header in;
	}
	""")


# -- fixed point and BCD (section 8.1) ---------------------------------------


def test_fixed_point_hands_back_the_stored_integer() -> None:
	"""No floating point is generated. The target may have none, and the scale
	is exact, so the header carries it and the caller does the arithmetic in
	whatever type it has."""
	header, _ = emit("struct s { q16_16 gain; }")

	assert "int32_t situ_s_gain_get(situ_view_t view)" in header
	assert "#define SITU_S_GAIN_FRAC_BITS 16u" in header
	assert "#define SITU_S_GAIN_SCALE 65536" in header

	# In the code, not the prose: the comment explaining that no floating point
	# is generated contains the word "floating".
	code = [line for line in header.splitlines()
	        if line.strip() and not line.strip().startswith(("/*", "*", "//"))]
	assert not [line for line in code if "float" in line or "double" in line]


def test_the_unsigned_fixed_point_form_is_unsigned() -> None:
	header, _ = emit("struct s { uq8_8 ratio; }")

	assert "uint16_t situ_s_ratio_get(situ_view_t view)" in header
	assert "#define SITU_S_RATIO_SCALE 256" in header


def test_bcd_decodes_on_read_and_encodes_on_write() -> None:
	"""Returning the packed nibbles would make BCD a u32 with a comment. The
	point of the type is that the accessor knows what the nibbles mean."""
	header, _ = emit("struct s { bcd8 serial; }")

	assert "situ_bcd_decode(" in header
	assert "situ_bcd_encode(" in header
	assert "#define SITU_S_SERIAL_DIGITS 8u" in header
	assert "#define SITU_S_SERIAL_MAX 99999999u" in header


def test_a_bcd_field_is_validated_nibble_by_nibble() -> None:
	"""A BCD field can hold a bit pattern that is not a number, and the getter
	cannot report that -- it returns a number either way."""
	_, source = emit("struct s { bcd2 seconds; }")

	assert "situ_bcd_valid(" in source
	assert "SITU_ERR_CONSTRAINT" in source


def test_fixed_point_is_not_validated_at_all() -> None:
	"""Every bit pattern is a valid fixed-point number, which is the difference
	from BCD and the reason only one of them costs a check on parse."""
	_, source = emit("struct s { q16_16 gain; }")

	assert "situ_bcd_valid" not in source


# -- what a schema costs that does not use it (section 13.1) -----------------


def test_generated_code_includes_nothing_but_the_situ_runtime() -> None:
	"""The property that makes optional dependencies actually optional.

	situ ships the codecs it can generate and leaves the ones needing a real
	library to `extern`. That split is only worth anything if a schema pays for
	what it names and nothing else -- otherwise every user links a crypto
	library so that the few who seal a region can.
	"""
	header, source = emit("struct s { u16 a; u32 b; }")
	included = [line for line in (header + "\n" + source).splitlines()
	            if line.startswith("#include")]

	# The runtime, and the header the source belongs to. `<stdint.h>` and
	# `<stddef.h>` arrive through situ.h; generated code names nothing else.
	assert included == ['#include "situ.h"', '#include "unit.h"']


def test_a_sealed_region_pulls_in_no_crypto() -> None:
	"""`sealed(aes_gcm_128)` is the heaviest thing a schema can say, and it
	still links nothing: the gate takes the verification result as a parameter,
	so situ guards the bytes and the caller runs the cipher. A schema that seals
	a region and one that does not have the same dependencies -- none."""
	plain, _ = emit("struct s { u16 a; }")
	sealed, _ = emit("""codec aes_gcm_128 {
		granularity = byte;
		length_preserving;
		seekable;
		authenticated;
		invertible;
		deterministic;
	}
	impl aes_gcm_128 extern "my_aes_gcm_128";

	struct h { u8 v; u16 length; }
	struct s {
		u8   hop;
		authenticated { h hdr; u8 nonce[12] [nonce]; }
		sealed(aes_gcm_128, nonce = nonce) { u16 inner; }
		tag  u8[16];
	}
	""")

	def includes(text: str) -> set[str]:
		return {line for line in text.splitlines() if line.startswith("#include")}

	assert includes(sealed) == includes(plain)
	for name in ("openssl", "sodium", "mbedtls", "aes.h"):
		assert name not in sealed


def test_declared_text_encoding_is_validated() -> None:
	"""Section 8.6 offers `[encoding]`; it used to parse and be dropped, so a
	schema could call a field ASCII and the generated code would neither check
	it nor record it."""
	_, ascii_source = emit("struct s { u8 tag[4] [encoding = ascii]; }")
	_, utf8_source  = emit("struct s { u8 name[8] [encoding = utf8]; }")

	assert "situ_ascii_valid((view.base) + 0u, 4u)" in ascii_source
	assert "situ_utf8_valid((view.base) + 0u, 8u)" in utf8_source
	assert "SITU_ERR_CONSTRAINT" in utf8_source


def test_text_without_an_encoding_is_not_validated() -> None:
	"""The attribute is the claim. A plain byte array makes none, and paying
	for a check nobody asked for would be the other way to get this wrong."""
	_, source = emit("struct s { u8 name[8]; }")

	# Named calls, not the substring: `situ_s_validate` contains "valid".
	assert "situ_ascii_valid" not in source
	assert "situ_utf8_valid" not in source


def test_a_nul_terminated_field_reports_its_content_length() -> None:
	"""The declared size is the capacity, so the other number -- how much of it
	is content -- is the one a caller would otherwise compute by hand."""
	header, source = emit("struct s { u8 name[8] [nul_terminated]; }")

	assert "uint32_t situ_s_name_len(situ_view_t view)" in header
	assert "situ_nul_len(view.base + 0u, 8u)" in header

	# And the terminator has to be there, or nobody knows where content stops.
	assert "situ_nul_terminated((view.base) + 0u, 8u)" in source


def test_a_nul_terminated_field_does_not_move_what_follows() -> None:
	"""Capacity, not content: the field is its declared size whatever it
	holds, which is the whole reason this reading was chosen."""
	terminated, _ = emit("struct s { u8 name[8] [nul_terminated]; u16 after; }")
	plain, _      = emit("struct s { u8 name[8]; u16 after; }")

	assert "#define SITU_S_SIZE_FIXED 10u" in terminated
	assert terminated.count("situ_get_be16(view.base + 8u)") == \
	       plain.count("situ_get_be16(view.base + 8u)") == 1


def test_a_plain_byte_array_gets_no_length_accessor() -> None:
	"""The attribute is the claim; without it the bytes are just bytes."""
	header, _ = emit("struct s { u8 name[8]; }")

	assert "situ_s_name_len" not in header
	assert "situ_s_name_ptr" in header


def test_an_enum_rejects_a_value_that_is_not_a_member() -> None:
	"""Section 8.7 makes `default = error` the default and says unknown values
	are rejected on parse. Every backend emitted that as a comment and
	validated nothing, so a field declared to admit two values took all 256."""
	header, source = emit('enum k : u8 { one = 1, two = 2 }\nstruct s { k kind; u8 pad; }')

	assert "int situ_k_is_known(situ_k_t value)" in header
	assert "situ_k_is_known(situ_s_kind_get(view))" in source


def test_default_pass_admits_what_it_says_it_admits() -> None:
	"""The other half of 8.7: a schema that opts out is not second-guessed."""
	header, source = emit('enum k : u8 { one = 1, two = 2, default = pass }\nstruct s { k kind; u8 pad; }')

	assert "situ_k_is_known" in header		# still offered
	assert "situ_k_is_known(situ_s_kind_get" not in source	# not demanded


# -- invariants (open question 3) --------------------------------------------


INVARIANT = (
	"struct h { u8 v; u16 kind; }\n"
	"struct s { u16 total; h hdr; u8 body[remaining]; }\n"
	"invariant s.total == size(s.hdr) + size(s.body);\n"
)


def test_a_derived_field_gets_no_setter() -> None:
	"""An invariant decides its value. A direct write would make the schema's
	own statement false, so the lattice refuses it before the backend does."""
	header, _ = emit(INVARIANT)

	assert "situ_s_total_get" in header
	assert "situ_s_total_set" not in header
	assert "mutate=Immutable" in header


def test_a_derived_field_gets_a_recompute() -> None:
	"""Refusing the write without offering the recompute would leave a schema
	that can state a relationship and never satisfy it."""
	header, _ = emit(INVARIANT)

	assert "void situ_s_total_recompute(situ_msg_t *msg, situ_view_t view)" in header
	assert "situ_msg_clear_dirty(msg, SITU_S_TOTAL_STALE)" in header
	assert "int situ_s_total_is_stale(const situ_msg_t *msg)" in header


def test_the_recompute_evaluates_the_expression() -> None:
	"""`size(hdr) + size(body)` over a 3-byte header and a `remaining` tail is
	3 plus whatever the view has left."""
	header, _ = emit(INVARIANT)

	assert "(3u + situ_remaining_u32(view.limit, 5u))" in header


def test_what_the_invariant_reads_is_marked_covered() -> None:
	"""The same words a tag gets, because it is the same obligation."""
	header, _ = emit(INVARIANT)

	assert "writing through this pointer leaves invariant total stale" in header


def test_an_expression_the_backend_cannot_evaluate_says_so() -> None:
	"""And leaves the refusal to write standing, so the invariant cannot be
	broken -- only left unsatisfiable, which is the honest half."""
	header, _ = emit("struct s { u16 total; u8 body[4]; }\n"
	                 "invariant s.total == size(s.body) * count(s.body) / 0;\n")

	assert "situ_s_total_set" not in header


# -- delimited members (section 8.6.1) --------------------------------------

FRAMED = (
	'struct s {\n'
	'\tu8  magic[4];\n'
	'\tu8  line[] until "\\r\\n";\n'
	'\tu16 count;\n'
	'}\n'
)


def test_a_delimited_member_gets_content_and_span() -> None:
	"""Two numbers, because a caller and the next member want different ones.
	`_len` is the content; `_span` is content plus delimiter, which is what the
	following member's offset is computed from."""
	header, _ = emit(FRAMED)

	assert "static inline uint32_t situ_s_line_len(situ_view_t view)" in header
	assert "static inline uint32_t situ_s_line_span(situ_view_t view)" in header
	# The scan reads from the member's own base rather than through `_ptr`.
	# With `[trim]` those differ -- `_ptr` skips the leading whitespace, and a
	# scan starting there would measure the wrong run.
	# The scan takes its base as a parameter now, and `_len(view)` passes
	# the resolved offset in. What the test is about -- that the scan starts
	# at the member's own base and not at `_ptr`, which `[trim]` moves -- is
	# unchanged.
	assert "situ_scan(view.base + at" in header
	assert ("situ_s_line_len(situ_view_t view)\n{\n"
		"\treturn situ_s_line_len_from(view, 4u);") in header


def test_the_delimiter_is_part_of_the_span_and_not_the_content() -> None:
	"""Members partition their struct's bytes exactly, so a delimiter nobody
	owned would be a hole between two members -- the same reason
	`nul_terminated` counts its capacity."""
	header, _ = emit(FRAMED)

	assert ("situ_s_line_len_from(view, at) + "
		"(situ_s_line_terminated_from(view, at) ? 2u : 0u)") in header


def test_a_missing_delimiter_adds_nothing_to_the_span() -> None:
	"""The member ran to the end of the buffer. Claiming the delimiter's bytes
	anyway would put the next member past the limit its own bounds check
	trusts, which is a read outside the view the caller established."""
	header, _ = emit(FRAMED)

	# `_from(view, at)` now: everything that accumulates offsets has the
	# base in hand, and re-resolving it there is what made a loop over M
	# members cost M^2 scans while reading as one pass.
	assert "situ_s_line_terminated_from(view, at) ? 2u : 0u" in header


def test_a_later_members_offset_sums_the_scan() -> None:
	"""Not an inlined search: the member emits its own `_span` and everything
	downstream calls it, so one scan is described in one place.

	`_span_from(view, offset)` rather than `_span(view)`, because this loop
	has the running sum in hand and the plain form re-resolves the base by
	rescanning every member before it -- which made the sum cost far more
	than the scans it is adding up.
	"""
	header, _ = emit(FRAMED)

	assert "situ_s_count_offset" in header
	assert "offset = offset + (situ_s_line_span_from(view, offset));" in header


def test_validate_refuses_a_frame_with_no_delimiter_in_it() -> None:
	"""The one thing parse can check. That the content excludes the delimiter
	needs no check: the scan stops at the first one, so it holds by
	construction."""
	_, source = emit(FRAMED)

	assert "if (!situ_s_line_terminated(view))" in source
	assert "SITU_ERR_CONSTRAINT" in source


def test_a_capped_scan_stops_at_the_smaller_of_cap_and_buffer() -> None:
	"""A cap larger than what is left would read past the extent the one bounds
	check established, so the cap alone is not the limit."""
	header, _ = emit('struct s { u8 line[] until "\\r\\n" max 16; u8 rest[remaining]; }')

	assert "situ_min_u32(16u, situ_remaining_u32(view.limit, at))" in header


def test_a_relaxed_delimiter_scans_for_an_inert_one() -> None:
	header, _ = emit('struct s { u8 f[] until "," [quoted = "\\""]; u8 rest[remaining]; }')

	assert "situ_scan_relaxed(" in header
	assert "0x22u" in header or "34u" in header	# the quote byte
	assert "SITU_NO_BYTE" in header			# no escape byte


def test_the_delimiter_comment_reads_as_the_spec_writes_it() -> None:
	"""`"\\r\\n"` rather than `{0x0D, 0x0A}`: the comment exists to be checked
	against the specification somebody is implementing, and that document says
	CRLF."""
	header, _ = emit(FRAMED)

	assert '`"\\r\\n"`' in header


# -- text-encoded numbers (section 8.6.2) -----------------------------------

TEXTY = 'struct s { decimal u16 count until "\\r\\n" max 8; u8 body[count]; }'


def test_a_text_number_getter_can_fail() -> None:
	"""Every other scalar getter here returns the value, because every other
	conversion is total. A decimal parse is not, and a getter that returned 0
	for `12x4` would be handing back a number nobody wrote -- which is the
	whole of what `repr = TextConverted` means."""
	header, _ = emit(TEXTY)

	assert ("static inline situ_err_t situ_s_count_get"
	        "(situ_view_t view, uint16_t *out)") in header
	assert "situ_parse_uint(situ_s_count_ptr(view)" in header
	assert "return SITU_ERR_CONSTRAINT;" in header


def test_the_range_checked_is_the_declared_types() -> None:
	"""`u16` gives the value's domain, not its width in the buffer: a text
	number is as wide as the number."""
	header, _ = emit(TEXTY)

	assert "10u, 65535u, &value)" in header


def test_a_length_written_in_digits_is_parsed_not_loaded() -> None:
	"""Loading it read the ASCII as a big-endian integer, which is the kind of
	wrong that produces a plausible number: "10" came out as 0x3130."""
	header, _ = emit(TEXTY)

	assert "situ_s_body_len" in header
	assert "situ_get_be16" not in header.split("situ_s_body_len")[1][:200]
	assert "situ_s_count_value(view)" in header


def test_a_text_number_gets_no_raw_setter() -> None:
	"""Writing 4096 where 12 was takes two more digits than the field holds, so
	the write moves everything after it. The ordinary setter stored four raw
	bytes over the digits, which is not even the wrong number."""
	header, _ = emit(TEXTY)

	assert "No situ_s_count_set()" in header
	assert "static inline void situ_s_count_set(" not in header


def test_validate_refuses_digits_that_are_not_digits() -> None:
	"""A constraint like any other, so parse refuses it rather than leaving
	every caller of the getter to be the first to find out."""
	_, source = emit(TEXTY)

	assert "situ_s_count_get(view, &parsed)" in source


def test_a_signed_text_number_is_refused() -> None:
	"""situ reads digits as a magnitude. A sign or a point is a grammar rather
	than a number, which is the line decision 0020 draws."""
	with pytest.raises(SituError, match="must be an unsigned integer"):
		emit('struct s { decimal i32 n until "\\r\\n"; }')


def test_a_text_number_needs_somewhere_to_stop() -> None:
	"""And the diagnostic offers both ways, because there are two and a
	reader who only hears about `until` writes SMTP's three-digit reply code
	as a delimited field with nothing to delimit it."""
	with pytest.raises(SituError, match="has no end") as caught:
		emit("struct s { decimal u32 n; }")

	rendered = caught.value.diagnostic.render()
	assert 'until ":"` stops it at a delimiter' in rendered
	assert "gives it a fixed width, padded" in rendered


# -- runs of records (section 8.6.3) ----------------------------------------

BLOCK = (
	'struct kv {\n'
	'\tu8 key[]   until ": ";\n'
	'\tu8 value[] until "\\r\\n";\n'
	'}\n'
	'struct blk {\n'
	'\tkv entries[] until "\\r\\n";\n'
	'\tu8 payload[remaining];\n'
	'}\n'
)


def test_a_run_of_records_is_walked_not_scanned() -> None:
	"""The distinction the two spellings hide. For a byte array the delimiter
	ends the content, so the scan looks anywhere; for a run it ends the run,
	and is a terminator only where an element would start. Scanning anywhere
	found the CRLF at the end of the first header line and stopped there."""
	header, _ = emit(BLOCK)

	assert "static inline uint32_t situ_blk_entries_count(situ_view_t view)" in header
	assert ("static inline situ_err_t situ_blk_entries_at"
	        "(situ_view_t view, uint32_t index, situ_view_t *out)") in header
	assert "situ_kv_extent(element)" in header


def test_the_element_type_gets_an_extent_function() -> None:
	"""The next element starts where this one ends, and for a struct whose own
	members are delimited that is not a constant."""
	header, _ = emit(BLOCK)

	assert "static inline uint32_t situ_kv_extent(situ_view_t view)" in header
	assert "extent = extent + (situ_kv_key_span(view));" in header


def test_only_a_run_element_gets_one() -> None:
	"""Emitted for every variable struct it was dead code in most headers, and
	in one case a function that summed a member with no resolvable length and
	returned a confident zero."""
	header, _ = emit("struct s { u8 n; u8 body[n]; }")

	assert "situ_s_extent" not in header


def test_the_walk_cannot_run_forever() -> None:
	"""A record whose members are all delimited and all empty occupies no
	bytes. A walk that advanced by that would not terminate on input somebody
	chose, which is a denial of service rather than a wrong answer."""
	header, _ = emit(BLOCK)

	assert "if (size == 0u || at + size > view.limit)" in header


def test_the_terminator_belongs_to_the_run() -> None:
	"""So the member after it starts past the blank line, not on it."""
	header, _ = emit(BLOCK)

	assert "at = at + 2u;" in header
	assert "situ_blk_payload" in header


def test_a_run_whose_element_has_no_extent_says_so() -> None:
	"""A `[remaining]` member inside the element consumes whatever view it is
	given, so a second element has nowhere to begin. Saying nothing would leave
	a reader looking for a typo."""
	header, _ = emit(
		'struct e { u8 all[remaining]; }\n'
		'struct s { e items[] until "\\r\\n"; }\n'
	)

	assert "No accessors for `items`" in header
	assert "has no extent this build can compute" in header


def test_the_harness_compiles_for_a_variable_struct() -> None:
	"""It never did. The harness declared `buf[SITU_X_SIZE_FIXED]` for every
	struct, and that macro is emitted only where a struct has one size -- so
	`gen-fuzz` produced C that did not compile for anything with a length
	field, a `[remaining]` tail or a delimiter. Those are the structs most
	likely to have a parsing bug, and none of them had ever been fuzzed."""
	text = fuzz_source("struct S { u8 n; u8 body[n]; }")

	assert "SITU_S_SIZE_FIXED" not in text
	assert "extent = size < sizeof buf" in text


def test_the_harness_gives_a_variable_struct_the_fuzzers_own_length() -> None:
	"""Better fuzzing than a constant: the extent that reaches the bounds
	check is one the fuzzer chose, and one it can shrink."""
	text = fuzz_source("struct S { u8 a; u8 rest[remaining]; }")

	assert "situ_S_view(&msg, 0, extent, &view)" in text


def test_the_harness_reads_the_last_byte_a_length_claims() -> None:
	"""Not the first. The length is attacker-controlled and the pointer is
	where it aims, so an off-by-one in the extent shows up at the end."""
	text = fuzz_source("struct S { u8 n; u8 body[n]; }")

	assert "situ_S_body_ptr(view)[n - 1u]" in text


def test_the_harness_walks_a_run_of_records() -> None:
	"""A run has a count and an indexed accessor rather than a pointer and a
	length, and the walk is the read that can run off the end."""
	text = fuzz_source('struct kv { u8 k[] until ":"; u8 v[] until "\\r\\n"; }\n'
	                   'struct S { kv items[] until "\\r\\n"; u8 r[remaining]; }')

	assert "situ_S_items_count(view)" in text
	assert "situ_S_items_at(view, i, &element)" in text


def test_the_harness_does_not_read_an_array_element_entry() -> None:
	"""`recs[]` describes every element at once and owns no bytes. Reading it
	emitted an accessor for a member that does not exist -- the shared walk
	knows that, and this loop had its own copy that did not."""
	text = fuzz_source("struct e { u16 x; }\nstruct S { u8 n; e recs[n]; }")

	assert "situ_S_recs_view" not in text


def test_the_harness_gates_a_versioned_read() -> None:
	text = fuzz_source("struct S [version = v] { u8 v; u32 b [since = 2]; }")

	assert "situ_S_b_get(view, &held) == SITU_OK" in text


def test_a_fixed_width_text_number_is_its_digits_wide() -> None:
	"""`decimal u16 code[3]` is three digits, which is three bytes. The
	generic array path multiplied the count by the scalar's width and made it
	six -- because everywhere else `[n]` counts elements of the declared type,
	and here the type is the value's domain rather than its storage."""
	header, _ = emit("struct s { decimal u16 code[3]; u8 sep; }")

	assert "SITU_S_SIZE_FIXED 4u" in header


def test_it_is_checked_against_the_fields_range_not_the_types() -> None:
	"""Three bytes hold 0..999, whatever `u16` would allow. A check written
	against the type accepts a value the field cannot represent."""
	header, _ = emit("struct s { decimal u16 code[3]; u8 sep; }")

	assert "10u, 999u, &value" in header


def test_a_fixed_width_text_number_needs_no_delimiter() -> None:
	with pytest.raises(SituError, match="says twice how wide it is"):
		emit('struct s { decimal u16 c[3] until ":"; u8 r[remaining]; }')



def test_a_coded_region_may_be_delimited() -> None:
	"""Scan first, decode second -- the order the protocols needing this
	specify. SMTP's dot-stuffing protects its own terminator, so `CRLF . CRLF`
	is unambiguous in the encoded bytes and would not be in the decoded ones.
	A decoder running first would have to know where to stop, which is what
	the scan is for."""
	header, _ = emit(
		"codec stuff { kernel = stuffing(worst_case = 4, per = 3, "
		"unit = stream, code = dot); }\n"
		"impl stuff derived;\n"
		'struct s { coded body(stuff) until "\\r\\n.\\r\\n" { u8 c[remaining]; } }')

	assert "situ_s_body_span" in header
	assert "0x0Du, 0x0Au, 0x2Eu, 0x0Du, 0x0Au" in header


def test_a_coded_region_gets_no_token_comparison() -> None:
	"""`_eq` over a transform's output would compare stuffed text -- or
	ciphertext -- against a literal somebody wrote in the clear."""
	header, _ = emit(
		"codec stuff { kernel = stuffing(worst_case = 4, per = 3, "
		"unit = stream, code = dot); }\n"
		"impl stuff derived;\n"
		'struct s { coded body(stuff) until "\\r\\n.\\r\\n" { u8 c[remaining]; } }')

	assert "situ_s_body_eq" not in header
	assert "the pointer above is" in header


# -- runs ending on a condition (section 8.6.6) -----------------------------

CHAIN = (
	"struct e { u8 next; u8 len; u8 d[(len + 1) * 8 - 2]; }\n"
	"struct s { e chain[] while (next == 43 || next == 44); u8 rest[remaining]; }\n"
)


def test_a_while_run_is_walked_and_the_condition_asked_after() -> None:
	"""The whole difference from a delimiter. `until` asks about the position
	before an element; this asks about the element just read, so the one that
	ends the run is part of it."""
	header, _ = emit(CHAIN)

	assert "situ_s_chain_count" in header
	assert "situ_e_next_get(element) == 43" in header
	# The condition is tested after the cursor advances, so the failing
	# element has already been counted.
	body = header.split("situ_s_chain_count")[1]
	assert body.index("n  = n + 1u") < body.index("if (!(situ_e_next_get")


def test_a_length_in_units_is_not_zero() -> None:
	"""`sized_by` holds a field path and holds nothing for arithmetic over
	one, so the length branch returned zero -- for a length counted in units,
	which is about as common as a length gets."""
	header, _ = emit(CHAIN)

	assert "(situ_e_len_get(view) + 1) * 8 - 2" in header


def test_the_substitution_takes_the_longest_name_first() -> None:
	"""Or `len` rewrites the `len` inside `hdr_ext_len` and the expression
	names a getter that does not exist."""
	header, _ = emit(
		"struct e { u8 len; u8 hdr_ext_len; u8 d[(hdr_ext_len + 1) * 8 - 2]; }\n"
		"struct s { e c[] while (len == 1); u8 r[remaining]; }\n")

	assert "situ_e_hdr_ext_len_get" in header
	assert "situ_e_len_get(view)_ext_len" not in header


def test_a_capped_run_stops_counting() -> None:
	"""RFC 8200 sets no limit on an extension chain, and a receiver walking an
	unbounded one on attacker-chosen input is the denial of service its own
	security section warns about."""
	header, _ = emit(
		"struct e { u8 next; u8 len; u8 d[(len + 1) * 8 - 2]; }\n"
		"struct s { e c[] while (next == 43) max 8; u8 r[remaining]; }\n")

	assert "n < 8u" in header


def test_the_member_after_a_run_is_placed() -> None:
	header, _ = emit(CHAIN)

	assert "offset = offset + (situ_s_chain_span(view));" in header


def test_the_fuzz_harness_walks_a_while_run() -> None:
	"""Walking is the read that can run off the end, and this is the harness
	whose whole job is to try. It fell through to the nested-struct branch and
	named a `_view` accessor a run does not have."""
	text = fuzz_source(
		"struct e { u8 k; u8 u; u8 b[(u + 1) * 4 - 2]; }\n"
		"struct S { e chain[] while (k == 1) max 6; u8 tail[remaining]; }\n")

	assert "situ_S_chain_count(view)" in text
	assert "situ_S_chain_at(view, i, &element)" in text
	assert "situ_S_chain_view" not in text


# -- measuring a struct whose members are fractions of a byte ---------------

PACKED = """
struct label { u2 form; u6 rest; u8 text[rest]; }
struct run { label labels[] while (form == 0) max 8; }
"""


def test_a_bit_packed_element_contributes_its_bits_to_the_extent() -> None:
	"""It measured each member in whole bytes and summed those, so `u2` and
	`u6` contributed 0 each and a label came out one byte long -- one byte
	being `text`'s. A run over it then walked the same byte forever, or
	rather until `max`, reading each label at the offset of the last.

	Two bits and six bits are one byte only when they are added as bits.
	"""
	header, _ = emit(PACKED)

	# One byte for the packed pair, plus however many `rest` says.
	assert "uint32_t extent = 1u;" in header
	assert "extent = extent + ((uint32_t)((uint8_t)situ_bits_get_msb(" in header


@pytest.mark.skipif(HOST_CC is None, reason="no host compiler")
def test_the_run_over_it_advances_by_that_extent(tmp_path: Path) -> None:
	"""The half a grep cannot check: whether the walk actually moves. Built
	and *run*, because a probe that only compiles asserts that the accessors
	exist and nothing at all about where they land."""
	header, source = emit(PACKED)
	(tmp_path / "unit.h").write_text(header, encoding="ascii")
	(tmp_path / "unit.c").write_text(source, encoding="ascii")
	(tmp_path / "probe.c").write_text("""
#include "unit.h"

int main(void)
{
	/* Two labels: (form 0, rest 2) "hi", then (form 0, rest 1) "x". */
	uint8_t bytes[] = { 0x02, 'h', 'i', 0x01, 'x' };

	situ_msg_t msg;
	situ_view_t view, first, second;

	situ_msg_init(&msg, bytes, (uint32_t)sizeof bytes);
	if (situ_run_view(&msg, 0, (uint32_t)sizeof bytes, &view) != SITU_OK)
		return 1;
	if (situ_run_labels_at(view, 0, &first) != SITU_OK)
		return 2;
	if (situ_run_labels_at(view, 1, &second) != SITU_OK)
		return 3;

	/* Three bytes on, not one: the packed pair plus the two of text. */
	if ((uint32_t)(second.base - first.base) != 3u)
		return 4;
	return 0;
}
""", encoding="ascii")

	binary = tmp_path / "probe"
	build = subprocess.run(
		[HOST_CC or "cc", *WARNINGS, f"-I{RUNTIME}", f"-I{tmp_path}",
		 str(tmp_path / "probe.c"), str(tmp_path / "unit.c"),
		 str(RUNTIME / "situ.c"), "-o", str(binary)],
		capture_output=True, text=True)
	assert build.returncode == 0, build.stderr

	assert subprocess.run([str(binary)]).returncode == 0


# -- a run whose element is a variant ---------------------------------------

DNS_LABEL = """
struct label {
	u2 form;
	u6 rest;
	variant body switch (form) {
		case 0:  u8 text[rest];
		case 3:  u8 pointer_low;
		default: error;
	}
}
struct name { label labels[] while (form == 0 && rest != 0) max 128; }
"""


def test_a_variants_extent_is_a_switch_on_the_discriminant() -> None:
	"""It was "unknowable", which is true of the *constant* and false of the
	value: each arm has a length, and which one applies is a question the
	generated code can ask. Refusing the whole class refused every run over a
	variant, which is most of what a compressed DNS name is."""
	header, _ = emit(DNS_LABEL)

	assert "situ_label_form_get(view) == 0u ?" in header
	assert "situ_label_form_get(view) == 3u ? 1u" in header


@pytest.mark.skipif(HOST_CC is None, reason="no host compiler")
def test_a_compressed_name_walks(tmp_path: Path) -> None:
	"""The four shapes a DNS name comes in, against a hand-checked count and
	extent. Uncompressed ends at the root label; a pointer ends the run
	wherever it appears, including at the front.

	Not a grep: an extent that is short by one still emits every accessor and
	still compiles, and only walking real bytes says which byte it stopped on.
	"""
	header, source = emit(DNS_LABEL)
	(tmp_path / "unit.h").write_text(header, encoding="ascii")
	(tmp_path / "unit.c").write_text(source, encoding="ascii")
	(tmp_path / "probe.c").write_text("""
#include "unit.h"

static int walk(uint8_t *b, uint32_t n, uint32_t labels, uint32_t extent,
                situ_err_t want)
{
	situ_msg_t msg;
	situ_view_t view, element;
	uint32_t i;

	situ_msg_init(&msg, b, n);
	if (situ_name_view(&msg, 0, n, &view) != SITU_OK)
		return 1;
	if (situ_name_labels_count(view) != labels)
		return 2;
	if (situ_name_labels_span(view) != extent)
		return 3;

	for (i = 0; i < labels; i++) {
		if (situ_name_labels_at(view, i, &element) != SITU_OK)
			return 4;
		if (situ_label_validate(element) != want)
			return 5;
	}
	return 0;
}

int main(void)
{
	uint8_t plain[]  = { 3,'w','w','w', 7,'e','x','a','m','p','l','e',
	                     3,'c','o','m', 0 };
	uint8_t whole[]  = { 0xC0, 0x0C };
	uint8_t suffix[] = { 3,'w','w','w', 0xC0, 0x0C };
	uint8_t bad[]    = { 0x40, 0x00 };

	/* www + example + com + the root label, and 4+8+4+1 bytes. */
	if (walk(plain, sizeof plain, 4, 17, SITU_OK))
		return 1;
	/* A pointer is a whole name by itself: one label, two bytes. */
	if (walk(whole, sizeof whole, 1, 2, SITU_OK))
		return 2;
	/* www, then a pointer to the rest. */
	if (walk(suffix, sizeof suffix, 2, 6, SITU_OK))
		return 3;
	/* form 1 selects no arm, and `default: error` says so. */
	if (walk(bad, sizeof bad, 1, 1, SITU_ERR_VERSION))
		return 4;
	return 0;
}
""", encoding="ascii")

	binary = tmp_path / "probe"
	build = subprocess.run(
		[HOST_CC or "cc", *WARNINGS, f"-I{RUNTIME}", f"-I{tmp_path}",
		 str(tmp_path / "probe.c"), str(tmp_path / "unit.c"),
		 str(RUNTIME / "situ.c"), "-o", str(binary)],
		capture_output=True, text=True)
	assert build.returncode == 0, build.stderr

	assert subprocess.run([str(binary)]).returncode == 0


def test_an_unrecognised_discriminant_is_rejected() -> None:
	"""Section 14.5 has always said `default: error` rejects one, and no
	backend rejected it: `SITU_ERR_VERSION` was defined, commented "unknown
	version or variant discriminant", and returned by nothing. It stayed
	invisible while a variant had no extent, because nothing walked one."""
	_, source = emit(DNS_LABEL)

	assert "return SITU_ERR_VERSION;" in source
	assert "situ_label_form_get(view) != 0u" in source
	assert "situ_label_form_get(view) != 3u" in source


def test_an_arm_for_every_value_needs_no_such_check() -> None:
	"""A `default` arm that selects a member accepts anything, so there is no
	unrecognised discriminant to reject."""
	_, source = emit("struct s { u8 k; variant v switch (k) { case 0: u8 a; "
	                 "default: u32 b; } }")

	assert "SITU_ERR_VERSION" not in source


def test_the_check_names_the_arms_as_the_schema_spelled_them() -> None:
	"""The comparison is against the folded integer, because `case K.a:` has
	to become one in four languages and each spells an enum member
	differently. The comment keeps the name the author wrote."""
	_, source = emit("enum k : u8 { a = 1, b = 2 }\n"
	                 "struct s { k which; variant v switch (which) "
	                 "{ case k.a: u8 p; case k.b: u32 q; default: error; } }")

	assert "an arm for k.a, k.b" in source
	assert "!= 1u" in source and "!= 2u" in source


# -- a length the message declares, and the frame it has to fit -------------

OVERLONG = "struct s { u8 n; u16 want; u8 body[want]; u8 tail[remaining]; }"


def test_a_declared_length_is_clamped_to_the_frame() -> None:
	"""`u8 body[want]` with a `u16` length claims up to 65535 bytes. The
	accessor returned that number beside a pointer at the frame base, so
	`ptr(view)[len(view) - 1]` read 65 kilobytes past a 32-byte frame.

	Section 20.2 amortises the bounds check at the frame boundary, and that
	argument holds for offsets the frame is known to contain. A length the
	*message* chooses is not one of those, and nothing had noticed the gap.
	"""
	header, _ = emit(OVERLONG)

	assert "situ_min_u32((uint32_t)(situ_get_be16(view.base + 1u))," in header
	assert "situ_remaining_u32(view.limit," in header


def test_and_validate_calls_such_a_message_malformed() -> None:
	"""Clamping alone silently turns a lie into a truncation. The accessor
	keeps a caller who skipped validation safe; this is what tells a caller
	who did not that the message is wrong rather than short."""
	_, source = emit(OVERLONG)

	assert "the length the message declares has to fit" in source
	assert "return SITU_ERR_BOUNDS;" in source


def test_a_remaining_member_saturates_rather_than_wrapping() -> None:
	"""Its length is `limit - offset`, and the offset is arithmetic over
	fields the message controls. In `uint32_t` that wrapped to about four
	billion, with a pointer past the end -- which is how a fuzzer found this,
	on a schema that had been in the tree since phase 5 and had never been
	fuzzed."""
	header, _ = emit(OVERLONG)

	assert "situ_remaining_u32(view.limit," in header
	assert "view.limit -" not in header


@pytest.mark.skipif(HOST_CC is None, reason="no host compiler")
def test_the_two_answers_agree_on_a_real_message(tmp_path: Path) -> None:
	"""Run, because both halves are about what happens at run time."""
	header, source = emit(OVERLONG)
	(tmp_path / "unit.h").write_text(header, encoding="ascii")
	(tmp_path / "unit.c").write_text(source, encoding="ascii")
	(tmp_path / "probe.c").write_text("""
#include "unit.h"

int main(void)
{
	/* Says 1000 bytes of body; the frame is 16. */
	uint8_t buf[16] = { 0 };
	situ_msg_t msg;
	situ_view_t view;

	buf[1] = 0x03; buf[2] = 0xE8;

	situ_msg_init(&msg, buf, (uint32_t)sizeof buf);
	if (situ_s_view(&msg, 0, (uint32_t)sizeof buf, &view) != SITU_OK)
		return 1;

	/* 16 bytes of frame, 3 before the body: 13 are really there. */
	if (situ_s_body_len(view) != 13u)
		return 2;
	/* And the last byte it hands out is inside the buffer. */
	if (situ_s_body_ptr(view) + situ_s_body_len(view) > buf + sizeof buf)
		return 3;
	/* The message is malformed, not short, and validate says so. */
	if (situ_s_validate(view) != SITU_ERR_BOUNDS)
		return 4;
	return 0;
}
""", encoding="ascii")

	binary = tmp_path / "probe"
	build = subprocess.run(
		[HOST_CC or "cc", *WARNINGS, f"-I{RUNTIME}", f"-I{tmp_path}",
		 str(tmp_path / "probe.c"), str(tmp_path / "unit.c"),
		 str(RUNTIME / "situ.c"), "-o", str(binary)],
		capture_output=True, text=True)
	assert build.returncode == 0, build.stderr

	assert subprocess.run([str(binary)]).returncode == 0


# -- a member the data positions (section 9.8) ------------------------------

LOCATED = "struct s { u32 off; u16 n; u8 body[n] at off; u16 after; }"


def test_a_located_member_takes_the_message_as_well_as_the_view() -> None:
	"""`situ_view_t` is `{ base, limit, generation }` and carries no message
	origin; only `situ_msg_t` knows where offset zero is. An offset measured
	from the start of the message therefore needs both.

	The alternative was a fourth word in every view, growing the core type by
	half for a construct few schemas use.
	"""
	header, _ = emit(LOCATED)

	assert ("static inline situ_err_t situ_s_body_view(const situ_msg_t *msg,"
	        " situ_view_t view, situ_view_t *out)") in header


def test_it_places_nothing_after_itself() -> None:
	"""The property that makes the construct worth having, rather than a
	variable-length member with extra steps. A located member joins no offset
	chain, so `after` is where it would be if `body` were not written at
	all."""
	header, _ = emit(LOCATED)

	assert "s.after : u16  at AbsoluteStatic(0x06)" in header
	assert "static inline uint16_t situ_s_after_get(situ_view_t view)" in header


@pytest.mark.skipif(HOST_CC is None, reason="no host compiler")
def test_the_offset_is_checked_on_every_use(tmp_path: Path) -> None:
	"""Section 20.2 amortises the bounds check at the frame boundary, and that
	covers offsets the frame is known to contain. This one is a number the
	message chooses and can point anywhere, so it is checked per call -- which
	is what `offset = DataPlaced` in the map is telling you it costs.
	"""
	header, source = emit(LOCATED)
	(tmp_path / "unit.h").write_text(header, encoding="ascii")
	(tmp_path / "unit.c").write_text(source, encoding="ascii")
	(tmp_path / "probe.c").write_text("""
#include <string.h>
#include "unit.h"

int main(void)
{
	/* The struct starts at 4, not at 0. With it at 0 the view base and the
	 * message base are the same pointer, and reading the offset from the
	 * wrong one gives the right answer -- which is how the first version of
	 * this test passed against a generator that used the view. */
	uint8_t buf[28] = { 0 };
	situ_msg_t msg;
	situ_view_t view, body;

	buf[4 + 3] = 16;		/* off = 16, from the message base */
	buf[4 + 5] = 4;			/* n = 4 */
	buf[4 + 6] = 0xBE; buf[4 + 7] = 0xEF;	/* after */
	memcpy(buf + 16, "DATA", 4);

	situ_msg_init(&msg, buf, (uint32_t)sizeof buf);
	if (situ_s_view(&msg, 4, &view) != SITU_OK)
		return 1;

	/* `after` sits where it would if `body` were not declared at all. */
	if (situ_s_after_get(view) != 0xBEEF)
		return 2;

	if (situ_s_body_view(&msg, view, &body) != SITU_OK)
		return 3;
	if (body.base != buf + 16 || body.limit != 4u)
		return 4;
	if (memcmp(body.base, "DATA", 4) != 0)
		return 5;

	/* An offset the message chooses, pointing outside the buffer. */
	buf[4 + 3] = 200;
	if (situ_s_body_view(&msg, view, &body) != SITU_ERR_BOUNDS)
		return 6;

	/* And one whose length runs off the end from a legal start. */
	buf[4 + 3] = 26; buf[4 + 5] = 8;
	if (situ_s_body_view(&msg, view, &body) != SITU_ERR_BOUNDS)
		return 7;
	return 0;
}
""", encoding="ascii")

	binary = tmp_path / "probe"
	build = subprocess.run(
		[HOST_CC or "cc", *WARNINGS, f"-I{RUNTIME}", f"-I{tmp_path}",
		 str(tmp_path / "probe.c"), str(tmp_path / "unit.c"),
		 str(RUNTIME / "situ.c"), "-o", str(binary)],
		capture_output=True, text=True)
	assert build.returncode == 0, build.stderr

	assert subprocess.run([str(binary)]).returncode == 0


# -- framing a stream -------------------------------------------------------

STREAM_FRAMED = "struct s { u8 version; u16 n; u8 body[n]; u16 trailer; }"


@pytest.mark.skipif(HOST_CC is None, reason="no host compiler")
def test_a_length_field_is_never_read_before_it_has_arrived(tmp_path: Path) -> None:
	"""The case every hand-written framing loop gets wrong: a `u16` length
	read from one byte that has arrived and one that has not is a guess, and
	the guess sizes the next read.

	Fed one byte at a time, `required` must never report a total derived from
	a field it could not yet see -- so it reports `SIZE_MIN` until the length
	is wholly present, and the real total afterwards.
	"""
	header, source = emit(STREAM_FRAMED)
	(tmp_path / "unit.h").write_text(header, encoding="ascii")
	(tmp_path / "unit.c").write_text(source, encoding="ascii")
	(tmp_path / "probe.c").write_text("""
#include "unit.h"

int main(void)
{
	/* version(1) + n(2) + body(4) + trailer(2) = 9. */
	uint8_t whole[9] = { 1, 0, 4, 'D','A','T','A', 0xBE, 0xEF };
	uint32_t have, need;

	for (have = 0; have < sizeof whole; have++) {
		if (situ_s_required(whole, have, &need) != SITU_ERR_TRUNCATED)
			return 1;
		/* Never more than the truth, and never less than what is here. */
		if (need > sizeof whole || need <= have)
			return 2;
		/* Before the length has arrived, the only honest answer is the
		 * minimum -- reading `n` from byte 1 alone would say 0x0004 or
		 * 0x0400 depending on which byte turned up first. */
		if (have < 3u && need != SITU_S_SIZE_MIN)
			return 3;
		/* Once it has, the answer is exact. */
		if (have >= SITU_S_SIZE_MIN && need != sizeof whole)
			return 4;
	}

	if (situ_s_required(whole, (uint32_t)sizeof whole, &need) != SITU_OK)
		return 5;
	if (need != sizeof whole)
		return 6;

	/* More than a whole message is still a whole message, and the answer is
	 * where this one ends -- which is what lets a caller consume and shift. */
	if (situ_s_required(whole, 64u, &need) != SITU_OK || need != sizeof whole)
		return 7;
	return 0;
}
""", encoding="ascii")

	binary = tmp_path / "probe"
	build = subprocess.run(
		[HOST_CC or "cc", *WARNINGS, f"-I{RUNTIME}", f"-I{tmp_path}",
		 str(tmp_path / "probe.c"), str(tmp_path / "unit.c"),
		 str(RUNTIME / "situ.c"), "-o", str(binary)],
		capture_output=True, text=True)
	assert build.returncode == 0, build.stderr

	assert subprocess.run([str(binary)]).returncode == 0


def test_a_fixed_size_struct_is_framed_too() -> None:
	"""Its answer never depends on the bytes, and it is generated anyway: a
	caller framing a stream should not need one loop for the fixed messages
	and another for the rest, and a struct that gains a length field later
	keeps the same call."""
	header, _ = emit("struct s { u8 a; u32 b; }")

	assert "situ_s_required(const uint8_t *data, uint32_t have" in header
	assert "*need = SITU_S_SIZE_FIXED;" in header


def test_a_remaining_tail_is_declined_and_says_why() -> None:
	"""It ends where the view ends, so how long one is is the transport's
	answer rather than the message's. Framing it is the layer below's job."""
	header, _ = emit("struct s { u8 a; u8 rest[remaining]; }")

	assert "situ_s_required" not in header.replace("No `s_required", "")
	assert "the transport's answer rather than the message's" in header


def test_a_record_run_is_declined_and_says_why() -> None:
	"""The walk that finds its terminator stops just as readily at the end of
	what has arrived, and nothing it emits tells the two apart."""
	header, _ = emit('struct f { u8 k; u8 v[] until ";"; }\n'
	                 'struct s { u16 n; f fields[] until "\\r\\n"; }')

	assert "a run of records ends at a terminator" in header


TWO_LENGTHS = "struct s { u16 n; u8 a[n]; u16 m; u8 b[m]; }"


@pytest.mark.skipif(HOST_CC is None, reason="no host compiler")
def test_a_length_behind_a_variable_member_is_guarded(tmp_path: Path) -> None:
	"""The case the per-member check exists for, and the one a simpler design
	misses.

	`m` sits at `2 + n`, so reading it is only safe once `n` bytes of `a` have
	arrived. Where every length field is at a static offset the `SIZE_MIN`
	gate already covers them all, and the per-member check looks redundant --
	which is how the first version of this test passed with the check deleted.
	Here it is the only thing standing between `required` and a read past the
	end of the caller's buffer, so this is built under ASan.
	"""
	header, source = emit(TWO_LENGTHS)
	(tmp_path / "unit.h").write_text(header, encoding="ascii")
	(tmp_path / "unit.c").write_text(source, encoding="ascii")
	(tmp_path / "probe.c").write_text("""
#include <stdlib.h>
#include <string.h>
#include "unit.h"

int main(void)
{
	/* n = 200, so `m` claims to sit at offset 202. Six bytes have arrived.
	 * A heap buffer of exactly six, so a read past it is a fault ASan sees
	 * rather than whatever happened to be on the stack. */
	uint8_t *part = malloc(6);
	uint32_t need;
	situ_err_t got;

	if (part == NULL)
		return 1;
	part[0] = 0; part[1] = 200;
	memset(part + 2, 'x', 4);

	got = situ_s_required(part, 6u, &need);
	free(part);

	if (got != SITU_ERR_TRUNCATED)
		return 2;
	/* 4 fixed bytes plus the 200 `n` claims, and not one byte of `m`. */
	if (need != 204u)
		return 3;
	return 0;
}
""", encoding="ascii")

	binary = tmp_path / "probe"
	build = subprocess.run(
		[HOST_CC or "cc", *WARNINGS, "-fsanitize=address",
		 f"-I{RUNTIME}", f"-I{tmp_path}",
		 str(tmp_path / "probe.c"), str(tmp_path / "unit.c"),
		 str(RUNTIME / "situ.c"), "-o", str(binary)],
		capture_output=True, text=True)
	if build.returncode != 0 and "sanitize" in build.stderr:
		pytest.skip("no address sanitizer")
	assert build.returncode == 0, build.stderr

	run = subprocess.run([str(binary)], capture_output=True, text=True)
	assert run.returncode == 0, run.stderr


# -- the second accessor family (decision 0022) -----------------------------

INDEXED_RUN = """
struct label { u2 form; u6 rest; u8 text[rest]; }
struct name { label labels[] while (form == 0 && rest != 0) max 128; }
"""


def materialized(body: str, preamble: str = PREAMBLE) -> tuple[str, str]:
	schema   = parse_text(preamble + body)
	resolved = resolve(schema, solve(schema))
	built    = generate(schema, resolved, "unit", materialize=True)
	return built.header, built.source


def test_the_second_family_is_off_by_default() -> None:
	"""It is the consumer's choice, and the default is the one that costs
	nothing: an index is memory a caller did not ask for."""
	header, _ = emit(INDEXED_RUN)

	assert "situ_name_labels_index" not in header


def test_the_index_build_does_not_call_the_walking_accessor() -> None:
	"""The trap this exists to avoid, and the first version fell in it.

	Building an index by calling `_at` per element is a walk per element,
	which is the quadratic cost the index is meant to remove -- so the build
	is quadratic and the lookups are free, and the total is quadratic again.
	Measured at 13% faster than the plain walk, where one pass measures 20x.
	"""
	header, _ = materialized(INDEXED_RUN)
	build     = header[header.index("situ_name_labels_index(situ_view_t"):]
	build     = build[:build.index("\n}")]

	assert "situ_name_labels_at(" not in build
	assert "while (at < view.limit" in build		# the run's own walk


def test_a_run_without_max_gets_no_index_and_says_why() -> None:
	"""How many offsets to hold is the cap, and without one the array would
	have to be allocated -- which generated code does not do (invariant 4)."""
	header, _ = materialized("""
struct e { u8 k; u8 v; }
struct s { e run[] while (k == 0); }
""")

	assert "No index for `run`" in header
	assert "Add `max N`" in header


@pytest.mark.skipif(HOST_CC is None, reason="no host compiler")
def test_the_index_and_the_walk_agree_on_every_element(tmp_path: Path) -> None:
	"""Two routes to the same offsets, which is the only property that
	matters: an index that disagreed with the walk would be worse than none.

	They share the walk's loop for exactly this reason -- a second copy, with
	its two break conditions, is how they would come to differ.
	"""
	header, source = materialized(INDEXED_RUN)
	(tmp_path / "unit.h").write_text(header, encoding="ascii")
	(tmp_path / "unit.c").write_text(source, encoding="ascii")
	(tmp_path / "probe.c").write_text("""
#include "unit.h"

int main(void)
{
	uint8_t buf[128];
	uint32_t n = 0, i;
	situ_msg_t msg;
	situ_view_t view, walked, got;
	situ_name_labels_index_t idx;

	/* Forty one-byte labels, then the root label that ends the run. */
	for (i = 0; i < 40; i++) {
		buf[n++] = 1;
		buf[n++] = (uint8_t)('a' + (i % 26));
	}
	buf[n++] = 0;

	situ_msg_init(&msg, buf, n);
	if (situ_name_view(&msg, 0, n, &view) != SITU_OK)
		return 1;
	if (situ_name_labels_index(view, &idx) != SITU_OK)
		return 2;
	if (idx.count != situ_name_labels_count(view))
		return 3;

	for (i = 0; i < idx.count; i++) {
		if (situ_name_labels_at(view, i, &walked) != SITU_OK)
			return 4;
		if (situ_name_labels_indexed(&idx, view, i, &got) != SITU_OK)
			return 5;
		if (got.base != walked.base || got.limit != walked.limit)
			return 6;
	}

	/* Past the end, and the last entry is where the run ends. */
	if (situ_name_labels_indexed(&idx, view, idx.count, &got) != SITU_ERR_BOUNDS)
		return 7;
	if (idx.start[idx.count] != situ_name_labels_span(view))
		return 8;
	return 0;
}
""", encoding="ascii")

	binary = tmp_path / "probe"
	build = subprocess.run(
		[HOST_CC or "cc", *WARNINGS, f"-I{RUNTIME}", f"-I{tmp_path}",
		 str(tmp_path / "probe.c"), str(tmp_path / "unit.c"),
		 str(RUNTIME / "situ.c"), "-o", str(binary)],
		capture_output=True, text=True)
	assert build.returncode == 0, build.stderr

	assert subprocess.run([str(binary)]).returncode == 0


def test_an_offset_sum_does_not_re_resolve_each_term() -> None:
	"""The bug this whole family found, and the largest one in the tree.

	Every loop that accumulates offsets -- `_offset`, `_required`, the offset
	cache -- has the running sum in hand, and every one of them called
	`_span(view)`, which resolves the member's base by rescanning everything
	before it. So a sum over M delimited members did far more work than the M
	scans it was adding up, on the *default* path with no flag involved.

	Measured on an eight-member record, reading seven offsets five thousand
	times: 10.3 seconds before, 45 milliseconds after.
	"""
	header, _ = emit("""
struct s {
	u8 a[] until ";";
	u8 b[] until ";";
	u8 c[] until ";";
}
""")

	assert "situ_s_b_span_from(view, offset)" in header
	assert "offset = offset + (situ_s_a_span(view));" not in header


@pytest.mark.skipif(HOST_CC is None, reason="no host compiler")
def test_and_the_two_forms_agree(tmp_path: Path) -> None:
	"""Faster is only interesting if it is the same answer."""
	body = 'struct s { u8 a[] until ";"; u8 b[] until ";"; u8 c[] until ";"; }'
	header, source = materialized(body)
	(tmp_path / "unit.h").write_text(header, encoding="ascii")
	(tmp_path / "unit.c").write_text(source, encoding="ascii")
	(tmp_path / "probe.c").write_text("""
#include <string.h>
#include "unit.h"

int main(void)
{
	char raw[] = "one;two;three;";
	situ_msg_t msg;
	situ_view_t view;
	situ_s_offsets_t o;

	situ_msg_init(&msg, (uint8_t *)raw, (uint32_t)strlen(raw));
	if (situ_s_view(&msg, 0, (uint32_t)strlen(raw), &view) != SITU_OK)
		return 1;

	situ_s_offsets(view, &o);
	if (o.b != situ_s_b_offset(view)) return 2;
	if (o.c != situ_s_c_offset(view)) return 3;
	if (o.b != 4u || o.c != 8u)       return 4;

	/* And the span from a base equals the span that resolves its own. */
	if (situ_s_b_span_from(view, o.b) != situ_s_b_span(view)) return 5;
	return 0;
}
""", encoding="ascii")

	binary = tmp_path / "probe"
	build = subprocess.run(
		[HOST_CC or "cc", *WARNINGS, f"-I{RUNTIME}", f"-I{tmp_path}",
		 str(tmp_path / "probe.c"), str(tmp_path / "unit.c"),
		 str(RUNTIME / "situ.c"), "-o", str(binary)],
		capture_output=True, text=True)
	assert build.returncode == 0, build.stderr

	assert subprocess.run([str(binary)]).returncode == 0


CAPPED_BLOCK = """
struct hf  { u8 name[] until ":"; u8 value[] until "\\r\\n"; }
struct blk { hf fields[] until "\\r\\n" max 64; }
"""


@pytest.mark.skipif(HOST_CC is None, reason="no host compiler")
def test_a_record_run_is_indexable_too(tmp_path: Path) -> None:
	"""A header block is the case that wants this most, and it was left out:
	the index took a `while` run's cap and a record run carries its own.

	The two walks differ -- one stops on a condition, the other where the
	terminator stands in for an element -- so each shares its own prologue
	with the index rather than the index carrying a third copy.
	"""
	header, source = materialized(CAPPED_BLOCK)
	(tmp_path / "unit.h").write_text(header, encoding="ascii")
	(tmp_path / "unit.c").write_text(source, encoding="ascii")
	(tmp_path / "probe.c").write_text("""
#include <stdio.h>
#include <string.h>
#include "unit.h"

int main(void)
{
	static char raw[2048];
	uint32_t n = 0, i, count;
	situ_msg_t msg;
	situ_view_t view, walked, got;
	situ_blk_fields_index_t idx;

	for (i = 0; i < 30; i++)
		n += (uint32_t)sprintf(raw + n, "X-H%02u: v%02u\\r\\n", i, i);
	n += (uint32_t)sprintf(raw + n, "\\r\\n");

	situ_msg_init(&msg, (uint8_t *)raw, n);
	if (situ_blk_view(&msg, 0, n, &view) != SITU_OK)
		return 1;

	count = situ_blk_fields_count(view);
	if (count != 30u)
		return 2;
	if (situ_blk_fields_index(view, &idx) != SITU_OK || idx.count != count)
		return 3;

	for (i = 0; i < count; i++) {
		if (situ_blk_fields_at(view, i, &walked) != SITU_OK)
			return 4;
		if (situ_blk_fields_indexed(&idx, view, i, &got) != SITU_OK)
			return 5;
		if (got.base != walked.base || got.limit != walked.limit)
			return 6;
	}
	if (situ_blk_fields_indexed(&idx, view, count, &got) != SITU_ERR_BOUNDS)
		return 7;
	return 0;
}
""", encoding="ascii")

	binary = tmp_path / "probe"
	build = subprocess.run(
		[HOST_CC or "cc", *WARNINGS, f"-I{RUNTIME}", f"-I{tmp_path}",
		 str(tmp_path / "probe.c"), str(tmp_path / "unit.c"),
		 str(RUNTIME / "situ.c"), "-o", str(binary)],
		capture_output=True, text=True)
	assert build.returncode == 0, build.stderr

	assert subprocess.run([str(binary)]).returncode == 0


def test_an_uncapped_record_run_says_what_would_buy_an_index() -> None:
	"""HTTP's header block has no `max`, so it gets the note rather than the
	index -- and capping a header count is worth doing on its own account."""
	header, _ = materialized(
		'struct hf { u8 v[] until "\\r\\n"; }\n'
		'struct blk { hf fields[] until "\\r\\n"; }')

	assert "No index for `fields`" in header
	assert "Add `max N`" in header


# -- reaching into a variant's arms (section 9.6) ---------------------------

ARMS = """
struct label {
	u2 form;
	u6 rest;
	variant body switch (form) {
		case 0:  u8 text[rest];
		case 3:  u8 pointer_low;
		default: error;
	}
}
struct name { label labels[] while (form == 0 && rest != 0) max 128; }
"""


def test_an_arm_member_asks_the_discriminant_first() -> None:
	"""A variant's members had no accessors at all: situ could measure one and
	validate its discriminant, and its contents were unreachable.

	Each arm's members are this struct's to emit -- an arm is not a type, so
	there is nowhere else they could go -- and each asks whether its arm is
	the one present. Reading another arm's bytes stays inside the view, so it
	is a wrong answer rather than a fault, which is the kind situ refuses.
	"""
	header, _ = emit(ARMS)

	assert "situ_label_body_text_ptr(situ_view_t view, const uint8_t **out," \
		in header
	assert "if (situ_label_form_get(view) != 0u) {" in header
	assert "if (situ_label_form_get(view) != 3u) {" in header
	assert "return SITU_ERR_VERSION;" in header


@pytest.mark.skipif(HOST_CC is None, reason="no host compiler")
def test_both_arms_read_and_the_wrong_one_refuses(tmp_path: Path) -> None:
	"""A suffix-compressed DNS name: one text label, then a pointer."""
	header, source = emit(ARMS)
	(tmp_path / "unit.h").write_text(header, encoding="ascii")
	(tmp_path / "unit.c").write_text(source, encoding="ascii")
	(tmp_path / "probe.c").write_text("""
#include <string.h>
#include "unit.h"

int main(void)
{
	uint8_t buf[] = { 3, 'w', 'w', 'w', 0xC0, 0x0C };
	situ_msg_t msg;
	situ_view_t view, l;
	const uint8_t *p;
	uint32_t n;
	uint8_t low;

	situ_msg_init(&msg, buf, (uint32_t)sizeof buf);
	if (situ_name_view(&msg, 0, (uint32_t)sizeof buf, &view) != SITU_OK)
		return 1;

	/* Element 0 selects the text arm. */
	if (situ_name_labels_at(view, 0, &l) != SITU_OK)
		return 2;
	if (situ_label_body_text_ptr(l, &p, &n) != SITU_OK)
		return 3;
	if (n != 3u || memcmp(p, "www", 3) != 0)
		return 4;
	if (situ_label_body_pointer_low_get(l, &low) != SITU_ERR_VERSION)
		return 5;

	/* Element 1 selects the pointer arm. */
	if (situ_name_labels_at(view, 1, &l) != SITU_OK)
		return 6;
	if (situ_label_body_pointer_low_get(l, &low) != SITU_OK)
		return 7;
	/* The member's own byte, not the arm's first: 0x0C, not 0xC0. */
	if (low != 0x0Cu)
		return 8;
	if (situ_label_body_text_ptr(l, &p, &n) != SITU_ERR_VERSION)
		return 9;
	return 0;
}
""", encoding="ascii")

	binary = tmp_path / "probe"
	build = subprocess.run(
		[HOST_CC or "cc", *WARNINGS, f"-I{RUNTIME}", f"-I{tmp_path}",
		 str(tmp_path / "probe.c"), str(tmp_path / "unit.c"),
		 str(RUNTIME / "situ.c"), "-o", str(binary)],
		capture_output=True, text=True)
	assert build.returncode == 0, build.stderr

	assert subprocess.run([str(binary)]).returncode == 0


STRUCT_ARMS = """
enum K : u8 { a = 1, b = 2, }
struct A { u16 x; }
struct B { u32 y; }
struct S {
	K k;
	variant v switch (k) {
		case K.a: A p;
		case K.b: B q;
		default:  error;
	}
}
"""


@pytest.mark.skipif(HOST_CC is None, reason="no host compiler")
def test_a_struct_typed_arm_gets_a_guarded_sub_view(tmp_path: Path) -> None:
	"""`case msg_type.hello: Hello hello;` is section 9.6's own example, so
	this is the common shape rather than the exotic one -- and it was the one
	left declined when scalar and byte-array arms landed.

	The arm's own members belong to its type, so a sub-view over it is the
	whole of the work. The guard is the same.
	"""
	header, source = emit(STRUCT_ARMS)
	(tmp_path / "unit.h").write_text(header, encoding="ascii")
	(tmp_path / "unit.c").write_text(source, encoding="ascii")
	(tmp_path / "probe.c").write_text("""
#include "unit.h"

int main(void)
{
	uint8_t buf[8] = { 0 };
	situ_msg_t msg;
	situ_view_t view, arm;

	buf[0] = 1;			/* k = K.a: `p`, an A, is present */
	buf[1] = 0xBE; buf[2] = 0xEF;

	situ_msg_init(&msg, buf, (uint32_t)sizeof buf);
	if (situ_S_view(&msg, 0, (uint32_t)sizeof buf, &view) != SITU_OK)
		return 1;

	if (situ_S_v_p_view(view, &arm) != SITU_OK)
		return 2;
	/* The arm starts after the discriminant and is exactly an A. */
	if (arm.base != buf + 1 || arm.limit != 2u)
		return 3;
	if (situ_A_x_get(arm) != 0xBEEF)
		return 4;
	if (situ_S_v_q_view(view, &arm) != SITU_ERR_VERSION)
		return 5;

	buf[0] = 2;			/* k = K.b: `q`, a B */
	buf[1] = 0xDE; buf[2] = 0xAD; buf[3] = 0xBE; buf[4] = 0xEF;
	if (situ_S_v_q_view(view, &arm) != SITU_OK)
		return 6;
	if (arm.limit != 4u || situ_B_y_get(arm) != 0xDEADBEEFu)
		return 7;
	if (situ_S_v_p_view(view, &arm) != SITU_ERR_VERSION)
		return 8;
	return 0;
}
""", encoding="ascii")

	binary = tmp_path / "probe"
	build = subprocess.run(
		[HOST_CC or "cc", *WARNINGS, f"-I{RUNTIME}", f"-I{tmp_path}",
		 str(tmp_path / "probe.c"), str(tmp_path / "unit.c"),
		 str(RUNTIME / "situ.c"), "-o", str(binary)],
		capture_output=True, text=True)
	assert build.returncode == 0, build.stderr

	assert subprocess.run([str(binary)]).returncode == 0


# -- running the transform once (section 13.5) ------------------------------

CODED_TABLE = """
codec halve { kernel = table(input_bits = 1, output_bits = 2, code = manchester); }
impl halve derived;
struct S {
	coded body(halve) { u8 raw[4]; }
}
"""


def test_a_coded_region_has_its_encoded_bytes() -> None:
	"""A coded region with no delimiter got a comment header and nothing
	else, so the bytes on the wire were unreachable -- a strange thing for a
	treat-as-bytes region. The delimited case has had a pointer all along,
	because the scan path emits one and this path emitted nothing.
	"""
	header, _ = emit("struct S { coded body(halve) { u8 raw[4]; } }",
	                 preamble=PREAMBLE + CODED_TABLE.split("struct")[0])

	assert "situ_S_body_ptr(situ_view_t view)" in header
	assert "situ_S_body_len(situ_view_t view)" in header


@pytest.mark.skipif(HOST_CC is None, reason="no host compiler")
def test_the_decode_runs_once_into_the_callers_buffer(tmp_path: Path) -> None:
	"""The interior of a coded region is `stage = TransformTime`: the
	plaintext is not in the message, so this is the one accessor that needs
	somewhere to put its answer. Nothing allocates, so the buffer is the
	caller's and the bound is a macro beside it.

	Only for a `table` kernel -- the generated decoder's shape is settled
	there, `(in, bits, out) -> bits`, and it is not for the families that are
	described and not yet generated.
	"""
	schema   = parse_text(PREAMBLE + CODED_TABLE)
	resolved = resolve(schema, solve(schema))
	built    = generate(schema, resolved, "unit")

	(tmp_path / "unit.h").write_text(built.header, encoding="ascii")
	(tmp_path / "unit.c").write_text(built.source, encoding="ascii")

	from situc.codegen.c import derived
	(tmp_path / "unit_derived.c").write_text(
		derived.generate(schema, "unit"), encoding="ascii")

	(tmp_path / "probe.c").write_text("""
#include <string.h>
#include "unit.h"

int main(void)
{
	uint8_t plain[4] = { 0xA5, 0x3C, 0xF0, 0x0F };
	uint8_t buf[8];
	uint8_t out[SITU_S_BODY_DECODED_MAX];
	uint32_t len = 0;
	situ_msg_t msg;
	situ_view_t view;

	situ_halve_encode(plain, 32u, buf);
	situ_msg_init(&msg, buf, (uint32_t)sizeof buf);
	if (situ_S_view(&msg, 0, &view) != SITU_OK)
		return 1;

	/* Eight bytes on the wire, four of value: the codec is 2:1. */
	if (situ_S_body_len(view) != 8u)
		return 2;
	if (SITU_S_BODY_DECODED_MAX != 4u)
		return 3;
	if (situ_S_body_decode(view, out, (uint32_t)sizeof out, &len) != SITU_OK)
		return 4;
	if (len != 4u || memcmp(out, plain, 4) != 0)
		return 5;

	/* A buffer one byte short is refused rather than half-filled: half a
	 * decode is not a shorter message. */
	if (situ_S_body_decode(view, out, 3u, &len) != SITU_ERR_BOUNDS)
		return 6;
	return 0;
}
""", encoding="ascii")

	binary = tmp_path / "probe"
	build = subprocess.run(
		[HOST_CC or "cc", *WARNINGS, f"-I{RUNTIME}", f"-I{tmp_path}",
		 str(tmp_path / "probe.c"), str(tmp_path / "unit.c"),
		 str(tmp_path / "unit_derived.c"),
		 str(RUNTIME / "situ.c"), "-o", str(binary)],
		capture_output=True, text=True)
	assert build.returncode == 0, build.stderr

	assert subprocess.run([str(binary)]).returncode == 0


STUFFED = ('struct S { coded body(stuff) until "\\r\\n" '
	'{ u8 content[remaining]; } }')
STUFF_PREAMBLE = (PREAMBLE + "codec stuff { kernel = stuffing(worst_case = 4,"
	" per = 3, unit = stream, code = smtp_dot); }\n"
	"impl stuff derived;\n")


def test_a_stuffing_kernel_gets_a_decode_accessor() -> None:
	"""It got none, on the argument that the decoder's shape is settled only
	for `table` because the other families were described and not generated.
	They were generated; the note had not noticed."""
	header, _ = emit(STUFFED, preamble=STUFF_PREAMBLE)

	assert "situ_S_body_decode(situ_view_t view, uint8_t *out," in header
	assert "situ_stuff_decode(situ_S_body_ptr(view)," in header
	# The note above it has to agree with what follows: it said "there is no
	# accessor for the decoded bytes" whatever came after, and became a
	# contradiction sitting directly on top of one.
	assert "The decoded bytes are below" in header
	assert "There is no accessor for the decoded bytes" not in header


def test_a_byte_kernel_is_handed_bytes_and_a_bit_kernel_bits() -> None:
	"""`unit` decides, and getting it wrong passes a byte count to a bit loop
	and decodes an eighth of the region. HDLC counts bits where COBS scans
	bytes, and both are `stuffing`."""
	stream, _ = emit(STUFFED, preamble=STUFF_PREAMBLE)
	bitwise, _ = emit(
		STUFFED,
		preamble=PREAMBLE + "codec stuff { kernel = stuffing(worst_case = 6,"
		                    " per = 5, unit = bit, code = hdlc); }\n"
		                    "impl stuff derived;\n")

	assert "encoded, out);" in stream
	assert "uint32_t len, uint8_t *out);" in stream
	assert "encoded * 8u, out) / 8u;" in bitwise
	assert "uint32_t bits, uint8_t *out);" in bitwise


def test_the_decode_runs_over_the_content_and_not_the_delimiter() -> None:
	"""`_span` includes the delimiter and `_len` does not. Decoding the span
	put SMTP's `CRLF . CRLF` through the unstuffer, which nothing caught while
	the accessor was emitted for `table` kernels alone and no delimited region
	used one."""
	header, _ = emit(STUFFED, preamble=STUFF_PREAMBLE)

	assert "const uint32_t encoded = situ_S_body_len(view);" in header
	assert "situ_S_body_span(view);" not in header


def test_a_stuffing_code_with_no_implementation_gets_no_decode() -> None:
	"""The family's shape is settled; a named code nobody generates still has
	no function to call."""
	header, _ = emit(
		STUFFED,
		preamble=PREAMBLE + "codec stuff { kernel = stuffing(worst_case = 2,"
		                    " per = 1, unit = stream, code = nonesuch); }\n"
		                    "impl stuff derived;\n")

	assert "situ_S_body_decode" not in header
	assert "There is no accessor for the decoded bytes" in header


# -- tlv regions (section 9.5) ----------------------------------------------

TLV_PREAMBLE = (PREAMBLE
	+ "varint_type pb_varint { encoding = leb128; max_bits = 64; }\n")

TLV = """struct S {
	tlv fields (
		tag_type     = pb_varint,
		tag_decode   = { field = tag >> 3, wire = tag & 0x7 },
		tag_identity = field,
		value_size   = switch (wire) {
			case 0: self_delimiting,
			case 1: 8,
			case 2: prefixed(pb_varint),
			case 5: 4,
			default: error,
		},
		known = {
			1 : { name = user_id, wire = 0, type = pb_varint },
			2 : { name = label,   wire = 2, type = u8 },
		},
		unknown = preserve
	);
}"""


def test_a_tlv_region_gets_a_walk() -> None:
	"""It got nothing at all before: the region fell through every branch to
	the scalar accessors, which emit nothing for a placement with no scalar.
	So the one construct the language exists to describe -- section 9.7 makes
	protobuf the conformance gate -- was described and unreadable."""
	header, _ = emit(TLV, preamble=TLV_PREAMBLE)

	assert "situ_S_fields_first" in header
	assert "situ_S_fields_next" in header
	assert "situ_S_fields_count" in header


def test_the_decoded_parts_are_named_by_the_schema() -> None:
	"""`field` and `wire` are this schema's words. A backend inventing its own
	would be describing protobuf rather than the region in front of it."""
	header, _ = emit(TLV, preamble=TLV_PREAMBLE)

	assert "uint32_t    field;" in header
	assert "uint32_t    wire;" in header
	assert "out->field = (uint32_t)(tag >> 3);" in header
	assert "out->wire  = (uint32_t)(tag & 0x7);" in header


def test_each_wire_type_is_sized_as_the_dispatch_says() -> None:
	header, _ = emit(TLV, preamble=TLV_PREAMBLE)

	assert "switch (out->wire) {" in header
	assert "case 1u:" in header and "size = 8u;" in header
	assert "case 5u:" in header and "size = 4u;" in header


def test_a_refused_wire_type_stops_the_walk() -> None:
	"""`default: error` is a rejection rather than a gap: protobuf's groups
	have no extent this schema can compute, so guessing one would walk into
	the middle of an item."""
	header, _ = emit(TLV, preamble=TLV_PREAMBLE)

	assert "return SITU_ERR_CONSTRAINT;" in header


def test_each_known_tag_gets_an_accessor() -> None:
	header, _ = emit(TLV, preamble=TLV_PREAMBLE)

	assert "situ_S_fields_user_id(situ_view_t view" in header
	assert "situ_S_fields_label(situ_view_t view" in header
	assert "situ_S_fields_find(view, 1u, item)" in header


def test_by_name_accessors_match_the_identity_part() -> None:
	"""Decision 0023. Matching `wire` where `field` was meant finds an item
	and not the one asked for, which nothing about the message would say."""
	header, _ = emit(TLV, preamble=TLV_PREAMBLE)

	assert "if (item->field == tag) {" in header


def test_the_tag_width_comes_from_the_varint_type() -> None:
	"""Not the 10 bytes a 64-bit leb128 happens to need: a schema that bounds
	its tags at 16 bits gets a bound the walk can use."""
	narrow = TLV.replace("tag_type     = pb_varint", "tag_type     = small")
	header, _ = emit(narrow, preamble=TLV_PREAMBLE
		+ "varint_type small { encoding = leb128; max_bits = 16; }\n")

	assert "view.limit - at, 3u, &tag)" in header


def test_a_region_that_does_not_size_its_values_says_so() -> None:
	"""A grammar with no `value_size` and no `length_type` has nowhere to put
	the second item."""
	header, _ = emit("""struct S {
		tlv fields (
			tag_type   = pb_varint,
			tag_decode = { field = tag >> 3 },
			unknown    = error
		);
	}""", preamble=TLV_PREAMBLE)

	assert "No accessors for `fields`" in header
	assert "not how long their values are" in header


def test_the_simple_form_sizes_every_value_the_same_way() -> None:
	"""`length_type = u8` and no dispatch: there is nothing to switch on."""
	header, _ = emit("""struct S {
		tlv opts (
			tag_type    = u8,
			length_type = u8,
			known       = { 1 : mtu, 2 : window },
			unknown     = error
		);
	}""", preamble=TLV_PREAMBLE)

	assert "switch (out->" not in header
	assert "situ_S_opts_mtu" in header
	# No `tag_decode`, so a `known` key is the raw tag itself.
	assert "if ((uint32_t)item->tag == tag) {" in header


def test_the_generated_walk_compiles(tmp_path: Path) -> None:
	compile_generated(tmp_path, TLV, preamble=TLV_PREAMBLE)


def test_the_generated_walk_reads_protoc_output(tmp_path: Path) -> None:
	"""The vectors are the ones in tests/generated/test_protobuf.c, which came
	out of protoc. A description that agrees only with its own compiler has
	demonstrated nothing."""
	if HOST_CC is None:
		pytest.skip("no C compiler")

	header, source = emit(TLV, preamble=TLV_PREAMBLE)
	(tmp_path / "unit.h").write_text(header, encoding="ascii")
	(tmp_path / "unit.c").write_text(source, encoding="ascii")
	(tmp_path / "probe.c").write_text("""
#include <string.h>
#include "unit.h"

/* protoc --encode=User <<< 'user_id: 150; username: "situ"' */
static const uint8_t WIRE[] = {
	0x08, 0x96, 0x01,
	0x12, 0x04, 0x73, 0x69, 0x74, 0x75,
};

int main(void)
{
	uint8_t buf[sizeof(WIRE)];
	situ_msg_t msg;
	situ_view_t view;
	situ_S_fields_item_t item;
	uint64_t user_id = 0;

	memcpy(buf, WIRE, sizeof(WIRE));
	situ_msg_init(&msg, buf, sizeof(buf));
	if (situ_view_at(&msg, 0, sizeof(WIRE), &view) != SITU_OK) return 1;

	if (situ_S_fields_count(view) != 2u) return 2;

	if (situ_S_fields_user_id(view, &item) != SITU_OK) return 3;
	if (item.wire != 0u) return 4;
	situ_varint_get(view.base + item.value_at, item.value_len, 10u, &user_id);
	if (user_id != 150u) return 5;

	if (situ_S_fields_label(view, &item) != SITU_OK) return 6;
	if (item.wire != 2u || item.value_len != 4u) return 7;
	if (memcmp(view.base + item.value_at, "situ", 4) != 0) return 8;

	return 0;
}
""", encoding="ascii")

	binary = tmp_path / "probe"
	build = subprocess.run(
		[HOST_CC, *WARNINGS, f"-I{RUNTIME}", f"-I{tmp_path}",
		 str(tmp_path / "probe.c"), str(tmp_path / "unit.c"),
		 str(RUNTIME / "situ.c"), "-o", str(binary)],
		capture_output=True, text=True)
	assert build.returncode == 0, build.stderr

	assert subprocess.run([str(binary)]).returncode == 0


@pytest.mark.skipif(HOST_CC is None, reason="no host compiler")
def test_the_index_reaches_elements_in_any_order(tmp_path: Path) -> None:
	"""The whole point of the table: element N is one read plus a base, and
	the elements need not sit in the order the table names them. A walk that
	happened to work on a table in ascending order would prove nothing."""
	header, source = emit("struct R { u32 id; u16 kind; }"
	                      "struct V { u16 len; u8 body[len]; }"
	                      "struct S { u16 n; indexed(offset_type = u16,"
	                      " count = n) { R fixed[]; } }"
	                      "struct T { u16 n; indexed(offset_type = u16,"
	                      " count = n) { V varying[]; } }")
	(tmp_path / "unit.h").write_text(header, encoding="ascii")
	(tmp_path / "unit.c").write_text(source, encoding="ascii")
	(tmp_path / "probe.c").write_text("""
#include <string.h>
#include "unit.h"

/* Offsets deliberately out of order, and measured from the region start. */
static const uint8_t S_BYTES[] = {
	0x00, 0x03,
	0x00, 0x12, 0x00, 0x06, 0x00, 0x0C,
	0x00, 0x00, 0x00, 0xBB, 0x00, 0x02,
	0x00, 0x00, 0x00, 0xCC, 0x00, 0x03,
	0x00, 0x00, 0x00, 0xAA, 0x00, 0x01,
};

/* Two elements of different sizes, which is what the table is paying for. */
static const uint8_t T_BYTES[] = {
	0x00, 0x02,
	0x00, 0x04, 0x00, 0x0B,
	0x00, 0x05, 'h', 'e', 'l', 'l', 'o',
	0x00, 0x02, 'h', 'i',
};

int main(void)
{
	uint8_t buf[64];
	situ_msg_t msg;
	situ_view_t view, elem;

	memcpy(buf, S_BYTES, sizeof(S_BYTES));
	situ_msg_init(&msg, buf, sizeof(S_BYTES));
	if (situ_S_view(&msg, 0, sizeof(S_BYTES), &view) != SITU_OK) return 1;
	if (situ_S_fixed_count(view) != 3u) return 2;

	if (situ_S_fixed_at(view, 0, &elem) != SITU_OK) return 3;
	if (situ_R_id_get(elem) != 170u || situ_R_kind_get(elem) != 1u) return 4;
	if (situ_S_fixed_at(view, 1, &elem) != SITU_OK) return 5;
	if (situ_R_id_get(elem) != 187u) return 6;
	if (situ_S_fixed_at(view, 2, &elem) != SITU_OK) return 7;
	if (situ_R_id_get(elem) != 204u) return 8;
	if (situ_S_fixed_at(view, 3, &elem) != SITU_ERR_BOUNDS) return 9;

	memcpy(buf, T_BYTES, sizeof(T_BYTES));
	situ_msg_init(&msg, buf, sizeof(T_BYTES));
	if (situ_T_view(&msg, 0, sizeof(T_BYTES), &view) != SITU_OK) return 10;

	/* Each element is narrowed to its own extent, not to the rest. */
	if (situ_T_varying_at(view, 0, &elem) != SITU_OK) return 11;
	if (elem.limit != 7u) return 12;
	if (memcmp(situ_V_body_ptr(elem), "hello", 5) != 0) return 13;

	if (situ_T_varying_at(view, 1, &elem) != SITU_OK) return 14;
	if (elem.limit != 4u) return 15;
	if (memcmp(situ_V_body_ptr(elem), "hi", 2) != 0) return 16;

	return 0;
}
""", encoding="ascii")

	binary = tmp_path / "probe"
	build = subprocess.run(
		[HOST_CC or "cc", *WARNINGS, f"-I{RUNTIME}", f"-I{tmp_path}",
		 str(tmp_path / "probe.c"), str(tmp_path / "unit.c"),
		 str(RUNTIME / "situ.c"), "-o", str(binary)],
		capture_output=True, text=True)
	assert build.returncode == 0, build.stderr

	assert subprocess.run([str(binary)]).returncode == 0


BE128 = ("varint_type sq { encoding = be128; max_bits = 64; max_bytes = 9; }")


def test_a_be128_field_uses_the_big_endian_reader() -> None:
	"""The groups come from the other end, so decoding one as leb128 gives a
	plausible number and not the one on the wire."""
	header, _ = emit(BE128 + "struct S { sq n; }")

	assert "situ_varint_be_get(view.base + at, view.limit - at, 9u, 8u, &raw)" \
		in header


def test_the_nine_byte_form_is_what_max_bytes_produces() -> None:
	"""`max_bytes = 9` with `max_bits = 64` leaves eight bits for the last
	byte, and eight bits leaves no room for a continuation flag. SQLite's ninth
	byte falls out of the arithmetic rather than being a second flag."""
	header, _ = emit(BE128 + "struct S { sq n; }")

	assert ", 9u, 8u, &raw)" in header


def test_a_be128_that_fits_seven_bit_groups_has_an_ordinary_last_byte() -> None:
	header, _ = emit("varint_type t { encoding = be128; max_bits = 32;"
	                 " max_bytes = 5; }struct S { t n; }")

	assert ", 5u, 4u, &raw)" in header


def test_a_type_too_narrow_for_its_bits_is_refused() -> None:
	with pytest.raises(SituError) as caught:
		emit("varint_type t { encoding = be128; max_bits = 64; max_bytes = 8; }"
		     "struct S { t n; }")

	report = caught.value.diagnostic.render()
	assert "cannot hold 64 bits in 8 bytes" in report
	assert "the last byte would have to carry 15" in report


def test_a_type_with_a_byte_it_cannot_reach_is_refused() -> None:
	with pytest.raises(SituError) as caught:
		emit("varint_type t { encoding = be128; max_bits = 8; max_bytes = 4; }"
		     "struct S { t n; }")

	report = caught.value.diagnostic.render()
	assert "declares more bytes than 8 bits can fill" in report


@pytest.mark.skipif(HOST_CC is None, reason="no host compiler")
def test_a_be128_field_reads_what_sqlite_wrote(tmp_path: Path) -> None:
	"""The nine-byte form against rowids sqlite3 produced. 2^56-1 is the
	longest eight-byte value and 2^60-1 needs the ninth byte, which is the
	boundary distinguishing this encoding from every other base-128."""
	header, source = emit(
		BE128 + "struct cell { sq payload_size; sq rowid; u8 payload[payload_size]; }")
	(tmp_path / "unit.h").write_text(header, encoding="ascii")
	(tmp_path / "unit.c").write_text(source, encoding="ascii")
	(tmp_path / "probe.c").write_text("""
#include <string.h>
#include "unit.h"

static int check(const uint8_t *bytes, uint32_t len, uint64_t want)
{
	uint8_t buf[32];
	situ_msg_t msg;
	situ_view_t view;
	uint64_t rowid = 0;

	memcpy(buf, bytes, len);
	situ_msg_init(&msg, buf, len);
	if (situ_cell_view(&msg, 0, len, &view) != SITU_OK) return 1;
	if (situ_cell_rowid_get(view, &rowid) != SITU_OK) return 2;
	return rowid == want ? 0 : 3;
}

int main(void)
{
	/* sqlite3, rowid 1: 07 01 then the record */
	static const uint8_t SMALL[] = { 0x07, 0x01, 0x02, 0x17, 'a','l','p','h','a' };
	/* sqlite3, rowid 2^56-1: eight bytes */
	static const uint8_t EIGHT[] = { 0x03, 0xFF,0xFF,0xFF,0xFF,0xFF,0xFF,0xFF,0x7F,
	                                 0x02, 0x0F, 'x' };
	/* sqlite3, rowid 2^60-1: nine, the last carrying all eight of its bits */
	static const uint8_t NINE[] = { 0x03, 0x87,0xFF,0xFF,0xFF,0xFF,0xFF,0xFF,0xFF,0xFF,
	                                0x02, 0x0F, 'y' };

	if (check(SMALL, sizeof(SMALL), 1u)) return 1;
	if (check(EIGHT, sizeof(EIGHT), 72057594037927935ULL)) return 2;
	if (check(NINE, sizeof(NINE), 1152921504606846975ULL)) return 3;
	return 0;
}
""", encoding="ascii")

	binary = tmp_path / "probe"
	build = subprocess.run(
		[HOST_CC or "cc", *WARNINGS, f"-I{RUNTIME}", f"-I{tmp_path}",
		 str(tmp_path / "probe.c"), str(tmp_path / "unit.c"),
		 str(RUNTIME / "situ.c"), "-o", str(binary)],
		capture_output=True, text=True)
	assert build.returncode == 0, build.stderr

	assert subprocess.run([str(binary)]).returncode == 0


def test_a_struct_is_emitted_before_the_one_that_reaches_into_it() -> None:
	"""C emitted in the solver's insertion order, on the argument that the
	solver resolves dependencies before dependents. An `indexed` element is not
	a layout dependency, so the first schema declaring one after its container
	produced a header calling an `extent` defined below it."""
	header, _ = emit("struct outer { u16 n;"
	                 " indexed(offset_type = u16, count = n) { inner e[]; } }"
	                 "struct inner { u16 len; u8 body[len]; }")

	assert header.index("situ_inner_extent(situ_view_t") \
		< header.index("situ_inner_extent(probe)")
