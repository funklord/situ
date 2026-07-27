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

ROOT     = Path(__file__).resolve().parents[2]
RUNTIME  = ROOT / "runtime" / "c"
SCHEMAS  = ROOT / "tests" / "schemas"
EXAMPLES = ROOT / "examples"

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
def test_every_buildable_example_generates_and_compiles(tmp_path: Path) -> None:
	"""The examples are the broadest codegen coverage available."""
	compiled = 0
	for path in sorted(EXAMPLES.glob("*/*.situ")):
		if "STATUS: needs phase" in path.read_text(encoding="ascii"):
			continue

		source    = Source(str(path), path.read_text(encoding="ascii"))
		schema    = parse(source)
		resolved  = resolve(schema, solve(schema))
		generated = generate(schema, resolved, path.stem)

		out = tmp_path / path.stem
		out.mkdir()
		for name, text in generated.files().items():
			(out / name).write_text(text, encoding="ascii")

		result = subprocess.run(
			[HOST_CC or "cc", *WARNINGS, f"-I{RUNTIME}", f"-I{out}",
			 "-c", str(out / f"{path.stem}.c"), "-o", str(out / "out.o")],
			capture_output=True, text=True)
		assert result.returncode == 0, f"{path.parent.name}:\n{result.stderr}"
		compiled += 1

	assert compiled >= 8


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
	assert "view.limit - 1u" in header


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


def test_a_varint_gets_no_accessor() -> None:
	"""Emitting one would pretend the width is fixed."""
	header, _ = emit("varint_type v { encoding = leb128; max_bits = 64; minimal; }"
	                 "struct S { u16 a; v n; }")
	assert "No accessor" in header
	assert "situ_S_n_get" not in header


def test_a_variant_gets_no_single_accessor() -> None:
	header, _ = emit("enum K : u8 { a = 1, b = 2, }"
	                 "struct A { u16 x; } struct B { u32 y; }"
	                 "struct S { K k; variant v switch (k) "
	                 "{ case K.a: A p; case K.b: B q; } }")
	assert "exactly one of" in header
	assert "read the discriminant and take the matching" in header


def test_an_indexed_region_says_insertion_is_not_an_operation() -> None:
	header, _ = emit("struct R { u32 id; }"
	                 "struct S { u16 n; indexed(offset_type = u16, count = n) "
	                 "{ R entries[]; } }")
	assert "reached through an offset table" in header
	assert "Insertion is not an operation here at all" in header


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
