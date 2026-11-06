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

from situc.codegen.c.vectors import parse_vectors
from situc.diagnostics import Source
from test_walker_c import (COMPILER, VERDICT, _build_driver, _inline_image,
                           c_verdict, image_for, python_verdict, shape_named,
                           shape_named_text)

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

struct gap {
	u8  a;
	u8  b [max = a - 1];
	u8  held[a - b - 1];
	u8  after;
}

struct list   { node  all[remaining]; }
struct twices { twice all[remaining]; }
struct gaps   { gap   all[remaining]; }

struct trailer {
	peek u8  count at end;
	peek u16 mark at end - 1;
	u8  body[remaining];
	u8  padding[count];
}
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
		if (situ_gaps_view(&msg, 0, length, &view) == SITU_OK
				&& situ_gaps_validate(view) == SITU_OK) {
			accepted++;
		}
		if (situ_trailer_view(&msg, 0, length, &view) == SITU_OK
				&& situ_trailer_validate(view) == SITU_OK) {
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
		::situ::gaps   gaps;
		::situ::trailer trailer;

		if (::situ::list::at(msg, 0u, length, list) == ok
				&& list.validate() == ok) {
			accepted++;
		}
		if (::situ::twices::at(msg, 0u, length, twices) == ok
				&& twices.validate() == ok) {
			accepted++;
		}
		if (::situ::gaps::at(msg, 0u, length, gaps) == ok
				&& gaps.validate() == ok) {
			accepted++;
		}
		if (::situ::trailer::at(msg, 0u, length, trailer) == ok
				&& trailer.validate() == ok) {
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


#: The short forms stop at the floor; the rest are whole headers with nothing
#: present, and a header was all the cycle needed.
RADIOTAP = ["", "00", "0000", "00000800", "0000080000000000",
            "0000090000000000"]


@pytest.mark.skipif(COMPILER is None, reason="needs a C compiler")
@pytest.mark.parametrize("message", RADIOTAP)
def test_a_pad_arm_is_a_step_in_the_c_walkers_chain(message: str,
		tmp_path: Path) -> None:
	"""An arm's `[align]` asked its variant's offset, which asked the arm.

	`walk.py` took the pad as a step in the chain and measured it from the
	variant (26.889); `situ_walk.c` did neither, and the public
	`situ_walk_offset_bits` restarted `depth` on each lap, so the ceiling never
	fired. It ran to the end of the stack. The agreement sweeps read the dead
	driver as a shape they could not acquire.
	"""
	path  = ROOT / "example" / "radiotap" / "radiotap.situ"
	image = image_for(path)
	shape = shape_named(path, "radiotap")
	data  = bytes.fromhex(message)
	assert c_verdict(tmp_path, image, data, shape) \
		== python_verdict(image, data, shape)


VECTORS = sorted(ROOT.glob("example/*/*.vectors"))

#: Where the C walker says it cannot and the Python walker answers. Each read
#: is one `situ_walk.c` declines rather than guess, and says so at the read.
DECLINED = {
	"netlink": "`endian native`: the capture's machine is not the reader's",
}


@pytest.mark.skipif(COMPILER is None, reason="needs a C compiler")
@pytest.mark.parametrize("vectors", VECTORS, ids=lambda path: path.stem)
def test_both_walkers_agree_about_every_committed_vector(vectors: Path,
		tmp_path: Path) -> None:
	"""Real bytes through both walkers, which nothing did.

	Radiotap's nine captures killed the C walker from 26.889 to 26.901, and it
	refused JSON's `-1.5e3` from 0056, while every random sweep went on
	passing: random bytes rarely make a header or spell a decimal, and a dead
	driver read as a skip.
	"""
	path  = next(vectors.parent.glob("*.situ"))
	cases = parse_vectors(Source(str(vectors),
	                             vectors.read_text(encoding="utf-8")))
	assert cases, f"{vectors} states nothing"
	image = image_for(path)
	(tmp_path / "img").write_bytes(image)
	drive = _build_driver(tmp_path, image, VERDICT)
	for case in cases:
		shape = shape_named(path, case.struct)
		data  = bytes(case.data)
		assert data, case.name
		said = c_verdict(tmp_path, image, data, shape, drive)
		if vectors.stem in DECLINED and said == "cannot-say":
			continue
		assert said == python_verdict(image, data, shape), case.name
