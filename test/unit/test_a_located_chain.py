"""A recursive member placed by `at`: a chain the message links together.

openmlx4's FS2 image lists its sections this way -- each section's header
says where the next one starts, measured from the start of the image:

	struct chain [depth = 8] {
		hdr   head;
		u8    data[head.size];
		chain following at head.next;
	}

`situc build` accepted it and generated code that compiled in no backend: C
called a `_following_extent_at` and a `_following_offset` it never emitted,
C++ and Rust a `following_count` and an `extent`, and Python's probe read a
count that did not exist. All four had walked the located member as though
it followed `data` inside the frame.

It does not. A located member is reached from the message, so it adds
nothing to its struct's extent -- the layout's minimum already left it out,
and `traverse.extent_parts` now does too -- and the depth probe follows it
through the message, as `validate` already did. Depth is then counted along
the chain like any nested member's, which is what makes a chain that loops
back on itself a CONSTRAINT refusal rather than a hang.

A chain with no way to end cannot validate at all: its last `next` points
somewhere, and outside the message that is BOUNDS. The cases below hold
both refusals and the nesting count, which separates chains of different
lengths where the verdicts alone cannot.
"""

from __future__ import annotations

import shutil
import struct
import subprocess
import sys
from pathlib import Path

import pytest

from situc.cli import main

ROOT = Path(__file__).resolve().parents[2]
RUNTIME = ROOT / "runtime"

SCHEMA = ("target buffer;\nendian big;\n"
          "struct hdr { u32 size; u32 next; }\n"
          "struct chain [depth = 8] {\n"
          "\thdr   head;\n"
          "\tu8    data[head.size];\n"
          "\tchain following at head.next;\n"
          "}\n")


def _sections(*pairs: tuple[int, int, int]) -> bytes:
	"""Sections at the given offsets: (at, size, next), zero-filled between."""
	end = max(at + 8 + size for at, size, _ in pairs)
	out = bytearray(end)
	for at, size, nxt in pairs:
		out[at:at + 8] = struct.pack(">II", size, nxt)
	return bytes(out)


#: name -> (message, verdict, nesting). Nesting saturates at 10, one above
#: the depth this build follows; past `[depth = 8]` the verdict is
#: CONSTRAINT, and a `next` outside the message is BOUNDS.
CASES: dict[str, tuple[bytes, str, int]] = {
	"loops_to_itself": (_sections((0, 2, 0)), "constraint", 10),
	"three_then_out":  (_sections((0, 2, 10), (10, 1, 19), (19, 0, 200)),
	                    "bounds", 2),
	"six_then_out":    (_sections(*((8 * i, 0, 8 * (i + 1)) for i in range(5)),
	                              (40, 0, 1000)), "bounds", 5),
	"twelve":          (_sections(*((8 * i, 0, 8 * (i + 1))
	                                for i in range(12))), "constraint", 10),
	"loops_after_three": (_sections((0, 0, 8), (8, 0, 16), (16, 0, 0)),
	                      "constraint", 10),
}


def _build(tmp_path: Path, target: str) -> Path:
	schema = tmp_path / "chain.situ"
	schema.write_text(SCHEMA, encoding="ascii")
	out = tmp_path / target
	assert main(["build", str(schema), "--target", target,
	             "--out", str(out)]) == 0
	return out


def _table(ran: "subprocess.CompletedProcess[str]") -> dict[str, tuple[str, int]]:
	assert ran.returncode == 0, ran.stdout + ran.stderr
	rows = {}
	for line in ran.stdout.splitlines():
		name, verdict, nesting = line.split()
		rows[name] = (verdict, int(nesting))
	return rows


def _want() -> dict[str, tuple[str, int]]:
	return {name: (verdict, nesting)
	        for name, (_, verdict, nesting) in CASES.items()}


def _c_bytes(data: bytes) -> str:
	return "{" + ", ".join(f"0x{b:02x}" for b in data) + "}"


@pytest.mark.skipif(shutil.which("gcc") is None, reason="no C compiler")
def test_c(tmp_path: Path) -> None:
	out = _build(tmp_path, "c")
	rows = "\n".join(
		f'\t{{ "{name}", (const uint8_t[]){_c_bytes(data)}, {len(data)}u }},'
		for name, (data, _, _) in CASES.items())
	(out / "main.c").write_text(f'''#include <stdio.h>
#include <string.h>
#include "chain.h"
static const struct {{ const char *name; const uint8_t *data; uint32_t n; }} cases[] = {{
{rows}
}};
static const char *word(situ_err_t e)
{{
	return e == SITU_OK ? "ok" : e == SITU_ERR_BOUNDS ? "bounds"
	     : e == SITU_ERR_CONSTRAINT ? "constraint" : e == SITU_ERR_DEPTH ? "depth" : "other";
}}
int main(void)
{{
	unsigned i;
	for (i = 0; i < sizeof cases / sizeof cases[0]; i++) {{
		uint8_t     buf[128];
		situ_msg_t  msg;
		situ_view_t view;

		memcpy(buf, cases[i].data, cases[i].n);
		situ_msg_init(&msg, buf, cases[i].n);
		if (situ_chain_view(&msg, 0u, cases[i].n, &view) != SITU_OK) {{
			return 1;
		}}
		printf("%s %s %u\\n", cases[i].name, word(situ_chain_validate(view)),
		       situ_chain_nesting(view));
	}}
	return 0;
}}
''', encoding="ascii")
	built = subprocess.run(
		["gcc", "-std=c99", "-Os", "-Wall", "-Wextra", "-Werror",
		 f"-I{RUNTIME / 'c'}", f"-I{out}", str(out / "main.c"),
		 str(out / "chain.c"), str(RUNTIME / "c" / "situ.c"),
		 "-o", str(out / "r")], capture_output=True, text=True)
	assert built.returncode == 0, built.stderr
	assert _table(subprocess.run([str(out / "r")], capture_output=True,
	                             text=True)) == _want()


