"""A generated C suite needs a C compiler and nothing else (26.977).

`gen-checks`, `gen-tests` and `gen-codec-tests` emitted cmocka suites, so a
user handed one had to install a test framework first, and a cross target
without a cmocka build could not run them at all. Each now carries its own
checks and runner (`situc/codegen/c/selfcheck.py`).

Both halves are asserted, because a runner that cannot fail passes every
suite: a generated suite builds with no library beyond the runtime and
passes, and a check that fails reports where and what, fails its test,
lets the next test run, and makes the program exit non-zero.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from situc.codegen.c import selfcheck

ROOT = Path(__file__).resolve().parents[2]
CC   = shutil.which("cc") or shutil.which("gcc")
FLAGS = ["-std=c11", "-pedantic-errors", "-Wall", "-Wextra", "-Werror",
         "-Wconversion", "-Wsign-conversion"]


@pytest.mark.skipif(CC is None, reason="no C compiler")
def test_a_generated_suite_builds_and_passes_alone(tmp_path: Path) -> None:
	schema = ROOT / "example" / "tiff" / "tiff.situ"
	for args in (["gen-checks", str(schema)], ["build", str(schema)]):
		subprocess.run([sys.executable, "-m", "situc", *args, "--out",
		                str(tmp_path)], check=True, capture_output=True,
		               cwd=ROOT)
	subprocess.run([sys.executable, "-m", "situc", "build", str(schema),
	                "--layer", "frame", "--out", str(tmp_path)], check=True,
	               capture_output=True, cwd=ROOT)
	source = (tmp_path / "tiff_checks.c").read_text(encoding="ascii")
	assert "cmocka" not in source
	assert CC is not None
	built = subprocess.run(
		[CC, *FLAGS, f"-I{ROOT / 'runtime' / 'c'}", f"-I{tmp_path}",
		 str(tmp_path / "tiff_checks.c"), str(tmp_path / "tiff.c"),
		 str(ROOT / "runtime" / "c" / "situ.c"), "-o", str(tmp_path / "run")],
		capture_output=True, text=True)
	assert built.returncode == 0, built.stderr
	ran = subprocess.run([str(tmp_path / "run")], capture_output=True,
	                     text=True, timeout=60)
	assert ran.returncode == 0, ran.stdout
	assert "[  PASSED  ]" in ran.stdout and "[  FAILED  ]" not in ran.stdout


@pytest.mark.skipif(CC is None, reason="no C compiler")
def test_a_failing_check_fails_its_test_and_the_run(tmp_path: Path) -> None:
	program = "\n".join([
		selfcheck.header(),
		"static void test_fails(void **state)",
		"{",
		"\t(void)state;",
		"\tsitu_check_eq(2 + 2, 5);",
		"\tsitu_check_true(0);    /* not reached: the test has left */",
		"}",
		"static void test_bytes(void **state)",
		"{",
		"\tconst unsigned char a[3] = {1, 2, 3}, b[3] = {1, 9, 3};",
		"\t(void)state;",
		"\tsitu_check_bytes(a, b, 3);",
		"}",
		"static void test_passes(void **state)",
		"{",
		"\t(void)state;",
		"\tsitu_check_eq(2 + 2, 4);",
		"\tsitu_check_ne(1, 2);",
		"}",
		"",
		*selfcheck.main_lines("tests", ["test_fails", "test_bytes",
		                                "test_passes"], "probe"),
		"",
	])
	(tmp_path / "probe.c").write_text(program, encoding="ascii")
	assert CC is not None
	built = subprocess.run([CC, *FLAGS, str(tmp_path / "probe.c"), "-o",
	                        str(tmp_path / "probe")],
	                       capture_output=True, text=True)
	assert built.returncode == 0, built.stderr
	ran = subprocess.run([str(tmp_path / "probe")], capture_output=True,
	                     text=True, timeout=60)
	assert ran.returncode == 1, ran.stdout
	lines = ran.stdout.splitlines()
	assert "[  FAILED  ] test_fails" in lines, lines
	assert "[  FAILED  ] test_bytes" in lines, lines
	assert "[       OK ] test_passes" in lines, lines
	assert any("0x4 != 0x5" in one for one in lines), lines
	assert any("byte 1 of 3 is 0x02, not 0x09" in one for one in lines), lines
	assert "[  FAILED  ] 2 test(s)." in lines, lines
