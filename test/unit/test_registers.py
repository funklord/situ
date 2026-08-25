"""The MMIO target (project.md section 15).

The other half of the thesis: SystemRDL and CMSIS-SVD already had a vocabulary
for access modes and side effects, and what nobody did was unify it with
wire-protocol description so that one lattice answers both. These tests are
mostly about that unification holding -- a register is a struct to the solver
and to the capability map, and only the backend knows it is emitting bus
transactions.

The headline is 15.3: `access_width = 32` plus a one-bit field means a
single-bit write needs a read-modify-write, `no_rmw` says that read is unsafe,
and together they remove the setter. Setting one bit becomes a compile error
rather than a runtime hazard, and the header says why.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from situc import requirements
from situc.capability import Axis, Value
from situc.codegen.c import generate
from situc.diagnostics import SituError
from situc.dump import dump
from situc.layout import solve
from situc.parser import parse_text
from situc.propagate import Resolved
from situc.resolve import ResolvedSchema, resolve
from situc.unparse import unparse

ROOT     = Path(__file__).resolve().parents[2]
RUNTIME  = ROOT / "runtime" / "c"
HOST_CC  = shutil.which("gcc") or shutil.which("cc")
OBJDUMP  = shutil.which("objdump")

WARNINGS = ["-std=c11", "-O2", "-Wall", "-Wextra", "-Werror",
	"-Wconversion", "-Wsign-conversion"]

PREAMBLE = "target mmio;\nendian little;\nbit_order lsb_first;\n"

CTRL = """register ctrl @ 0x00 {
	width        = 32;
	access_width = 32;
	volatile;
	no_rmw;

	bit  enable  [rw];
	bit  start   [wo, on_write = trigger];
	u3   mode    [rw];
	bit  busy    [ro];
	bit  error   [w1c];
	reserved u25 [preserve];
}
"""


def build(body: str, preamble: str = PREAMBLE) -> ResolvedSchema:
	schema = parse_text(preamble + body)
	return resolve(schema, solve(schema))


def entries(body: str, preamble: str = PREAMBLE) -> dict[str, Resolved]:
	return {entry.placement.path: entry
	        for struct in build(body, preamble).structs.values()
	        for entry in struct.entries}


def header(body: str, preamble: str = PREAMBLE) -> str:
	schema   = parse_text(preamble + body)
	resolved = resolve(schema, solve(schema))
	return generate(schema, resolved, "unit").header


def refusal(body: str, preamble: str = PREAMBLE) -> str:
	with pytest.raises(SituError) as caught:
		build(body, preamble)
	return caught.value.diagnostic.render()


# -- the front end ----------------------------------------------------------


def test_a_register_is_a_struct_to_everything_but_the_backend() -> None:
	"""Which is the whole reason one lattice answers registers and protocols."""
	resolved = build(CTRL)
	struct   = resolved.find_struct("ctrl")

	assert struct is not None
	assert struct.layout.size_bytes == 4
	assert struct.layout.register is not None
	assert struct.layout.register.access_width == 32


def test_fields_are_bit_ranges_within_the_word() -> None:
	held = entries(CTRL)
	assert held["ctrl.enable"].placement.offset_bits == 0
	assert held["ctrl.mode"].placement.offset_bits == 2
	assert held["ctrl.mode"].placement.size_bits == 3


def test_a_whole_byte_field_may_start_mid_word() -> None:
	"""A register is one access, so the byte is not the unit inside it.

	The buffer rules would refuse a `u8` three bits into a byte; here it is an
	ordinary bit range, and SystemRDL descriptions are full of them.
	"""
	held = entries("""register status @ 0x00 {
		width = 32; access_width = 32;
		bit ready [ro];
		bit fifo_empty [ro];
		bit fifo_full [ro];
		u8  fill_level [ro];
		reserved u21;
	}
	""")
	assert held["status.fill_level"].placement.offset_bits == 3


def test_a_register_needs_a_width_and_an_access_width() -> None:
	assert "declares no `width`" in refusal(
		"register r @ 0 { access_width = 32; bit a [rw]; }")
	assert "declares no `access_width`" in refusal(
		"register r @ 0 { width = 32; bit a [rw]; }")


def test_a_register_block_declares_defaults_once() -> None:
	held = entries("""register_block dma {
		width        = 32;
		access_width = 32;
		no_rmw;

		register src @ 0x00 { u32 addr [rw]; }
		register dst @ 0x04 { u32 addr [rw]; }
	}
	""")
	assert "src.addr" in held and "dst.addr" in held


def test_a_register_needs_the_mmio_target() -> None:
	rendered = refusal(
		"register r @ 0 { width = 32; access_width = 32; bit a [rw]; }",
		preamble="target buffer;\n")
	assert "needs `target mmio`" in rendered
	assert "a bus transaction, not bytes in a buffer" in rendered


def test_a_schema_may_not_mix_the_two_targets() -> None:
	"""15.1: the API looks the same and the codegen is entirely different."""
	rendered = refusal(CTRL + "struct payload { u32 x; }\n")
	assert "is a buffer layout under `target mmio`" in rendered
	assert "may not mix the two targets" in rendered


def test_two_access_modes_on_one_field_are_refused() -> None:
	rendered = refusal("register r @ 0 { width = 32; access_width = 32; "
		"bit a [rw, ro]; reserved u31; }")
	assert "access mode `ro` is declared more than once" in rendered


def test_a_side_effect_on_an_impossible_access_is_refused() -> None:
	rendered = refusal("register r @ 0 { width = 32; access_width = 32; "
		"bit a [ro, on_write = trigger]; reserved u31; }")
	assert "is `ro` but declares `on_write`" in rendered
	assert "an access that cannot happen" in rendered


def test_a_register_field_may_not_be_an_array() -> None:
	rendered = refusal("register r @ 0 { width = 32; access_width = 32; "
		"u8 bytes[4] [rw]; }")
	assert "may not be an array" in rendered
	assert "declare several registers" in rendered


def test_registers_round_trip() -> None:
	first = parse_text(PREAMBLE + CTRL)
	again = parse_text(unparse(first))
	assert dump(again) == dump(first)


# -- the capability interactions of 15.3 ------------------------------------


def test_a_partial_field_with_unsafe_reads_loses_its_setter() -> None:
	"""The headline. Either fact alone would have been fine."""
	entry = entries(CTRL)["ctrl.enable"]

	assert entry.vector.get(Axis.MUTATE) == Value("RewriteRequired")
	assert [w.rule.name for w in entry.blame(Axis.MUTATE)] == ["register-rmw-unsafe"]


def test_the_diagnostic_names_both_facts_that_combined() -> None:
	"""A message naming one would send the reader to change the wrong thing."""
	schema   = parse_text(PREAMBLE + CTRL + "require in_place(ctrl.enable);\n")
	resolved = resolve(schema, solve(schema))

	with pytest.raises(SituError) as caught:
		requirements.discharge(schema, resolved)

	rendered = caught.value.diagnostic.render()
	assert "the field is 1 of 32 bits" in rendered
	assert "would need a read-modify-write" in rendered
	assert "`no_rmw` is declared" in rendered
	assert "compose the whole word and write it once" in rendered


def test_a_full_width_field_keeps_its_setter() -> None:
	"""Nothing needs reading first when the field is the whole access."""
	entry = entries("""register r @ 0 {
		width = 32; access_width = 32; no_rmw;
		u32 word [rw];
	}
	""")["r.word"]
	assert entry.vector.get(Axis.MUTATE) == Value("InPlaceFixed")


def test_a_partial_field_without_no_rmw_keeps_its_setter() -> None:
	"""A read-modify-write is ordinary when the read is free."""
	entry = entries("""register r @ 0 {
		width = 32; access_width = 32;
		bit a [rw];
		reserved u31;
	}
	""")["r.a"]
	assert entry.vector.get(Axis.MUTATE) == Value("InPlaceFixed")


def test_a_destructive_read_is_as_disqualifying_as_no_rmw() -> None:
	entry = entries("""register r @ 0 {
		width = 32; access_width = 32;
		bit a [rw, on_read = pop];
		reserved u31;
	}
	""")["r.a"]

	assert entry.vector.get(Axis.MUTATE) == Value("RewriteRequired")
	assert "`on_read = pop` makes the read destructive" in \
		entry.blame(Axis.MUTATE)[0].effect.because


def test_a_read_only_field_is_immutable() -> None:
	assert entries(CTRL)["ctrl.busy"].vector.get(Axis.MUTATE) == Value("Immutable")


def test_side_effects_reach_the_effect_axis() -> None:
	held = entries(CTRL)
	assert held["ctrl.start"].vector.get(Axis.EFFECT) == Value("EffectOnWrite")

	both = entries("""register r @ 0 {
		width = 32; access_width = 32;
		bit a [rw, on_read = clear, on_write = trigger];
		reserved u31;
	}
	""")["r.a"]
	assert both.vector.get(Axis.EFFECT) == Value("EffectBoth")


def test_a_partial_field_is_not_the_memory() -> None:
	"""It is shifted and masked out of a bus word, whatever its width."""
	held = entries(CTRL)
	assert held["ctrl.mode"].vector.get(Axis.REPR) == Value("ValueConverted")
	assert held["ctrl.mode"].vector.get(Axis.ATOMIC) == Value("NonAtomic")


def test_the_register_shows_its_address_in_the_map() -> None:
	"""What decides every mutate value under it belongs in the map."""
	from situc import capmap

	rendered = capmap.render(parse_text(PREAMBLE + CTRL), build(CTRL), "r.situ")
	assert "register ctrl @ 0x00 access_width=32 no_rmw" in rendered


# -- the generated API (15.3) -----------------------------------------------


def test_there_is_no_set_enable_and_the_header_says_why() -> None:
	"""Section 26.10's acceptance criterion, verbatim."""
	generated = header(CTRL)

	assert "situ_ctrl_enable_set" not in generated
	assert "No enable_set(): mutate is RewriteRequired" in generated
	assert "Compose a word with the function below and write it once" in generated
	assert "situ_ctrl_enable_with(uint32_t word, uint32_t value)" in generated


