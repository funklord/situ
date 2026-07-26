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
from situc.diagnostics import Source
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
