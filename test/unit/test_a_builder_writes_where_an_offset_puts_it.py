"""A builder writes a trailing byte run where an offset puts it (26.1015).

BMP's `bitmap_file` ends in `u8 pixels[info.image_size] at
file.pixel_offset`, and every builder refused it: "located by an expression,
so where it lands is not where the writer had reached" (26.610, 26.629).
That holds for an offset into bytes already written. For the struct's LAST
member it need not: every byte before it is down when the writer reaches it,
so the offset is read -- through the reader's own accessor, over the whole
backing -- and the writer refuses one behind its cursor and zeroes any gap.

Held to Pillow, which decodes the file C builds: one 24-bit pixel, stored
blue, green, red, read back as (0x30, 0x20, 0x10), with the pixels right
after the headers and ten bytes further on.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT     = Path(__file__).resolve().parents[2]
COMPILER = shutil.which("cc") or shutil.which("gcc")

DRIVER = r"""#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "bmp_build.h"

static void le32(uint8_t *p, uint32_t v)
{
	p[0] = (uint8_t)v; p[1] = (uint8_t)(v >> 8);
	p[2] = (uint8_t)(v >> 16); p[3] = (uint8_t)(v >> 24);
}

int main(int argc, char **argv)
{
	uint8_t file[14] = { 'B', 'M' }, info[40] = { 0 }, out[128];
	static const uint8_t pixels[4] = { 0x10, 0x20, 0x30, 0x00 };
	uint32_t offset, wrote = 0;
	FILE *f;

	if (argc < 3) {
		return 2;
	}
	offset = (uint32_t)strtoul(argv[1], NULL, 10);
	le32(file + 2, offset + 4u);
	le32(file + 10, offset);
	le32(info + 0, 40u);
	le32(info + 4, 1u);
	le32(info + 8, 1u);
	info[12] = 1;
	info[14] = 24;
	le32(info + 20, 4u);
	memset(out, 0xAA, sizeof out);
	if (situ_bitmap_file_build(out, sizeof out, file, 14u, info, 40u,
	                           pixels, 4u, &wrote) != SITU_OK) {
		return 3;
	}
	if ((f = fopen(argv[2], "wb")) == NULL) {
		return 4;
	}
	fwrite(out, 1, wrote, f);
	fclose(f);
	return 0;
}
"""


@pytest.mark.skipif(COMPILER is None, reason = "needs a C compiler")
def test_pillow_reads_the_bitmap_c_builds(tmp_path: Path) -> None:
	image = pytest.importorskip("PIL.Image")
	out = tmp_path / "gen"
	done = subprocess.run(
		[sys.executable, "-m", "situc", "build",
		 str(ROOT / "example/bmp/bmp.situ"), "--target", "c", "--layer",
		 "edit", "--out", str(out)],
		cwd = ROOT, capture_output = True, text = True)
	assert done.returncode == 0, done.stderr
	(tmp_path / "drive.c").write_text(DRIVER, encoding = "ascii")
	runtime = ROOT / "runtime" / "c"
	built = subprocess.run(
		[COMPILER or "cc", "-std=c11", "-Wall", "-Wextra", "-Werror",
		 "-Wconversion", "-Wsign-conversion", f"-I{runtime}", f"-I{out}",
		 str(tmp_path / "drive.c"), str(out / "bmp.c"),
		 str(runtime / "situ.c"), "-o", str(tmp_path / "drive")],
		capture_output = True, text = True)
	assert built.returncode == 0, built.stderr

	for offset in (54, 64):
		path = tmp_path / f"at{offset}.bmp"
		ran = subprocess.run([str(tmp_path / "drive"), str(offset), str(path)])
		assert ran.returncode == 0, offset
		assert path.stat().st_size == offset + 4
		with image.open(path) as held:
			assert held.size == (1, 1)
			assert held.convert("RGB").getpixel((0, 0)) == (0x30, 0x20, 0x10)
		assert path.read_bytes()[54:offset] == bytes(offset - 54)

	# Into the headers: refused, not written over them.
	ran = subprocess.run([str(tmp_path / "drive"), "40",
	                      str(tmp_path / "bad.bmp")])
	assert ran.returncode == 3
