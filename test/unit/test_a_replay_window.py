"""A replay window, in all four backends, held to verdicts worked by hand.

26.1120, after fuzznet's review: a counter window keyed by the caller's
state and a nonce set with expiries; a pure `check` asked before the tag
and `record` after it. The cases are the same in every backend, and the
answers are worked here, not taken from one backend and held to the rest:

- the counter window (64): 10 accepted, 10 again REPLAY, 12 accepted, 11
  checked OK (inside the window, unseen), 200 accepted, then 136 -- 64
  back -- TOO_OLD and 137 -- 63 back -- accepted;
- the nonce set (4 slots, 1000 ms ahead, a zero expiry optional): nonce 7
  until 1500 accepted at 1000, again REPLAY, an expiry of 900 EXPIRED,
  2001 HORIZON (1001 ahead), 0 accepted without being remembered; three
  more fill the four slots, a fifth is FULL, and at 2000 -- the first two
  expired -- it is accepted.

Verdicts: OK 0, REPLAY 1, TOO_OLD 2, EXPIRED 3, HORIZON 4, FULL 5.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from every_schema import ROOT
from situc.diagnostics import SituError
from situc.parser import parse_text

SCHEMA = """target buffer;
endian big;
struct h { u32 seq; u64 nonce; u64 exp; }
struct f { h head; u8 body[remaining]; }
replay w(m: f) [counter = m.head.seq, window = 64];
replay n(m: f) [nonce = m.head.nonce, expires = m.head.exp, capacity = 4,
                ahead = 1000, unit = ms, expiry = optional];
