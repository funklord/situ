"""Rung 2's write half: four builders, one message (26.858).

C has built since 26.610, Python since 26.855, and C++ and Rust since
26.858, each for the class `plain_build` states. A round trip through
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
from situc.codegen.plain_build import Unit			# noqa: E402
from situc.codegen.c.build import (BITS, RUN, SCALAR, SIZE,	# noqa: E402
                                   SPAN, Part, Shape)
from situc.codegen.c.names import c_name, ident		# noqa: E402
from situc.codegen.cpp.edit import _scalar as cpp_scalar	# noqa: E402
from situc.codegen.cpp.names import class_name		# noqa: E402
from situc.codegen.python.emit import py_name		# noqa: E402
from situc.codegen.rust.edit import _scalar as rust_scalar	# noqa: E402
from situc.codegen.rust.emit import _ident		# noqa: E402
from situc.layout import BITS_PER_BYTE, Placement, solve	# noqa: E402
from situc.resolve import resolve			# noqa: E402

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


def _span_length(part: Part, parts: list[Part]) -> int:
	"""A length the run's size field can state exactly.

	Three bytes for a plain size; for arithmetic, the length the field's
	value 2 describes, so that `(n + 1) * 8 - 2` comes out whole rather than
	exercising only the refusal.
	"""
	for one in parts:
		if one.role == SIZE \
				and one.sizes is part.placement:
			if one.solved is not None:
				return max(one.solved.scale * 2 + one.solved.offset, 0)
	return 3


def _run_bytes(length: int, index: int) -> bytes:
	return bytes((0x41 + index + at) & 0xFF for at in range(length))


def _given(parts: list[Part]) -> list[tuple[Part, int | bytes]]:
	"""What the caller hands each part that takes anything."""
	found: list[tuple[Part, int | bytes]] = []
	for at, part in enumerate(parts):
		if part.role == BITS and plain_build.takes_value(part):
			# Small enough for its field: a wider value is the refusal, and
			# one of those would say nothing about where the bits go.
			width = part.placement.size_bits or 1
			found.append((part, (0x5 + at) & ((1 << width) - 1) or 1))
		elif part.role == SCALAR:
			found.append((part, _raw(part.placement, at)))
		elif part.role == RUN:
			found.append((part, _run_bytes(part.bytes, at)))
		elif part.role == SPAN:
			found.append((part, _run_bytes(_span_length(part, parts), at)))
	return found


def _array(name: str, value: bytes, kind: str) -> str:
	items = ", ".join(f"{one:#x}" for one in value) or "0"
	return f"static const {kind} {name}[] = {{{items}}};"


def _label(unit: Unit) -> str:
	return f"{unit.struct.name}.{unit.suffix}"


def _c_driver(stem: str, units: list[Unit]) -> str:
	lines = ["#include <stdio.h>", f'#include "{stem}_build.h"',
	         f"static uint8_t buf[{ROOM}];", "static uint32_t n;",
	         "static void show(const char *name, situ_err_t e) {",
	         '\tprintf("%s ", name);',
	         '\tif (e != SITU_OK) { puts("refused"); return; }',
	         '\tfor (uint32_t i = 0; i < n; i++) printf("%02x", buf[i]);',
	         '\tputs("");', "}"]
	calls = []
	for index, unit in enumerate(units):
		values: list[str] = []
		for at, (part, value) in enumerate(_given(list(unit.parts))):
			if isinstance(value, int):
				values.append(f"{value:#x}ull")
				continue
			name = f"run_{index}_{at}"
			lines.append(_array(name, value, "uint8_t"))
			values.append(name)
			if part.role == SPAN:
				values.append(f"{len(value)}u")
		calls.append(f'\tshow("{_label(unit)}", '
		             f'{ident("situ", unit.struct.name, "build", unit.suffix)}'
		             f'(buf, {ROOM}u, {", ".join([*values, "&n"])}));')
	return "\n".join([*lines, "int main(void) {", *calls, "\treturn 0;",
	                  "}", ""])


def _cpp_driver(stem: str, units: list[Unit]) -> str:
	lines = ["#include <cstdio>", f'#include "{stem}_build.hpp"',
	         f"static std::uint8_t buf[{ROOM}];", "static std::uint32_t n;",
	         "static void show(const char *name, ::situ::rt::err e) {",
	         '\tstd::printf("%s ", name);',
	         "\tif (e != ::situ::rt::err::ok) {",
	         '\t\tstd::puts("refused");', "\t\treturn;", "\t}",
	         '\tfor (std::uint32_t i = 0; i < n; i++) '
	         'std::printf("%02x", buf[i]);',
	         '\tstd::puts("");', "}"]
	calls = []
	for index, unit in enumerate(units):
		values: list[str] = []
		for at, (part, value) in enumerate(_given(list(unit.parts))):
			if isinstance(value, int):
				values.append(f"static_cast<{cpp_scalar(part.placement)}>"
				              f"({value:#x}ull)")
				continue
			name = f"run_{index}_{at}"
			lines.append(_array(name, value, "std::uint8_t"))
			values.append(name)
			if part.role == SPAN:
				values.append(f"{len(value)}u")
		held = class_name(unit.struct)
		if unit.suffix:
			held = f"{held}_{c_name(unit.suffix)}"
		calls.append(f'\tshow("{_label(unit)}", '
		             f"::situ_build::{held}"
		             f'(buf, {ROOM}u, {", ".join([*values, "n"])}));')
	return "\n".join([*lines, "int main() {", *calls, "}", ""])


def _rust_driver(stem: str, units: list[Unit]) -> str:
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
	for unit in units:
		values: list[str] = []
		for part, value in _given(list(unit.parts)):
			if isinstance(value, int):
				values.append(f"{value:#x}u64 as "
				              f"{rust_scalar(part.placement)}")
				continue
			items = ", ".join(f"{one:#x}u8" for one in value)
			values.append(f"&[{items}]")
		name = _ident(unit.struct.name).removeprefix("r#")
		if unit.suffix:
			name = f"{name}_{_ident(unit.suffix).removeprefix('r#')}"
		lines.extend([f"\tlet got = {stem}_build::build_{name}"
		              f'({", ".join(["&mut buf", *values])});',
		              f'\tshow("{_label(unit)}", &buf, got);'])
	return "\n".join([*lines, "}", ""])


def _python(tmp: Path, stem: str, units: list[Unit]) -> str:
	shutil.copy(RUNTIME / "python" / "situ_runtime.py", tmp)
	sys.path.insert(0, str(tmp))
	try:
		module = importlib.import_module(f"{stem}_build")
		base   = importlib.import_module("situ_runtime")
		lines  = []
		for unit in units:
			values = [_signed(part.placement, value)
			          if isinstance(value, int) else value
			          for part, value in _given(list(unit.parts))]
			buf  = bytearray(ROOM)
			name = py_name(unit.struct.name)
			if unit.suffix:
				name = f"{name}_{py_name(unit.suffix)}"
			try:
				wrote = getattr(module, f"build_{name}")(buf, *values)
			except base.SituError:
				lines.append(f"{_label(unit)} refused")
				continue
			lines.append(f"{_label(unit)} {bytes(buf[:wrote]).hex()}")
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
	units    = plain_build.units(resolved, shape)
	if not units:
		pytest.skip("no struct here is in the class")

	stem = schema.stem
	dirs = {target: tmp_path / target
	        for target in ("c", "cpp", "rust", "python")}
	for target, out in dirs.items():
		out.mkdir()
		_emit(schema, target, out)

	(dirs["c"] / "main.c").write_text(_c_driver(stem, units),
	                                  encoding="ascii")
	assert fourway.HOST_CC is not None and fourway.HOST_CXX is not None
	_run([fourway.HOST_CC, "-std=c11", "-w", f"-I{RUNTIME / 'c'}", "-I.",
	      "main.c", f"{stem}.c", str(RUNTIME / "c" / "situ.c"),
	      "-o", "probe"], dirs["c"])

	(dirs["cpp"] / "main.cpp").write_text(_cpp_driver(stem, units),
	                                      encoding="ascii")
	_run([fourway.HOST_CXX, "-std=c++17", "-Wall", "-Wextra", "-Werror",
	      f"-I{RUNTIME / 'c'}", f"-I{RUNTIME / 'cpp'}", "-I.", "main.cpp",
	      str(RUNTIME / "c" / "situ.c"), "-o", "probe"],
	     dirs["cpp"])

	shutil.copy(RUNTIME / "rust" / "situ_rt.rs", dirs["rust"])
	rt = dirs["rust"] / "situ_rt.rs"
	rt.write_text(rt.read_text(encoding="ascii").replace("#![no_std]\n", ""),
	              encoding="ascii")
	(dirs["rust"] / "main.rs").write_text(_rust_driver(stem, units),
	                                      encoding="ascii")
	assert fourway.RUSTC is not None
	_run([fourway.RUSTC, "--edition", "2021", "-A", "warnings", "main.rs",
	      "-o", "probe"], dirs["rust"])

	said = {"python": _python(dirs["python"], stem, units),
	        **{target: _run([str(dirs[target] / "probe")], dirs[target])
	           for target in ("c", "cpp", "rust")}}
	assert said["python"].count("\n") == len(units)
	assert len(set(said.values())) == 1, said
