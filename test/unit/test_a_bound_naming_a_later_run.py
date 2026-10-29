"""A bound that names an element of a later run reads inside the frame.

`[max = n[0].f]` compares against element zero of `n`. `validate` guarded
that the count reaches the element -- not that the frame holds it -- and
where the run is declared AFTER the bounded member, the run's own frame
check comes after the comparison. So `u8 v [max = n[0].f]; u8 c; rec n[c];`
over `00 01` read `base[2]`, past a two-byte view, in C and in C++; Rust
and Python read safely and answered BOUNDS (26.751).

Found from 26.747's lens -- a load at a computed offset in C++ -- by asking
which loads `validate` makes before anything has checked the frame.
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
from situc.codegen.c import generate as generate_c	# noqa: E402
from situc.codegen.cpp import generate as generate_cpp	# noqa: E402
from situc.layout import solve				# noqa: E402
from situc.parser import parse_text			# noqa: E402
from situc.resolve import resolve			# noqa: E402

HOST_CC  = shutil.which("cc") or shutil.which("gcc")
HOST_CXX = shutil.which("g++") or shutil.which("clang++")
SANITIZE = ["-g", "-fsanitize=address,undefined", "-fno-sanitize-recover=all"]

SCHEMA = """target buffer;
endian big;
struct rec { u8 f; u8 g; }
struct s {
	u8   v [max = n[0].f];
	u8   c;
	rec  n[c];
}
"""

#: The verdict all four give, and the bytes.
CASES = {
	"one element claimed, none there": ("1", bytes.fromhex("0001")),
	"one element, within the bound":   ("0", bytes.fromhex("00010506")),
	"one element, over the bound":     ("2", bytes.fromhex("07010506")),
}

C_PROBE = """#include "unit.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
int main(void)
{
	static const unsigned char msg[] = { 0x00, 0x01 };
	uint8_t *p = malloc(sizeof msg);
	situ_msg_t m;
	situ_view_t view;
	memcpy(p, msg, sizeof msg);
	situ_msg_init(&m, p, sizeof msg);
	if (situ_s_view(&m, 0, sizeof msg, &view) != SITU_OK) {
		return 2;
	}
	printf("%d\\n", (int)situ_s_validate(view));
	free(p);
	return 0;
}
"""

CPP_PROBE = """#include "unit.hpp"
#include <cstdio>
#include <cstdlib>
#include <cstring>
int main()
{
	static const unsigned char msg[] = { 0x00, 0x01 };
	auto *p = static_cast<std::uint8_t *>(std::malloc(sizeof msg));
	std::memcpy(p, msg, sizeof msg);
	::situ::rt::message m(p, sizeof msg);
	::situ::s view;
	if (::situ::s::at(m, 0, sizeof msg, view) != ::situ::rt::err::ok) {
		return 2;
	}
	std::printf("%d\\n", static_cast<int>(view.validate()));
	std::free(p);
	return 0;
}
"""


def _built() -> tuple[object, object]:
	schema = parse_text(SCHEMA)
	return schema, resolve(schema, solve(schema))


@pytest.mark.skipif(HOST_CC is None, reason="no host C compiler")
def test_c_stays_inside_the_view(tmp_path: Path) -> None:
	schema, resolved = _built()
	built = generate_c(schema, resolved, "unit")	# type: ignore[arg-type]
	(tmp_path / "unit.h").write_text(built.header, encoding="ascii")
	(tmp_path / "unit.c").write_text(built.source, encoding="ascii")
	(tmp_path / "main.c").write_text(C_PROBE, encoding="ascii")
	made = subprocess.run(
		[HOST_CC or "cc", "-std=c11", *SANITIZE,
		 f"-I{ROOT / 'runtime' / 'c'}", f"-I{tmp_path}",
		 str(tmp_path / "main.c"), str(tmp_path / "unit.c"),
		 str(ROOT / "runtime" / "c" / "situ.c"), "-o", str(tmp_path / "bin")],
		capture_output=True, text=True)
	assert made.returncode == 0, made.stderr[-2000:]
	ran = subprocess.run([str(tmp_path / "bin")], capture_output=True,
	                     text=True)
	assert ran.returncode == 0, ran.stderr[-2000:]
	assert ran.stdout.strip() == "1", ran.stdout


@pytest.mark.skipif(HOST_CXX is None, reason="no host C++ compiler")
def test_cpp_stays_inside_the_view(tmp_path: Path) -> None:
	schema, resolved = _built()
	(tmp_path / "unit.hpp").write_text(
		generate_cpp(schema, resolved, "unit").header,	# type: ignore[arg-type]
		encoding="ascii")
	(tmp_path / "main.cpp").write_text(CPP_PROBE, encoding="ascii")
	made = subprocess.run(
		[HOST_CXX or "g++", "-std=c++17", *SANITIZE,
		 f"-I{ROOT / 'runtime' / 'c'}", f"-I{ROOT / 'runtime' / 'cpp'}",
		 f"-I{tmp_path}", str(tmp_path / "main.cpp"),
		 str(ROOT / "runtime" / "c" / "situ.c"), "-o", str(tmp_path / "bin")],
		capture_output=True, text=True)
	assert made.returncode == 0, made.stderr[-2000:]
	ran = subprocess.run([str(tmp_path / "bin")], capture_output=True,
	                     text=True)
	assert ran.returncode == 0, ran.stderr[-2000:]
	assert ran.stdout.strip() == "1", ran.stdout


def test_all_four_give_the_same_verdicts(tmp_path: Path) -> None:
	schema = tmp_path / "unit.situ"
	schema.write_text(SCHEMA, encoding="ascii")
	command = fourway.build(tmp_path, schema)
	for label, (verdict, packet) in CASES.items():
		for name, argv in command.items():
			lines = [line for line in fourway.answers(argv, packet, tmp_path)
			         .splitlines() if line.startswith("validate")]
			assert lines and lines[-1] == f"validate {verdict}", (
				f"{label}: {name} answered {lines!r}")
