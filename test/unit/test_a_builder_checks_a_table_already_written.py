"""A builder checks a located table the message already holds (26.1191).

ZIP's central directory is `at end.cd_offset`, after a body that takes
everything before the 22-byte end record: so the message is whole once the
body and the record are down, and the directory can only lie inside the
body. 26.1190 placed a located table where a caller's field said, past what
was written; this one is behind it. The builder takes no value for it --
the caller's body holds it -- and the closing validation reads it where the
reader does: an offset that misses it, one past the body, or a count the
directory does not hold is refused.

The archive is the one Python's `zipfile` writes, so the bytes are a real
tool's rather than this file's, and the built archive is read back by it.
"""

from __future__ import annotations

import io
import shutil
import struct
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT    = Path(__file__).resolve().parents[2]
RUNTIME = ROOT / "runtime"
SCHEMA  = ROOT / "example" / "zip" / "zip.situ"
HOST_CC = shutil.which("gcc") or shutil.which("cc")


def _archive() -> bytes:
	held = io.BytesIO()
	with zipfile.ZipFile(held, "w", zipfile.ZIP_STORED) as archive:
		archive.writestr(zipfile.ZipInfo("hi.txt", (2026, 11, 27, 0, 0, 0)),
		                 b"hello\n")
	return held.getvalue()


def _ends(archive: bytes) -> list[bytes]:
	"""The end record as written, then three it does not agree with."""
	body, end = archive[:-22], archive[-22:]
	at = struct.unpack_from("<I", end, 16)[0]
	missed = bytearray(end)
	struct.pack_into("<I", missed, 16, at + 1)
	past = bytearray(end)
	struct.pack_into("<I", past, 16, len(body) + 50)
	two = bytearray(end)
	struct.pack_into("<HH", two, 8, 2, 2)
	return [end, bytes(missed), bytes(past), bytes(two)]


C_MAIN = """#include <stdio.h>
#include "zip_build.h"
static const uint8_t body[] = {%s};
static const uint8_t ends[4][22] = {%s};
static uint8_t out[512];
int main(void)
{
	for (int i = 0; i < 4; i++) {
		uint32_t n = 0;
		situ_err_t e = situ_archive_build(out, sizeof out, body, sizeof body,
		                                  ends[i], 22, &n);
		printf("%%d", (int)e);
		for (uint32_t k = 0; e == SITU_OK && k < n; k++)
			printf(" %%02x", out[k]);
		puts("");
	}
	return 0;
}
"""


def _bytes(data: bytes) -> str:
	return ", ".join(str(one) for one in data)


@pytest.mark.skipif(HOST_CC is None, reason="no host C compiler")
def test_c_checks_zip_s_directory_in_the_body(tmp_path: Path) -> None:
	archive = _archive()
	done = subprocess.run([sys.executable, "-m", "situc", "build", "--target",
	                       "c", "--layer", "edit", str(SCHEMA), "--out",
	                       str(tmp_path)], cwd=ROOT, capture_output=True,
	                      text=True, timeout=300)
	assert done.returncode == 0, done.stderr[-2000:]
	assert "no builder for `archive`" not in done.stderr, done.stderr
	(tmp_path / "main.c").write_text(C_MAIN % (
		_bytes(archive[:-22]),
		", ".join("{" + _bytes(end) + "}" for end in _ends(archive))))
	assert HOST_CC is not None
	made = subprocess.run([HOST_CC, "-std=c11", "-O1", "-Wall", "-Wextra",
	                       "-Werror", f"-I{RUNTIME / 'c'}", "-I.", "main.c",
	                       "zip.c", str(RUNTIME / "c" / "situ.c"), "-o", "t"],
	                      cwd=tmp_path, capture_output=True, text=True,
	                      timeout=300)
	assert made.returncode == 0, made.stderr[-2000:]
	ran = subprocess.run(["./t"], cwd=tmp_path, capture_output=True,
	                     text=True, timeout=60)
	lines = ran.stdout.splitlines()
	ok, constraint = "0", "2"
	assert [line.split()[0] for line in lines] == [ok] + [constraint] * 3, \
		lines
	built = bytes(int(one, 16) for one in lines[0].split()[1:])
	assert built == archive
	with zipfile.ZipFile(io.BytesIO(built)) as read:
		assert read.testzip() is None
		assert read.read("hi.txt") == b"hello\n"


@pytest.mark.skipif(HOST_CC is None, reason="no host C compiler")
def test_four_builders_check_the_directory_alike(tmp_path: Path) -> None:
	sys.path.insert(0, str(ROOT / "test" / "unit"))
	import fourway
	from test_a_builder_holds_an_arm_to_its_reader import _case, _said, _units
	if not fourway.COMPLETE:
		pytest.skip("needs gcc, g++ and rustc")
	archive = _archive()
	unit = _units(SCHEMA)["archive."]
	cases = [_case(unit, body=archive[:-22], end=end)
	         for end in _ends(archive)]
	assert _said(SCHEMA, tmp_path, cases) == [archive.hex()] + \
		["refused"] * 3
