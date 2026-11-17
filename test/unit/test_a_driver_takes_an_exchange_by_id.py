"""What fuzznet's own schedule needs from the generated driver (26.1055).

fuzznet (inbox 20261117T1123Z, its sec 1209) would replace node/asks.c with
the driver if it could: be told WHICH exchanges expire, not how many; send
the first copy through the id-aware hook its resends already use, since it
seals every send under the exchange's id; give one exchange its own wait
and resends; and end an exchange by id, since it matches replies itself.
Plus `track`, for an exchange the caller sent already or only awaits.

One scenario, the same in all four:
  send 1 with a resend hook set   -> the hook sees id 1, `submit` nothing
  track 2 at 100 ms, no retries   -> nothing sent
  step at 150                     -> 2 expires, and the expiry hook says 2
  end 1                           -> ok; end 1 again -> constraint
  step at 200                     -> nothing outstanding
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
WANT = ["resend 1 submit 0", "tracked, sent nothing", "expired 2 count 1",
        "end ok", "end again constraint", "empty"]


@pytest.mark.skipif(COMPILER is None, reason="no C compiler")
def test_c(tmp_path: Path) -> None:
	schema, resolved = analysed(SCHEMA)
	for name, text in generate_c(schema, resolved, "t").files().items():
		(tmp_path / name).write_text(text, encoding="ascii")
	for emitted in (converse.generate(schema, resolved, "t"),
	                drive.generate(schema, resolved, "t")):
		for name, text in emitted.items():
			(tmp_path / name).write_text(text, encoding="ascii")
	(tmp_path / "byid.c").write_text(r"""#include <stdio.h>
#include <string.h>
#include "t_drive.h"

static unsigned submits, resends, last_resend = 99u, last_expired = 99u;

