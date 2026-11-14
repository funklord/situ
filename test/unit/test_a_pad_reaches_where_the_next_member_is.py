"""A builder writes a pad to where the reader puts the member after it
(26.1024).

cpio's `reserved u8 [align_up(HEADER_BYTES + header.namesize, 4) -
(HEADER_BYTES + header.namesize)]` was refused: the expression calls
`align_up`, so no field can be solved for -- and none needs to be. When the
writer reaches the pad, every field its length reads is down, so the
reader's own offset of the member after it says where the pad ends. 26.1004
answered the trailing pad after the data the same way with `_required`; this
is the one in the middle. C only.

Judged by the real consumer: GNU cpio lists the archive and extracts the
file from it.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from test_an_arm_sized_by_its_argument_is_written import COMPILER, ROOT, _built

CPIO = shutil.which("cpio")

DRIVER = r"""#include <stdio.h>
#include <string.h>
#include "cpio_build.h"

static uint8_t  archive[1024];
static uint32_t used;

static int entry(const char *name, uint32_t mode, const uint8_t *data,
                 uint32_t size)
{
	uint8_t  header[110];
	uint32_t wrote = 0, namesize = (uint32_t)strlen(name) + 1u;

	if (situ_cpio_header_build(header, sizeof header,
	                           (const uint8_t *)"070701", 1u, mode, 0u, 0u,
	                           1u, 0u, size, 0u, 0u, 0u, 0u, namesize, 0u,
	                           &wrote) != SITU_OK || wrote != 110u) {
		return 1;
	}
	if (situ_cpio_entry_build(archive + used, (uint32_t)sizeof archive - used,
	                          header, 110u, (const uint8_t *)name, namesize,
	                          data, size, &wrote) != SITU_OK) {
		return 2;
	}
	used += wrote;
	return 0;
}

int main(int argc, char **argv)
{
	static const uint8_t hi[3] = { 'h', 'i', '\n' };
	FILE *f;

	if (argc != 2) {
		return 9;
	}
	/* 110 + 7 is padded to 120, and three bytes of data to four. */
	if (entry("hi.txt", 0100644u, hi, 3u) != 0 || used != 124u) {
		return 3;
	}
	/* 110 + 11 is padded to 124. */
	if (entry("TRAILER!!!", 0u, NULL, 0u) != 0 || used != 248u) {
		return 4;
	}
	f = fopen(argv[1], "wb");
	if (f == NULL || fwrite(archive, 1u, used, f) != used) {
		return 5;
	}
	return fclose(f) != 0 ? 6 : 0;
}
"""


@pytest.mark.skipif(COMPILER is None, reason = "needs a C compiler")
def test_cpio_reads_what_the_builder_wrote(tmp_path: Path) -> None:
	out = _built(tmp_path, "cpio")
	(tmp_path / "drive.c").write_text(DRIVER, encoding = "ascii")
	runtime = ROOT / "runtime" / "c"
	built = subprocess.run(
		[COMPILER or "cc", "-std=c11", "-pedantic-errors", "-Wall", "-Wextra",
		 "-Werror", "-Wconversion", "-Wsign-conversion", f"-I{runtime}",
		 f"-I{out}", str(tmp_path / "drive.c"), str(out / "cpio.c"),
		 str(runtime / "situ.c"), "-o", str(tmp_path / "drive")],
		capture_output = True, text = True)
	assert built.returncode == 0, built.stderr
	archive = tmp_path / "out.cpio"
	ran = subprocess.run([str(tmp_path / "drive"), str(archive)],
	                     capture_output = True)
	assert ran.returncode == 0, f"check {ran.returncode} failed"

	data = archive.read_bytes()
	assert data[117:120] == b"\0\0\0", "the name padding is zeros"
	assert data[120:124] == b"hi\n\0"

	if CPIO is None:
		pytest.skip("no cpio to read it")
	listed = subprocess.run([CPIO, "-it", "--quiet"], input = data,
	                        capture_output = True, timeout = 60)
	assert listed.returncode == 0, listed.stderr
	assert listed.stdout.split() == [b"hi.txt"], listed.stdout
	extracted = subprocess.run([CPIO, "-i", "--to-stdout", "--quiet",
	                            "hi.txt"], input = data,
	                           capture_output = True, timeout = 60)
	assert extracted.returncode == 0, extracted.stderr
	assert extracted.stdout == b"hi\n"
