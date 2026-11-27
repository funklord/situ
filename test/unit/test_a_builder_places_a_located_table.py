"""A builder places a located table where its offset says (26.1190).

ELF's program and section tables sit `at phoff` and `at shoff`, and a
table's extent is in its own bytes -- its count is an argument the header
hands it -- so the reader cannot say where one ends before it is down.
The builder was refused for every member located by an expression but a
last byte run (26.1015). Now the table goes where the caller's offset
says, zeros before it, and the reader is asked afterwards, over what was
written, where it finds it and how long it is.

Asked of C, then of all four alike, with a minimal
executable -- one PT_LOAD, one null section, a gap between the tables --
which `readelf` takes, and of three refusals: a table longer than its
count, an offset behind what is written, and an offset past the buffer.
"""

from __future__ import annotations

import shutil
import struct
import subprocess
import sys
from pathlib import Path

import pytest

ROOT    = Path(__file__).resolve().parents[2]
RUNTIME = ROOT / "runtime"
SCHEMA  = ROOT / "example" / "elf" / "elf.situ"
HOST_CC = shutil.which("gcc") or shutil.which("cc")
READELF = shutil.which("readelf")

IDENT   = b"\x7fELF\x02\x01\x01" + bytes(9)
PROGRAM = struct.pack("<IIQQQQQQ", 1, 5, 0, 0x400000, 0x400000, 192, 192,
                      0x1000)
SECTION = bytes(64)

C_MAIN = """#include <stdio.h>
#include "elf_build.h"
static const uint8_t ident[] = {%s};
static const uint8_t program[] = {%s};
static const uint8_t two[] = {%s};
static const uint8_t section[] = {%s};
static uint8_t out[512];
static void show(situ_err_t e, uint32_t n)
{
	printf("%%d", (int)e);
	for (uint32_t i = 0; e == SITU_OK && i < n; i++) printf(" %%02x", out[i]);
	puts("");
}
static situ_err_t make(uint32_t cap, uint64_t phoff, uint64_t shoff,
		const uint8_t *programs, uint32_t programs_len, uint32_t *n)
{
	return situ_elf64_build(out, cap, ident, sizeof ident, 2, 62, 1,
	                        0x400000, phoff, shoff, 0, 64, 56, 1, 64, 1, 0,
	                        programs, programs_len, section, sizeof section,
	                        n);
}
int main(void)
{
	uint32_t n = 0;
	situ_err_t e = make(sizeof out, 64, 128, program, sizeof program, &n);
	show(e, n);
	/* Two headers under `phnum = 1`, the second where the gap is. */
	show(make(sizeof out, 64, 184, two, sizeof two, &n), 0);
	show(make(sizeof out, 64, 100, program, sizeof program, &n), 0);
	show(make(150, 64, 128, program, sizeof program, &n), 0);
	return 0;
}
"""


def _bytes(data: bytes) -> str:
	return ", ".join(str(one) for one in data)


@pytest.mark.skipif(HOST_CC is None, reason="no host C compiler")
def test_c_places_elf_s_tables(tmp_path: Path) -> None:
	done = subprocess.run([sys.executable, "-m", "situc", "build", "--target",
	                       "c", "--layer", "edit", str(SCHEMA), "--out",
	                       str(tmp_path)], cwd=ROOT, capture_output=True,
	                      text=True, timeout=300)
	assert done.returncode == 0, done.stderr[-2000:]
	(tmp_path / "main.c").write_text(
		C_MAIN % (_bytes(IDENT), _bytes(PROGRAM), _bytes(PROGRAM * 2),
		          _bytes(SECTION)))
	assert HOST_CC is not None
	made = subprocess.run([HOST_CC, "-std=c11", "-O1", "-Wall", "-Wextra",
	                       "-Werror", f"-I{RUNTIME / 'c'}", "-I.", "main.c",
	                       "elf.c", str(RUNTIME / "c" / "situ.c"), "-o", "t"],
	                      cwd=tmp_path, capture_output=True, text=True,
	                      timeout=300)
	assert made.returncode == 0, made.stderr[-2000:]
	ran = subprocess.run(["./t"], cwd=tmp_path, capture_output=True,
	                     text=True, timeout=60)
	lines = ran.stdout.splitlines()
	ok, bounds, constraint = "0", "1", "2"
	assert [line.split()[0] for line in lines] == [ok, constraint,
	                                               constraint, bounds], lines

	built = bytes(int(one, 16) for one in lines[0].split()[1:])
	header = (IDENT + struct.pack("<HHIQQQIHHHHHH", 2, 62, 1, 0x400000, 64,
	                              128, 0, 64, 56, 1, 64, 1, 0))
	assert built == header + PROGRAM + bytes(8) + SECTION

	if READELF is not None:
		(tmp_path / "built.elf").write_bytes(built)
		read = subprocess.run([READELF, "-h", "-l", "-W", "built.elf"],
		                      cwd=tmp_path, capture_output=True, text=True,
		                      timeout=60)
		assert read.returncode == 0, read.stderr
		assert "Number of program headers:         1" in read.stdout
		assert "LOAD" in read.stdout


