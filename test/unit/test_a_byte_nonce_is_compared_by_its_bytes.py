"""A replay window's nonce may be a run of bytes (26.1120, amended).

fuzznet's nonce is XChaCha's 24 random bytes (inbox 20261123T1215Z), and
`nonce =` refused anything but a scalar. A fixed-size byte run is now held
in the slot as its bytes and compared by them, in all four backends. The
verdicts, worked by hand for a two-slot table at 1000 with expiries at
1500: `AAAA` OK, `AAAA` again REPLAY, `AAAB` checked OK and recorded OK,
`AAAC` FULL, and at 1600 -- both slots expired -- `AAAA` until 2000 OK.

Verdicts: OK 0, REPLAY 1, FULL 5.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from every_schema import ROOT

SCHEMA = """target buffer;
endian big;
struct h { u8 nonce[4]; u64 exp [time = ms, epoch = unix]; }
struct f { h head; }
replay n(m: f) [nonce = m.head.nonce, expires = m.head.exp, capacity = 2,
                ahead = 1000];
"""

WANT = [0, 1, 0, 0, 5, 0]

#: (nonce, expires, now, record)
STEPS = [(b"AAAA", 1500, 1000, True), (b"AAAA", 1500, 1000, True),
         (b"AAAB", 1500, 1000, False), (b"AAAB", 1500, 1000, True),
         (b"AAAC", 1500, 1000, True), (b"AAAA", 2000, 1600, False)]


def _generate(where: Path, target: str) -> None:
	(where / "rb.situ").write_text(SCHEMA, encoding="ascii")
	done = subprocess.run(
		[sys.executable, str(ROOT / "bin" / "situc"), "build", "--target",
		 target, "--layer", "converse", "--out", str(where),
		 str(where / "rb.situ")], capture_output=True, text=True,
		timeout=300)
	assert done.returncode == 0, done.stderr


def _calls(form: str) -> str:
	"""The steps as calls in one language, `form` taking the six fields."""
	return "\n".join(form.format(nonce=", ".join(str(b) for b in nonce),
	                             text=nonce.decode(), exp=exp, now=now,
	                             op="record" if record else "check")
	                 for nonce, exp, now, record in STEPS)


def test_python(tmp_path: Path) -> None:
	_generate(tmp_path, "python")
	sys.path[:0] = [str(tmp_path), str(ROOT / "runtime" / "python")]
	try:
		import rb					# type: ignore[import-not-found]
		import rb_replay as replay			# type: ignore[import-not-found]
		from situ_runtime import Message		# type: ignore[import-not-found]
		table = replay.N(2)
		said = []
		for nonce, exp, now, record in STEPS:
			view = rb.f.at(Message(bytearray(nonce + exp.to_bytes(8, "big"))),
			               0)
			said.append((table.record if record else table.check)(view, now))
		assert said == WANT
	finally:
		del sys.path[:2]
		for name in ("rb", "rb_replay"):
			sys.modules.pop(name, None)


C_DRIVER = """#include <stdio.h>
#include "rb_replay.h"
static uint8_t buf[12];
static situ_msg_t msg;
static situ_view_t at(const char *nonce, uint64_t exp)
{
	situ_view_t view;
	for (int i = 0; i < 4; i++) buf[i] = (uint8_t)nonce[i];
	for (int i = 0; i < 8; i++) buf[4 + i] = (uint8_t)(exp >> (56 - 8 * i));
	situ_msg_init(&msg, buf, sizeof buf);
	situ_f_view(&msg, 0, &view);
	return view;
}
int main(void)
{
	static situ_n_slot_t slots[2];
	situ_n_t n = {slots, 2, 0};
	situ_replay_verdict v;
%s
	printf("\\n");
	return 0;
}
"""


def _run(argv: list[str], cwd: Path) -> str:
	done = subprocess.run(argv, capture_output=True, text=True, timeout=600,
	                      cwd=cwd)
	assert done.returncode == 0, done.stderr
	return done.stdout


@pytest.mark.skipif(shutil.which("cc") is None, reason="no C compiler")
def test_c(tmp_path: Path) -> None:
	_generate(tmp_path, "c")
	calls = _calls('\tsitu_n_{op}(&n, at("{text}", {exp}), {now}, &v);'
	               ' printf(" %d", v);')
	(tmp_path / "t.c").write_text(C_DRIVER % calls, encoding="ascii")
	_run(["cc", "-Wall", "-Wextra", "-Werror", f"-I{ROOT / 'runtime' / 'c'}",
	      f"-I{tmp_path}", "t.c", "rb.c", str(ROOT / "runtime" / "c" / "situ.c"),
	      "-o", "t"], tmp_path)
	said = _run([str(tmp_path / "t")], tmp_path)
	assert [int(word) for word in said.split()] == WANT, said


CPP_DRIVER = """#include <cstdio>
#include "rb_replay.hpp"
static std::uint8_t buf[12];
static ::situ::f at(const char *nonce, std::uint64_t exp)
{
	for (int i = 0; i < 4; i++) buf[i] = std::uint8_t(nonce[i]);
	for (int i = 0; i < 8; i++) buf[4 + i] = std::uint8_t(exp >> (56 - 8 * i));
	static ::situ::rt::message m(buf, 12);
	::situ::f out; (void)::situ::f::at(m, 0, out); return out;
}
int main()
{
	using namespace situ_replay; n set; verdict v;
	n::slot slots[2] = {}; set.slots = slots; set.count = 2;
%s
	std::printf("\\n");
}
"""


@pytest.mark.skipif(shutil.which("g++") is None, reason="no C++ compiler")
def test_cpp(tmp_path: Path) -> None:
	_generate(tmp_path, "cpp")
	calls = _calls('\t(void)set.{op}(at("{text}", {exp}), {now}, v);'
	               ' std::printf(" %d", int(v));')
	(tmp_path / "t.cpp").write_text(CPP_DRIVER % calls, encoding="ascii")
	_run(["g++", "-std=c++17", "-Wall", "-Wextra", "-Werror",
	      f"-I{ROOT / 'runtime' / 'c'}", f"-I{ROOT / 'runtime' / 'cpp'}",
	      f"-I{tmp_path}", "t.cpp", str(ROOT / "runtime" / "c" / "situ.c"),
	      "-o", "t"], tmp_path)
	said = _run([str(tmp_path / "t")], tmp_path)
	assert [int(word) for word in said.split()] == WANT, said


RUST_DRIVER = """mod situ_rt;
#[allow(dead_code)]
mod rb;
mod rb_replay;
use rb_replay::*;
fn view(nonce: &[u8], exp: u64) -> Vec<u8> {
	let mut b = nonce.to_vec(); b.extend(exp.to_be_bytes()); b
}
fn main() {
	let mut table = [([0u8; 4], 0u64); 2];
	let mut n = N { slots: &mut table, max_ahead: 0 };
	let mut out = vec![];
%s
	for v in out { print!(" {}", v as i32); }
	println!();
}
"""


@pytest.mark.skipif(shutil.which("rustc") is None, reason="no Rust compiler")
def test_rust(tmp_path: Path) -> None:
	_generate(tmp_path, "rust")
	runtime = (ROOT / "runtime" / "rust" / "situ_rt.rs").read_text(
		encoding="ascii").replace("#![no_std]\n", "")
	(tmp_path / "situ_rt.rs").write_text(runtime, encoding="ascii")
	calls = _calls('\t{{ let b = view(&[{nonce}], {exp}); let f = '
	               'rb::F::new(&b).unwrap(); '
	               'out.push(n.{op}(&f, {now}).unwrap()); }}')
	(tmp_path / "main.rs").write_text(RUST_DRIVER % calls, encoding="ascii")
	_run(["rustc", "--edition", "2021", "-D", "warnings", "-A", "dead_code",
	      "main.rs", "-o", "t"],
	     tmp_path)
	said = _run([str(tmp_path / "t")], tmp_path)
	assert [int(word) for word in said.split()] == WANT, said