@pytest.mark.skipif(shutil.which("g++") is None, reason="no C++ compiler")
def test_cpp(tmp_path: Path) -> None:
	out = _build(tmp_path, "cpp")
	rows = "\n".join(
		f'\t{{ "{name}", {{{", ".join(f"0x{b:02x}" for b in data)}}},'
		f' {len(data)}u }},'
		for name, (data, _, _) in CASES.items())
	(out / "main.cpp").write_text(f'''#include <cstdio>
#include <cstring>
#include "chain.hpp"
struct one {{ const char *name; std::uint8_t data[128]; std::uint32_t n; }};
static const one cases[] = {{
{rows}
}};
static const char *word(::situ::rt::err e)
{{
	return e == ::situ::rt::err::ok ? "ok" : e == ::situ::rt::err::bounds ? "bounds"
	     : e == ::situ::rt::err::constraint ? "constraint" : "other";
}}
int main()
{{
	for (const one &c : cases) {{
		std::uint8_t buf[128];
		std::memcpy(buf, c.data, c.n);
		situ_msg_t msg;
		situ_msg_init(&msg, buf, c.n);
		situ_view_t view;
		situ_view_at(&msg, 0u, c.n, &view);
		const ::situ::chain top(view);
		std::printf("%s %s %u\\n", c.name, word(top.validate()), top.nesting());
	}}
	return 0;
}}
''', encoding="ascii")
	built = subprocess.run(
		["g++", "-std=c++17", "-Os", "-Wall", "-Wextra", "-Werror",
		 f"-I{RUNTIME / 'c'}", f"-I{RUNTIME / 'cpp'}", f"-I{out}",
		 str(out / "main.cpp"), str(RUNTIME / "c" / "situ.c"),
		 "-o", str(out / "r")], capture_output=True, text=True)
	assert built.returncode == 0, built.stderr
	assert _table(subprocess.run([str(out / "r")], capture_output=True,
	                             text=True)) == _want()


@pytest.mark.skipif(shutil.which("rustc") is None, reason="no Rust compiler")
def test_rust(tmp_path: Path) -> None:
	out = _build(tmp_path, "rust")
	shutil.copy(RUNTIME / "rust" / "situ_rt.rs", out / "situ_rt.rs")
	rows = "\n".join(
		f'\t\t("{name}", vec![{", ".join(str(b) for b in data)}]),'
		for name, (data, _, _) in CASES.items())
	(out / "main.rs").write_text(f'''mod situ_rt;
mod chain;
fn main() {{
	let cases: Vec<(&str, Vec<u8>)> = vec![
{rows}
	];
	for (name, d) in cases {{
		let top = chain::Chain::new(&d).unwrap();
		let word = match top.validate() {{
			Ok(()) => "ok",
			Err(situ_rt::Error::Bounds) => "bounds",
			Err(situ_rt::Error::Constraint) => "constraint",
			Err(_) => "other",
		}};
		println!("{{}} {{}} {{}}", name, word, top.nesting());
	}}
}}
''', encoding="ascii")
	built = subprocess.run(
		["rustc", "--edition", "2021", "-O", "-o", str(out / "r"),
		 str(out / "main.rs")], capture_output=True, text=True)
	assert built.returncode == 0, built.stderr
	assert _table(subprocess.run([str(out / "r")], capture_output=True,
	                             text=True)) == _want()


def test_python(tmp_path: Path) -> None:
	out = _build(tmp_path, "python")
	shutil.copy(RUNTIME / "python" / "situ_runtime.py",
	            out / "situ_runtime.py")
	cases = ", ".join(f"({name!r}, {data!r})"
	                  for name, (data, _, _) in CASES.items())
	(out / "main.py").write_text(f'''import situ_runtime as rt
import chain
for name, data in [{cases}]:
	top = chain.chain.at(rt.Message(bytearray(data)), 0, len(data))
	try:
		top.validate()
		word = "ok"
	except rt.BoundsError:
		word = "bounds"
	except rt.ConstraintError:
		word = "constraint"
	print(name, word, top.nesting)
''', encoding="ascii")
	assert _table(subprocess.run([sys.executable, str(out / "main.py")],
	                             capture_output=True, text=True,
	                             cwd=out)) == _want()


def test_python_type_checks(tmp_path: Path) -> None:
	"""The probe's `try`/`else` is shaped for mypy: binding the element to
	`None` on a miss would retype a variable `mypy` reads as the struct."""
	if subprocess.run([sys.executable, "-m", "mypy", "--version"],
	                  capture_output=True).returncode != 0:
		pytest.skip("no mypy")
	out = _build(tmp_path, "python")
	shutil.copy(RUNTIME / "python" / "situ_runtime.py",
	            out / "situ_runtime.py")
	checked = subprocess.run(
		[sys.executable, "-m", "mypy", "--strict", "--no-pretty",
		 "--no-error-summary", str(out / "chain.py")],
		capture_output=True, text=True)
	assert checked.returncode == 0, checked.stdout + checked.stderr
