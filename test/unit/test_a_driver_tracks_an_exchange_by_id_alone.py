"""A driver tracks an exchange by its id alone (26.1064).

fuzznet (inbox 20261117T1816Z) runs its resends on the generated driver
(26.1055) and matches replies itself, after opening them: the relation's
key is inside sealed bytes, which the driver cannot read. `track` asked
for a request view all the same, so fuzznet handed it a blank 144-byte
frame per exchange -- a key slot spent per exchange on a key no reply was
meant to match, and one an all-zero reply WOULD have matched.

`track_by_id` reads no request and holds no key. One scenario, the same in
all four:

  track_by_id 1 at 0, 100 ms, no retries -> nothing sent
  peek a blank reply                     -> constraint: no key is held
  track 2 (keyed) and peek its reply     -> 2, so keyed exchanges still match
  step at 150                            -> 1 expires, the hook says 1
  track_by_id 3, end 3                   -> ok; end 3 again -> constraint
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from situc.codegen.c import converse, drive
from situc.codegen.c import generate as generate_c
from situc.codegen.cpp import converse as converse_cpp
from situc.codegen.cpp import drive as drive_cpp
from situc.codegen.cpp import frame as frame_cpp
from situc.codegen.cpp import generate as generate_cpp
from situc.codegen.cpp import relate as relate_cpp
from situc.codegen.python import converse as converse_py
from situc.codegen.python import drive as drive_py
from situc.codegen.python import generate as generate_py
from situc.codegen.python import relate as relate_py
from situc.codegen.rust import converse as converse_rs
from situc.codegen.rust import drive as drive_rs
from situc.codegen.rust import frame as frame_rs
from situc.codegen.rust import generate as generate_rs
from situc.codegen.rust import relate as relate_rs
from test_a_part_schedule import SCHEMA
from test_relations import (COMPILER, HOST_CXX, RUNTIME, RUSTC, WARNINGS,
                            analysed)

ROOT = Path(__file__).resolve().parents[2]

#: What each driver prints, in order.
WANT = ["tracked, sent nothing", "peek blank constraint", "peek 2",
        "expired 1 count 1", "end ok", "end again constraint"]


@pytest.mark.skipif(COMPILER is None, reason="no C compiler")
def test_c(tmp_path: Path) -> None:
	schema, resolved = analysed(SCHEMA)
	for name, text in generate_c(schema, resolved, "t").files().items():
		(tmp_path / name).write_text(text, encoding="ascii")
	for emitted in (converse.generate(schema, resolved, "t"),
	                drive.generate(schema, resolved, "t")):
		for name, text in emitted.items():
			(tmp_path / name).write_text(text, encoding="ascii")
	(tmp_path / "alone.c").write_text(r"""#include <stdio.h>
#include <string.h>
#include "t_drive.h"

static unsigned sends, last_expired = 99u;

static situ_err_t submit(void *ctx, const uint8_t *d, uint32_t n)
{ (void)ctx; (void)d; (void)n; sends++; return SITU_OK; }
static situ_err_t resend(void *ctx, uint32_t id, const uint8_t *d, uint32_t n)
{ (void)ctx; (void)id; (void)d; (void)n; sends++; return SITU_OK; }
static void expire(void *ctx, uint32_t id) { (void)ctx; last_expired = id; }

