"""A builder holds a run to the count its argument states (26.1177).

`struct program_table { parameter u16 count; program_header entries[count]; }`
-- ELF's tables, placed `at` an offset and handed the header's count. A count
FIELD is written by the builder from the elements it is handed; a count that
is an argument is written by nothing, so nothing tied the two: all four
builders wrote three program headers under `count = 2`, and the reader,
told 2, saw two and a stray tail. Found adding example/elf, by the four
builders agreeing with each other and the reader disagreeing with all four.

Asked of C (compiled) and Python (in process), the two ends of the four the
agreement test compares: three headers under 3 are written, under 2 and 4
refused as CONSTRAINT.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT    = Path(__file__).resolve().parents[2]
RUNTIME = ROOT / "runtime"
SCHEMA  = ROOT / "example" / "elf" / "elf.situ"
HOST_CC = shutil.which("gcc") or shutil.which("cc")

#: Three 56-byte program headers, each PT_LOAD and otherwise zero.
ENTRIES = (b"\x01" + bytes(55)) * 3


def _build(target: str, out: Path) -> None:
	done = subprocess.run([sys.executable, "-m", "situc", "build", "--target",
	                       target, "--layer", "edit", str(SCHEMA), "--out",
	                       str(out)], cwd=ROOT, capture_output=True, text=True,
	                      timeout=300)
	assert done.returncode == 0, done.stderr[-2000:]


def test_python_refuses_a_count_the_elements_do_not_make(tmp_path: Path) -> None:
	_build("python", tmp_path)
	sys.path.insert(0, str(RUNTIME / "python"))
	sys.path.insert(0, str(tmp_path))
	try:
		spec = importlib.util.spec_from_file_location(
			"elf_build", tmp_path / "elf_build.py")
		assert spec is not None and spec.loader is not None
		built = importlib.util.module_from_spec(spec)
		spec.loader.exec_module(built)
	finally:
		sys.path.remove(str(tmp_path))
	out = bytearray(512)
	assert built.build_program_table(out, 3, ENTRIES) == len(ENTRIES)
	for wrong in (2, 4):
		with pytest.raises(built.ConstraintError):
			built.build_program_table(bytearray(512), wrong, ENTRIES)


C_MAIN = """#include <stdio.h>
#include "elf_build.h"
static const uint8_t entries[] = {%s};
int main(void)
{
	uint8_t out[512];
	uint16_t counts[] = {3, 2, 4};
	for (unsigned i = 0; i < 3; i++) {
		uint32_t len = 0;
		situ_err_t e = situ_program_table_build(out, sizeof out, counts[i],
		                                        entries, sizeof entries, &len);
		printf("%%u %%d %%u\\n", (unsigned)counts[i], (int)e, (unsigned)len);
	}
	return 0;
}
"""


@pytest.mark.skipif(HOST_CC is None, reason="no host C compiler")
def test_c_refuses_a_count_the_elements_do_not_make(tmp_path: Path) -> None:
	_build("c", tmp_path)
	(tmp_path / "main.c").write_text(
		C_MAIN % ", ".join(str(one) for one in ENTRIES))
	assert HOST_CC is not None
	made = subprocess.run([HOST_CC, "-std=c11", "-O1", "-Wall", "-Wextra",
	                       "-Werror", f"-I{RUNTIME / 'c'}", "-I.", "main.c",
	                       "elf.c", str(RUNTIME / "c" / "situ.c"), "-o", "t"],
	                      cwd=tmp_path, capture_output=True, text=True,
	                      timeout=300)
	assert made.returncode == 0, made.stderr[-2000:]
	ran = subprocess.run(["./t"], cwd=tmp_path, capture_output=True,
	                     text=True, timeout=60)
	ok, constraint = 0, 2
	assert ran.stdout.split("\n")[:3] == [
		f"3 {ok} {len(ENTRIES)}", f"2 {constraint} 0", f"4 {constraint} 0"]
