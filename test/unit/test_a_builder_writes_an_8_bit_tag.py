"""A builder fills an 8-bit tag (26.1005).

Every builder refused FLAC's frame header: its `crc` is a CRC-8, and the
tag filler stored 16, 32 and 64 bits. A byte has no order and the C runtime
no `put` for one, so C and C++ store it directly; Rust's and Python's
`to_bytes` already took one.

RFC 9639's first example frame header is `ff f8 69 18 00 00`, then the
CRC-8 `bf` (polynomial 0x07 from zero). `test_the_four_builders_agree`
holds the four to each other over `frame_header`; this holds them to that.
"""

from __future__ import annotations

import importlib
import shutil
import subprocess
import sys
from pathlib import Path

ROOT     = Path(__file__).resolve().parents[2]
COMPILER = shutil.which("cc") or shutil.which("gcc")
FIELDS   = bytes.fromhex("fff869180000")
HEADER   = bytes.fromhex("fff869180000bf")


def _built(tmp_path: Path, target: str) -> Path:
	out = tmp_path / target
	for command in (["build", "--target", target, "--layer", "edit"],
	                ["gen-derived", "--target", target]):
		done = subprocess.run(
			[sys.executable, "-m", "situc", command[0],
			 str(ROOT / "example/flac/flac.situ"), *command[1:],
			 "--out", str(out)], cwd = ROOT, capture_output = True,
			text = True)
		assert done.returncode == 0, done.stderr
	return out


def test_python_writes_rfc_9639s_header(tmp_path: Path) -> None:
	out = _built(tmp_path, "python")
	sys.path[:0] = [str(out), str(ROOT / "runtime" / "python")]
	for name in ("flac", "flac_build"):
		sys.modules.pop(name, None)
	try:
		build = importlib.import_module("flac_build")
	finally:
		del sys.path[:2]
		for name in ("flac", "flac_build"):
			sys.modules.pop(name, None)
	buf = bytearray(16)
	assert bytes(buf[:build.build_frame_header(buf, FIELDS)]) == HEADER


def test_c_writes_rfc_9639s_header(tmp_path: Path) -> None:
	if COMPILER is None:
		return
	out = _built(tmp_path, "c")
	(tmp_path / "drive.c").write_text("""#include <string.h>
#include "flac_build.h"

int main(void)
{
	static const uint8_t fields[6] = { 0xff, 0xf8, 0x69, 0x18, 0x00, 0x00 };
	uint8_t  buf[16];
	uint32_t wrote = 0;

	if (situ_frame_header_build(buf, sizeof buf, fields, 6u, &wrote)
	    != SITU_OK || wrote != 7u || buf[6] != 0xbfu) {
		return 1;
	}
	return 0;
}
""", encoding = "ascii")
	runtime = ROOT / "runtime" / "c"
	compiled = subprocess.run(
		[COMPILER, "-std=c11", "-pedantic-errors", "-Wall", "-Wextra",
		 "-Werror", "-Wconversion", "-Wsign-conversion", f"-I{runtime}",
		 f"-I{out}", str(tmp_path / "drive.c"), str(out / "flac.c"),
		 str(out / "flac_derived.c"), str(runtime / "situ.c"),
		 "-o", str(tmp_path / "drive")],
		capture_output = True, text = True)
	assert compiled.returncode == 0, compiled.stderr
	ran = subprocess.run([str(tmp_path / "drive")], capture_output = True)
	assert ran.returncode == 0, f"check {ran.returncode} failed"
