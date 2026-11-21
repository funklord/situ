"""A relayed exchange is awaited under two keys (26.1126).

fuzznet's relay_carry seals a request for the target under the target's
session and wraps it for the relay under the relay's, and either the
target's answer or the relay's refusal ends the one exchange. The drive
machine held one (peer, key) per exchange, so fuzznet kept the second
beside its caller by hand. `_track_also_from` and `_track_also_key_from`
add another way a live exchange may be answered: whichever key a reply
matches ends every key held for it, as expiry and `_end` do, and the
exchange keeps its one deadline. fuzznet's review (inbox 20261121T1300Z):
one relation, the original deadline, never more than two keys.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT  = Path(__file__).resolve().parents[2]
RUNTIME = ROOT / "runtime"
CC    = shutil.which("cc") or shutil.which("gcc")

SCHEMA = r"""target buffer;
endian big;

struct frame {
	u32 msg;
	u32 to;
	u8  body[4];
}

relation reply_to(request: frame, reply: frame) [timeout_ms = 100, retries = 1] {
	must reply.msg == request.msg;
}
"""


def _build(tmp_path: Path, target: str) -> Path:
	(tmp_path / "x.situ").write_text(SCHEMA, encoding="ascii")
	out = tmp_path / target
	done = subprocess.run(
		[sys.executable, "-m", "situc", "build", str(tmp_path / "x.situ"),
		 "--target", target, "--layer", "drive", "--out", str(out)],
		cwd=ROOT, capture_output=True, text=True)
	assert done.returncode == 0, done.stderr
	return out


C_PROGRAM = r"""
#include <stdio.h>
#include <string.h>
#include "x_drive.h"

#define CHECK(c) do { if (!(c)) { printf("FAIL %d: %s\n", __LINE__, #c); fail = 1; } } while (0)

static uint8_t bytes[4][12];

static situ_view_t frame(int at, uint32_t msg)
{
	static situ_msg_t m[4];
	situ_view_t v;
	memset(bytes[at], 0, 12u);
	for (unsigned b = 0; b < 4u; b++)
		bytes[at][b] = (uint8_t)(msg >> (24u - 8u * b));
	situ_msg_init(&m[at], bytes[at], 12u);
	(void)situ_frame_view(&m[at], 0u, &v);
	return v;
}

static situ_err_t submit(void *ctx, const uint8_t *data, uint32_t len)
{
	(void)ctx; (void)data; (void)len;
	return SITU_OK;
}