static situ_err_t submit(void *ctx, const uint8_t *d, uint32_t n)
{ (void)ctx; (void)d; (void)n; submits++; return SITU_OK; }
static situ_err_t resend(void *ctx, uint32_t id, const uint8_t *d, uint32_t n)
{ (void)ctx; (void)d; (void)n; resends++; last_resend = id; return SITU_OK; }
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
	uint32_t next = 0u, expired = 0u;

	for (unsigned i = 0; i < 2; i++) {
		memset(bufs[i], 0, 17);
		bufs[i][1] = (uint8_t)(i + 1);
		situ_msg_init(&msgs[i], bufs[i], 17);
		if (situ_frame_view(&msgs[i], 0u, &views[i]) != SITU_OK) return 9;
	}
	situ_drive_response_to_init(&d, slots, keys, 3u, io, 3000u, 3u, 600u, 300u, 8u);
	situ_drive_response_to_on_resend(&d, resend);
	situ_drive_response_to_on_expire(&d, expire);
	if (situ_drive_response_to_send(&d, views[0], bufs[0], 17u, 1u, 0u) != SITU_OK) return 2;
	printf("resend %u submit %u\n", last_resend, submits);
	if (situ_drive_response_to_track_with(&d, views[1], bufs[1], 17u, 2u, 0u, &own) != SITU_OK) return 3;
	if (resends == 1u && submits == 0u) printf("tracked, sent nothing\n");
	(void)situ_drive_response_to_step(&d, 150u, &next, &expired);
	printf("expired %u count %u\n", last_expired, expired);
	printf("end %s\n", situ_drive_response_to_end(&d, 1u) == SITU_OK ? "ok" : "no");
	printf("end again %s\n", situ_drive_response_to_end(&d, 1u) == SITU_ERR_CONSTRAINT ? "constraint" : "no");
	if (situ_drive_response_to_step(&d, 200u, &next, &expired) == SITU_ERR_TRUNCATED) printf("empty\n");
	return 0;
}
""", encoding="ascii")
	assert COMPILER is not None
	built = subprocess.run(
		[COMPILER, *WARNINGS, f"-I{tmp_path}", f"-I{RUNTIME}",
		 str(tmp_path / "byid.c"), str(tmp_path / "t.c"),
		 str(RUNTIME / "situ.c"), "-o", str(tmp_path / "byid")],
		capture_output=True, text=True)
	assert built.returncode == 0, built.stderr
	ran = subprocess.run([str(tmp_path / "byid")], capture_output=True,
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
	unsigned submits = 0u, resends = 0u, last_resend = 99u, last_expired = 99u;
	::situ::rt::err submit(const std::uint8_t *, std::uint32_t) noexcept override
	{ submits++; return ::situ::rt::err::ok; }
	::situ::rt::err resend(std::uint32_t id, const std::uint8_t *, std::uint32_t) noexcept override
	{ resends++; last_resend = id; return ::situ::rt::err::ok; }
	void expired(std::uint32_t id) noexcept override { last_expired = id; }
};
}

int main()
{
	std::uint8_t bytes[2][::situ::frame::size_bytes];
	std::memset(bytes, 0, sizeof bytes);
	bytes[0][1] = 1; bytes[1][1] = 2;
	::situ::rt::message one(bytes[0], ::situ::frame::size_bytes);
	::situ::rt::message two(bytes[1], ::situ::frame::size_bytes);
	::situ::frame a, b;
	if (::situ::frame::at(one, 0, a) != ::situ::rt::err::ok) return 1;
	if (::situ::frame::at(two, 0, b) != ::situ::rt::err::ok) return 1;
	hooked sink;
	::situ::response_to_driver::slot slots[3];
	::situ::response_to_driver d(slots, 3u, sink, 3000u, 3u, 600u, 300u, 8u);
	const ::situ::response_to_driver::policy own{100u, 0u, 600u, 300u, 8u};
	std::uint32_t next = 0u, expired = 0u;
	if (d.send(a, bytes[0], ::situ::frame::size_bytes, 1u, 0u) != ::situ::rt::err::ok) return 2;
	std::printf("resend %u submit %u\n", sink.last_resend, sink.submits);
	if (d.track(b, bytes[1], ::situ::frame::size_bytes, 2u, 0u, &own) != ::situ::rt::err::ok) return 3;
	if (sink.resends == 1u && sink.submits == 0u) std::printf("tracked, sent nothing\n");
	(void)d.step(150u, next, expired);
	std::printf("expired %u count %u\n", sink.last_expired, expired);
	std::printf("end %s\n", d.end(1u) == ::situ::rt::err::ok ? "ok" : "no");
	std::printf("end again %s\n", d.end(1u) == ::situ::rt::err::constraint ? "constraint" : "no");
	if (d.step(200u, next, expired) == ::situ::rt::err::truncated) std::printf("empty\n");
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
struct Seen { submits: Cell<u32>, resends: Cell<u32>, last_resend: Cell<u32>, last_expired: Cell<u32> }

struct Hooked { seen: Rc<Seen> }

impl t_drive::Io for Hooked {
	fn submit(&mut self, _data: &[u8]) -> Result<()> {
		self.seen.submits.set(self.seen.submits.get() + 1);
		Ok(())
	}
	fn resend(&mut self, id: u32, _data: &[u8]) -> Result<()> {
		self.seen.resends.set(self.seen.resends.get() + 1);
		self.seen.last_resend.set(id);
		Ok(())
	}
	fn expired(&mut self, id: u32) {
		self.seen.last_expired.set(id);
	}
}

fn main() {
	let mut one = [0u8; t::Frame::SIZE];
	let mut two = [0u8; t::Frame::SIZE];
	one[1] = 1;
	two[1] = 2;
	let a = t::Frame::new(&one).expect("a view");
	let b = t::Frame::new(&two).expect("a view");
	let seen = Rc::new(Seen::default());
	let mut slots = [t_drive::ResponseToSlot::default(); 3];
	let mut d = t_drive::ResponseToDriver::new(
		&mut slots, Hooked { seen: seen.clone() }, 3000, 3, 600, 300, 8);
	let own = t_drive::ResponseToPolicy { timeout_ms: 100, retries: 0, resend_ms: 600,
	                                      resend_part_ms: 300, retries_part: 8 };
	d.send(&a, &one, 1, 0).expect("a slot");
	println!("resend {} submit {}", seen.last_resend.get(), seen.submits.get());
	d.track_with(&b, &two, 2, 0, Some(&own)).expect("a slot");
	if seen.resends.get() == 1 && seen.submits.get() == 0 {
		println!("tracked, sent nothing");
	}
	let (_, expired) = d.step(150);
	println!("expired {} count {}", seen.last_expired.get(), expired);
	println!("end {}", if d.end(1).is_ok() { "ok" } else { "no" });
	println!("end again {}", if matches!(d.end(1), Err(situ_rt::Error::Constraint)) { "constraint" } else { "no" });
	if d.step(200).0.is_none() {
		println!("empty");
	}
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

submits, resends, expired_ids = [], [], []
one = bytearray(t.frame.SIZE_BYTES); one[1] = 1
two = bytearray(t.frame.SIZE_BYTES); two[1] = 2
a = t.frame(Message(one), 0, len(one))
b = t.frame(Message(two), 0, len(two))
d = t_drive.response_to_driver(submits.append, 3, 3000, 3, 600, 300, 8,
                               resend=lambda i, data: resends.append(i),
                               expired=expired_ids.append)
d.send(a, bytes(one), 1, 0)
print("resend", resends[-1], "submit", len(submits))
d.track(b, bytes(two), 2, 0, timeout_ms=100, retries=0)
if len(resends) == 1 and not submits:
	print("tracked, sent nothing")
_, count = d.step(150)
print("expired", expired_ids[-1], "count", count)
d.end(1)
print("end ok")
try:
	d.end(1)
	print("end again no")
except ConstraintError:
	print("end again constraint")
if d.step(200)[0] is None:
	print("empty")
""", encoding="ascii")
	ran = subprocess.run(["python3", str(tmp_path / "run.py")],
	                     capture_output=True, text=True, cwd=tmp_path)
	assert ran.stdout.splitlines() == WANT, ran.stdout + ran.stderr