TIFF_MAIN = """#include <stdio.h>
#include "tiff_build.h"
static uint8_t out[64];
int main(void)
{
	/* No entries and no next directory, in the order the header says. */
	static const uint8_t empty[] = {0, 0, 0, 0, 0, 0};
	/* Says one entry and holds none: shorter than the reader reads. */
	static const uint8_t short_one[] = {1, 0, 0, 0, 0, 0};
	uint32_t n = 0;
	situ_err_t e = situ_tiff_file_build(out, sizeof out, 0x4949, 8, empty,
	                                    sizeof empty, &n);
	printf("%d", (int)e);
	for (uint32_t i = 0; e == SITU_OK && i < n; i++) printf(" %02x", out[i]);
	puts("");
	printf("%d\\n", (int)situ_tiff_file_build(out, sizeof out, 0x4949, 8,
	                                         short_one, sizeof short_one,
	                                         &n));
	return 0;
}
"""


@pytest.mark.skipif(HOST_CC is None, reason="no host C compiler")
def test_c_places_a_tiff_directory(tmp_path: Path) -> None:
	"""TIFF's first directory, whose extent is its own count: read from
	the bytes the builder has just put down, not from whatever the buffer
	held there before."""
	schema = ROOT / "example" / "tiff" / "tiff.situ"
	done = subprocess.run([sys.executable, "-m", "situc", "build", "--target",
	                       "c", "--layer", "edit", str(schema), "--out",
	                       str(tmp_path)], cwd=ROOT, capture_output=True,
	                      text=True, timeout=300)
	assert done.returncode == 0, done.stderr[-2000:]
	(tmp_path / "main.c").write_text(TIFF_MAIN)
	assert HOST_CC is not None
	made = subprocess.run([HOST_CC, "-std=c11", "-O1", "-Wall", "-Wextra",
	                       "-Werror", f"-I{RUNTIME / 'c'}", "-I.", "main.c",
	                       "tiff.c", str(RUNTIME / "c" / "situ.c"), "-o", "t"],
	                      cwd=tmp_path, capture_output=True, text=True,
	                      timeout=300)
	assert made.returncode == 0, made.stderr[-2000:]
	ran = subprocess.run(["./t"], cwd=tmp_path, capture_output=True,
	                     text=True, timeout=60)
	assert ran.stdout.splitlines() == [
		"0 49 49 2a 00 08 00 00 00 00 00 00 00 00 00", "2"], ran.stdout


@pytest.mark.skipif(HOST_CC is None, reason="no host C compiler")
def test_four_builders_place_the_tables_alike(tmp_path: Path) -> None:
	"""All four, handed the same ELF and TIFF, write the same bytes and
	refuse the same table longer than its count."""
	sys.path.insert(0, str(ROOT / "test" / "unit"))
	import fourway
	from test_a_builder_holds_an_arm_to_its_reader import _case, _said, _units
	if not fourway.COMPLETE:
		pytest.skip("needs gcc, g++ and rustc")
	elf = _units(SCHEMA)["elf64."]
	given: dict[str, int | bytes] = dict(
		ident=IDENT, type=2, machine=62, version=1, entry=0x400000,
		phoff=64, shoff=128, ehsize=64, phentsize=56, phnum=1,
		shentsize=64, shnum=1, sections=SECTION)
	header = (IDENT + struct.pack("<HHIQQQIHHHHHH", 2, 62, 1, 0x400000, 64,
	                              128, 0, 64, 56, 1, 64, 1, 0))
	assert _said(SCHEMA, tmp_path, [
		_case(elf, programs=PROGRAM, **given),
		_case(elf, programs=PROGRAM * 2, **{**given, "shoff": 184}),
	]) == [(header + PROGRAM + bytes(8) + SECTION).hex(), "refused"]

	tiff_schema = ROOT / "example" / "tiff" / "tiff.situ"
	tiff = _units(tiff_schema)["tiff_file."]
	assert _said(tiff_schema, tmp_path, [
		_case(tiff, byte_order=0x4949, magic=42, ifd_offset=8,
		      first=bytes(6)),
		_case(tiff, byte_order=0x4949, magic=42, ifd_offset=8,
		      first=b"\x01" + bytes(5)),
	]) == ["49492a0008000000" + "00" * 6, "refused"]
