"""A message built around an arm already in its buffer: one buffer, no copy.

fuzznet builds each answer twice -- `X_row_build` into one buffer, then
`message_build_X` copying it into a second -- in some thirty places
(inbox 20261120T1231Z, item b). The builders copied the caller's bytes
with `memcpy`, which is undefined when they are already where they go, so
building the row at its place in the message was not allowed. They now
put them with `memmove`, and not at all when they are the same bytes
(26.1121): build the row at the arm's offset, then the message around it.

What this pins is the pattern's RESULT -- the same bytes as two buffers.
It cannot show the undefined behaviour it removes: `memcpy` onto identical
bytes happens to work here, and ASan, tried, does not report a copy whose
source and destination are the same address. The change is to what the C
standard allows, which no run here can measure.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from every_schema import ROOT

SCHEMA = """target buffer;
endian big;
struct row { u8 n; u8 name[n]; u32 at; }
struct message { u8 cmd; variant body switch (cmd) { case 5: row r; default: error; } }
"""

C_DRIVER = r"""
#include <stdio.h>
#include <string.h>
#include "m_build.h"
int main(void)
{
	uint8_t rowbuf[64], two[64], one[64];
	uint32_t row_len = 0, n2 = 0, n1 = 0, inner = 0;
	const uint8_t name[] = { 'l', 'a', 'm', 'p' };

	if (situ_row_build(rowbuf, sizeof rowbuf, name, 4, 1700000000u, &row_len)
	    || situ_message_build_r(two, sizeof two, rowbuf, row_len, &n2))
		return 1;
	memset(one, 0xEE, sizeof one);
	if (situ_row_build(one + 1, sizeof one - 1, name, 4, 1700000000u, &inner)
	    || situ_message_build_r(one, sizeof one, one + 1, inner, &n1))
		return 2;
	printf("%u %u %d\n", n2, n1, n1 == n2 && !memcmp(one, two, n1));
	return 0;
}
"""

CPP_DRIVER = r"""
#include <cstdio>
#include <cstring>
#include "m_build.hpp"
int main()
{
	std::uint8_t rowbuf[64], two[64], one[64];
	std::uint32_t row_len = 0, n2 = 0, n1 = 0, inner = 0;
	const std::uint8_t name[] = { 'l', 'a', 'm', 'p' };
	if (::situ_build::row(rowbuf, sizeof rowbuf, name, 4, 1700000000u, row_len)
	        != ::situ::rt::err::ok
	    || ::situ_build::message_r(two, sizeof two, rowbuf, row_len, n2)
	        != ::situ::rt::err::ok)
		return 1;
	std::memset(one, 0xEE, sizeof one);
	if (::situ_build::row(one + 1, sizeof one - 1, name, 4, 1700000000u, inner)
	        != ::situ::rt::err::ok
	    || ::situ_build::message_r(one, sizeof one, one + 1, inner, n1)
	        != ::situ::rt::err::ok)
		return 2;
	std::printf("%u %u %d\n", n2, n1, n1 == n2 && !std::memcmp(one, two, n1));
}
"""


def _generate(tmp_path: Path, target: str) -> None:
	(tmp_path / "m.situ").write_text(SCHEMA, encoding="ascii")
	for layer in ("view", "edit"):
		done = subprocess.run(
			[sys.executable, str(ROOT / "bin" / "situc"), "build", "--target",
			 target, "--layer", layer, "--out", str(tmp_path),
			 str(tmp_path / "m.situ")], capture_output=True, text=True,
			timeout=300)
		assert done.returncode == 0, done.stderr


def _runtime(tmp_path: Path) -> Path:
	"""libsitu.a, built here rather than taken from the tree's build."""
	out = tmp_path / "runtime"
	done = subprocess.run(["make", "-C", str(ROOT / "runtime" / "c"),
	                       f"BUILD_DIR={out}"], capture_output=True,
	                      text=True, timeout=300)
	assert done.returncode == 0, done.stderr
	return next(out.rglob("libsitu.a"))


@pytest.mark.skipif(shutil.which("cc") is None, reason="no C compiler")
def test_c_builds_the_message_around_a_row_already_in_place(
		tmp_path: Path) -> None:
	_generate(tmp_path, "c")
	(tmp_path / "t.c").write_text(C_DRIVER, encoding="ascii")
	built = subprocess.run(
		["cc", "-Wall", "-Wextra", "-Werror", f"-I{ROOT / 'runtime' / 'c'}",
		 f"-I{tmp_path}", str(tmp_path / "t.c"), str(tmp_path / "m.c"),
		 str(_runtime(tmp_path)), "-o", str(tmp_path / "t")],
		capture_output=True, text=True, timeout=300)
	assert built.returncode == 0, built.stderr
	ran = subprocess.run([str(tmp_path / "t")], capture_output=True,
	                     text=True, timeout=60)
	assert ran.stdout.split() == ["10", "10", "1"], (ran.returncode,
	                                                ran.stdout)


@pytest.mark.skipif(shutil.which("g++") is None, reason="no C++ compiler")
def test_cpp_builds_the_message_around_a_row_already_in_place(
		tmp_path: Path) -> None:
	_generate(tmp_path, "cpp")
	(tmp_path / "t.cpp").write_text(CPP_DRIVER, encoding="ascii")
	built = subprocess.run(
		["g++", "-std=c++17", "-Wall", "-Wextra", "-Werror",
		 f"-I{ROOT / 'runtime' / 'c'}", f"-I{ROOT / 'runtime' / 'cpp'}",
		 f"-I{tmp_path}", str(tmp_path / "t.cpp"), str(_runtime(tmp_path)),
		 "-o", str(tmp_path / "t")],
		capture_output=True, text=True, timeout=300)
	assert built.returncode == 0, built.stderr
	ran = subprocess.run([str(tmp_path / "t")], capture_output=True,
	                     text=True, timeout=60)
	assert ran.stdout.split() == ["10", "10", "1"], (ran.returncode,
	                                                ran.stdout)
