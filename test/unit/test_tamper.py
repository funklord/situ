"""The tamper harness: watch the gate refuse (26.131, suggestion/netcfgd.md).

The adopter who chose situ for the verify gate also holds that a gate nobody
has watched fail is not evidence. The harness generates the watching: every
covered byte flipped one at a time, the caller's verifier required to refuse
each, and -- for a fixed layout -- every uncovered byte flipped and required
to change nothing.

The executable test here is its own control, and it needs one per direction
because the harness makes two claims. An honest XOR verifier survives every
flip. A lying one that ignores the last covered byte is caught with
`failed_at` naming that byte's exact offset -- that is the covered half. A
greedy one that notices a byte outside every covered span is caught at that
byte -- that is the "and only those" half, which for a long time was
asserted only as a string in the generated text and never run against a
verifier it should catch. A harness only the honest half exercised could
pass vacuously; each liar is what proves one half's flips reach the
verifier.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from situc.codegen.c import generate as generate_c
from situc.codegen.c import tamper
from situc.layout import solve
from situc.parser import parse_text
from situc.resolve import resolve

ROOT     = Path(__file__).resolve().parents[2]
RUNTIME  = ROOT / "runtime" / "c"
COMPILER = shutil.which("gcc") or shutil.which("cc")

WARNINGS = ["-std=c11", "-pedantic-errors", "-O1", "-Wall", "-Wextra",
	"-Werror", "-Wconversion", "-Wsign-conversion"]

FIXED = ("target buffer;\nendian big;\n\n"
         "struct s {\n\tu8 hop;\n"
         "\tauthenticated {\n\t\tu16 a;\n\t\tu8  b;\n\t}\n"
         "\ttag u8[2];\n}\n")

DYNAMIC = ("target buffer;\nendian big;\n\n"
           "struct s {\n\tu8 n;\n"
           "\tauthenticated {\n\t\tu8 body[n];\n\t}\n"
           "\ttag u8[2];\n}\n")


def emitted(source: str) -> str:
	schema   = parse_text(source)
	resolved = resolve(schema, solve(schema))
	files    = tamper.generate(schema, resolved, "unit")
	return files.get("unit_tamper.h", "")


def test_a_schema_with_no_tag_gets_no_harness() -> None:
	assert emitted("target buffer;\nendian big;\n\nstruct s { u8 a; }\n") == ""


def test_a_fixed_layout_gets_both_halves() -> None:
	"""Covered bytes must matter; outside a fixed layout, the rest must
	not. The second half is what catches a verifier covering more than the
	schema says."""
	text = emitted(FIXED)
	assert "situ_s_tamper" in text
	assert "And only those" in text


def test_a_dynamic_layout_gets_only_the_covered_half() -> None:
	"""Outside a dynamic layout a flipped byte can be a length, and a parse
	that legitimately moved is not an over-covering verifier -- so the
	outside half is deliberately absent, and the header says why."""
	text = emitted(DYNAMIC)
	assert "situ_s_tamper" in text
	assert "And only those" not in text


@pytest.mark.skipif(COMPILER is None, reason="no C compiler")
def test_the_harness_passes_honesty_and_catches_both_liars(tmp_path: Path) -> None:
	"""Run, not read, with a liar per direction as the control on the run."""
	schema   = parse_text(FIXED)
	resolved = resolve(schema, solve(schema))

	for name, text in generate_c(schema, resolved, "unit").files().items():
		(tmp_path / name).write_text(text, encoding="ascii")
	for name, text in tamper.generate(schema, resolved, "unit").items():
		(tmp_path / name).write_text(text, encoding="ascii")

	(tmp_path / "main.c").write_text("""#include "unit_tamper.h"

static int checksum(situ_view_t view, uint8_t *out, int drop_last)
{
	uint32_t at, span;
	uint8_t  x = 0;

	if (situ_s_tag_covered(view, &at, &span) != SITU_OK) return 0;
	for (uint32_t i = at; i < at + span - (drop_last ? 1u : 0u); i++) {
		x ^= view.base[i];
	}
	*out = x;
	return 1;
}

static int honest(situ_view_t view, void *ctx)
{
	uint8_t x;

	(void)ctx;
	if (!checksum(view, &x, 0)) return 0;
	const uint8_t *tag = situ_s_tag_ptr(view);
	return tag[0] == x && tag[1] == (uint8_t)~x;
}

static int lying(situ_view_t view, void *ctx)
{
	uint8_t x;

	(void)ctx;
	if (!checksum(view, &x, 1)) return 0;
	const uint8_t *tag = situ_s_tag_ptr(view);
	return tag[0] == x && tag[1] == (uint8_t)~x;
}

/* The other direction: covers one byte more than the schema says. `hop` at
 * offset 0 is outside every covered span and outside the tag, and this
 * verifier notices it -- which the "and only those" half exists to catch. */
static int greedy(situ_view_t view, void *ctx)
{
	uint8_t x;

	(void)ctx;
	if (!checksum(view, &x, 0)) return 0;
	x ^= view.base[0];
	const uint8_t *tag = situ_s_tag_ptr(view);
	return tag[0] == x && tag[1] == (uint8_t)~x;
}

int main(void)
{
	uint8_t  buf[6] = {0x55, 0x12, 0x34, 0x99, 0, 0};
	uint8_t  x = 0x12 ^ 0x34 ^ 0x99;
	uint32_t failed = 0;

	buf[4] = x; buf[5] = (uint8_t)~x;
	if (situ_s_tamper(buf, 6, honest, 0, &failed) != SITU_OK) return 1;

	x = 0x12 ^ 0x34;
	buf[4] = x; buf[5] = (uint8_t)~x;
	if (situ_s_tamper(buf, 6, lying, 0, &failed) != SITU_ERR_CONSTRAINT) {
		return 2;
	}
	if (failed != 3) return 3;	/* `b` sits at offset 3 */

	x = 0x12 ^ 0x34 ^ 0x99 ^ 0x55;
	buf[4] = x; buf[5] = (uint8_t)~x;
	if (situ_s_tamper(buf, 6, greedy, 0, &failed) != SITU_ERR_CONSTRAINT) {
		return 4;
	}
	return failed == 0 ? 0 : 5;	/* `hop` sits at offset 0 */
}
""", encoding="ascii")

	built = subprocess.run(
		[COMPILER or "cc", *WARNINGS, f"-I{tmp_path}", f"-I{RUNTIME}",
		 str(tmp_path / "main.c"), str(tmp_path / "unit.c"),
		 str(RUNTIME / "situ.c"), "-o", str(tmp_path / "run")],
		capture_output=True, text=True)
	assert built.returncode == 0, built.stderr

	ran = subprocess.run([str(tmp_path / "run")], capture_output=True, text=True)
	assert ran.returncode == 0, f"the harness answered wrongly at step {ran.returncode}"
