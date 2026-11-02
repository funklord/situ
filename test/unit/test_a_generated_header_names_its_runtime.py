"""A generated header refuses a runtime it was not generated against.

26.816: fmake compiled this tree's committed tests and got twelve signature
errors, which it read as "your generated tests are out of step with situc".
They were in step. The header it found was `/usr/include/situ.h` from an
installed `libsitu-dev`, byte-identical to `runtime/c/situ.h` at `af50443`
-- 2026-09-08 -- in which `situ_view_check` still took two arguments.

Nothing could detect that. Both headers say `SITU_VERSION_MINOR 1`, and the
package version is `1.0` on both sides, so the ABI moved and every number
that might have said so stood still. A quoted `#include "situ.h"` resolves
against whatever the caller put on the include path, and an old install
wins silently.

`SITU_RUNTIME_ABI` is one integer with one job, and generated headers
assert it. The point is not that a mismatch is caught -- a mismatch was
always caught, as a dozen type errors -- but that the message names the
include path instead of the symbol that happened to change, because the
symbol is in the wrong file and that is where the reader goes.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from every_schema import ROOT, load_schema
from situc.codegen.c import generate as generate_c
from situc.codegen.c.names import RUNTIME_ABI
from situc.layout import solve
from situc.resolve import resolve

#: The runtime's own copy of the serial, which must agree with situc's.
DEFINE = re.compile(r"^#define\s+SITU_RUNTIME_ABI\s+(\d+)\s*$", re.M)

SCHEMA = ROOT / "test" / "schema" / "edges.situ"


def test_the_runtime_and_the_generator_agree_on_the_serial() -> None:
	"""One number, two files. This is the gate on the copy.

	situc holds it because situc emits the requirement, and the runtime
	holds it because the runtime is what a consumer compiles against. Two
	places answering one question is the fault this whole family of
	defects has had, so the copy gets a gate rather than a convention.
	"""
	header = (ROOT / "runtime" / "c" / "situ.h").read_text(encoding="ascii")
	found  = DEFINE.search(header)
	assert found is not None, (
		"runtime/c/situ.h defines no SITU_RUNTIME_ABI. Generated headers "
		"assert it, so a runtime without it refuses every generated header "
		"in the tree -- 26.816")
	assert int(found.group(1)) == RUNTIME_ABI, (
		f"runtime/c/situ.h says SITU_RUNTIME_ABI {found.group(1)} and "
		f"situc emits a requirement for {RUNTIME_ABI}. Whichever moved, "
		f"move the other: generated code refuses a runtime whose serial is "
		f"not the one it was generated for")


def _generated_header(tmp_path: Path) -> Path:
	schema   = load_schema(SCHEMA)
	resolved = resolve(schema, solve(schema))
	out = tmp_path / "edges.h"
	out.write_text(generate_c(schema, resolved, "situ").header,
	               encoding="ascii")
	return out


#: Each stub stands in for a runtime on the include path, and each names the
#: arm of the guard it is meant to reach. The third is the real runtime.
STUBS = (
	("predates the serial", "/* an old libsitu-dev */\n",
	 "predates SITU_RUNTIME_ABI"),
	("a different serial", f"#define SITU_RUNTIME_ABI {RUNTIME_ABI + 1}\n",
	 "different runtime ABI"),
)


@pytest.mark.parametrize("name,stub,wanted", STUBS,
                         ids=[s[0] for s in STUBS])
def test_a_wrong_runtime_is_refused_by_name(tmp_path, name, stub,  # type: ignore[no-untyped-def]
		wanted) -> None:
	"""Preprocess only, so the `#error` is the whole diagnostic.

	A stub with nothing in it would otherwise bury the message under every
	missing type, which is exactly the failure this replaces.
	"""
	if shutil.which("gcc") is None:
		pytest.skip("no gcc")
	header = _generated_header(tmp_path)
	(tmp_path / "situ.h").write_text(stub, encoding="ascii")
	run = subprocess.run(
		["gcc", "-E", "-I", str(tmp_path), str(header)],
		capture_output=True, text=True)
	assert run.returncode != 0, (
		f"a situ.h that {name} was accepted. The guard is what makes a "
		f"shadowed runtime say so -- 26.816")
	assert wanted in run.stderr, (
		f"the refusal did not name the case: wanted {wanted!r} in\n"
		f"{run.stderr[:800]}")
	assert "include path" in run.stderr, (
		"the refusal has to name the include path rather than a symbol, "
		"because the symbol is in the wrong file")


def test_the_real_runtime_is_accepted(tmp_path) -> None:  # type: ignore[no-untyped-def]
	"""The negative control. Without it the two above pass on a guard that
	refuses everything."""
	if shutil.which("gcc") is None:
		pytest.skip("no gcc")
	header = _generated_header(tmp_path)
	shutil.copy(ROOT / "runtime" / "c" / "situ.h", tmp_path / "situ.h")
	run = subprocess.run(
		["gcc", "-E", "-I", str(tmp_path), str(header)],
		capture_output=True, text=True)
	assert run.returncode == 0, (
		f"the tree's own runtime was refused by the tree's own generated "
		f"header:\n{run.stderr[:800]}")
