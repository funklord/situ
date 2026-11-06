"""Measuring an element reads nothing past the view it was handed (26.901).

A walk hands `extent` whatever remains of the frame, one byte of it included.
The plain form was guarded for that (26.322, 26.325); the recursive form was
not, and neither was a run's count read from a field the message places. Both
were reached from `validate`, so a one-byte message read past its buffer in C
and C++ on any recursive struct with a length after its first byte.

Every message here sits in an allocation of exactly its own size, so a read one
byte long is an AddressSanitizer abort and not a value nobody looks at.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from test_walker_c import (COMPILER, _inline_image, c_verdict,
                           python_verdict, shape_named_text)

ROOT     = Path(__file__).resolve().parents[2]
HOST_CC  = shutil.which("cc") or shutil.which("gcc")
HOST_CXX = shutil.which("g++") or shutil.which("clang++")
SANITIZE = ["-g", "-O1", "-fsanitize=address,undefined",
            "-fno-sanitize-recover=all"]

SCHEMA = """target buffer;
endian big;

struct leafy {
	u8  tag;
	u8  len;
	u8  data[len];
}

struct node [depth = 4] {
	u8    tag;
	u8    len;
	u8    data[len];
	u8    kids;
	node  child[kids];
}

struct twice {
	u8     a;
	u8     first[a];
	u8     b;
	u8     second[b];
	u16    c;
	leafy  third[c];
	u8     d;
	u8     fourth[d + 1];
	u8     e;
	leafy  fifth[e + 1];
}

struct list   { node  all[remaining]; }
struct twices { twice all[remaining]; }
"""

#: Small lengths matter most, and half the bytes small keeps a message past
#: its first refusal: 39170 of 1.2 million validated when this was measured.
FILL = """
static unsigned long seed = 12345u;

static unsigned draw(void)
{
	seed = seed * 6364136223846793005ul + 1442695040888963407ul;
	return (unsigned)(seed >> 33);
}

static void fill(unsigned char *bytes, unsigned length)
{
	for (unsigned i = 0u; i < length; i++) {
		const unsigned value = draw() & 0xffu;

		bytes[i] = (unsigned char)((value & 0x80u) ? value : value & 3u);
	}
}
"""

C_PROBE = """#include "unit.h"
#include <stdio.h>
#include <stdlib.h>
""" + FILL + """
int main(void)
{
	long accepted = 0;

	for (long i = 0; i < 60000; i++) {
		const unsigned length = draw() % 32u;
		unsigned char *bytes = malloc(length ? length : 1u);
		situ_msg_t     msg;
		situ_view_t    view;

		fill(bytes, length);
		situ_msg_init(&msg, bytes, length);
		if (situ_list_view(&msg, 0, length, &view) == SITU_OK
				&& situ_list_validate(view) == SITU_OK) {
			accepted++;
		}
		if (situ_twices_view(&msg, 0, length, &view) == SITU_OK
				&& situ_twices_validate(view) == SITU_OK) {
			accepted++;
		}
		free(bytes);
	}
	printf("%ld\\n", accepted);
	return 0;
}
"""

CXX_PROBE = """#include "unit.hpp"
#include <cstdio>
#include <cstdlib>
""" + FILL + """
int main()
{
	long accepted = 0;
	const auto ok = ::situ::rt::err::ok;

	for (long i = 0; i < 60000; i++) {
		const unsigned length = draw() % 32u;
		auto *bytes = static_cast<unsigned char *>(
			std::malloc(length ? length : 1u));

		fill(bytes, length);
		::situ::rt::message msg(bytes, length);
		::situ::list   list;
		::situ::twices twices;

		if (::situ::list::at(msg, 0u, length, list) == ok
				&& list.validate() == ok) {
			accepted++;
		}
		if (::situ::twices::at(msg, 0u, length, twices) == ok
				&& twices.validate() == ok) {
			accepted++;
		}
		std::free(bytes);
	}
	std::printf("%ld\\n", accepted);
	return 0;
}
"""


def _generate(tmp_path: Path, target: str) -> None:
	schema = tmp_path / "unit.situ"
	schema.write_text(SCHEMA, encoding="ascii")
	subprocess.run([str(ROOT / "bin" / "situc"), "build", "--target", target,
	                "--out", str(tmp_path), str(schema)],
	               check=True, capture_output=True, text=True)


def _run(binary: Path) -> None:
	ran = subprocess.run([str(binary)], capture_output=True, text=True,
	                     timeout=240)
	assert ran.returncode == 0, ran.stderr[:2000]
	# Nothing validated would mean every message stopped at the first
	# refusal, and the walk this file is about was never taken.
	assert int(ran.stdout) > 0


@pytest.mark.skipif(HOST_CC is None, reason="no host C compiler")
def test_c_reads_nothing_past_the_view(tmp_path: Path) -> None:
	assert HOST_CC is not None
	_generate(tmp_path, "c")
	(tmp_path / "main.c").write_text(C_PROBE, encoding="ascii")
	subprocess.run([HOST_CC, "-std=c11", *SANITIZE,
	                f"-I{tmp_path}", f"-I{ROOT / 'runtime' / 'c'}",
	                str(tmp_path / "main.c"), str(tmp_path / "unit.c"),
	                str(ROOT / "runtime" / "c" / "situ.c"),
	                "-o", str(tmp_path / "probe")],
	               check=True, capture_output=True, text=True)
	_run(tmp_path / "probe")


@pytest.mark.skipif(HOST_CXX is None, reason="no host C++ compiler")
def test_cpp_reads_nothing_past_the_view(tmp_path: Path) -> None:
	assert HOST_CXX is not None
	_generate(tmp_path, "cpp")
	(tmp_path / "main.cpp").write_text(CXX_PROBE, encoding="ascii")
	subprocess.run([HOST_CXX, "-std=c++17", *SANITIZE,
	                f"-I{tmp_path}", f"-I{ROOT / 'runtime' / 'cpp'}",
	                f"-I{ROOT / 'runtime' / 'c'}",
	                "-x", "c++", str(tmp_path / "main.cpp"),
	                str(ROOT / "runtime" / "c" / "situ.c"),
	                "-o", str(tmp_path / "probe")],
	               check=True, capture_output=True, text=True)
	_run(tmp_path / "probe")


HELD = """target buffer;
endian big;
struct box   [depth = 2] { u8 kind; u8 len; u8 body[len] [holds = boxes]; }
struct boxes [depth = 2] { box all[remaining]; }
"""

#: Four deep with a last element the frame cuts short. The four backends ask
#: `nesting` first, so the depth answers; a walker that met the cut element
#: first called it bounds, a different refusal of the same bytes.
TOO_DEEP_AND_CUT = bytes.fromhex("0006" "0004" "0002" "0000" "07")

#: Within `[depth]`, so the cut element is the only thing wrong.
CUT = bytes.fromhex("0000" "07")


@pytest.mark.skipif(COMPILER is None, reason="needs a C compiler")
def test_both_walkers_ask_the_depth_first(tmp_path: Path) -> None:
	image = _inline_image(HELD)
	shape = shape_named_text(HELD, "boxes")

	assert python_verdict(image, CUT, shape) == "1"
	assert c_verdict(tmp_path, image, CUT, shape) == "1"

	verdict = python_verdict(image, TOO_DEEP_AND_CUT, shape)
	assert verdict not in ("0", "1")
	assert c_verdict(tmp_path, image, TOO_DEEP_AND_CUT, shape) == verdict
