"""A builder writes a trimmed run (26.1014).

SMTP's `command` -- `MAIL FROM:<a@example.org>` and its CRLF -- was refused by
every builder, because its argument is `[trim]`: "the bytes it holds are not
the bytes it was given". They are, exactly when the content neither starts
nor ends with a byte the reader trims; so the writer refuses those, and what
it writes reads back as it was given.
"""

from __future__ import annotations

import importlib
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT     = Path(__file__).resolve().parents[2]
COMPILER = shutil.which("cc") or shutil.which("gcc")
SCHEMA   = ROOT / "example" / "smtp" / "smtp.situ"


def _built(tmp_path: Path, target: str) -> Path:
	out = tmp_path / target
	done = subprocess.run(
		[sys.executable, "-m", "situc", "build", str(SCHEMA), "--target",
		 target, "--layer", "edit", "--out", str(out)],
		cwd = ROOT, capture_output = True, text = True)
	assert done.returncode == 0, done.stderr
	return out


def test_python_writes_an_smtp_command(tmp_path: Path) -> None:
	out = _built(tmp_path, "python")
	sys.path[:0] = [str(out), str(ROOT / "runtime" / "python")]
	for name in ("smtp", "smtp_build"):
		sys.modules.pop(name, None)
	try:
		base  = importlib.import_module("smtp")
		build = importlib.import_module("smtp_build")
	finally:
		del sys.path[:2]
		for name in ("smtp", "smtp_build"):
			sys.modules.pop(name, None)
	buf = bytearray(64)
	n = build.build_command(buf, b"MAIL", b"FROM:<a@example.org>")
	assert bytes(buf[:n]) == b"MAIL FROM:<a@example.org>\r\n"
	held = base.acquire(base.command, base.Message(bytearray(buf[:n])), 0, n)
	assert bytes(held.argument) == b"FROM:<a@example.org>"
	for bad in (b" FROM:<a>", b"FROM:<a> "):
		with pytest.raises(build.ConstraintError):
			build.build_command(bytearray(64), b"MAIL", bad)


@pytest.mark.skipif(COMPILER is None, reason = "needs a C compiler")
def test_c_writes_an_smtp_command(tmp_path: Path) -> None:
	out = _built(tmp_path, "c")
	(tmp_path / "drive.c").write_text("""#include <string.h>
#include "smtp_build.h"

int main(void)
{
	static const char want[] = "MAIL FROM:<a@example.org>\\r\\n";
	uint8_t  buf[64];
	uint32_t wrote = 0;

	if (situ_command_build(buf, sizeof buf, (const uint8_t *)"MAIL", 4u,
	                       (const uint8_t *)"FROM:<a@example.org>", 20u,
	                       &wrote) != SITU_OK
	    || wrote != sizeof want - 1u || memcmp(buf, want, wrote) != 0) {
		return 1;
	}
	if (situ_command_build(buf, sizeof buf, (const uint8_t *)"MAIL", 4u,
	                       (const uint8_t *)" FROM:<a>", 9u, &wrote)
	    != SITU_ERR_CONSTRAINT) {
		return 2;
	}
	return 0;
}
""", encoding = "ascii")
	runtime = ROOT / "runtime" / "c"
	compiled = subprocess.run(
		[COMPILER or "cc", "-std=c11", "-pedantic-errors", "-Wall", "-Wextra",
		 "-Werror", "-Wconversion", "-Wsign-conversion", f"-I{runtime}",
		 f"-I{out}", str(tmp_path / "drive.c"), str(out / "smtp.c"),
		 str(runtime / "situ.c"), "-o", str(tmp_path / "drive")],
		capture_output = True, text = True)
	assert compiled.returncode == 0, compiled.stderr
	ran = subprocess.run([str(tmp_path / "drive")], capture_output = True)
	assert ran.returncode == 0, f"check {ran.returncode} failed"
