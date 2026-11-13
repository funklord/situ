"""A section a walker does not know: skipped if ancillary, refused if critical.

Set by the copyright holder 2026-11-13, "as PNG does" (26.991). The image
directory had one rule -- skip a kind you do not know -- which made a section
carrying a check (`image_ordering`, 26.988) a refusal an older walker passed
as clean. A directory entry's kind is now a `u16` tag and a `u16` of flags,
bit 0 ancillary; the print-only names and vectors are the ancillary ones.

Each case rewrites one entry of a real image and asks both walkers.
"""

from __future__ import annotations

import shutil
import struct
import subprocess
from pathlib import Path

import pytest

from situc.pack import (FORMAT_VERSION, HEADER_BYTES, SECTION_BYTES,
                        SECTION_ORDERINGS, SECTION_VECTORS, pack)
from situc.layout import solve
from situc.parser import parse_text
from situc.resolve import resolve
from walker.image import ImageError, load

ROOT     = Path(__file__).resolve().parents[2]
WALKER   = ROOT / "walker" / "c"
COMPILER = shutil.which("cc") or shutil.which("gcc")

SCHEMA = """target buffer;
endian big;
struct s {
	u8 n;
	u16 keys[n] [ascending];
}
"""

DRIVER = r"""#include <stdio.h>
#include <stdlib.h>
#include "situ_walk.h"

int main(int argc, char **argv)
{
	static uint8_t img[65536];
	situ_walk_image image;
	FILE *f;
	size_t n;

	if (argc < 2 || (f = fopen(argv[1], "rb")) == NULL) {
		return 2;
	}
	n = fread(img, 1, sizeof img, f);
	fclose(f);
	printf("%d\n", (int)situ_walk_open(&image, img, (uint32_t)n));
	return 0;
}
"""


def image() -> bytes:
	schema = parse_text(SCHEMA, path = "s.situ")
	blob, _ = pack(schema, resolve(schema, solve(schema)), metadata = True)
	return blob


def retagged(blob: bytes, tag: int, to: int, flags: int) -> bytes:
	"""`blob` with the entry for `tag` renamed `to` and its flags set."""
	out   = bytearray(blob)
	count, directory = struct.unpack_from("<II", blob, 12)
	assert directory == HEADER_BYTES
	for i in range(count):
		at = directory + i * SECTION_BYTES
		kind, _ = struct.unpack_from("<HH", blob, at)
		if kind == tag:
			struct.pack_into("<HH", out, at, to, flags)
			return bytes(out)
	raise AssertionError(f"no section {tag} in the image")


def test_the_format_says_it() -> None:
	blob = image()
	assert struct.unpack_from("<H", blob, 4)[0] == FORMAT_VERSION == 10
	flags = {}
	count, directory = struct.unpack_from("<II", blob, 12)
	for i in range(count):
		kind, flag = struct.unpack_from("<HH", blob, directory + i * SECTION_BYTES)
		flags[kind] = flag
	assert flags[SECTION_VECTORS] == 1		# print-only
	assert flags[SECTION_ORDERINGS] == 0		# carries a check


@pytest.mark.parametrize("tag, flags, loads", [
	(SECTION_VECTORS, 1, True),			# unknown and ancillary: skipped
	(SECTION_VECTORS, 0, False),		# the same, marked critical
	(SECTION_ORDERINGS, 0, False),		# a check this walker cannot read
])
def test_the_python_walker(tag: int, flags: int, loads: bool) -> None:
	blob = retagged(image(), tag, 900, flags)
	if loads:
		load(blob)
	else:
		with pytest.raises(ImageError, match = "critical"):
			load(blob)


@pytest.mark.skipif(COMPILER is None, reason = "no C compiler")
def test_the_c_walker(tmp_path: Path) -> None:
	(tmp_path / "drive.c").write_text(DRIVER, encoding = "ascii")
	built = subprocess.run(
		[COMPILER or "cc", "-std=c11", "-Wall", "-Wextra", "-Werror",
		 f"-I{WALKER}", str(tmp_path / "drive.c"),
		 str(WALKER / "situ_walk.c"), "-o", str(tmp_path / "drive")],
		capture_output = True, text = True)
	assert built.returncode == 0, built.stderr

	def opened(blob: bytes) -> int:
		(tmp_path / "img").write_bytes(blob)
		ran = subprocess.run([str(tmp_path / "drive"), str(tmp_path / "img")],
		                     capture_output = True, text = True, check = True)
		return int(ran.stdout)

	assert opened(image()) == 0
	assert opened(retagged(image(), SECTION_VECTORS, 900, 1)) == 0
	assert opened(retagged(image(), SECTION_VECTORS, 900, 0)) == 9
	assert opened(retagged(image(), SECTION_ORDERINGS, 900, 0)) == 9