"""

COUNTER = [0, 1, 0, 0, 0, 2, 0]
NONCES  = [0, 1, 3, 4, 0, 5, 0]

C_DRIVER = r"""
#include <stdio.h>
#include <string.h>
#include "rp_replay.h"
static uint8_t buf[20];
static situ_msg_t msg;
static situ_replay_verdict v;
static situ_view_t at(uint32_t seq, uint64_t nonce, uint64_t exp)
{
	situ_view_t view;
	for (int i = 0; i < 4; i++) buf[i] = (uint8_t)(seq >> (24 - 8 * i));
	for (int i = 0; i < 8; i++) buf[4 + i] = (uint8_t)(nonce >> (56 - 8 * i));
	for (int i = 0; i < 8; i++) buf[12 + i] = (uint8_t)(exp >> (56 - 8 * i));
	situ_msg_init(&msg, buf, sizeof buf);
	situ_f_view(&msg, 0, sizeof buf, &view);
	return view;
}
int main(void)
{
	static situ_w_t w;
	static situ_n_slot_t slots[4];
	situ_n_t n = {slots, 4};
	situ_n_t small = {slots, 3};
	printf("w");
	situ_w_record(&w, at(10, 0, 0), &v); printf(" %d", v);
	situ_w_record(&w, at(10, 0, 0), &v); printf(" %d", v);
	situ_w_record(&w, at(12, 0, 0), &v); printf(" %d", v);
	situ_w_check(&w, at(11, 0, 0), &v); printf(" %d", v);
	situ_w_record(&w, at(200, 0, 0), &v); printf(" %d", v);
	situ_w_record(&w, at(136, 0, 0), &v); printf(" %d", v);
	situ_w_record(&w, at(137, 0, 0), &v); printf(" %d", v);
	printf("\nn");
	situ_n_record(&n, at(0, 7, 1500), 1000, &v); printf(" %d", v);
	situ_n_record(&n, at(0, 7, 1500), 1000, &v); printf(" %d", v);
	situ_n_check(&n, at(0, 8, 900), 1000, &v); printf(" %d", v);
	situ_n_check(&n, at(0, 8, 2001), 1000, &v); printf(" %d", v);
	situ_n_check(&n, at(0, 9, 0), 1000, &v); printf(" %d", v);
	for (int i = 0; i < 3; i++)
		situ_n_record(&n, at(0, 20 + (uint64_t)i, 1900), 1000, &v);
	situ_n_record(&n, at(0, 30, 1900), 1000, &v); printf(" %d", v);
	situ_n_record(&n, at(0, 30, 2600), 2000, &v); printf(" %d", v);
	printf("\nsmall %d\n",
	       situ_n_check(&small, at(0, 40, 2900), 2000, &v) == SITU_ERR_BOUNDS);
	return 0;
}
"""

CPP_DRIVER = r"""#include <cstdio>
#include "rp_replay.hpp"
static std::uint8_t buf[20];
static ::situ::rt::message *msg;
static ::situ::f at(std::uint32_t seq, std::uint64_t nonce, std::uint64_t exp)
{
	for (int i = 0; i < 4; i++) buf[i] = std::uint8_t(seq >> (24 - 8 * i));
	for (int i = 0; i < 8; i++) buf[4 + i] = std::uint8_t(nonce >> (56 - 8 * i));
	for (int i = 0; i < 8; i++) buf[12 + i] = std::uint8_t(exp >> (56 - 8 * i));
	static ::situ::rt::message m(buf, 20); msg = &m;
	::situ::f out; (void)::situ::f::at(*msg, 0, 20, out); return out;
}
int main()
{
	using namespace situ_replay; w win; n set; verdict v;
	n::slot slots[4] = {}; set.slots = slots; set.count = 4;
	auto r = [&](::situ::f m) { (void)win.record(m, v); std::printf(" %d", int(v)); };
	auto c = [&](::situ::f m) { (void)win.check(m, v); std::printf(" %d", int(v)); };
	std::printf("w");
	r(at(10,0,0)); r(at(10,0,0)); r(at(12,0,0)); c(at(11,0,0));
	r(at(200,0,0)); r(at(136,0,0)); r(at(137,0,0));
	auto s = [&](::situ::f m, std::uint64_t now) { (void)set.record(m, now, v); std::printf(" %d", int(v)); };
	auto k = [&](::situ::f m, std::uint64_t now) { (void)set.check(m, now, v); std::printf(" %d", int(v)); };
	std::printf("\nn");
	s(at(0,7,1500),1000); s(at(0,7,1500),1000); k(at(0,8,900),1000);
	k(at(0,8,2001),1000); k(at(0,9,0),1000);
	for (int i = 0; i < 3; i++) (void)set.record(at(0,20+i,1900),1000,v);
	s(at(0,30,1900),1000); s(at(0,30,2600),2000);
	std::printf("\n");
}
"""

RUST_DRIVER = r"""mod situ_rt;
mod rp;
mod rp_replay;
use rp_replay::*;
fn bytes(seq: u32, nonce: u64, exp: u64) -> Vec<u8> {
	let mut b = seq.to_be_bytes().to_vec(); b.extend(nonce.to_be_bytes()); b.extend(exp.to_be_bytes()); b
}
fn main() {
	let mut table = [(0u64, 0u64); 4];
	let mut w = W::default(); let mut n = N { slots: &mut table }; let mut out = vec![];
	for (seq, check) in [(10, false), (10, false), (12, false), (11, true), (200, false), (136, false), (137, false)] {
		let b = bytes(seq, 0, 0); let f = rp::F::new(&b).unwrap();
		out.push(if check { w.check(&f) } else { w.record(&f) }.unwrap() as i32);
	}
	println!("w {:?}", out); out.clear();
	for (nonce, exp, now, check) in [(7, 1500, 1000, false), (7, 1500, 1000, false), (8, 900, 1000, true), (8, 2001, 1000, true), (9, 0, 1000, true)] {
		let b = bytes(0, nonce, exp); let f = rp::F::new(&b).unwrap();
		out.push(if check { n.check(&f, now) } else { n.record(&f, now) }.unwrap() as i32);
	}
	for i in 0..3 { let b = bytes(0, 20 + i, 1900); let f = rp::F::new(&b).unwrap(); n.record(&f, 1000).unwrap(); }
	for (exp, now) in [(1900u64, 1000u64), (2600, 2000)] { let b = bytes(0, 30, exp); let f = rp::F::new(&b).unwrap(); out.push(n.record(&f, now).unwrap() as i32); }
	println!("n {:?}", out);
}
"""


def _generate(where: Path, target: str) -> None:
	(where / "rp.situ").write_text(SCHEMA, encoding="ascii")
	done = subprocess.run(
		[sys.executable, str(ROOT / "bin" / "situc"), "build", "--target",
		 target, "--layer", "converse", "--out", str(where),
		 str(where / "rp.situ")], capture_output=True, text=True,
		timeout=300)
	assert done.returncode == 0, done.stderr


def _runtime(where: Path) -> Path:
	out = where / "runtime"
	done = subprocess.run(["make", "-C", str(ROOT / "runtime" / "c"),
	                       f"BUILD_DIR={out}"], capture_output=True,
	                      text=True, timeout=300)
	assert done.returncode == 0, done.stderr
	return next(out.rglob("libsitu.a"))


def _numbers(text: str) -> list[list[int]]:
	return [[int(word) for word in line.split()[1:]]
	        for line in text.splitlines() if line.strip()]


@pytest.mark.parametrize("attrs,refusal", [
	("[counter = m.head.seq, window = 64, nonce = m.head.nonce]",
	 "one form of replay window"),
	("[counter = m.head.seq, window = 65]", "from 1 to 64"),
	("[nonce = m.head.nonce, expires = m.head.exp, capacity = 4, ahead = 0, "
	 "unit = ms]", "from 1 to"),
	("[nonce = m.head.nonce, expires = m.head.exp, capacity = 4, "
	 "ahead = 5]", "does not state `unit`"),
	("[counter = seq, window = 8]", "names a field of `m`"),
	("[counter = m.head.seq, window = 8, expiry = optional]",
	 "has no expiry"),
	("[counter = m.head.seq, window = 8, capacity = 4]", "has no capacity"),
])
def test_what_cannot_hold_is_refused(attrs: str, refusal: str) -> None:
	with pytest.raises(SituError, match=refusal):
		parse_text("target buffer;\nendian big;\n"
		           "struct h { u32 seq; u64 nonce; u64 exp; }\n"
		           "struct f { h head; }\n"
		           f"replay w(m: f) {attrs};\n")


def test_python(tmp_path: Path) -> None:
	_generate(tmp_path, "python")
	sys.path[:0] = [str(tmp_path), str(ROOT / "runtime" / "python")]
	try:
		import rp					# type: ignore[import-not-found]
		import rp_replay as replay			# type: ignore[import-not-found]
		from situ_runtime import Message		# type: ignore[import-not-found]

		def at(seq: int, nonce: int, exp: int) -> object:
			raw = bytearray(seq.to_bytes(4, "big") + nonce.to_bytes(8, "big")
			                + exp.to_bytes(8, "big"))
			return rp.f.at(Message(raw), 0, 20)

		w, n = replay.W(), replay.N(4)
		said = [w.record(at(10, 0, 0)), w.record(at(10, 0, 0)),
		        w.record(at(12, 0, 0)), w.check(at(11, 0, 0)),
		        w.record(at(200, 0, 0)), w.record(at(136, 0, 0)),
		        w.record(at(137, 0, 0))]
		assert said == COUNTER
		said = [n.record(at(0, 7, 1500), 1000), n.record(at(0, 7, 1500), 1000),
		        n.check(at(0, 8, 900), 1000), n.check(at(0, 8, 2001), 1000),
		        n.check(at(0, 9, 0), 1000)]
		for i in range(3):
			n.record(at(0, 20 + i, 1900), 1000)
		said += [n.record(at(0, 30, 1900), 1000),
		         n.record(at(0, 30, 2600), 2000)]
		assert said == NONCES
	finally:
		del sys.path[:2]
		for name in ("rp", "rp_replay"):
			sys.modules.pop(name, None)


@pytest.mark.skipif(shutil.which("cc") is None, reason="no C compiler")
def test_c(tmp_path: Path) -> None:
	_generate(tmp_path, "c")
	(tmp_path / "t.c").write_text(C_DRIVER, encoding="ascii")
	built = subprocess.run(
		["cc", "-Wall", "-Wextra", "-Werror", f"-I{ROOT / 'runtime' / 'c'}",
		 f"-I{tmp_path}", str(tmp_path / "t.c"), str(tmp_path / "rp.c"),
		 str(_runtime(tmp_path)), "-o", str(tmp_path / "t")],
		capture_output=True, text=True, timeout=300)
	assert built.returncode == 0, built.stderr
	ran = subprocess.run([str(tmp_path / "t")], capture_output=True,
	                     text=True, timeout=60)
	assert _numbers(ran.stdout) == [COUNTER, NONCES, [1]], ran.stdout


@pytest.mark.skipif(shutil.which("g++") is None, reason="no C++ compiler")
def test_cpp(tmp_path: Path) -> None:
	_generate(tmp_path, "cpp")
	(tmp_path / "t.cpp").write_text(CPP_DRIVER, encoding="ascii")
	built = subprocess.run(
		["g++", "-std=c++17", "-Wall", "-Wextra", "-Werror",
		 f"-I{ROOT / 'runtime' / 'c'}", f"-I{ROOT / 'runtime' / 'cpp'}",
		 f"-I{tmp_path}", str(tmp_path / "t.cpp"), str(_runtime(tmp_path)),
		 "-o", str(tmp_path / "t")],
		capture_output=True, text=True, timeout=300)
	assert built.returncode == 0, built.stderr
	ran = subprocess.run([str(tmp_path / "t")], capture_output=True,
	                     text=True, timeout=60)
	assert _numbers(ran.stdout) == [COUNTER, NONCES], ran.stdout


@pytest.mark.skipif(shutil.which("rustc") is None, reason="no Rust compiler")
def test_rust(tmp_path: Path) -> None:
	_generate(tmp_path, "rust")
	shutil.copy(ROOT / "runtime" / "rust" / "situ_rt.rs", tmp_path)
	(tmp_path / "main.rs").write_text(RUST_DRIVER, encoding="ascii")
	built = subprocess.run(
		["rustc", "--edition", "2021", "-A", "dead_code",
		 str(tmp_path / "main.rs"), "-o", str(tmp_path / "t")],
		capture_output=True, text=True, timeout=600, cwd=tmp_path)
	assert built.returncode == 0, built.stderr
	ran = subprocess.run([str(tmp_path / "t")], capture_output=True,
	                     text=True, timeout=60)
	lines = [line.replace("[", "").replace("]", "").replace(",", "")
	         for line in ran.stdout.splitlines()]
	assert _numbers("\n".join(lines)) == [COUNTER, NONCES], ran.stdout
