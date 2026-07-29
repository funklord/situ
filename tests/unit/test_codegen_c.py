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

	assert "(3u + view.limit - 5u)" in header


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
	assert "situ_scan(view.base + 4u" in header


def test_the_delimiter_is_part_of_the_span_and_not_the_content() -> None:
	"""Members partition their struct's bytes exactly, so a delimiter nobody
	owned would be a hole between two members -- the same reason
	`nul_terminated` counts its capacity."""
	header, _ = emit(FRAMED)

	assert "situ_s_line_len(view) + (situ_s_line_terminated(view) ? 2u : 0u)" in header


def test_a_missing_delimiter_adds_nothing_to_the_span() -> None:
	"""The member ran to the end of the buffer. Claiming the delimiter's bytes
	anyway would put the next member past the limit its own bounds check
	trusts, which is a read outside the view the caller established."""
	header, _ = emit(FRAMED)

	assert "situ_s_line_terminated(view) ? 2u : 0u" in header


def test_a_later_members_offset_sums_the_scan() -> None:
	"""Not an inlined search: the member emits its own `_span` and everything
	downstream calls it, so one scan is described in one place."""
	header, _ = emit(FRAMED)

	assert "situ_s_count_offset" in header
	assert "offset = offset + (situ_s_line_span(view));" in header


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

	assert "situ_min_u32(16u, view.limit - 0u)" in header


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
