"""Two faults in the generated C reader, found by libFuzzer on `example/cbor`
once the harness stopped spinning on a claimed count (4672cfec).

**A read past the buffer.** `item` opens with `peek u3 major`, which takes
no bytes, so the extent's guard -- the struct's constant prefix, zero --
was never emitted, and an item starting where the bytes end read its
major type anyway: a map of one pair missing its value, two bytes, read a
third under ASan from `validate`. The guard now covers the struct's own
peeks, counted as the layout counts its minimum.

**Time exponential in nesting.** The nesting probe stepped over each
sibling by an extent measured from depth zero, and went on probing after
a child had already answered "too deep" -- an answer nothing later can
change, every such answer being one above the cap. A run of 40 CBOR maps
behind a few other items took a second to validate at -O1, about 1.6
times per map added. The probe now stops at the first too-deep answer.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT     = Path(__file__).resolve().parents[2]
COMPILER = shutil.which("cc") or shutil.which("gcc")

VALIDATE = r"""
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "cbor.h"
int main(void)
{
	static uint8_t input[8192];
	const size_t n = fread(input, 1, sizeof input, stdin);
	uint8_t *buf = malloc(n == 0 ? 1 : n);	/* exactly its bytes, for ASan */
	memcpy(buf, input, n);
	situ_msg_t m;
	situ_view_t v;
	situ_msg_init(&m, buf, (uint32_t)n);
	int said = -1;
	if (situ_map_view(&m, 0u, (uint32_t)n, &v) == SITU_OK)
		said = (int)situ_map_validate(v);
	free(buf);
	printf("%d\n", said);
	return 0;
}
"""

#: libFuzzer's slow input, minimized: other items, then a run of 0xb2 --
#: maps each claiming 18 pairs -- then pairs of zeros and a tail.
SLOW = bytes.fromhex(
	"3afe3fe5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5543afeeb8b36"
	"a518d790e7964f" + "b2" * 34 + "00" * 16 +
	"8223a1ac72b2b2b2cab39419" "8358af0a86af22b2")


@pytest.fixture(scope="module")
def reader(tmp_path_factory: pytest.TempPathFactory) -> Path:
	if COMPILER is None:
		pytest.skip("no C compiler")
	out = tmp_path_factory.mktemp("cbor")
	done = subprocess.run(
		[sys.executable, "-m", "situc", "build",
		 str(ROOT / "example" / "cbor" / "cbor.situ"), "--out", str(out)],
		cwd=ROOT, capture_output=True, text=True)
	assert done.returncode == 0, done.stderr
	(out / "validate.c").write_text(VALIDATE, encoding="ascii")
	runtime = ROOT / "runtime" / "c"
	built = subprocess.run(
		[COMPILER or "cc", "-std=c11", "-O1", "-g",
		 "-fsanitize=address,undefined", "-fno-sanitize-recover=all",
		 f"-I{runtime}", f"-I{out}", str(out / "validate.c"),
		 str(out / "cbor.c"), str(runtime / "situ.c"), "-o",
		 str(out / "validate")], capture_output=True, text=True)
	assert built.returncode == 0, built.stderr
	return out / "validate"


def test_a_map_missing_its_last_value_reads_nothing_past_it(reader: Path) -> None:
	ran = subprocess.run([str(reader)], input=bytes([0xA1, 0x00]),
	                     capture_output=True, timeout=60)
	assert ran.returncode == 0, ran.stderr.decode(errors="replace")[-1500:]
	assert ran.stdout.strip() not in (b"0", b"-1"), ran.stdout


def test_nested_maps_behind_other_items_validate_in_time(reader: Path) -> None:
	run = re.search(rb"(\xb2)+", SLOW)
	assert run is not None and run.end() - run.start() == 34
	deeper = SLOW[:run.start()] + b"\xb2" * 60 + SLOW[run.end():]
	ran = subprocess.run([str(reader)], input=deeper, capture_output=True,
	                     timeout=20)
	assert ran.returncode == 0, ran.stderr.decode(errors="replace")[-1500:]


CXX = shutil.which("g++") or shutil.which("c++")

VALIDATE_CPP = r"""
#include <cstdio>
#include <vector>
#include "cbor.hpp"
int main()
{
	std::vector<std::uint8_t> in;
	int c;
	while ((c = std::getchar()) != EOF)
		in.push_back(static_cast<std::uint8_t>(c));
	std::vector<std::uint8_t> buf(in);	/* exactly its bytes, for ASan */
	::situ::rt::message m(buf.data(), static_cast<std::uint32_t>(buf.size()));
	::situ::map v;
	int said = -1;
	if (::situ::map::at(m, 0u, static_cast<std::uint32_t>(buf.size()), v)
	    == ::situ::rt::err::ok)
		said = static_cast<int>(v.validate());
	std::printf("%d\n", said);
	return 0;
}
"""


@pytest.mark.skipif(CXX is None, reason="no C++ compiler")
def test_the_cpp_reader_reads_nothing_past_a_missing_value(tmp_path: Path) -> None:
	"""C++ carried the same guard on the constant prefix alone."""
	done = subprocess.run(
		[sys.executable, "-m", "situc", "build",
		 str(ROOT / "example" / "cbor" / "cbor.situ"), "--target", "cpp",
		 "--out", str(tmp_path)], cwd=ROOT, capture_output=True, text=True)
	assert done.returncode == 0, done.stderr
	(tmp_path / "v.cpp").write_text(VALIDATE_CPP, encoding="ascii")
	runtime = ROOT / "runtime"
	built = subprocess.run(
		[CXX or "c++", "-std=c++17", "-O1", "-g",
		 "-fsanitize=address,undefined", "-fno-sanitize-recover=all",
		 f"-I{runtime / 'c'}", f"-I{runtime / 'cpp'}", f"-I{tmp_path}",
		 str(tmp_path / "v.cpp"), str(runtime / "c" / "situ.c"), "-o",
		 str(tmp_path / "v")], capture_output=True, text=True)
	assert built.returncode == 0, built.stderr
	ran = subprocess.run([str(tmp_path / "v")], input=bytes([0xA1, 0x00]),
	                     capture_output=True, timeout=60)
	assert ran.returncode == 0, ran.stderr.decode(errors="replace")[-1500:]
	assert ran.stdout.strip() not in (b"0", b"-1"), ran.stdout


def test_the_python_reader_judges_nested_maps_in_time(tmp_path: Path) -> None:
	"""Python's probe had the same shape, and it was worse: 40 maps took
	159 seconds."""
	done = subprocess.run(
		[sys.executable, "-m", "situc", "build",
		 str(ROOT / "example" / "cbor" / "cbor.situ"), "--target", "python",
		 "--out", str(tmp_path)], cwd=ROOT, capture_output=True, text=True)
	assert done.returncode == 0, done.stderr
	run = re.search(rb"(\xb2)+", SLOW)
	assert run is not None
	deeper = SLOW[:run.start()] + b"\xb2" * 40 + SLOW[run.end():]
	script = (
		"import sys\n"
		f"sys.path[:0] = [{str(tmp_path)!r}, {str(ROOT / 'runtime' / 'python')!r}]\n"
		"from situ_runtime import Message, SituError\n"
		"import cbor\n"
		"data = sys.stdin.buffer.read()\n"
		"try:\n"
		"\tcbor.map.at(Message(bytearray(data)), 0, len(data)).validate()\n"
		"except SituError:\n"
		"\tpass\n"
		"print('judged')\n")
	ran = subprocess.run([sys.executable, "-c", script], input=deeper,
	                     capture_output=True, timeout=20)
	assert ran.stdout.strip() == b"judged", ran.stderr.decode()[-1500:]