int main(void)
{
	uint8_t bufs[2][17];
	situ_msg_t msgs[2];
	situ_view_t views[2];
	situ_drive_response_to_slot_t slots[3];
	situ_conv_response_to_slot_t keys[3];
	situ_drive_response_to_t d;
	situ_io_t io = { submit, NULL };
	situ_drive_response_to_policy_t own = { 100u, 0u, 600u, 300u, 8u };
	uint32_t next = 0u, expired = 0u, id = 0u;

	for (unsigned i = 0; i < 2; i++) {
		memset(bufs[i], 0, 17);
		bufs[i][1] = (uint8_t)(i * 2u);	/* a blank one, and msg 2 */
		situ_msg_init(&msgs[i], bufs[i], 17);
		if (situ_frame_view(&msgs[i], 0u, &views[i]) != SITU_OK) return 9;
	}
	situ_drive_response_to_init(&d, slots, keys, 3u, io, 3000u, 3u, 600u, 300u, 8u);
	situ_drive_response_to_on_resend(&d, resend);
	situ_drive_response_to_on_expire(&d, expire);
	if (situ_drive_response_to_track_by_id_with(&d, bufs[0], 17u, 1u, 0u, &own) != SITU_OK) return 2;
	if (sends == 0u) printf("tracked, sent nothing\n");
	printf("peek blank %s\n", situ_drive_response_to_peek(&d, views[0], &id) == SITU_ERR_CONSTRAINT ? "constraint" : "matched");
	if (situ_drive_response_to_track(&d, views[1], bufs[1], 17u, 2u, 0u) != SITU_OK) return 3;
	if (situ_drive_response_to_peek(&d, views[1], &id) == SITU_OK) printf("peek %u\n", id);
	(void)situ_drive_response_to_step(&d, 150u, &next, &expired);
	printf("expired %u count %u\n", last_expired, expired);
	if (situ_drive_response_to_track_by_id(&d, bufs[0], 17u, 3u, 200u) != SITU_OK) return 4;
	printf("end %s\n", situ_drive_response_to_end(&d, 3u) == SITU_OK ? "ok" : "no");
	printf("end again %s\n", situ_drive_response_to_end(&d, 3u) == SITU_ERR_CONSTRAINT ? "constraint" : "no");
	return 0;
}
""", encoding="ascii")
	assert COMPILER is not None
	built = subprocess.run(
		[COMPILER, *WARNINGS, f"-I{tmp_path}", f"-I{RUNTIME}",
		 str(tmp_path / "alone.c"), str(tmp_path / "t.c"),
		 str(RUNTIME / "situ.c"), "-o", str(tmp_path / "alone")],
		capture_output=True, text=True)
	assert built.returncode == 0, built.stderr
	ran = subprocess.run([str(tmp_path / "alone")], capture_output=True,
	                     text=True, timeout=60)
	assert ran.stdout.splitlines() == WANT, ran.stdout


@pytest.mark.skipif(HOST_CXX is None, reason="no C++ compiler")
def test_cpp(tmp_path: Path) -> None:
	schema, resolved = analysed(SCHEMA)
	for name, text in generate_cpp(schema, resolved, "t").files().items():
		(tmp_path / name).write_text(text, encoding="ascii")
	for module in (relate_cpp, frame_cpp, converse_cpp, drive_cpp):
		for name, text in module.generate(schema, resolved, "t").items():
			(tmp_path / name).write_text(text, encoding="ascii")
	(tmp_path / "main.cpp").write_text(r"""#include <cstdint>
#include <cstdio>
#include <cstring>
#include "t_drive.hpp"

namespace {
class hooked : public ::situ::io {
public:
	unsigned sends = 0u, last_expired = 99u;
	::situ::rt::err submit(const std::uint8_t *, std::uint32_t) noexcept override
	{ sends++; return ::situ::rt::err::ok; }
	::situ::rt::err resend(std::uint32_t, const std::uint8_t *, std::uint32_t) noexcept override
	{ sends++; return ::situ::rt::err::ok; }
	void expired(std::uint32_t id) noexcept override { last_expired = id; }
};
}

