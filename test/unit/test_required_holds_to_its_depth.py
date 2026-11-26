"""`required` refuses a message nested past its bound, in all four (26.1174).

fuzznet (inbox 20261126T1843Z) framed 100000 `[` with example/json's
generated `required` and the process died: framing recursed one call
chain per bracket and never asked `[depth]`, which only `validate` spent,
so a reader crashed on exactly the input the schema says it refuses. Each
backend now carries the depth through the recursion's own `required` and
refuses one level past the bound `validate` judges by -- `[depth]` as
malformed (CONSTRAINT), a smaller `[limit]` as more than this build
follows (DEPTH) -- and hands the refusal up through every level, because
reading it as "send more" would ask a framer for bytes that cannot help.

Asked of a JSON array exactly at its bound (16 brackets: four structs a
level, 64 deep), one past it, a prefix of each, and 100000 deep; and of
`edges.unbounded_nest`, whose `[limit = 2]` is a refusal of the reader's
rather than the format's. Every answer is compared with what the backend's
own `validate` says of the same bytes where they are whole.
"""

from __future__ import annotations

import importlib.util
import shutil
import struct as packing
import subprocess
import sys
from pathlib import Path

import pytest

ROOT    = Path(__file__).resolve().parents[2]
RUNTIME = ROOT / "runtime"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(RUNTIME / "python"))

from situc.cli import read_source			# noqa: E402
from situc.codegen.c import generate as c_generate	# noqa: E402
from situc.codegen.cpp import generate as cpp_generate	# noqa: E402
from situc.codegen.python import generate as py_generate	# noqa: E402
from situc.codegen.rust import generate as rust_generate	# noqa: E402
from situc.layout import solve				# noqa: E402
from situc.parser import parse				# noqa: E402
from situc.resolve import ResolvedSchema, resolve	# noqa: E402
from situc import ast					# noqa: E402

HOST_CC  = shutil.which("gcc") or shutil.which("cc")
HOST_CXX = shutil.which("g++") or shutil.which("clang++")
RUSTC    = shutil.which("rustc")


def _array(levels: int) -> bytes:
	return b"[" * levels + b"1" + b"]" * levels


def _chain(levels: int) -> bytes:
	"""`unbounded_nest` `levels` deep: each `n = 200` holds one kid."""
	return bytes([200] * levels + [0] + [9] * (levels + 1))


#: (schema, struct, [(bytes, required, validate)]). `required` answers
#: `ok N`, `need N`, `constraint` or `depth`; `validate` the same words, or
#: `-` for any other refusal. A whole message: the two agree. A prefix:
#: `validate` refuses it as short (`-`) while `required` already refuses
#: it as too deep -- no byte to come can make it shallower.
CASES = [
	("example/json/json.situ", "value", [
		(_array(16), "ok 33", "ok"),
		(_array(16)[:-1], "need 33", "-"),
		(_array(17), "constraint", "constraint"),
		(_array(17)[:20], "constraint", "-"),
		(b"[" * 100000 + b"]" * 100000, "constraint", "constraint"),
	]),
	("test/schema/edges.situ", "unbounded_nest", [
		(_chain(2), "ok 6", "ok"),
		(_chain(2)[:-1], "need 6", "-"),
		(_chain(3), "depth", "depth"),
		(_chain(100000), "depth", "depth"),
	]),
]


Case = tuple[bytes, str, str]


def _inputs(path: Path, cases: list[Case]) -> None:
	path.write_bytes(b"".join(packing.pack("<I", len(data)) + data
	                          for data, _, _ in cases))


def _expected(cases: list[Case]) -> list[str]:
	"""What `required` says, then what `validate` says."""
	return [f"{framed} {verdict}" for _, framed, verdict in cases]


def _generated(schema: str) -> tuple[ast.Schema, ResolvedSchema]:
	parsed = parse(read_source(ROOT / schema))
	return parsed, resolve(parsed, solve(parsed))


def _run(command: list[str], cwd: Path) -> str:
	done = subprocess.run(command, capture_output=True, text=True, cwd=cwd,
	                      timeout=600)
	assert done.returncode == 0, (command[0], done.stderr[-3000:])
	return done.stdout