def test_w1c_generates_a_clear_rather_than_a_setter() -> None:
	generated = header(CTRL)

	assert "situ_ctrl_error_clear(volatile uint8_t *block)" in generated
	assert "situ_ctrl_error_set" not in generated
	assert "the write is not an assignment" in generated


def test_read_only_and_write_only_are_asymmetric() -> None:
	generated = header(CTRL)

	# `ro`: a getter and no writer of any kind.
	assert "situ_ctrl_busy_get(uint32_t word)" in generated
	assert "situ_ctrl_busy_with" not in generated
	assert "No setter: `busy` is `ro`" in generated

	# `wo`: a writer and no getter.
	assert "situ_ctrl_start_get" not in generated
	assert "No getter: `start` is `wo`" in generated
	assert "situ_ctrl_start_with(uint32_t word, uint32_t value)" in generated


def test_a_getter_decodes_a_word_rather_than_reading_the_register() -> None:
	"""A read is an event, so it happens once and is decoded many times.

	With `on_read = pop` an API that read per field would drain a FIFO to
	decode a status word, so this is correctness rather than performance.
	"""
	generated = header(CTRL)
	assert "situ_ctrl_enable_get(uint32_t word)" in generated
	assert "situ_ctrl_enable_get(volatile" not in generated


def test_a_trigger_is_named_for_what_it_does() -> None:
	generated = header(CTRL)
	assert "situ_ctrl_start_trigger(volatile uint8_t *block)" in generated


