"""A bound the member's own type guarantees generates no comparison (0061).

`u8 a [max = 255]` is true and nothing can violate it, so the comparison
generated for it is one the compiler proves false:

    error: comparison is always false due to limited range of data type
           [-Werror=type-limits]

`-Wextra` turns that on and this project's own flags make it an error, so six
spellings generated C that situc's own gate would not compile: `[max]` at an
unsigned ceiling, `[min = 0]` on an unsigned, and either limit of a signed
type. All four backends emitted the check and all four agreed on the ids, so
the fix had to be in all four or it would have renumbered one against the
rest.

**Not a refusal**, and that was measured rather than argued: a bound may be a
`const`, and `--define CAP=100` on the same schema is a bound that bites.
Refusing at 255 would refuse a schema correct for every other value of its
own constant. `test_a_const_at_the_ceiling_is_not_a_schema_error` pins it.

**And no warning**, which `example/usb` decides. `u7 address [max = 127]` is
written there with a comment citing `usb.h:1980` and `usb.h:471` -- it records
what the format's own source says, and it is the only instance in the corpus.
A warning calling it redundant would be wrong about the one real case.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Protocol

import pytest

from situc import ast
from situc.codegen.c import generate as generate_c
from situc.codegen.cpp import generate as generate_cpp
from situc.codegen.python import generate as generate_python
from situc.codegen.rust import generate as generate_rust
from situc.layout import solve
from situc.parser import parse_text
from situc.resolve import ResolvedSchema, resolve

ROOT    = Path(__file__).resolve().parents[2]
RUNTIME = ROOT / "runtime" / "c"
HOST_CC = shutil.which("gcc") or shutil.which("cc")

#: The flags the generated-code suites use. `-Wextra` implies `-Wtype-limits`,
#: which is the whole point of this file.
WARNINGS = ("-std=c11", "-O2", "-Wall", "-Wextra", "-Werror",
            "-Wconversion", "-Wsign-conversion")

PREAMBLE = "target buffer;\nbit_order msb_first;\nendian big;\n\n"

#: One of each spelling the compiler refused, plus `h`, which is a real bound
#: and is what makes the assertions below measurements rather than a count of
#: zero.
RANGES = PREAMBLE + """struct ranges {
	u8   a [max = 255];
	u8   b [min = 0];
	u16  c [max = 65535];
	u32  d [max = 4294967295];
	i8   e [min = -128];
	i8   f [max = 127];
	u7   g [max = 127];
	u1   spare;
	u8   h [max = 200];
	u16  pad;
}
"""


class Emitted(Protocol):
	def files(self) -> dict[str, str]: ...


class Backend(Protocol):
	def __call__(self, schema: ast.Schema, resolved: ResolvedSchema,
	             basename: str) -> Emitted: ...


BACKENDS: dict[str, Backend] = {
	"c":      generate_c,
	"cpp":    generate_cpp,
	"python": generate_python,
	"rust":   generate_rust,
}

#: How each backend spells the DEFINITION of a check id. The definition
#: rather than the name, because every one of these is also written at the
#: refusal that reports it -- a mark matching the bare name counted two for
#: C++ and three for Python, which is this file's own subject arriving in its
#: instrument. One definition is expected: `h`, the only bound that can fail.
IDS = {
	"c":      "_CHECK ",
	"cpp":    "static constexpr std::uint32_t check_",
	"python": "\n\tCHECK_",
	"rust":   "pub const CHECK_",
}


def _emitted(backend: str, text: str) -> str:
	schema   = parse_text(text, path="ranges.situ")
	resolved = resolve(schema, solve(schema))
	built    = BACKENDS[backend](schema, resolved, "ranges")
	return "".join(one for _, one in sorted(built.files().items()))


@pytest.mark.parametrize("backend", sorted(BACKENDS))
def test_only_the_bound_that_can_fail_gets_a_check(backend: str) -> None:
	"""Seven of the eight bounds generate nothing, and `h` generates one.

	Asserted as the COUNT of check ids rather than as the absence of a
	comparison, because an id is what a consumer keys on (0051) and because
	the four backends spell the comparison four ways and the id once.
	"""
	source = _emitted(backend, RANGES)
	ids    = source.count(IDS[backend])
	assert ids == 1, (
		f"the {backend} backend numbers {ids} checks for a struct with one "
		"bound that can fail; the other seven are guaranteed by the member's "
		"own type")


@pytest.mark.parametrize("backend", sorted(BACKENDS))
def test_a_real_bound_is_still_compared(backend: str) -> None:
	"""The control. Without it the test above passes for a backend that
	stopped checking bounds altogether, which is the vacuous pass this
	tree's own notes keep arriving at."""
	source = _emitted(backend, RANGES)
	assert "200" in source, (
		f"the {backend} backend does not compare `h` against 200 at all")


