"""A C++ arm accessor read its discriminant one byte past the view.

An enum discriminant behind a variable member sits at a dynamic offset.
C++'s `_over_fields` rendered it as a raw load at `situ_advance_u32(...)`,
which clamps an offset to the frame's limit, so a discriminant the frame
does not reach was read at `base[limit]`: one byte past the view, and an
arm accessor answered `ok` off whatever that byte was. Its own `which()`
getter checked; the arm test did not ask it (26.747).

C++ is the one backend whose accessors are not lax, so this is a memory
fault rather than a wrong number. Found by the composed sweep, in every
cell with an arm behind a nested-struct count, as C++ answering `ok=1`
where the other three answered `ok=0`.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import fourway						# noqa: E402
from situc.codegen.cpp import generate as generate_cpp	# noqa: E402
from situc.layout import solve				# noqa: E402
from situc.parser import parse_text			# noqa: E402
from situc.resolve import resolve			# noqa: E402

HOST_CXX = shutil.which("g++") or shutil.which("clang++")

SCHEMA = """target buffer;
endian big;
struct hdr { u16 lead; u8 n; }
enum arm_kind : u8 {
	first   = 0x11,
	second  = 0x22,
	default = error,
}
struct s {
	hdr  head;
	u8   gap[head.n];
	arm_kind  which;
	variant body switch (which) {
		case arm_kind.first:  u8  run[head.n];
		case arm_kind.second: u8  other[2];
	}
}
"""

#: `head.n` is 5, so `gap` runs off the end and `which` is past the frame.
SHORT = bytes([0x00, 0x00, 0x05, 0x01, 0x02])

PROBE = """#include "unit.hpp"
#include <cstdio>
#include <cstdlib>
#include <cstring>

int main()
{
	static const unsigned char msg[] = { 0x00, 0x00, 0x05, 0x01, 0x02 };
	auto *p = static_cast<std::uint8_t *>(std::malloc(sizeof msg));
	std::memcpy(p, msg, sizeof msg);
	::situ::rt::message m(p, sizeof msg);
	::situ::s view;
	if (::situ::s::at(m, 0, sizeof msg, view) != ::situ::rt::err::ok) {
		return 2;
	}
	::situ::rt::bytes out;
	std::printf("%d\\n", static_cast<int>(view.body_run(out)));
	std::free(p);
	return 0;
}
"""


@pytest.mark.skipif(HOST_CXX is None, reason="no host C++ compiler")
def test_the_arm_test_stays_inside_the_view(tmp_path: Path) -> None:
	"""The buffer is exactly the message, so a read past it is ASan's."""
	schema   = parse_text(SCHEMA)
	resolved = resolve(schema, solve(schema))
	(tmp_path / "unit.hpp").write_text(
		generate_cpp(schema, resolved, "unit").header, encoding="ascii")
	(tmp_path / "main.cpp").write_text(PROBE, encoding="ascii")
	made = subprocess.run(
		[HOST_CXX or "g++", "-std=c++17", "-g",
		 "-fsanitize=address,undefined", "-fno-sanitize-recover=all",
		 f"-I{ROOT / 'runtime' / 'c'}", f"-I{ROOT / 'runtime' / 'cpp'}",
		 f"-I{tmp_path}", str(tmp_path / "main.cpp"),
		 str(ROOT / "runtime" / "c" / "situ.c"), "-o", str(tmp_path / "bin")],
		capture_output=True, text=True)
	assert made.returncode == 0, made.stderr[-2000:]
	ran = subprocess.run([str(tmp_path / "bin")], capture_output=True,
	                     text=True)
	assert ran.returncode == 0, ran.stderr[-2000:]
	assert ran.stdout.strip() != "0", "the arm was answered from past the view"


def test_all_four_refuse_the_arm(tmp_path: Path) -> None:
	schema = tmp_path / "unit.situ"
	schema.write_text(SCHEMA, encoding="ascii")
	try:
		command = fourway.build(tmp_path, schema)
	except fourway.BuildFailed as failed:
		pytest.skip(f"a backend could not be built here: {failed}")
	for name, argv in command.items():
		answer = fourway.answers(argv, SHORT, tmp_path)
		assert "body_run ok=0 len=0" in answer.splitlines(), (
			f"{name}: {answer!r}")