C_PROBE = """#include <stdio.h>
#include <stdlib.h>
#include "%(stem)s.h"

static const char *said(situ_err_t e)
{
	return e == SITU_ERR_CONSTRAINT ? "constraint" : e == SITU_ERR_DEPTH ? "depth" : "other";
}

int main(int argc, char **argv)
{
	FILE *f = fopen(argv[argc - 1], "rb");
	unsigned char head[4];
	if (f == NULL) return 2;
	while (fread(head, 1, 4, f) == 4) {
		uint32_t n = head[0] | head[1] << 8 | head[2] << 16 | (uint32_t)head[3] << 24, need = 0;
		uint8_t *data = malloc(n ? n : 1);
		situ_msg_t msg; situ_view_t view; situ_err_t e;
		if (data == NULL || fread(data, 1, n, f) != n) return 2;
		e = situ_%(name)s_required(data, n, &need);
		if (e == SITU_OK) printf("ok %%u ", (unsigned)need);
		else if (e == SITU_ERR_TRUNCATED) printf("need %%u ", (unsigned)need);
		else printf("%%s ", said(e));
		situ_msg_init(&msg, data, n);
		e = situ_%(name)s_view(&msg, 0, n, &view);
		if (e == SITU_OK) e = situ_%(name)s_validate(view);
		puts(e == SITU_OK ? "ok" : e == SITU_ERR_CONSTRAINT || e == SITU_ERR_DEPTH ? said(e) : "-");
		free(data);
	}
	fclose(f);
	return 0;
}
"""


@pytest.mark.skipif(HOST_CC is None, reason="no host C compiler")
@pytest.mark.parametrize("schema,name,cases", CASES)
def test_c_refuses_past_the_bound(schema: str, name: str,
		cases: list[Case], tmp_path: Path) -> None:
	parsed, resolved = _generated(schema)
	stem  = Path(schema).stem
	built = c_generate(parsed, resolved, stem)
	(tmp_path / f"{stem}.h").write_text(built.header)
	(tmp_path / f"{stem}.c").write_text(built.source)
	(tmp_path / "probe.c").write_text(C_PROBE % {"stem": stem, "name": name})
	_inputs(tmp_path / "in.bin", cases)
	assert HOST_CC is not None
	# -Os, as fuzznet built it: the stack a frame costs is the point.
	_run([HOST_CC, "-std=c11", "-Os", "-Wall", "-Wextra", "-Werror",
	      f"-I{RUNTIME / 'c'}", "-I.", "probe.c", f"{stem}.c",
	      str(RUNTIME / "c" / "situ.c"), "-o", "probe"], tmp_path)
	got = _run(["./probe", "in.bin"], tmp_path).splitlines()
	assert got == _expected(cases)


CPP_PROBE = """#include <cstdio>
#include <vector>
#include "%(stem)s.hpp"

static const char *said(::situ::rt::err e)
{
	return e == ::situ::rt::err::constraint ? "constraint" : e == ::situ::rt::err::depth ? "depth" : "other";
}

int main(int argc, char **argv)
{
	std::FILE *f = std::fopen(argv[argc - 1], "rb");
	unsigned char head[4];
	if (f == nullptr) return 2;
	while (std::fread(head, 1, 4, f) == 4) {
		std::uint32_t n = head[0] | head[1] << 8 | head[2] << 16 | (std::uint32_t)head[3] << 24, need = 0;
		std::vector<std::uint8_t> data(n ? n : 1);
		if (std::fread(data.data(), 1, n, f) != n) return 2;
		auto e = ::situ::%(name)s::required(data.data(), n, need);
		if (e == ::situ::rt::err::ok) std::printf("ok %%u ", (unsigned)need);
		else if (e == ::situ::rt::err::truncated) std::printf("need %%u ", (unsigned)need);
		else std::printf("%%s ", said(e));
		e = ::situ::%(name)s{ situ_view_t{ data.data(), n, 0u, nullptr } }.validate();
		std::puts(e == ::situ::rt::err::ok ? "ok" : e == ::situ::rt::err::constraint || e == ::situ::rt::err::depth ? said(e) : "-");
	}
	std::fclose(f);
	return 0;
}
"""


@pytest.mark.skipif(HOST_CC is None or HOST_CXX is None,
                    reason="no host C and C++ compilers")
