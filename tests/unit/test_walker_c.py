"""The embedded walker, held to the Python one (0035).

The C walker is the walker decision 0026 was argued from -- a device whose
framing changes without a firmware rebuild -- and the Python one is the
fifth column of the differential check. So the check that matters is that
they agree: two independent readers of one image over the same bytes, which
is the same argument the four backends are held to.

What this build of the C walker does not render is refused by name, and the
tests assert the refusals as well as the answers. A walker that returned a
number for a member it could not place would be returning a wrong length
that reads exactly like a right one.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from every_schema import ROOT
from situc.layout import solve
from situc.pack import pack
from situc.parser import parse
from situc.resolve import resolve
from situc.diagnostics import Source
from walker.image import load
from walker.walk import Refused, acquire, read_scalar

COMPILER = shutil.which("cc") or shutil.which("gcc")
WALKER   = ROOT / "walker" / "c"

#: The same flags `make test-c` builds generated code with. An embedded
#: walker that needs a relaxed warning set is one nobody can put in a build.
WARNINGS = ("-std=c11", "-O2", "-Wall", "-Wextra", "-Werror",
            "-Wconversion", "-Wsign-conversion")

DRIVER = """#include <stdio.h>
#include <stdlib.h>
#include "situ_walk.h"

int main(int argc, char **argv)
{
	static uint8_t img[65536];
	static uint8_t msg[512];

	if (argc < 3) {
		return 2;
	}

	FILE *f = fopen(argv[1], "rb");
	if (!f) {
		return 2;
	}
	const size_t n = fread(img, 1, sizeof img, f);
	fclose(f);

	situ_walk_image image;
	if (situ_walk_open(&image, img, (uint32_t)n) != SITU_WALK_OK) {
		printf("malformed\\n");
		return 1;
	}

	uint32_t len = 0;
	for (const char *p = argv[2]; p[0] && p[1]; p += 2) {
		char pair[3];
		pair[0] = p[0];
		pair[1] = p[1];
		pair[2] = 0;
		msg[len++] = (uint8_t)strtoul(pair, NULL, 16);
	}

	uint32_t first = 0;
	uint32_t count = 0;
	if (situ_walk_members(&image, 0, &first, &count) != SITU_WALK_OK) {
		return 1;
	}
	for (uint32_t i = 0; i < count; i++) {
		uint64_t value = 0;
		if (situ_walk_read(&image, msg, len, 0u, first + i, &value)
				== SITU_WALK_OK) {
			printf("%llu\\n", (unsigned long long)value);
		} else {
			printf("refused\\n");
		}
	}
	return 0;
}
"""


def image_for(path: Path) -> bytes:
	source   = Source(str(path), path.read_text(encoding="ascii"))
	schema   = parse(source)
	resolved = resolve(schema, solve(schema))
	return pack(schema, resolved)[0]


def python_answers(blob: bytes, message: bytes) -> list[str]:
	image = load(blob)
	view  = acquire(image, message, 0)
	found = []
	for index in image.members(image.structs[0]):
		try:
			found.append(str(read_scalar(view, index)))
		except Refused:
			found.append("refused")
	return found


def c_answers(tmp_path: Path, blob: bytes, message: bytes) -> list[str]:
	(tmp_path / "img").write_bytes(blob)
	(tmp_path / "drive.c").write_text(DRIVER, encoding="ascii")

	assert COMPILER is not None
	built = subprocess.run(
		[COMPILER, *WARNINGS, f"-I{WALKER}", str(tmp_path / "drive.c"),
		 str(WALKER / "situ_walk.c"), "-o", str(tmp_path / "drive")],
		capture_output=True, text=True)
	assert built.returncode == 0, built.stderr

	ran = subprocess.run([str(tmp_path / "drive"), str(tmp_path / "img"),
	                      message.hex()], capture_output=True, text=True)
	assert ran.returncode == 0, ran.stdout + ran.stderr
	return ran.stdout.split()


@pytest.mark.skipif(COMPILER is None, reason="no C compiler")
def test_it_compiles_under_the_same_warnings(tmp_path: Path) -> None:
	"""An embedded walker needing a relaxed warning set is one nobody can
	put in a build."""
	assert COMPILER is not None
	built = subprocess.run(
		[COMPILER, *WARNINGS, "-c", str(WALKER / "situ_walk.c"),
		 "-o", str(tmp_path / "o.o")],
		capture_output=True, text=True)

	assert built.returncode == 0, built.stderr


@pytest.mark.skipif(COMPILER is None, reason="no C compiler")
def test_it_agrees_with_the_python_walker(tmp_path: Path) -> None:
	"""Two independent readers of one image over the same bytes.

	Including the refusals: what this build declines is part of what it
	says, and a disagreement about *that* is as real as one about a value.
	"""
	blob    = image_for(ROOT / "examples" / "udp" / "udp.situ")
	message = bytes.fromhex("1f90238200105f2a")

	assert c_answers(tmp_path, blob, message) == python_answers(blob, message)


@pytest.mark.skipif(COMPILER is None, reason="no C compiler")
def test_a_variable_member_is_refused_rather_than_guessed(
		tmp_path: Path) -> None:
	"""udp's payload has no constant extent, and this build says so. A
	number here would be a wrong length that reads like a right one."""
	blob = image_for(ROOT / "examples" / "udp" / "udp.situ")

	answers = c_answers(tmp_path, blob, bytes.fromhex("1f90238200105f2a"))

	assert answers[-1] == "refused"
	assert all(one != "refused" for one in answers[:-1])


@pytest.mark.skipif(COMPILER is None, reason="no C compiler")
def test_a_truncated_image_is_malformed_rather_than_read(
		tmp_path: Path) -> None:
	"""The image is the least trusted input this component has, so every
	table it names is checked against the whole before anything indexes
	one."""
	blob = image_for(ROOT / "examples" / "udp" / "udp.situ")
	(tmp_path / "img").write_bytes(blob[:len(blob) // 2])
	(tmp_path / "drive.c").write_text(DRIVER, encoding="ascii")

	assert COMPILER is not None
	subprocess.run(
		[COMPILER, *WARNINGS, f"-I{WALKER}", str(tmp_path / "drive.c"),
		 str(WALKER / "situ_walk.c"), "-o", str(tmp_path / "drive")],
		capture_output=True, text=True, check=True)

	ran = subprocess.run([str(tmp_path / "drive"), str(tmp_path / "img"), "00"],
	                     capture_output=True, text=True)

	assert ran.stdout.strip() == "malformed"