def test_a_const_at_the_ceiling_is_not_a_schema_error() -> None:
	"""Which is why this is not a refusal.

	`u8 x [max = CAP]` is the same schema at every value of `CAP`. At 255 the
	bound is one the type guarantees and no comparison is generated; at 100 it
	bites. Refusing the first would refuse a schema that is correct for every
	other value of its own constant -- so the compiler says nothing and emits
	nothing, which is the only answer that works for both.
	"""
	konst = PREAMBLE + "const CAP = 255;\n\nstruct k { u8 x [max = CAP]; }\n"
	assert _emitted("c", konst).count("_CHECK ") == 0

	lower = konst.replace("const CAP = 255;", "const CAP = 100;")
	assert _emitted("c", lower).count("_CHECK ") == 1


@pytest.mark.skipif(HOST_CC is None, reason="no C compiler")
def test_the_generated_c_compiles_under_the_project_s_own_flags(
		tmp_path: Path) -> None:
	"""The defect in the compiler's own words, and the one check that would
	have caught it: six of these spellings were `-Werror=type-limits`."""
	schema   = parse_text(RANGES, path="ranges.situ")
	resolved = resolve(schema, solve(schema))
	built    = generate_c(schema, resolved, "ranges")
	(tmp_path / "ranges.h").write_text(built.header, encoding="ascii")
	(tmp_path / "ranges.c").write_text(built.source, encoding="ascii")

	assert HOST_CC is not None
	done = subprocess.run(
		[HOST_CC, *WARNINGS, f"-I{RUNTIME}", f"-I{tmp_path}", "-c",
		 str(tmp_path / "ranges.c"), "-o", str(tmp_path / "ranges.o")],
		capture_output=True, text=True)
	assert done.returncode == 0, done.stderr


@pytest.mark.skipif(HOST_CC is None, reason="no C compiler")
def test_the_bound_that_can_fail_still_refuses(tmp_path: Path) -> None:
	"""Removing a check is the kind of fix that removes coverage, so this
	asks the bytes: `h` at 201 must refuse and name itself, and a 255 in `a`
	-- whose own bound no longer generates a comparison -- must be accepted."""
	schema   = parse_text(RANGES, path="ranges.situ")
	resolved = resolve(schema, solve(schema))
	built    = generate_c(schema, resolved, "ranges")
	(tmp_path / "ranges.h").write_text(built.header, encoding="ascii")
	(tmp_path / "ranges.c").write_text(built.source, encoding="ascii")
	(tmp_path / "probe.c").write_text("""
#include "ranges.h"

int main(void)
{
	uint8_t     raw[SITU_RANGES_SIZE_FIXED] = { 0 };
	situ_msg_t  msg;
	situ_view_t view;
	uint32_t    which = 0u;

	situ_msg_init(&msg, raw, sizeof raw);
	if (situ_ranges_view(&msg, 0, &view) != SITU_OK)        return 1;
	if (situ_ranges_check(view, &which) != SITU_OK)         return 2;

	situ_ranges_h_set(view, 201);
	if (situ_ranges_check(view, &which) != SITU_ERR_CONSTRAINT) return 3;
	if (which != SITU_RANGES_H_CHECK)                       return 4;

	situ_ranges_h_set(view, 200);
	situ_ranges_a_set(view, 255);
	if (situ_ranges_check(view, &which) != SITU_OK)         return 5;
	return 0;
}
""", encoding="ascii")

	assert HOST_CC is not None
	built_ok = subprocess.run(
		[HOST_CC, *WARNINGS, f"-I{RUNTIME}", f"-I{tmp_path}",
		 str(tmp_path / "probe.c"), str(tmp_path / "ranges.c"),
		 str(RUNTIME / "situ.c"), "-o", str(tmp_path / "probe")],
		capture_output=True, text=True)
	assert built_ok.returncode == 0, built_ok.stderr
	ran = subprocess.run([str(tmp_path / "probe")])
	assert ran.returncode == 0, f"the probe failed at check {ran.returncode}"