def test_the_register_access_is_volatile() -> None:
	generated = header(CTRL)
	assert "volatile uint32_t *situ_ctrl_at(volatile uint8_t *block)" in generated
	assert "`volatile` is not decoration here" in generated


def test_a_read_only_register_gets_no_write() -> None:
	generated = header("""register status @ 0 {
		width = 32; access_width = 32;
		bit ready [ro];
		reserved u31;
	}
	""")
	assert "void situ_status_write(" not in generated
	assert "No situ_status_write(): every field in this" in generated


@pytest.mark.skipif(HOST_CC is None, reason="no host compiler")
def test_the_generated_register_api_compiles_warning_clean(tmp_path: Path) -> None:
	generated = header(CTRL)
	(tmp_path / "unit.h").write_text(generated, encoding="ascii")
	(tmp_path / "probe.c").write_text('#include "unit.h"\n', encoding="ascii")

	result = subprocess.run(
		[HOST_CC or "cc", *WARNINGS, f"-I{RUNTIME}", f"-I{tmp_path}", "-c",
		 str(tmp_path / "probe.c"), "-o", str(tmp_path / "probe.o")],
		capture_output=True, text=True)
	assert result.returncode == 0, result.stderr


@pytest.mark.skipif(HOST_CC is None or OBJDUMP is None, reason="no toolchain")
def test_volatile_reads_are_not_cached(tmp_path: Path) -> None:
	"""Section 26.10 asks for this by disassembly, and it is worth it.

	If the compiler were allowed to cache the first read, a status poll would
	spin forever on a stale word. Two reads in the source must be two loads in
	the object.
	"""
	(tmp_path / "unit.h").write_text(header(CTRL), encoding="ascii")
	(tmp_path / "probe.c").write_text(
		'#include "unit.h"\n'
		"uint32_t poll_twice(volatile uint8_t *block);\n"
		"uint32_t poll_twice(volatile uint8_t *block)\n"
		"{\n"
		"\tuint32_t a = situ_ctrl_read(block);\n"
		"\tuint32_t b = situ_ctrl_read(block);\n"
		"\treturn a + b;\n"
		"}\n", encoding="ascii")

	subprocess.run(
		[HOST_CC or "cc", *WARNINGS, f"-I{RUNTIME}", f"-I{tmp_path}", "-c",
		 str(tmp_path / "probe.c"), "-o", str(tmp_path / "probe.o")],
		check=True, capture_output=True, text=True)

	dumped = subprocess.run(
		[OBJDUMP or "objdump", "-d", "--no-show-raw-insn", str(tmp_path / "probe.o")],
		check=True, capture_output=True, text=True).stdout

	body  = dumped.partition("<poll_twice>:")[2].partition("ret")[0]
	loads = [line for line in body.splitlines() if "mov" in line and "(%r" in line]
	assert len(loads) == 2, f"a volatile read was elided:\n{body}"