int main(void)
{
	int fail = 0;
	situ_drive_reply_to_t drive;
	situ_drive_reply_to_slot_t slots[2];
	situ_conv_reply_to_slot_t keys[2], more[4];
	situ_io_t io = {submit, NULL};
	uint32_t id = 0u, next = 0u, expired = 0u;

	situ_drive_reply_to_init(&drive, slots, keys, 2u, io, 100u, 1u);
	situ_conv_reply_to_init(&drive.pending, more, 4u);

	/* The target's msg 5 under peer 1, the relay's 9 under peer 2. */
	CHECK(situ_drive_reply_to_track_from(&drive, frame(0, 5u), 1u, bytes[0], 12u, 7u, 0u, NULL) == SITU_OK);
	CHECK(situ_drive_reply_to_track_also_key_from(&drive, situ_conv_reply_to_key(9u), 2u, 7u) == SITU_OK);
	/* Never starts one. */
	CHECK(situ_drive_reply_to_track_also_key_from(&drive, situ_conv_reply_to_key(9u), 2u, 8u) == SITU_ERR_CONSTRAINT);
	/* The relay's refusal ends it; the target's late answer is unmatched. */
	CHECK(situ_drive_reply_to_on_message_from(&drive, frame(1, 9u), 2u, &id) == SITU_OK && id == 7u);
	CHECK(situ_drive_reply_to_on_message_from(&drive, frame(1, 5u), 1u, &id) == SITU_ERR_CONSTRAINT);

	/* The other way round, through the view form. */
	CHECK(situ_drive_reply_to_track_from(&drive, frame(0, 6u), 1u, bytes[0], 12u, 3u, 0u, NULL) == SITU_OK);
	CHECK(situ_drive_reply_to_track_also_from(&drive, frame(2, 10u), 2u, 3u) == SITU_OK);
	CHECK(situ_drive_reply_to_on_message_from(&drive, frame(1, 6u), 1u, &id) == SITU_OK && id == 3u);
	CHECK(situ_drive_reply_to_on_message_from(&drive, frame(1, 10u), 2u, &id) == SITU_ERR_CONSTRAINT);
	/* The peer is held beside the second key too. */
	CHECK(situ_drive_reply_to_track_from(&drive, frame(0, 11u), 1u, bytes[0], 12u, 4u, 0u, NULL) == SITU_OK);
	CHECK(situ_drive_reply_to_track_also_key_from(&drive, situ_conv_reply_to_key(12u), 2u, 4u) == SITU_OK);
	CHECK(situ_drive_reply_to_on_message_from(&drive, frame(1, 12u), 1u, &id) == SITU_ERR_CONSTRAINT);

	/* Expiry ends both keys, under the one deadline: one resend, then. */
	CHECK(situ_drive_reply_to_step(&drive, 1000u, &next, &expired) == SITU_OK && expired == 0u);
	(void)situ_drive_reply_to_step(&drive, 2000u, &next, &expired);	/* says it expired */
	CHECK(expired == 1u);
	CHECK(situ_drive_reply_to_on_message_from(&drive, frame(1, 11u), 1u, &id) == SITU_ERR_CONSTRAINT);
	CHECK(situ_drive_reply_to_on_message_from(&drive, frame(1, 12u), 2u, &id) == SITU_ERR_CONSTRAINT);

	/* `_end` too. */
	CHECK(situ_drive_reply_to_track_from(&drive, frame(0, 13u), 1u, bytes[0], 12u, 5u, 0u, NULL) == SITU_OK);
	CHECK(situ_drive_reply_to_track_also_key_from(&drive, situ_conv_reply_to_key(14u), 2u, 5u) == SITU_OK);
	CHECK(situ_drive_reply_to_end(&drive, 5u) == SITU_OK);
	CHECK(situ_drive_reply_to_on_message_from(&drive, frame(1, 14u), 2u, &id) == SITU_ERR_CONSTRAINT);

	printf(fail ? "failed\n" : "c ok\n");
	return fail;
}
"""


@pytest.mark.skipif(CC is None, reason="no C compiler")
def test_c_ends_both_keys_together(tmp_path: Path) -> None:
	out = _build(tmp_path, "c")
	(out / "t.c").write_text(C_PROGRAM, encoding="ascii")
	built = subprocess.run(
		[CC or "cc", "-std=c11", "-Wall", "-Wextra", "-Werror",
		 "-fsanitize=address,undefined", f"-I{RUNTIME / 'c'}", f"-I{out}",
		 str(out / "t.c"), str(out / "x.c"), str(RUNTIME / "c" / "situ.c"),
		 "-o", str(tmp_path / "t")], capture_output=True, text=True)
	assert built.returncode == 0, built.stderr
	ran = subprocess.run([str(tmp_path / "t")], capture_output=True,
	                     text=True, timeout=60)
	assert ran.stdout.strip() == "c ok", ran.stdout + ran.stderr


PY_PROGRAM = r"""
import sys
sys.path[:0] = [".", RUNTIME]
from situ_runtime import Message, ConstraintError, BoundsError
import x, x_drive

def frame(msg):
	b = bytearray(12)
	b[0:4] = msg.to_bytes(4, "big")
	return x.frame.at(Message(b), 0)

def refused(call, *args, **kw):
	try:
		call(*args, **kw)
	except ConstraintError:
		return True
	return False

sent = []
d = x_drive.reply_to_driver(sent.append, 2, 100, 1)
K = x_drive.reply_to_driver.key
d.track(frame(5), b"req", 7, 0, peer=1)
d.track_also_key(K(9), 7, peer=2)
assert refused(d.track_also_key, K(9), 8, peer=2)
assert d.peek(frame(9), peer=2) == 7
assert d.on_message(frame(9), peer=2) == 7
assert refused(d.on_message, frame(5), peer=1)

d.track(frame(6), b"req", 3, 0, peer=1)
d.track_also(frame(10), 3, peer=2)
assert d.on_message(frame(6), peer=1) == 3
assert refused(d.on_message, frame(10), peer=2)

d.track(frame(11), b"req", 4, 0, peer=1)
d.track_also_key(K(12), 4, peer=2)
assert refused(d.on_message, frame(12), peer=1)
assert d.step(1000) == (1100, 0) and len(sent) == 1
assert d.step(2000)[1] == 1 and len(sent) == 1
assert refused(d.on_message, frame(12), peer=2)

d.track(frame(13), b"req", 5, 0, peer=1)
d.track_also_key(K(14), 5, peer=2)
d.end(5)
assert refused(d.on_message, frame(14), peer=2)
d.track(frame(15), b"req", 6, 0, peer=1)
d.track_also_key(K(16), 6, peer=2)
d.track_also_key(K(17), 6, peer=3)
try:
	d.track_also_key(K(18), 6, peer=4)
	raise SystemExit("not bounded")
