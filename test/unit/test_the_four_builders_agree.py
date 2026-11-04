"""Rung 2's write half: four builders, one message (26.858).

C has built since 26.610, Python since 26.855, and C++ and Rust since
26.858, each for the plain class `plain_build` states. A round trip through
one backend's own accessors cannot see a fault that backend's reader and
writer share, so this holds the four to each other: every struct the class
serves, in every corpus schema, built by all four from the same values,
and the bytes compared. A refusal has to be one all four make.
"""

from __future__ import annotations

import importlib
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "test" / "unit"))

import fourway						# noqa: E402
from every_schema import ROOT as CORPUS_ROOT, load_schema	# noqa: E402

from situc.codegen import plain_build			# noqa: E402
from situc.codegen.c.build import Shape			# noqa: E402
from situc.codegen.c.names import ident			# noqa: E402
from situc.codegen.cpp.edit import _scalar as cpp_scalar	# noqa: E402
from situc.codegen.cpp.names import class_name		# noqa: E402
from situc.codegen.python.emit import py_name		# noqa: E402
from situc.codegen.rust.edit import _scalar as rust_scalar	# noqa: E402
from situc.codegen.rust.emit import _ident		# noqa: E402
from situc.layout import BITS_PER_BYTE, Placement, solve	# noqa: E402
from situc.resolve import ResolvedStruct, resolve	# noqa: E402

RUNTIME = ROOT / "runtime"
#: Comfortably past the largest struct the class serves; asserted below.
ROOM = 512


def schemas() -> list[Path]:
	found = sorted([*CORPUS_ROOT.glob("example/*/*.situ"),
	                *CORPUS_ROOT.glob("test/schema/*.situ")])
	assert len(found) > 40, f"only {len(found)} schemas; the corpus moved"
	return found


def _raw(placement: Placement, index: int) -> int:
	"""The value's bytes as an unsigned number, ascending and not symmetric.

	Not zero and not all-ones: both survive a wrong byte order, which is the
	fault this exists to catch.
	"""
	width = (placement.size_bits or 8) // BITS_PER_BYTE
	return int.from_bytes(bytes((0x12 + index + at) & 0xFF
	                            for at in range(width)), "big")


def _signed(placement: Placement, raw: int) -> int:
	bits = placement.size_bits or 8
	if placement.scalar and placement.scalar.signed and raw >> (bits - 1):
		return raw - (1 << bits)
	return raw


def _c_driver(stem: str, structs: list[ResolvedStruct]) -> str:
	lines = ["#include <stdio.h>", f'#include "{stem}_build.h"',
	         f"static uint8_t buf[{ROOM}];", "static uint32_t n;",
	         "static void show(const char *name, situ_err_t e) {",
	         '\tprintf("%s ", name);',
	         '\tif (e != SITU_OK) { puts("refused"); return; }',
	         '\tfor (uint32_t i = 0; i < n; i++) printf("%02x", buf[i]);',
	         '\tputs("");', "}", "int main(void) {"]
	for struct in structs:
		values = ", ".join(f"{_raw(one, at):#x}ull" for at, one in
		                   enumerate(plain_build.members(struct)))
		lines.append(f'\tshow("{struct.name}", '
		             f'{ident("situ", struct.name, "build")}'
		             f"(buf, {ROOM}u, {values}, &n));")
	return "\n".join([*lines, "\treturn 0;", "}", ""])


def _cpp_driver(stem: str, structs: list[ResolvedStruct]) -> str:
	lines = ["#include <cstdio>", f'#include "{stem}_build.hpp"',
	         f"static std::uint8_t buf[{ROOM}];", "static std::uint32_t n;",
	         "static void show(const char *name, ::situ::rt::err e) {",
	         '\tstd::printf("%s ", name);',
	         "\tif (e != ::situ::rt::err::ok) {",
	         '\t\tstd::puts("refused");', "\t\treturn;", "\t}",
	         '\tfor (std::uint32_t i = 0; i < n; i++) '
	         'std::printf("%02x", buf[i]);',
	         '\tstd::puts("");', "}", "int main() {"]
	for struct in structs:
		values = ", ".join(
			f"static_cast<{cpp_scalar(one)}>({_raw(one, at):#x}ull)"
			for at, one in enumerate(plain_build.members(struct)))
		lines.append(f'\tshow("{struct.name}", '
		             f"::situ_build::{class_name(struct)}"
		             f"(buf, {ROOM}u, {values}, n));")
	return "\n".join([*lines, "}", ""])