int main()
{
	std::uint8_t bytes[2][::situ::frame::size_bytes];
	std::memset(bytes, 0, sizeof bytes);
	bytes[1][1] = 2;
	::situ::rt::message blank(bytes[0], ::situ::frame::size_bytes);
	::situ::rt::message two(bytes[1], ::situ::frame::size_bytes);
	::situ::frame a, b;
	if (::situ::frame::at(blank, 0, a) != ::situ::rt::err::ok) return 1;
	if (::situ::frame::at(two, 0, b) != ::situ::rt::err::ok) return 1;
	hooked sink;
	::situ::response_to_driver::slot slots[3];
	::situ::response_to_driver d(slots, 3u, sink, 3000u, 3u, 600u, 300u, 8u);
	const ::situ::response_to_driver::policy own{100u, 0u, 600u, 300u, 8u};
	std::uint32_t next = 0u, expired = 0u, id = 0u;
	if (d.track_by_id(bytes[0], ::situ::frame::size_bytes, 1u, 0u, &own) != ::situ::rt::err::ok) return 2;
	if (sink.sends == 0u) std::printf("tracked, sent nothing\n");
	std::printf("peek blank %s\n", d.peek(a, id) == ::situ::rt::err::constraint ? "constraint" : "matched");
	if (d.track(b, bytes[1], ::situ::frame::size_bytes, 2u, 0u) != ::situ::rt::err::ok) return 3;
	if (d.peek(b, id) == ::situ::rt::err::ok) std::printf("peek %u\n", id);
	(void)d.step(150u, next, expired);
	std::printf("expired %u count %u\n", sink.last_expired, expired);
	if (d.track_by_id(bytes[0], ::situ::frame::size_bytes, 3u, 200u) != ::situ::rt::err::ok) return 4;
	std::printf("end %s\n", d.end(3u) == ::situ::rt::err::ok ? "ok" : "no");
	std::printf("end again %s\n", d.end(3u) == ::situ::rt::err::constraint ? "constraint" : "no");
	return 0;
}
""", encoding="ascii")
	built = subprocess.run(
		[HOST_CXX or "c++", "-std=c++17", "-O1", "-Wall", "-Wextra", "-Werror",
		 f"-I{tmp_path}", f"-I{ROOT / 'runtime' / 'cpp'}", f"-I{RUNTIME}",
		 str(tmp_path / "main.cpp"), str(RUNTIME / "situ.c"),
		 "-o", str(tmp_path / "run")], capture_output=True, text=True)
	assert built.returncode == 0, built.stderr
	ran = subprocess.run([str(tmp_path / "run")], capture_output=True,
	                     text=True)
	assert ran.stdout.splitlines() == WANT, ran.stdout


@pytest.mark.skipif(RUSTC is None, reason="no rustc")
def test_rust(tmp_path: Path) -> None:
	schema, resolved = analysed(SCHEMA)
	src = tmp_path / "src"
	src.mkdir()
	(src / "situ_rt.rs").write_text(
		(ROOT / "runtime" / "rust" / "situ_rt.rs")
		.read_text(encoding="ascii").replace("#![no_std]\n", ""),
		encoding="ascii")
	(src / "t.rs").write_text(
		generate_rs(schema, resolved, "t").module, encoding="ascii")
	for module in (relate_rs, frame_rs, converse_rs, drive_rs):
		for name, text in module.generate(schema, resolved, "t").items():
			(src / name).write_text(text, encoding="ascii")
	(src / "main.rs").write_text(r"""pub mod situ_rt;
pub mod t;
pub mod t_converse;
pub mod t_drive;
pub mod t_frame;
pub mod t_relate;

use std::cell::Cell;
use std::rc::Rc;
use situ_rt::Result;

#[derive(Default)]
struct Seen { sends: Cell<u32>, last_expired: Cell<u32> }

struct Hooked { seen: Rc<Seen> }

impl t_drive::Io for Hooked {
	fn submit(&mut self, _data: &[u8]) -> Result<()> {
		self.seen.sends.set(self.seen.sends.get() + 1);
		Ok(())
	}
	fn resend(&mut self, _id: u32, _data: &[u8]) -> Result<()> {
		self.seen.sends.set(self.seen.sends.get() + 1);
		Ok(())
	}
	fn expired(&mut self, id: u32) {
		self.seen.last_expired.set(id);
	}
}