except BoundsError:
	pass
print("python ok")
"""


def test_python_ends_both_keys_together(tmp_path: Path) -> None:
	out = _build(tmp_path, "python")
	ran = subprocess.run(
		[sys.executable, "-c", PY_PROGRAM.replace(
			"RUNTIME", repr(str(RUNTIME / "python")))],
		cwd=out, capture_output=True, text=True, timeout=60)
	assert ran.stdout.strip() == "python ok", ran.stderr


RUSTC = shutil.which("rustc")

RUST_PROGRAM = r"""
#![deny(warnings)]
#![allow(unused_attributes)]
#[allow(dead_code)] mod situ_rt;
#[allow(dead_code)] mod x;
#[allow(dead_code)] mod x_drive;
use situ_rt::Error;
use x_drive::{Io, ReplyToDriver, ReplyToSlot};

struct Count(u32);
impl Io for Count {
	fn submit(&mut self, _data: &[u8]) -> situ_rt::Result<()> {
		self.0 += 1;
		Ok(())
	}
}

fn frame(msg: u32) -> [u8; 12] {
	let mut b = [0u8; 12];
	b[0..4].copy_from_slice(&msg.to_be_bytes());
	b
}

fn v(b: &[u8; 12]) -> x::Frame<'_> {
	x::Frame::new(b).unwrap()
}

fn main() {
	let req = frame(0);
	let mut slots = [ReplyToSlot::default(); 4];
	let mut d = ReplyToDriver::new(&mut slots, Count(0), 100, 1);
	let k = ReplyToDriver::<Count>::key;
	let (f5, f6, f9, f10) = (frame(5), frame(6), frame(9), frame(10));

	d.track_from(&v(&f5), 1, &req, 7, 0, None).unwrap();
	d.track_also_key_from(k(9), 2, 7).unwrap();
	assert!(matches!(d.track_also_key_from(k(9), 2, 8), Err(Error::Constraint)));
	assert_eq!(d.peek_from(&v(&f9), 2).unwrap(), 7);
	assert_eq!(d.on_message_from(&v(&f9), 2).unwrap(), 7);
	assert!(d.on_message_from(&v(&f5), 1).is_err());

	d.track_from(&v(&f6), 1, &req, 3, 0, None).unwrap();
	d.track_also_from(&v(&f10), 2, 3).unwrap();
	assert_eq!(d.on_message_from(&v(&f6), 1).unwrap(), 3);
	assert!(d.on_message_from(&v(&f10), 2).is_err());

	let (f11, f12) = (frame(11), frame(12));
	d.track_from(&v(&f11), 1, &req, 4, 0, None).unwrap();
	d.track_also_key_from(k(12), 2, 4).unwrap();
	assert!(d.on_message_from(&v(&f12), 1).is_err());
	assert_eq!(d.step(1000), (Some(1100), 0));
	assert_eq!(d.step(2000).1, 1);
	assert!(d.on_message_from(&v(&f12), 2).is_err());

	let (f13, f14) = (frame(13), frame(14));
	d.track_from(&v(&f13), 1, &req, 5, 0, None).unwrap();
	d.track_also_key_from(k(14), 2, 5).unwrap();
	d.end(5).unwrap();
	assert!(d.on_message_from(&v(&f14), 2).is_err());
	assert!(matches!(d.end(5), Err(Error::Constraint)));
	println!("rust ok");
}
"""


@pytest.mark.skipif(RUSTC is None, reason="no rustc")
def test_rust_ends_both_keys_together(tmp_path: Path) -> None:
	out = _build(tmp_path, "rust")
	(out / "situ_rt.rs").write_text(
		(RUNTIME / "rust" / "situ_rt.rs").read_text(encoding="ascii"),
		encoding="ascii")
	(out / "main.rs").write_text(RUST_PROGRAM, encoding="ascii")
	built = subprocess.run(
		[RUSTC or "rustc", "--edition", "2021", "-o", str(tmp_path / "m"),
		 str(out / "main.rs")], capture_output=True, text=True)
	assert built.returncode == 0, built.stderr
	ran = subprocess.run([str(tmp_path / "m")], capture_output=True,
	                     text=True, timeout=60)
	assert ran.stdout.strip() == "rust ok", ran.stderr


CXX = shutil.which("g++") or shutil.which("c++")

CPP_PROGRAM = r"""
#include <cstdio>
#include <cstring>
#include "x_drive.hpp"

#define CHECK(c) do { if (!(c)) { std::printf("FAIL %d: %s\n", __LINE__, #c); fail = 1; } } while (0)