@pytest.mark.parametrize("schema,name,cases", CASES)
def test_cpp_refuses_past_the_bound(schema: str, name: str,
		cases: list[Case], tmp_path: Path) -> None:
	parsed, resolved = _generated(schema)
	stem   = Path(schema).stem
	cbuilt = c_generate(parsed, resolved, stem)
	(tmp_path / f"{stem}.hpp").write_text(
		cpp_generate(parsed, resolved, stem).header)
	(tmp_path / f"{stem}.h").write_text(cbuilt.header)
	(tmp_path / f"{stem}.c").write_text(cbuilt.source)
	(tmp_path / "probe.cpp").write_text(CPP_PROBE % {"stem": stem, "name": name})
	_inputs(tmp_path / "in.bin", cases)
	assert HOST_CC is not None and HOST_CXX is not None
	for source in (f"{stem}.c", str(RUNTIME / "c" / "situ.c")):
		_run([HOST_CC, "-std=c11", "-Os", "-c", f"-I{RUNTIME / 'c'}", "-I.",
		      source, "-o", Path(source).name + ".o"], tmp_path)
	_run([HOST_CXX, "-std=c++17", "-Os", "-Wall", "-Wextra", "-Werror",
	      f"-I{RUNTIME / 'c'}", f"-I{RUNTIME / 'cpp'}", "-I.", "probe.cpp",
	      f"{stem}.c.o", "situ.c.o", "-o", "probe"], tmp_path)
	got = _run(["./probe", "in.bin"], tmp_path).splitlines()
	assert got == _expected(cases)


RUST_MAIN = """#![allow(dead_code, unused)]
mod situ_rt;
mod %(stem)s;
use situ_rt::{Error, Framing};

fn said(e: Error) -> &'static str {
	match e { Error::Constraint => "constraint", Error::Depth => "depth", _ => "other" }
}

fn main() {
	let path = std::env::args().last().unwrap();
	let all = std::fs::read(path).unwrap();
	let mut at = 0;
	while at + 4 <= all.len() {
		let n = u32::from_le_bytes([all[at], all[at + 1], all[at + 2], all[at + 3]]) as usize;
		let data = &all[at + 4..at + 4 + n];
		at += 4 + n;
		let first = match %(stem)s::%(cls)s::required(data) {
			Framing::Complete(k) => format!("ok {}", k),
			Framing::Need(k) => format!("need {}", k),
			Framing::Refused(e) => said(e).to_string(),
		};
		let second = match %(stem)s::%(cls)s::new(data).and_then(|v| v.validate()) {
			Ok(_) => "ok",
			Err(e @ (Error::Constraint | Error::Depth)) => said(e),
			Err(_) => "-",
		};
		println!("{} {}", first, second);
	}
}
"""


@pytest.mark.skipif(RUSTC is None, reason="no rustc")
@pytest.mark.parametrize("schema,name,cases", CASES)
def test_rust_refuses_past_the_bound(schema: str, name: str,
		cases: list[Case], tmp_path: Path) -> None:
	parsed, resolved = _generated(schema)
	stem = Path(schema).stem
	(tmp_path / f"{stem}.rs").write_text(
		rust_generate(parsed, resolved, stem).module)
	(tmp_path / "situ_rt.rs").write_text(
		(RUNTIME / "rust" / "situ_rt.rs").read_text())
	cls = "".join(part.capitalize() for part in name.split("_"))
	(tmp_path / "main.rs").write_text(RUST_MAIN % {"stem": stem, "cls": cls})
	_inputs(tmp_path / "in.bin", cases)
	assert RUSTC is not None
	_run([RUSTC, "--edition", "2021", "-O", "-o", "probe", "main.rs"], tmp_path)
	got = _run(["./probe", "in.bin"], tmp_path).splitlines()
	assert got == _expected(cases)


@pytest.mark.parametrize("schema,name,cases", CASES)
def test_python_refuses_past_the_bound(schema: str, name: str,
		cases: list[Case], tmp_path: Path) -> None:
	parsed, resolved = _generated(schema)
	stem   = Path(schema).stem
	module = tmp_path / f"{stem}_depth.py"
	module.write_text(py_generate(parsed, resolved, stem).module)
	spec = importlib.util.spec_from_file_location(module.stem, module)
	assert spec is not None and spec.loader is not None
	loaded = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(loaded)
	rt   = sys.modules[loaded.Message.__module__]
	kind = getattr(loaded, name)

	def said(error: BaseException) -> str:
		return ("depth" if isinstance(error, rt.DepthError) else
		        "constraint" if isinstance(error, rt.ConstraintError) else
		        "other")

	got = []
	for data, _, _ in cases:
		try:
			first = f"ok {kind.required(data)}"
		except rt.TruncatedError as short:
			first = f"need {short.needed}"
		except rt.SituError as refused:
			first = said(refused)
		try:
			kind.at(rt.Message(bytearray(data)), 0, len(data)).validate()
			second = "ok"
		except (rt.ConstraintError, rt.DepthError) as refused:
			second = said(refused)
		except rt.SituError:
			second = "-"
		got.append(f"{first} {second}")
	assert got == _expected(cases)
