"""An arm of a variant inside a sealed region is guarded (26.760).

`traverse.arm_of` looked for the variant among the struct's own members by
the first component of the path, which for `inside.body.v.a` is `body`, the
region. So no backend knew `a` was an arm: each read it through the gate as
a plain member, wherever the discriminant pointed, and C, Rust and C++ wrote
it there through a covered setter too. Over

    tag u8 mac[16] covers(body);
    sealed body { u8 k; variant v switch (k) { case 1: u16 a; default: u8 b; } }

an 18-byte message is whole with `k = 2` -- the one-byte arm -- and C's
`situ_inside_body_v_a_get` read the two-byte arm one byte past it (ASan,
heap-buffer-overflow); C++'s getter was the same unbounded load.

Now each backend gives such an arm the plain arm's accessor through the
gate: `err::version` for the arm the discriminant did not pick, `bounds`
for one the frame does not hold, no setter. A waived region
(`[allow_unverified_read]`) reads the same arms on the plain view and is
guarded the same way. The differ asks both.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import fourway

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from situc.codegen.c import generate as generate_c	# noqa: E402
from situc.codegen.cpp import generate as generate_cpp	# noqa: E402
from situc.layout import solve				# noqa: E402
from situc.parser import parse_text			# noqa: E402
from situc.resolve import resolve			# noqa: E402

from test_a_tag_after_a_run_sized_inside_its_region import HEAD	# noqa: E402

HOST_CC  = shutil.which("cc") or shutil.which("gcc")
HOST_CXX = shutil.which("g++") or shutil.which("clang++")
SANITIZE = ["-g", "-fsanitize=address,undefined", "-fno-sanitize-recover=all"]

VARIANT = "variant v switch (k) { case 1: u16 a; default: u8 b; }"
SEALED  = ("struct inside {\n\ttag u8 mac[16] covers(body);\n"
           f"\tsealed body(sealing_aead) {{ u8 k; {VARIANT} }}\n}}\n")
WAIVED  = ("struct inside {\n\tu8 kind;\n"
           "\tsealed body(sealing_aead) [allow_unverified_read]"
           f" {{ u8 k; {VARIANT} }}\n"
           "\ttag u8 mac[16] covers(body);\n}\n")

MAC = bytes(16)

#: packet -> the arm lines all four print, for the sealed form.
SEALED_ANSWERS = {
	MAC + bytes([2, 7]):    ["v_a ok=0 value=0", "v_b ok=1 value=7"],
	MAC + bytes([1, 0, 9]): ["v_a ok=1 value=9", "v_b ok=0 value=0"],
	# Whole with the two-byte arm chosen and one byte of it present: the
	# frame holds neither arm's bytes as `a` needs them.
	MAC + bytes([1, 0]):    ["v_a ok=0 value=0", "v_b ok=0 value=0"],
}

WAIVED_ANSWERS = {
	bytes([2, 1, 0, 5]) + MAC: ["body_v_a ok=1 value=5",
	                            "body_v_b ok=0 value=0"],
	bytes([2, 2, 7]) + MAC:    ["body_v_a ok=0 value=0",
	                            "body_v_b ok=1 value=7"],
}


def _arm_lines(output: str) -> list[str]:
	return [line for line in output.splitlines()
	        if line.startswith(("v_", "body_v_"))]


@pytest.mark.parametrize("text, answers", [(SEALED, SEALED_ANSWERS),
                                           (WAIVED, WAIVED_ANSWERS)],
                         ids=["sealed", "waived"])
def test_all_four_guard_the_arm(text: str, answers: dict[bytes, list[str]],
		tmp_path: Path) -> None:
	schema = tmp_path / "unit.situ"
	schema.write_text(HEAD + text, encoding="ascii")
	command = fourway.build(tmp_path, schema)
	for packet, expected in answers.items():
		given = {name: fourway.answers(argv, packet, tmp_path)
		         for name, argv in command.items()}
		first = next(iter(given.values()))
		assert all(answer == first for answer in given.values()), given
		assert _arm_lines(first) == expected, (packet.hex(), first)


C_PROBE = """#include "unit.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
int main(void)
{
	uint8_t *p = calloc(18, 1);
	situ_msg_t m;
	situ_view_t view;
	situ_inside_body_t gate;
	uint16_t a = 0;
	uint8_t b = 0;
	p[16] = 2;		/* the one-byte arm */
	p[17] = 7;
	situ_msg_init(&m, p, 18);
	if (situ_inside_view(&m, 0, 18, &view) != SITU_OK
			|| situ_inside_body_open(view, 1, &gate) != SITU_OK) {
		return 2;
	}
	const int got_a = situ_inside_body_v_a_get(gate, &a) == SITU_OK;
	const int got_b = situ_inside_body_v_b_get(gate, &b) == SITU_OK;

	printf("%d %d %u\\n", got_a, got_b, b);
	free(p);
	return 0;
}
"""

CPP_PROBE = """#include "unit.hpp"
#include <cstdio>
#include <cstdlib>
int main()
{
	auto *p = static_cast<std::uint8_t *>(std::calloc(18, 1));
	p[16] = 2;
	p[17] = 7;
	::situ::rt::message m(p, 18);
	::situ::inside view;
	if (::situ::inside::at(m, 0, 18, view) != ::situ::rt::err::ok) {
		return 2;
	}
	int got_a = -1, got_b = -1;
	unsigned b_value = 0;
	(void)view.with_body(true, [&](auto gate) {
		std::uint16_t a = 0;
		std::uint8_t b = 0;
		got_a = gate.v_a(a) == ::situ::rt::err::ok;
		got_b = gate.v_b(b) == ::situ::rt::err::ok;
		b_value = b;
	});
	std::printf("%d %d %u\\n", got_a, got_b, b_value);
	std::free(p);
	return 0;
}
"""


def _built() -> tuple[object, object]:
	schema = parse_text(HEAD + SEALED)
	return schema, resolve(schema, solve(schema))


@pytest.mark.skipif(HOST_CC is None, reason="no host C compiler")
def test_c_reads_no_arm_past_the_frame(tmp_path: Path) -> None:
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
	assert ran.stdout.strip() == "0 1 7", ran.stdout


@pytest.mark.skipif(HOST_CXX is None, reason="no host C++ compiler")
def test_cpp_reads_no_arm_past_the_frame(tmp_path: Path) -> None:
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
	assert ran.stdout.strip() == "0 1 7", ran.stdout