struct counter : ::situ::io {
	unsigned sent = 0;
	::situ::rt::err submit(const std::uint8_t *, std::uint32_t) noexcept override
	{
		sent++;
		return ::situ::rt::err::ok;
	}
};

static std::uint8_t bytes[4][12];

static ::situ::frame frame(int at, std::uint32_t msg)
{
	static ::situ::rt::message m[4] = {
		{bytes[0], 12u}, {bytes[1], 12u}, {bytes[2], 12u}, {bytes[3], 12u}};
	std::memset(bytes[at], 0, 12u);
	for (unsigned b = 0; b < 4u; b++)
		bytes[at][b] = static_cast<std::uint8_t>(msg >> (24u - 8u * b));
	::situ::frame v;
	(void)::situ::frame::at(m[at], 0u, v);
	return v;
}

int main()
{
	int fail = 0;
	counter io;
	::situ::reply_to_driver::slot slots[4];
	::situ::reply_to_driver d(slots, 4u, io, 100u, 1u);
	using K = ::situ::reply_to_driver;
	std::uint32_t id = 0u, next = 0u, expired = 0u;

	CHECK(d.track_from(frame(0, 5u), 1u, bytes[0], 12u, 7u, 0u) == ::situ::rt::err::ok);
	CHECK(d.track_also_key_from(K::key(9u), 2u, 7u) == ::situ::rt::err::ok);
	CHECK(d.track_also_key_from(K::key(9u), 2u, 8u) == ::situ::rt::err::constraint);
	CHECK(d.on_message_from(frame(1, 9u), 2u, id) == ::situ::rt::err::ok && id == 7u);
	CHECK(d.on_message_from(frame(1, 5u), 1u, id) == ::situ::rt::err::constraint);

	CHECK(d.track_from(frame(0, 6u), 1u, bytes[0], 12u, 3u, 0u) == ::situ::rt::err::ok);
	CHECK(d.track_also_from(frame(2, 10u), 2u, 3u) == ::situ::rt::err::ok);
	CHECK(d.on_message_from(frame(1, 6u), 1u, id) == ::situ::rt::err::ok && id == 3u);
	CHECK(d.on_message_from(frame(1, 10u), 2u, id) == ::situ::rt::err::constraint);

	CHECK(d.track_from(frame(0, 11u), 1u, bytes[0], 12u, 4u, 0u) == ::situ::rt::err::ok);
	CHECK(d.track_also_key_from(K::key(12u), 2u, 4u) == ::situ::rt::err::ok);
	CHECK(d.on_message_from(frame(1, 12u), 1u, id) == ::situ::rt::err::constraint);
	CHECK(d.step(1000u, next, expired) == ::situ::rt::err::ok && expired == 0u && io.sent == 1u);
	(void)d.step(2000u, next, expired);
	CHECK(expired == 1u && io.sent == 1u);
	CHECK(d.on_message_from(frame(1, 12u), 2u, id) == ::situ::rt::err::constraint);

	CHECK(d.track_from(frame(0, 13u), 1u, bytes[0], 12u, 5u, 0u) == ::situ::rt::err::ok);
	CHECK(d.track_also_key_from(K::key(14u), 2u, 5u) == ::situ::rt::err::ok);
	CHECK(d.end(5u) == ::situ::rt::err::ok);
	CHECK(d.on_message_from(frame(1, 14u), 2u, id) == ::situ::rt::err::constraint);
	CHECK(d.end(5u) == ::situ::rt::err::constraint);

	std::printf(fail ? "failed\n" : "cpp ok\n");
	return fail;
}
"""


@pytest.mark.skipif(CXX is None, reason="no C++ compiler")
def test_cpp_ends_both_keys_together(tmp_path: Path) -> None:
	out = _build(tmp_path, "cpp")
	(out / "t.cpp").write_text(CPP_PROGRAM, encoding="ascii")
	built = subprocess.run(
		[CXX or "c++", "-std=c++17", "-Wall", "-Wextra", "-Werror",
		 "-fsanitize=address,undefined", f"-I{RUNTIME / 'c'}",
		 f"-I{RUNTIME / 'cpp'}", f"-I{out}", str(out / "t.cpp"),
		 str(RUNTIME / "c" / "situ.c"), "-o", str(tmp_path / "t")],
		capture_output=True, text=True)
	assert built.returncode == 0, built.stderr
	ran = subprocess.run([str(tmp_path / "t")], capture_output=True,
	                     text=True, timeout=60)
	assert ran.stdout.strip() == "cpp ok", ran.stdout + ran.stderr
