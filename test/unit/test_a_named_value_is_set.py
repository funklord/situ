"""A named value splits back into its fields, in all four (26.1061).

26.1056 gave a `let` a getter and no setter, because most arithmetic does
not invert. The shape a value assembled from two places has does: an `|`
of unsigned fields, each alone or shifted by a constant, with disjoint
bits inside the value's width. `.mfa`'s writer can then store its 48-bit
offset in one call instead of doing the shift by hand -- and a value with
a bit no field holds is refused, never cut, which is the one way a split
can go wrong silently.

Each backend sets `offset` and `pair`, prints the fields they wrote, then
tries an offset with bit 48 set and prints that it was refused and that
the fields are as they were. `diff = low - high` has no setter anywhere.
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
          "struct entry {\n"
          "\tu32 low;\n\tu16 high;\n\tu8 nib;\n\tu8 other;\n"
          "\tlet u64 offset = (high << 32) | low;\n"
          "\tlet u16 pair = (nib << 8) | other;\n"
          "\tlet u64 diff = low - high;\n"
          "}\n")

#: high, low after `offset = 0x123489ABCDEF`; nib, other after
#: `pair = 0xABCD`; then the refusal of `1 << 48`, and high and low again.
WANT = ["4660", "2309737967", "171", "205", "refused", "4660", "2309737967"]
TOO_WIDE = 1 << 48


def _build(tmp_path: Path, target: str) -> Path:
	schema = tmp_path / "split.situ"
	schema.write_text(SCHEMA, encoding="ascii")
	out = tmp_path / target
	assert main(["build", str(schema), "--target", target,
	             "--out", str(out)]) == 0
	return out


def test_only_an_invertible_value_has_a_setter(tmp_path: Path) -> None:
	text = (_build(tmp_path, "c") / "split.h").read_text(encoding="ascii")
	assert "situ_entry_offset_set(" in text
	assert "situ_entry_pair_set(" in text
	assert "situ_entry_diff_set(" not in text


@pytest.mark.skipif(shutil.which("gcc") is None, reason="no C compiler")
def test_c(tmp_path: Path) -> None:
	out = _build(tmp_path, "c")
	(out / "main.c").write_text(f'''#include <inttypes.h>
#include <stdio.h>
#include "split.h"
int main(void)
{{
	uint8_t buf[8] = {{0}};
	situ_msg_t msg;
	situ_view_t v;
	situ_msg_init(&msg, buf, sizeof buf);
	if (situ_entry_view(&msg, 0u, &v) != SITU_OK) return 1;
	if (situ_entry_offset_set(v, UINT64_C(0x123489ABCDEF)) != SITU_OK) return 2;
	if (situ_entry_pair_set(v, 0xABCDu) != SITU_OK) return 3;
	printf("%u\\n%" PRIu32 "\\n%u\\n%u\\n", (unsigned)situ_entry_high_get(v),
	       situ_entry_low_get(v), (unsigned)situ_entry_nib_get(v),
	       (unsigned)situ_entry_other_get(v));
	printf("%s\\n", situ_entry_offset_set(v, UINT64_C({TOO_WIDE}))
	                == SITU_ERR_CONSTRAINT ? "refused" : "stored");
	printf("%u\\n%" PRIu32 "\\n", (unsigned)situ_entry_high_get(v),
	       situ_entry_low_get(v));
	return 0;
}}
''', encoding="ascii")
	built = subprocess.run(
		["gcc", "-std=c99", "-Os", "-Wall", "-Wextra", "-Werror",
		 "-Wconversion", f"-I{RUNTIME / 'c'}", f"-I{out}",
		 str(out / "main.c"), str(out / "split.c"),
		 str(RUNTIME / "c" / "situ.c"), "-o", str(out / "r")],
		capture_output=True, text=True)
	assert built.returncode == 0, built.stderr
	ran = subprocess.run([str(out / "r")], capture_output=True, text=True)
	assert ran.returncode == 0 and ran.stdout.split() == WANT, ran.stdout


@pytest.mark.skipif(shutil.which("g++") is None, reason="no C++ compiler")
def test_cpp(tmp_path: Path) -> None:
	out = _build(tmp_path, "cpp")
	(out / "main.cpp").write_text(f'''#include <cstdio>
#include "split.hpp"
int main()
{{
	std::uint8_t buf[8] = {{0}};
	situ_msg_t msg;
	situ_view_t raw;
	situ_msg_init(&msg, buf, sizeof buf);
	if (situ_view_at(&msg, 0u, sizeof buf, &raw) != SITU_OK) return 1;
	::situ::entry v(raw);
	if (v.set_offset(0x123489ABCDEFull) != ::situ::rt::err::ok) return 2;
	if (v.set_pair(0xABCDu) != ::situ::rt::err::ok) return 3;
	std::printf("%u\\n%u\\n%u\\n%u\\n", (unsigned)v.high(), (unsigned)v.low(),
	            (unsigned)v.nib(), (unsigned)v.other());
	std::printf("%s\\n", v.set_offset({TOO_WIDE}ull)
	            == ::situ::rt::err::constraint ? "refused" : "stored");
	std::printf("%u\\n%u\\n", (unsigned)v.high(), (unsigned)v.low());
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
	assert ran.returncode == 0 and ran.stdout.split() == WANT, ran.stdout


@pytest.mark.skipif(shutil.which("rustc") is None, reason="no Rust compiler")
def test_rust(tmp_path: Path) -> None:
	out = _build(tmp_path, "rust")
	shutil.copy(RUNTIME / "rust" / "situ_rt.rs", out / "situ_rt.rs")
	(out / "main.rs").write_text(f'''mod situ_rt;
#[allow(dead_code)]
mod split;
fn main() {{
	let mut buf: Vec<u8> = vec![0; 8];
	{{
		let mut v = split::EntryMut::new(&mut buf).unwrap();
		v.set_offset(0x123489ABCDEF).unwrap();
		v.set_pair(0xABCD).unwrap();
		let r = v.as_ref();
		println!("{{}}\\n{{}}\\n{{}}\\n{{}}", r.high(), r.low(), r.nib(), r.other());
	}}
	let mut v = split::EntryMut::new(&mut buf).unwrap();
	println!("{{}}", match v.set_offset({TOO_WIDE}) {{
		Err(situ_rt::Error::Constraint) => "refused",
		_ => "stored",
	}});
	let r = v.as_ref();
	println!("{{}}\\n{{}}", r.high(), r.low());
}}
''', encoding="ascii")
	built = subprocess.run(
		["rustc", "--edition", "2021", "-O", "-D", "warnings", "-A",
		 "unused-attributes", "-o", str(out / "r"), str(out / "main.rs")],
		capture_output=True, text=True)
	assert built.returncode == 0, built.stderr
	ran = subprocess.run([str(out / "r")], capture_output=True, text=True)
	assert ran.stdout.split() == WANT, ran.stdout + ran.stderr


def test_python(tmp_path: Path) -> None:
	out = _build(tmp_path, "python")
	shutil.copy(RUNTIME / "python" / "situ_runtime.py",
	            out / "situ_runtime.py")
	(out / "main.py").write_text(f'''import situ_runtime as rt
import split
v = split.entry.at(rt.Message(bytearray(8)), 0)
v.offset = 0x123489ABCDEF
v.pair = 0xABCD
for x in (v.high, v.low, v.nib, v.other):
	print(x)
try:
	v.offset = {TOO_WIDE}
	print("stored")
except rt.ConstraintError:
	print("refused")
print(v.high)
print(v.low)
''', encoding="ascii")
	ran = subprocess.run([sys.executable, str(out / "main.py")],
	                     capture_output=True, text=True, cwd=out)
	assert ran.stdout.split() == WANT, ran.stdout + ran.stderr
	typed = subprocess.run([sys.executable, "-m", "mypy", "--strict",
	                        str(out / "split.py")], capture_output=True,
	                       text=True, cwd=out)
	assert typed.returncode == 0, typed.stdout
