"""A member present from a version, inside a sealed region (26.764).

    struct inside [version = ver] {
        u8 ver;
        tag u8 mac[16] covers(body);
        sealed body(sealing_aead) { u8 a; u8 b [since = 2]; }
    }

C's versioned getter took the gate and tested the version, and the frame,
on `view` -- which is not in scope there -- so the generated C did not
compile, at this session's starting commit already. C++, Rust and Python
read `b` through the gate with no version test and no bounds check, so
over an 18-byte version-1 message, which is whole, they read the byte
after it.

Each now asks the plain versioned member's two questions through the gate:
the version field (outside the region, plaintext) first, then the frame.
The differ asks the member in that form, sealed and waived.
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

from situc.codegen.cpp import generate as generate_cpp	# noqa: E402
from situc.layout import solve				# noqa: E402
from situc.parser import parse_text			# noqa: E402
from situc.resolve import resolve			# noqa: E402

from test_a_tag_after_a_run_sized_inside_its_region import HEAD	# noqa: E402

HOST_CXX = shutil.which("g++") or shutil.which("clang++")
SANITIZE = ["-g", "-fsanitize=address,undefined", "-fno-sanitize-recover=all"]


def _schema(waiver: str) -> str:
	return ("struct inside [version = ver] {\n\tu8 ver;\n"
	        "\ttag u8 mac[16] covers(body);\n"
	        f"\tsealed body(sealing_aead){waiver} {{ u8 a; u8 b [since = 2]; }}\n"
	        "}\n")


MAC = bytes(16)

#: packet -> what all four answer for `b`
ANSWERS = {
	bytes([2]) + MAC + bytes([5]):    "ok=0 value=0",	# version 2, cut short
	bytes([2]) + MAC + bytes([5, 6]): "ok=1 value=6",
	bytes([1]) + MAC + bytes([5, 6]): "ok=0 value=0",	# version 1: absent
}


@pytest.mark.parametrize("waiver, name", [("", "b"),
                                          (" [allow_unverified_read]",
                                           "body_b")],
                         ids=["sealed", "waived"])
def test_all_four_ask_the_version_first(waiver: str, name: str,
		tmp_path: Path) -> None:
	schema = tmp_path / "unit.situ"
	schema.write_text(HEAD + _schema(waiver), encoding="ascii")
	command = fourway.build(tmp_path, schema)
	for packet, answer in ANSWERS.items():
		given = {backend: fourway.answers(argv, packet, tmp_path)
		         for backend, argv in command.items()}
		first = next(iter(given.values()))
		assert all(one == first for one in given.values()), given
		assert f"\n{name} {answer}\n" in first, (packet.hex(), first)


CPP_PROBE = """#include "unit.hpp"
#include <cstdio>
#include <cstdlib>
int main()
{
	auto *p = static_cast<std::uint8_t *>(std::calloc(18, 1));
	p[0] = 2;		/* version 2 claimed, `b` not in the frame */
	::situ::rt::message m(p, 18);
	::situ::inside view;
	if (::situ::inside::at(m, 0, 18, view) != ::situ::rt::err::ok) {
		return 2;
	}
	int got = -1;
	(void)view.with_body(true, [&](auto gate) {
		std::uint8_t b = 0;
		got = gate.b(b) == ::situ::rt::err::ok;
	});
	std::printf("%d\\n", got);
	std::free(p);
	return 0;
}
"""


@pytest.mark.skipif(HOST_CXX is None, reason="no host C++ compiler")
def test_cpp_reads_nothing_past_the_frame(tmp_path: Path) -> None:
	schema = parse_text(HEAD + _schema(""))
	resolved = resolve(schema, solve(schema))
	(tmp_path / "unit.hpp").write_text(
		generate_cpp(schema, resolved, "unit").header,
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
	assert ran.stdout.strip() == "0", ran.stdout