fn main() {
	let blank = [0u8; t::Frame::SIZE];
	let mut two = [0u8; t::Frame::SIZE];
	two[1] = 2;
	let a = t::Frame::new(&blank).expect("a view");
	let b = t::Frame::new(&two).expect("a view");
	let seen = Rc::new(Seen::default());
	let mut slots = [t_drive::ResponseToSlot::default(); 3];
	let mut d = t_drive::ResponseToDriver::new(
		&mut slots, Hooked { seen: seen.clone() }, 3000, 3, 600, 300, 8);
	let own = t_drive::ResponseToPolicy { timeout_ms: 100, retries: 0, resend_ms: 600,
	                                      resend_part_ms: 300, retries_part: 8 };
	d.track_by_id_with(&blank, 1, 0, Some(&own)).expect("a slot");
	if seen.sends.get() == 0 {
		println!("tracked, sent nothing");
	}
	println!("peek blank {}", if matches!(d.peek(&a), Err(situ_rt::Error::Constraint)) { "constraint" } else { "matched" });
	d.track(&b, &two, 2, 0).expect("a slot");
	if let Ok(id) = d.peek(&b) {
		println!("peek {}", id);
	}
	let (_, expired) = d.step(150);
	println!("expired {} count {}", seen.last_expired.get(), expired);
	d.track_by_id(&blank, 3, 200).expect("a slot");
	println!("end {}", if d.end(3).is_ok() { "ok" } else { "no" });
	println!("end again {}", if matches!(d.end(3), Err(situ_rt::Error::Constraint)) { "constraint" } else { "no" });
}
""", encoding="ascii")
	built = subprocess.run(
		[RUSTC or "rustc", "--edition", "2021", "-D", "warnings",
		 "-A", "dead_code", "-C", "overflow-checks=on",
		 str(src / "main.rs"), "-o", str(tmp_path / "run")],
		capture_output=True, text=True, cwd=tmp_path)
	assert built.returncode == 0, built.stderr
	ran = subprocess.run([str(tmp_path / "run")], capture_output=True,
	                     text=True)
	assert ran.stdout.splitlines() == WANT, ran.stdout + ran.stderr


def test_python(tmp_path: Path) -> None:
	schema, resolved = analysed(SCHEMA)
	for name, text in generate_py(schema, resolved, "t").files().items():
		(tmp_path / name).write_text(text, encoding="ascii")
	for module in (relate_py, converse_py, drive_py):
		for name, text in module.generate(schema, resolved, "t").items():
			(tmp_path / name).write_text(text, encoding="ascii")
	(tmp_path / "situ_runtime.py").write_text(
		(ROOT / "runtime" / "python" / "situ_runtime.py").read_text(
			encoding="ascii"), encoding="ascii")
	(tmp_path / "run.py").write_text("""import sys
sys.path.insert(0, ".")
import t
import t_drive
from situ_runtime import ConstraintError, Message

sends, expired_ids = [], []
blank = bytearray(t.frame.SIZE_BYTES)
two = bytearray(t.frame.SIZE_BYTES); two[1] = 2
a = t.frame(Message(blank), 0, len(blank))
b = t.frame(Message(two), 0, len(two))
d = t_drive.response_to_driver(sends.append, 3, 3000, 3, 600, 300, 8,
                               resend=lambda i, data: sends.append(data),
                               expired=expired_ids.append)
d.track_by_id(bytes(blank), 1, 0, timeout_ms=100, retries=0)
if not sends:
	print("tracked, sent nothing")
try:
	d.peek(a)
	print("peek blank matched")
except ConstraintError:
	print("peek blank constraint")
d.track(b, bytes(two), 2, 0)
print("peek", d.peek(b))
_, count = d.step(150)
print("expired", expired_ids[-1], "count", count)
d.track_by_id(bytes(blank), 3, 200)
d.end(3)
print("end ok")
try:
	d.end(3)
	print("end again no")
except ConstraintError:
	print("end again constraint")
""", encoding="ascii")
	ran = subprocess.run(["python3", str(tmp_path / "run.py")],
	                     capture_output=True, text=True, cwd=tmp_path)
	assert ran.stdout.splitlines() == WANT, ran.stdout + ran.stderr
