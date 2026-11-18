"""`let`: a value a struct's fields assemble, named (26.1056).

openmlx4's `.mfa` table of contents keeps one 48-bit offset in two fields
twenty bytes apart, `data_offset_high << 32 | data_offset_low`, and each
caller did the shift. A `let` names it: every reader gets a getter, and a
later expression may use the name, which reads as the definition.

The four agree because the arithmetic is fixed: modulo 2^64 with each read
widened first, `/` and `%` by zero are 0, a shift by 64 or more is 0, and
the result is held to the declared width.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from situc.cli import main
from situc.diagnostics import SituError
from situc.parser import parse_text

ROOT = Path(__file__).resolve().parents[2]
RUNTIME = ROOT / "runtime"

SCHEMA = ("target buffer;\nendian big;\n"
          "struct values {\n"
          "\tu16 high;\n\tu32 low;\n\tu8 a;\n\tu8 b;\n\tu8 zero;\n\tu8 big;\n"
          "\tlet u64 offset = (high << 32) | low;\n"
          "\tlet i16 diff = a - b;\n"
          "\tlet u32 quotient = low / zero;\n"
          "\tlet u64 shifted = low << big;\n"
          "\tlet u8 wrapped = a * b;\n"
          "\tlet u8 m = a & 1;\n"
          "\tu8 tail[m];\n"
          "}\n")

MESSAGE = bytes([0x12, 0x34, 0x89, 0xAB, 0xCD, 0xEF, 3, 200, 0, 70, 0x5A])
#: offset, diff, quotient, shifted, wrapped, m, the tail's first byte
WANT = [str(0x123489ABCDEF), "-197", "0", "0", "88", "1", "90"]


def _build(tmp_path: Path, target: str) -> Path:
	schema = tmp_path / "lets.situ"
	schema.write_text(SCHEMA, encoding="ascii")
	out = tmp_path / target
	assert main(["build", str(schema), "--target", target,
	             "--out", str(out)]) == 0
	return out


@pytest.mark.parametrize("bad, why", [
	("let u8 x = size(a);", "not integer arithmetic"),
	("let u8 x = later;", "not a field declared before it"),
	("let u8 a = b;", "already a member"),
	("let u24 x = a;", "not 8, 16, 32 or 64"),
	# A comparison is 1 or 0 since 26.1079; a logical operator still is
	# not arithmetic every reader spells alike.
	("let u8 x = a && b;", "uses `&&`"),
])
def test_what_a_named_value_may_not_say(bad: str, why: str) -> None:
	with pytest.raises(SituError, match=why):
		parse_text("target buffer;\nendian big;\nstruct s { u8 a; u8 b; "
		           + bad + " u8 later; }\n", path="s.situ")


def test_a_use_before_the_let_is_refused() -> None:
	with pytest.raises(SituError, match="used before its `let`"):
		parse_text("target buffer;\nendian big;\nstruct s { u8 a; "
		           "u8 d[x]; let u8 x = a; }\n", path="s.situ")


@pytest.mark.skipif(shutil.which("gcc") is None, reason="no C compiler")
def test_c(tmp_path: Path) -> None:
	out = _build(tmp_path, "c")
	(out / "main.c").write_text(f'''#include <inttypes.h>
#include <stdio.h>
#include "lets.h"
int main(void)
{{
	uint8_t buf[] = {{{", ".join(str(b) for b in MESSAGE)}}};
	situ_msg_t msg;
	situ_view_t v;
	situ_msg_init(&msg, buf, sizeof buf);
	if (situ_values_view(&msg, 0u, sizeof buf, &v) != SITU_OK) return 1;
	printf("%" PRIu64 "\\n%d\\n%u\\n%" PRIu64 "\\n%u\\n%u\\n%u\\n",
	       situ_values_offset_get(v), (int)situ_values_diff_get(v),
	       (unsigned)situ_values_quotient_get(v), situ_values_shifted_get(v),
	       (unsigned)situ_values_wrapped_get(v), (unsigned)situ_values_m_get(v),
	       (unsigned)situ_values_tail_ptr(v)[0]);
	return (int)situ_values_tail_len(v) - 1;
}}
''', encoding="ascii")
	built = subprocess.run(
		["gcc", "-std=c99", "-Os", "-Wall", "-Wextra", "-Werror",
		 f"-I{RUNTIME / 'c'}", f"-I{out}", str(out / "main.c"),
		 str(out / "lets.c"), str(RUNTIME / "c" / "situ.c"),
		 "-o", str(out / "r")], capture_output=True, text=True)
	assert built.returncode == 0, built.stderr
	ran = subprocess.run([str(out / "r")], capture_output=True, text=True)
	assert ran.returncode == 0 and ran.stdout.split() == WANT, ran.stdout


@pytest.mark.skipif(shutil.which("g++") is None, reason="no C++ compiler")
def test_cpp(tmp_path: Path) -> None:
	out = _build(tmp_path, "cpp")
	(out / "main.cpp").write_text(f'''#include <cstdio>
#include "lets.hpp"
int main()
{{
	std::uint8_t buf[] = {{{", ".join(str(b) for b in MESSAGE)}}};
	situ_msg_t msg;
	situ_view_t raw;
	situ_msg_init(&msg, buf, sizeof buf);
	if (situ_view_at(&msg, 0u, sizeof buf, &raw) != SITU_OK) return 1;
	const ::situ::values v(raw);
	std::printf("%llu\\n%d\\n%u\\n%llu\\n%u\\n%u\\n%u\\n",
	            (unsigned long long)v.offset(), (int)v.diff(),
	            (unsigned)v.quotient(), (unsigned long long)v.shifted(),
	            (unsigned)v.wrapped(), (unsigned)v.m(),
	            (unsigned)v.tail().data()[0]);
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
	assert ran.stdout.split() == WANT, ran.stdout


@pytest.mark.skipif(shutil.which("rustc") is None, reason="no Rust compiler")
def test_rust(tmp_path: Path) -> None:
	out = _build(tmp_path, "rust")
	shutil.copy(RUNTIME / "rust" / "situ_rt.rs", out / "situ_rt.rs")
	(out / "main.rs").write_text(f'''mod situ_rt;
#[allow(dead_code)]
mod lets;
fn main() {{
	let buf: Vec<u8> = vec![{", ".join(str(b) for b in MESSAGE)}];
	let v = lets::Values::new(&buf).unwrap();
	println!("{{}}\\n{{}}\\n{{}}\\n{{}}\\n{{}}\\n{{}}\\n{{}}", v.offset(), v.diff(),
	         v.quotient(), v.shifted(), v.wrapped(), v.m(), v.tail()[0]);
}}
''', encoding="ascii")
	built = subprocess.run(
		["rustc", "--edition", "2021", "-O", "-D", "warnings", "-A", "unused-attributes", "-o", str(out / "r"),
		 str(out / "main.rs")], capture_output=True, text=True)
	assert built.returncode == 0, built.stderr
	ran = subprocess.run([str(out / "r")], capture_output=True, text=True)
	assert ran.stdout.split() == WANT, ran.stdout + ran.stderr


def test_python(tmp_path: Path) -> None:
	out = _build(tmp_path, "python")
	shutil.copy(RUNTIME / "python" / "situ_runtime.py",
	            out / "situ_runtime.py")
	(out / "main.py").write_text(f'''import situ_runtime as rt
import lets
v = lets.values.at(rt.Message(bytearray({MESSAGE!r})), 0, {len(MESSAGE)})
for x in (v.offset, v.diff, v.quotient, v.shifted, v.wrapped, v.m, v.tail[0]):
	print(x)
''', encoding="ascii")
	ran = subprocess.run([sys.executable, str(out / "main.py")],
	                     capture_output=True, text=True, cwd=out)
	assert ran.stdout.split() == WANT, ran.stdout + ran.stderr
	typed = subprocess.run([sys.executable, "-m", "mypy", "--strict",
	                        str(out / "lets.py")], capture_output=True,
	                       text=True, cwd=out)
	assert typed.returncode == 0, typed.stdout
