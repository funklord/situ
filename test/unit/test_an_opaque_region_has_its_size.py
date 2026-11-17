"""A fixed-length `opaque` region, read as the bytes it is.

`opaque reserved[16]` at a static offset got no accessor in C++, Rust or
Python -- "this backend cannot resolve where the region is" -- and one in C
whose `_len` returned 0. Its length is its size: nothing in the message
sizes it, so it has no length expression, and all four asked for one.
openmlx4's FS2 header carries two such regions (26.1049).
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from situc.cli import main

ROOT = Path(__file__).resolve().parents[2]
RUNTIME = ROOT / "runtime"

SCHEMA = ("target buffer;\nendian big;\n"
          "struct s {\n\tu8 a;\n\topaque o[4];\n\topaque p [3];\n\tu8 n;\n"
          "\topaque q[n];\n}\n")

MESSAGE = bytes([0xA0, 1, 2, 3, 4, 5, 6, 7, 2, 0xE1, 0xE2])
#: Each region's length and first byte.
WANT = ["4 1", "3 5", "2 225"]


def _build(tmp_path: Path, target: str) -> Path:
	schema = tmp_path / "opq.situ"
	schema.write_text(SCHEMA, encoding="ascii")
	out = tmp_path / target
	assert main(["build", str(schema), "--target", target,
	             "--out", str(out)]) == 0
	return out


def _bytes(data: bytes) -> str:
	return ", ".join(str(b) for b in data)


@pytest.mark.skipif(shutil.which("gcc") is None, reason="no C compiler")
def test_c(tmp_path: Path) -> None:
	out = _build(tmp_path, "c")
	(out / "main.c").write_text(f'''#include <stdio.h>
#include "opq.h"
int main(void)
{{
	uint8_t buf[] = {{{_bytes(MESSAGE)}}};
	situ_msg_t msg;
	situ_view_t v;
	situ_msg_init(&msg, buf, sizeof buf);
	if (situ_s_view(&msg, 0u, sizeof buf, &v) != SITU_OK) return 1;
	printf("%u %u\\n", situ_s_o_len(v), situ_s_o_ptr(v)[0]);
	printf("%u %u\\n", situ_s_p_len(v), situ_s_p_ptr(v)[0]);
	printf("%u %u\\n", situ_s_q_len(v), situ_s_q_ptr(v)[0]);
	return 0;
}}
''', encoding="ascii")
	built = subprocess.run(
		["gcc", "-std=c99", "-Os", "-Wall", "-Wextra", "-Werror",
		 f"-I{RUNTIME / 'c'}", f"-I{out}", str(out / "main.c"),
		 str(out / "opq.c"), str(RUNTIME / "c" / "situ.c"),
		 "-o", str(out / "r")], capture_output=True, text=True)
	assert built.returncode == 0, built.stderr
	ran = subprocess.run([str(out / "r")], capture_output=True, text=True)
	assert ran.stdout.splitlines() == WANT, ran.stdout


@pytest.mark.skipif(shutil.which("g++") is None, reason="no C++ compiler")
def test_cpp(tmp_path: Path) -> None:
	out = _build(tmp_path, "cpp")
	(out / "main.cpp").write_text(f'''#include <cstdio>
#include "opq.hpp"
int main()
{{
	std::uint8_t buf[] = {{{_bytes(MESSAGE)}}};
	situ_msg_t msg;
	situ_view_t raw;
	situ_msg_init(&msg, buf, sizeof buf);
	if (situ_view_at(&msg, 0u, sizeof buf, &raw) != SITU_OK) return 1;
	const ::situ::s v(raw);
	std::printf("%u %u\\n", (unsigned)v.o().size(), (unsigned)v.o().data()[0]);
	std::printf("%u %u\\n", (unsigned)v.p().size(), (unsigned)v.p().data()[0]);
	std::printf("%u %u\\n", (unsigned)v.q().size(), (unsigned)v.q().data()[0]);
	return 0;
}}
''', encoding="ascii")
	built = subprocess.run(
		["g++", "-std=c++17", "-Os", "-Wall", "-Wextra", "-Werror",
		 f"-I{RUNTIME / 'c'}", f"-I{RUNTIME / 'cpp'}", f"-I{out}",
		 str(out / "main.cpp"), str(RUNTIME / "c" / "situ.c"),
		 "-o", str(out / "r")], capture_output=True, text=True)
	assert built.returncode == 0, built.stderr
	ran = subprocess.run([str(out / "r")], capture_output=True, text=True)
	assert ran.stdout.splitlines() == WANT, ran.stdout


@pytest.mark.skipif(shutil.which("rustc") is None, reason="no Rust compiler")
def test_rust(tmp_path: Path) -> None:
	out = _build(tmp_path, "rust")
	shutil.copy(RUNTIME / "rust" / "situ_rt.rs", out / "situ_rt.rs")
	(out / "main.rs").write_text(f'''mod situ_rt;
#[allow(dead_code)]
mod opq;
fn main() {{
	let buf: Vec<u8> = vec![{_bytes(MESSAGE)}];
	let v = opq::S::new(&buf).unwrap();
	for r in [v.o(), v.p(), v.q()] {{
		println!("{{}} {{}}", r.len(), r[0]);
	}}
}}
''', encoding="ascii")
	built = subprocess.run(
		["rustc", "--edition", "2021", "-O", "-o", str(out / "r"),
		 str(out / "main.rs")], capture_output=True, text=True)
	assert built.returncode == 0, built.stderr
	ran = subprocess.run([str(out / "r")], capture_output=True, text=True)
	assert ran.stdout.splitlines() == WANT, ran.stdout


def test_python(tmp_path: Path) -> None:
	out = _build(tmp_path, "python")
	shutil.copy(RUNTIME / "python" / "situ_runtime.py",
	            out / "situ_runtime.py")
	(out / "main.py").write_text(f'''import situ_runtime as rt
import opq
v = opq.s.at(rt.Message(bytearray({MESSAGE!r})), 0, {len(MESSAGE)})
for r in (v.o, v.p, v.q):
	print(len(r), r[0])
''', encoding="ascii")
	ran = subprocess.run([sys.executable, str(out / "main.py")],
	                     capture_output=True, text=True, cwd=out)
	assert ran.stdout.splitlines() == WANT, ran.stdout + ran.stderr