def _rust_driver(stem: str, structs: list[ResolvedStruct]) -> str:
	lines = ["mod situ_rt;", "#[allow(dead_code)]", f"mod {stem};",
	         "#[allow(dead_code)]", f"mod {stem}_build;",
	         "fn show(name: &str, buf: &[u8], "
	         "got: situ_rt::Result<usize>) {",
	         '\tprint!("{} ", name);',
	         "\tmatch got {",
	         "\t\tOk(n) => {",
	         '\t\t\tfor b in &buf[..n] { print!("{:02x}", b); }',
	         "\t\t\tprintln!();", "\t\t}",
	         '\t\tErr(_) => println!("refused"),', "\t}", "}",
	         "fn main() {", f"\tlet mut buf = [0u8; {ROOM}];"]
	for struct in structs:
		values = ", ".join(
			f"{_raw(one, at):#x}u64 as {rust_scalar(one)}"
			for at, one in enumerate(plain_build.members(struct)))
		name = _ident(struct.name).removeprefix("r#")
		lines.extend([f"\tlet got = {stem}_build::build_{name}"
		              f"(&mut buf, {values});",
		              f'\tshow("{struct.name}", &buf, got);'])
	return "\n".join([*lines, "}", ""])


def _python(tmp: Path, stem: str, structs: list[ResolvedStruct]) -> str:
	shutil.copy(RUNTIME / "python" / "situ_runtime.py", tmp)
	sys.path.insert(0, str(tmp))
	try:
		module = importlib.import_module(f"{stem}_build")
		base   = importlib.import_module(stem)
		lines  = []
		for struct in structs:
			values = [_signed(one, _raw(one, at)) for at, one in
			          enumerate(plain_build.members(struct))]
			buf = bytearray(ROOM)
			try:
				wrote = getattr(module, f"build_{py_name(struct.name)}")(
					buf, *values)
			except base.SituError:
				lines.append(f"{struct.name} refused")
				continue
			lines.append(f"{struct.name} {bytes(buf[:wrote]).hex()}")
		return "\n".join(lines) + "\n"
	finally:
		sys.path.remove(str(tmp))
		for name in (f"{stem}_build", stem, "situ_runtime"):
			sys.modules.pop(name, None)


def _emit(schema: Path, target: str, out: Path) -> None:
	done = subprocess.run(
		[sys.executable, "-m", "situc", "build", str(schema), "--target",
		 target, "--layer", "edit", "--out", str(out)],
		capture_output=True, text=True, cwd=ROOT)
	assert done.returncode == 0, done.stderr[-800:]


def _run(argv: list[str], cwd: Path) -> str:
	done = subprocess.run(argv, capture_output=True, text=True, cwd=cwd)
	assert done.returncode == 0, done.stderr[-1500:]
	return done.stdout


@pytest.mark.skipif(not fourway.COMPLETE, reason="needs gcc, g++ and rustc")
@pytest.mark.parametrize("schema", schemas(), ids=lambda p: p.stem)
def test_four_builders_write_one_message(schema: Path, tmp_path: Path) -> None:
	parsed   = load_schema(schema)
	resolved = resolve(parsed, solve(parsed))
	shape    = Shape("", "situ", resolved.structs,
	                 {decl.name: decl for decl in parsed.varints()})
	structs  = plain_build.plain(resolved, shape)
	if not structs:
		pytest.skip("no struct here is in the class")
	assert all((one.layout.size_bytes or 0) <= ROOM for one in structs)

	stem = schema.stem
	dirs = {target: tmp_path / target
	        for target in ("c", "cpp", "rust", "python")}
	for target, out in dirs.items():
		out.mkdir()
		_emit(schema, target, out)

	(dirs["c"] / "main.c").write_text(_c_driver(stem, structs),
	                                  encoding="ascii")
	assert fourway.HOST_CC is not None and fourway.HOST_CXX is not None
	_run([fourway.HOST_CC, "-std=c11", "-w", f"-I{RUNTIME / 'c'}", "-I.",
	      "main.c", f"{stem}.c", str(RUNTIME / "c" / "situ.c"),
	      "-o", "probe"], dirs["c"])

	(dirs["cpp"] / "main.cpp").write_text(_cpp_driver(stem, structs),
	                                      encoding="ascii")
	_run([fourway.HOST_CXX, "-std=c++17", "-Wall", "-Wextra", "-Werror",
	      f"-I{RUNTIME / 'c'}", f"-I{RUNTIME / 'cpp'}", "-I.", "main.cpp",
	      str(RUNTIME / "c" / "situ.c"), "-o", "probe"],
	     dirs["cpp"])

	shutil.copy(RUNTIME / "rust" / "situ_rt.rs", dirs["rust"])
	rt = dirs["rust"] / "situ_rt.rs"
	rt.write_text(rt.read_text(encoding="ascii").replace("#![no_std]\n", ""),
	              encoding="ascii")
	(dirs["rust"] / "main.rs").write_text(_rust_driver(stem, structs),
	                                      encoding="ascii")
	assert fourway.RUSTC is not None
	_run([fourway.RUSTC, "--edition", "2021", "-A", "warnings", "main.rs",
	      "-o", "probe"], dirs["rust"])

	said = {"python": _python(dirs["python"], stem, structs),
	        **{target: _run([str(dirs[target] / "probe")], dirs[target])
	           for target in ("c", "cpp", "rust")}}
	assert said["python"].count("\n") == len(structs)
	assert len(set(said.values())) == 1, said
