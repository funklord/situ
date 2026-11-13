"""A builder writes an endian marker and the order it chooses (26.1007).

Every builder refused TIFF's header and pcap's: "is a marker ... not
written yet". The caller hands over the marker's literal, refused unless
it is one the schema declares; it is written as the table spells it, and
every field in its scope is written in the order it names. A pinned field
there -- TIFF's `magic [must_eq = 42]` -- is the schema's, in that order.

And a fault the work found: C's builder for a struct whose order is handed
down as an ARGUMENT -- TIFF's `ifd_entry`, which takes the header's --
wrote every field big-endian whatever the argument said. It reads the
argument now.

Held to TIFF 6.0 section 2: `II*\\0` then a little-endian offset, or
`MM\\0*` then a big-endian one.
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
LITTLE   = bytes.fromhex("4949" "2a00" "08000000")
BIG      = bytes.fromhex("4d4d" "002a" "00000008")


def _built(tmp_path: Path, target: str) -> Path:
	out = tmp_path / target
	done = subprocess.run(
		[sys.executable, "-m", "situc", "build",
		 str(ROOT / "example/tiff/tiff.situ"), "--target", target,
		 "--layer", "edit", "--out", str(out)],
		cwd = ROOT, capture_output = True, text = True)
	assert done.returncode == 0, done.stderr
	return out


def test_python_writes_both_tiff_headers(tmp_path: Path) -> None:
	out = _built(tmp_path, "python")
	sys.path[:0] = [str(out), str(ROOT / "runtime" / "python")]
	for name in ("tiff", "tiff_build"):
		sys.modules.pop(name, None)
	try:
		build = importlib.import_module("tiff_build")
	finally:
		del sys.path[:2]
		for name in ("tiff", "tiff_build"):
			sys.modules.pop(name, None)
	for marker, wanted in ((0x4949, LITTLE), (0x4D4D, BIG)):
		buf = bytearray(16)
		n = build.build_tiff_header(buf, marker, 8)
		assert bytes(buf[:n]) == wanted, hex(marker)
	with pytest.raises(build.ConstraintError):
		build.build_tiff_header(bytearray(16), 0x1234, 8)


def test_c_writes_both_headers_and_an_entry_in_its_order(
		tmp_path: Path) -> None:
	if COMPILER is None:
		return
	out = _built(tmp_path, "c")
	(tmp_path / "drive.c").write_text("""#include <string.h>
#include "tiff_build.h"

int main(void)
{
	static const uint8_t little[8] = { 0x49, 0x49, 0x2a, 0, 8, 0, 0, 0 };
	static const uint8_t big[8] = { 0x4d, 0x4d, 0, 0x2a, 0, 0, 0, 8 };
	/* ImageWidth (256), LONG (4), count 1, 640 -- little-endian. */
	static const uint8_t entry[12] = {
		0x00, 0x01, 0x04, 0x00, 0x01, 0, 0, 0, 0x80, 0x02, 0, 0 };
	uint8_t  buf[16];
	uint32_t wrote = 0;

	if (situ_tiff_header_build(buf, sizeof buf, 0x4949u, 8u, &wrote)
	    != SITU_OK || wrote != 8u || memcmp(buf, little, 8u) != 0) {
		return 1;
	}
	if (situ_tiff_header_build(buf, sizeof buf, 0x4d4du, 8u, &wrote)
	    != SITU_OK || wrote != 8u || memcmp(buf, big, 8u) != 0) {
		return 2;
	}
	if (situ_tiff_header_build(buf, sizeof buf, 0x1234u, 8u, &wrote)
	    != SITU_ERR_CONSTRAINT) {
		return 3;
	}
	if (situ_ifd_entry_build_long(buf, sizeof buf, 0x4949u, 256u, 4u, 1u,
	                              640u, &wrote) != SITU_OK
	    || wrote != 12u || memcmp(buf, entry, 12u) != 0) {
		return 4;
	}
	return 0;
}
""", encoding = "ascii")
	runtime = ROOT / "runtime" / "c"
	compiled = subprocess.run(
		[COMPILER, "-std=c11", "-pedantic-errors", "-Wall", "-Wextra",
		 "-Werror", "-Wconversion", "-Wsign-conversion", f"-I{runtime}",
		 f"-I{out}", str(tmp_path / "drive.c"), str(out / "tiff.c"),
		 str(runtime / "situ.c"), "-o", str(tmp_path / "drive")],
		capture_output = True, text = True)
	assert compiled.returncode == 0, compiled.stderr
	ran = subprocess.run([str(tmp_path / "drive")], capture_output = True)
	assert ran.returncode == 0, f"check {ran.returncode} failed"
