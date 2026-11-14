"""A located offset wider than 32 bits is computed wide (26.1037).

openmlx4's MFA TOC entry places its data at `(high << 32) | low`, a 48-bit
offset. C and C++ computed it in 32 bits: shifting a 16-bit field by 32 is
undefined behaviour in both, and the result was cut to the low half, so
with the high half 1 the accessor served the bytes at offset 0x18 rather
than refusing an offset of 0x1_0000_0018. Python's integers and Rust's
`usize` had it right. The offset is computed as a size expression's leaves
are -- each held, the sum in 64 bits -- and refused past the message before
it is narrowed.

C is built with UndefinedBehaviorSanitizer where the compiler has it, so
the old shift would also be caught as what it was.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from every_schema import ROOT

COMPILER = shutil.which("gcc") or shutil.which("cc")
HOST_CXX = shutil.which("g++") or shutil.which("c++")

SCHEMA = """target buffer;
endian big;
struct mfa_toc_entry {
	u32  data_offset_low;
	u32  data_size;
	opaque undescribed [12];
	u16  data_offset_high;
	opaque undescribed_tail [2];
}
struct toc_and_data {
	mfa_toc_entry toc;
	u8 data[toc.data_size] at (toc.data_offset_high << 32) | toc.data_offset_low;
}
"""

#: The two vectors: the high half 1, and 0. Data `ab cd` at 0x18 either way.
BYTES = ("{ 0,0,0,0x18, 0,0,0,2, 0,0,0,0,0,0,0,0,0,0,0,0, %d,%d, 0,0,"
         " 0xAB,0xCD }")


def _emit(tmp_path: Path, target: str) -> Path:
	schema = tmp_path / "g4.situ"
	schema.write_text(SCHEMA, encoding = "ascii")
	out = tmp_path / target
	done = subprocess.run(
		[sys.executable, "-m", "situc", "build", str(schema), "--target",
		 target, "--out", str(out)],
		cwd = ROOT, capture_output = True, text = True)
	assert done.returncode == 0, done.stderr
	return out


@pytest.mark.skipif(COMPILER is None, reason = "needs a C compiler")
def test_c(tmp_path: Path) -> None:
	out = _emit(tmp_path, "c")
	(tmp_path / "d.c").write_text(f"""#include "g4.h"
int main(void)
{{
	static uint8_t high[26] = {BYTES % (0, 1)};
	static uint8_t low[26]  = {BYTES % (0, 0)};
	situ_msg_t  m;
	situ_view_t v, out;

	situ_msg_init(&m, high, 26u);
	if (situ_toc_and_data_view(&m, 0, &v) != SITU_OK) return 1;
	if (situ_toc_and_data_data_view(&m, v, &out) != SITU_ERR_BOUNDS) return 2;
	situ_msg_init(&m, low, 26u);
	if (situ_toc_and_data_view(&m, 0, &v) != SITU_OK) return 3;
	if (situ_toc_and_data_data_view(&m, v, &out) != SITU_OK) return 4;
	if (out.limit != 2u || out.base[0] != 0xABu) return 5;
	return 0;
}}
""", encoding = "ascii")
	runtime = ROOT / "runtime" / "c"
	flags = ["-std=c11", "-Wall", "-Wextra", "-Werror", "-Wno-unused-parameter"]
	if "gcc" in Path(COMPILER or "").name:
		flags += ["-fsanitize=undefined", "-fno-sanitize-recover=all"]
	built = subprocess.run(
		[COMPILER or "cc", *flags, f"-I{runtime}", f"-I{out}",
		 str(tmp_path / "d.c"), str(out / "g4.c"), str(runtime / "situ.c"),
		 "-o", str(tmp_path / "d")], capture_output = True, text = True)
	assert built.returncode == 0, built.stderr
	ran = subprocess.run([str(tmp_path / "d")], capture_output = True,
	                     text = True)
	assert ran.returncode == 0, f"check {ran.returncode}: {ran.stderr}"


@pytest.mark.skipif(HOST_CXX is None, reason = "needs a C++ compiler")
def test_cpp(tmp_path: Path) -> None:
	out = _emit(tmp_path, "cpp")
	(tmp_path / "m.cpp").write_text(f"""#include "g4.hpp"
int main()
{{
	static std::uint8_t high[26] = {BYTES % (0, 1)};
	static std::uint8_t low[26]  = {BYTES % (0, 0)};
	::situ::rt::bytes out;
	{{
		::situ::rt::message m(high, 26);
		::situ::toc_and_data v;
		if (::situ::toc_and_data::at(m, 0, v) != ::situ::rt::err::ok) return 1;
		if (v.data(m.raw(), out) != ::situ::rt::err::bounds) return 2;
	}}
	{{
		::situ::rt::message m(low, 26);
		::situ::toc_and_data v;
		if (::situ::toc_and_data::at(m, 0, v) != ::situ::rt::err::ok) return 3;
		if (v.data(m.raw(), out) != ::situ::rt::err::ok) return 4;
		if (out.size() != 2u || out.data()[0] != 0xABu) return 5;
	}}
	return 0;
}}
""", encoding = "ascii")
	built = subprocess.run(
		[HOST_CXX or "c++", "-std=c++17", "-Wall", "-Wextra", "-Werror",
		 "-Wno-unused-parameter", f"-I{ROOT / 'runtime' / 'c'}",
		 f"-I{ROOT / 'runtime' / 'cpp'}", f"-I{out}", str(tmp_path / "m.cpp"),
		 str(ROOT / "runtime" / "c" / "situ.c"), "-o", str(tmp_path / "m")],
		capture_output = True, text = True)
	assert built.returncode == 0, built.stderr
	ran = subprocess.run([str(tmp_path / "m")], capture_output = True)
	assert ran.returncode == 0, f"check {ran.returncode}"
