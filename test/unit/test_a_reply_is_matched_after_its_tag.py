"""A reply is matched only after its tag verifies (26.1122).

fuzznet (inbox 20261120T1231Z, item 2): a reply's key read from the clear
head lets a forged head end an exchange its owner still waits on. With
`[after = REGION]` the table's match takes that sealed region's gate,
which only a verified open hands out, and the key may name fields behind
it. Its item 5, one driver over many sessions: a `peer` the caller
numbers, held beside the key, so two sessions' equal message numbers never
meet. fuzznet's review (inbox 20261120T2130Z) settled the peer as the
verifying context rather than the network peer.

Each backend is held to the same three answers -- a reply whose
gated field differs is not it, another peer's equal key is not it, the
right peer's is -- and the C drivers to a fourth: a reply whose `verify`
hook refuses never reaches the match.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from situc.diagnostics import SituError
from situc.parser import parse_text
from situc.wellformed import check

ROOT = Path(__file__).resolve().parents[2]
RUNTIME = ROOT / "runtime"
CC   = shutil.which("cc") or shutil.which("gcc")
CXX  = shutil.which("g++") or shutil.which("c++")
RUSTC = shutil.which("rustc")

SCHEMA = r"""target buffer;
endian big;

codec aead {
	tag_bytes   = 16;
	nonce_bytes = 12;
	granularity = byte;
	length_preserving;
	seekable;
	authenticated;
	invertible;
	deterministic;
}

impl aead extern "test_aead";

struct frame {
	authenticated {
		u32 msg;
		u32 to;
		u8  nonce[12];
	}
	sealed(aead, nonce = nonce) {
		u32 from;
		u8  body[4];
	}
	tag u8[16];
}

require verify_gated(frame.sealed);

relation reply_to(request: frame, reply: frame)
		[after = sealed, timeout_ms = 100, retries = 1] {
	must reply.msg == request.msg;
	must reply.sealed.from == request.to;
}
"""


def _build(tmp_path: Path, target: str, *extra: str) -> Path:
	schema = tmp_path / "g.situ"
	schema.write_text(SCHEMA, encoding="ascii")
	out = tmp_path / (target + "".join(extra).replace("-", ""))
	done = subprocess.run(
		[sys.executable, "-m", "situc", "build", str(schema), "--target",
		 target, "--layer", "drive", *extra, "--out", str(out)],
		cwd=ROOT, capture_output=True, text=True)
	assert done.returncode == 0, done.stderr
	return out


def _refused(source: str) -> str:
	with pytest.raises(SituError) as caught:
		check(parse_text(source))
	return str(caught.value)


def test_after_names_a_sealed_region_of_the_reply() -> None:
	assert "no sealed region `nonce`" in _refused(
		SCHEMA.replace("[after = sealed,", "[after = nonce,"))
	assert "no sealed region `elsewhere`" in _refused(
		SCHEMA.replace("[after = sealed,", "[after = elsewhere,"))


def test_after_is_a_relations_attribute() -> None:
	assert "after" in _refused(SCHEMA.replace(
		"u32 msg;", "u32 msg [after = sealed];"))


def test_the_predicate_says_why_it_cannot_read_behind_the_gate(
		tmp_path: Path) -> None:
	schema = tmp_path / "g.situ"
	schema.write_text(SCHEMA, encoding="ascii")
	done = subprocess.run(
		[sys.executable, "-m", "situc", "build", str(schema), "--target",
		 "c", "--layer", "drive", "--out", str(tmp_path / "c")],
		cwd=ROOT, capture_output=True, text=True)
	assert done.returncode == 0, done.stderr
	assert "behind `sealed`'s gate" in done.stderr, done.stderr
	assert "conversation table" in done.stderr


C_PROGRAM = r"""#define _GNU_SOURCE
#include <sys/socket.h>
#include <sys/time.h>
#include <pthread.h>
#include <signal.h>
#include <stdio.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#include "situ.h"
#include "g.h"
#include "g_drive.h"
#include "g_poll.h"

#define CHECK(c) do { if (!(c)) { printf("FAIL %d: %s\n", __LINE__, #c); fail = 1; } } while (0)

static void frame(uint8_t *out, uint32_t msg, uint32_t to, uint32_t from)
{
	memset(out, 0, 44u);
	for (unsigned b = 0; b < 4u; b++) {
		out[b]      = (uint8_t)(msg  >> (24u - 8u * b));
		out[4u + b] = (uint8_t)(to   >> (24u - 8u * b));
		out[20u + b] = (uint8_t)(from >> (24u - 8u * b));
	}
}

static situ_frame_sealed_t opened(uint8_t *bytes, situ_msg_t *m)
{
	situ_view_t view;
	situ_frame_sealed_t gate;
	situ_msg_init(m, bytes, 44u);
	(void)situ_frame_view(m, 0u, &view);
	(void)situ_frame_sealed_open(view, 1, &gate);
	return gate;
}

typedef struct { uint32_t replies, unmatched, id; situ_err_t why; int verifies; } seen_t;

static void on_reply(uint32_t id, situ_view_t reply, void *user)
{
	(void)reply;
	((seen_t *)user)->replies++;
	((seen_t *)user)->id = id;
}
static void on_unmatched(situ_view_t reply, situ_err_t why, void *user)
{
	(void)reply;
	((seen_t *)user)->unmatched++;
	((seen_t *)user)->why = why;
}
static situ_err_t verify(situ_view_t reply, void *user)
{
	(void)reply;
	return ((seen_t *)user)->verifies ? SITU_OK : SITU_ERR_TAG;
}

typedef struct { int fd; uint32_t from; volatile sig_atomic_t stop; } peer_t;

static void *peer_main(void *arg)
{
	peer_t *p = arg;
	uint8_t buf[64];
	while (p->stop == 0) {
		const ssize_t got = recv(p->fd, buf, sizeof buf, 0);
		if (got != 44) {
			continue;
		}
		uint8_t reply[44];
		uint32_t msg = (uint32_t)buf[0] << 24 | (uint32_t)buf[1] << 16
		             | (uint32_t)buf[2] << 8 | buf[3];
		frame(reply, msg, 0u, p->from);
		(void)send(p->fd, reply, sizeof reply, 0);
	}
	return NULL;
}

static uint32_t now_ms(void)
{
	struct timespec ts;
	(void)clock_gettime(CLOCK_MONOTONIC, &ts);
	return (uint32_t)((uint64_t)ts.tv_sec * 1000u + (uint64_t)ts.tv_nsec / 1000000u);
}

/* One exchange through the poll driver; the peer answers as `from`. */
static int over_poll(uint32_t from, int verifies, seen_t *seen)
{
	int sp[2];
	if (socketpair(AF_UNIX, SOCK_DGRAM, 0, sp) != 0) {
		return 1;
	}
	struct timeval tv = { 0, 20 * 1000 };
	(void)setsockopt(sp[1], SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof tv);
	peer_t peer = { sp[1], from, 0 };
	pthread_t th;
	if (pthread_create(&th, NULL, peer_main, &peer) != 0) {
		return 1;
	}
	situ_poll_ctx_t ctx;
	ctx.fd = sp[0];
	situ_drive_reply_to_slot_t slots[2];
	situ_conv_reply_to_slot_t keys[2];
	situ_drive_reply_to_t drive;
	situ_drive_reply_to_init(&drive, slots, keys, 2u, situ_poll_io(&ctx), 60u, 1u);
	static uint8_t req[44];
	frame(req, 7u, 9u, 0u);
	situ_msg_t m;
	situ_view_t view;
	situ_msg_init(&m, req, 44u);
	(void)situ_frame_view(&m, 0u, &view);
	memset(seen, 0, sizeof *seen);
	seen->verifies = verifies;
	situ_drive_hooks_t hooks;
	memset(&hooks, 0, sizeof hooks);
	hooks.on_reply = on_reply;
	hooks.on_unmatched = on_unmatched;
	hooks.verify = verify;
	hooks.user = seen;
	int rc = 0;
	if (situ_drive_reply_to_send(&drive, view, req, 44u, 5u, now_ms()) != SITU_OK) {
		rc = 1;
	} else {
		(void)situ_poll_reply_to_run(sp[0], &drive, &hooks);
	}
	peer.stop = 1;
	(void)pthread_join(th, NULL);
	(void)close(sp[0]);
	(void)close(sp[1]);
	return rc;
}

int main(void)
{
	int fail = 0;
	situ_conv_reply_to_slot_t keys[4];
	situ_conv_reply_to_t table;
	uint8_t req[44], rep[44];
	situ_msg_t m, rm;
	situ_view_t view;
	uint32_t id = 0;

	situ_conv_reply_to_init(&table, keys, 4u);
	frame(req, 7u, 9u, 0u);
	situ_msg_init(&m, req, 44u);
	(void)situ_frame_view(&m, 0u, &view);
	CHECK(situ_conv_reply_to_record_from(&table, view, 3u, 11u) == SITU_OK);

	/* The key reads `from` behind the gate: a reply from 8 is not it. */
	frame(rep, 7u, 0u, 8u);
	CHECK(situ_conv_reply_to_match_from(&table, opened(rep, &rm), 3u, &id) == SITU_ERR_CONSTRAINT);
	/* Another peer's equal key is not it either. */
	frame(rep, 7u, 0u, 9u);
	CHECK(situ_conv_reply_to_peek_from(&table, opened(rep, &rm), 4u, &id) == SITU_ERR_CONSTRAINT);
	CHECK(situ_conv_reply_to_match(&table, opened(rep, &rm), &id) == SITU_ERR_CONSTRAINT);
	CHECK(situ_conv_reply_to_peek_from(&table, opened(rep, &rm), 3u, &id) == SITU_OK && id == 11u);
	CHECK(situ_conv_reply_to_match_from(&table, opened(rep, &rm), 3u, &id) == SITU_OK && id == 11u);
	CHECK(situ_conv_reply_to_match_from(&table, opened(rep, &rm), 3u, &id) == SITU_ERR_CONSTRAINT);

	/* The plain calls are peer 0. */
	CHECK(situ_conv_reply_to_record(&table, view, 12u) == SITU_OK);
	CHECK(situ_conv_reply_to_match(&table, opened(rep, &rm), &id) == SITU_OK && id == 12u);

	/* An exchange this side only awaits, holding no request: tracked by
	 * the key's values, and a verified reply from the same peer ends it
	 * (fuzznet, inbox 20261121T0600Z). */
	{
		situ_poll_ctx_t kctx;
		kctx.fd = -1;
		situ_drive_reply_to_slot_t kslots[2];
		situ_conv_reply_to_slot_t kkeys[2];
		situ_drive_reply_to_t kd;
		situ_drive_reply_to_init(&kd, kslots, kkeys, 2u, situ_poll_io(&kctx),
		                         60u, 0u);
		CHECK(situ_drive_reply_to_track_key_from(&kd,
		          situ_conv_reply_to_key(7u, 9u), 6u, NULL, 0u, 21u, 0u,
		          NULL) == SITU_OK);
		CHECK(situ_drive_reply_to_on_message_from(&kd, opened(rep, &rm), 5u,
		                                          &id) == SITU_ERR_CONSTRAINT);
		CHECK(situ_drive_reply_to_on_message_from(&kd, opened(rep, &rm), 6u,
		                                          &id) == SITU_OK && id == 21u);
	}

	/* The driver: a verified reply ends the exchange; a forged one,
	 * whose tag the hook refuses, never reaches the match. */
	seen_t seen;
	CHECK(over_poll(9u, 1, &seen) == 0);
	CHECK(seen.replies == 1u && seen.id == 5u && seen.unmatched == 0u);
	CHECK(over_poll(9u, 0, &seen) == 0);
	CHECK(seen.replies == 0u && seen.unmatched >= 1u && seen.why == SITU_ERR_TAG);
	CHECK(over_poll(8u, 1, &seen) == 0);
	CHECK(seen.replies == 0u && seen.unmatched >= 1u && seen.why == SITU_ERR_CONSTRAINT);

	if (!fail) printf("ALL PASS\n");
	return fail;
}
"""


@pytest.mark.skipif(CC is None, reason="needs a C compiler")
def test_c_matches_through_the_gate_and_the_peer(tmp_path: Path) -> None:
	out = _build(tmp_path, "c", "--driver", "poll")
	(tmp_path / "gated.c").write_text(C_PROGRAM, encoding="ascii")
	compiled = subprocess.run(
		[CC or "cc", "-std=c11", "-Wall", "-Wextra", "-Werror",
		 "-Wconversion", "-Wsign-conversion", "-pthread",
		 f"-I{RUNTIME / 'c'}", f"-I{out}", str(tmp_path / "gated.c"),
		 str(out / "g.c"), str(out / "g_poll.c"),
		 str(RUNTIME / "c" / "situ.c"), "-o", str(tmp_path / "gated")],
		capture_output=True, text=True)
	assert compiled.returncode == 0, compiled.stderr
	ran = subprocess.run([str(tmp_path / "gated")], capture_output=True,
	                     text=True, timeout=60)
	assert ran.returncode == 0 and "ALL PASS" in ran.stdout, ran.stdout


@pytest.mark.skipif(CC is None, reason="needs a C compiler")
@pytest.mark.parametrize("facility", ["epoll", "select", "ppoll",
                                      "blocking", "io_uring"])
def test_every_c_driver_takes_a_verify_hook(tmp_path: Path,
		facility: str) -> None:
	out = _build(tmp_path, "c", "--driver", facility)
	main = tmp_path / "main.c"
	main.write_text(f'#include "g_{facility}.h"\nint main(void) {{ '
	                "situ_drive_hooks_t h = {0}; (void)h.verify; "
	                "return 0; }\n", encoding="ascii")
	compiled = subprocess.run(
		[CC or "cc", "-std=c11", "-Wall", "-Wextra", "-Werror",
		 "-Wconversion", "-Wsign-conversion", "-pthread",
		 f"-I{RUNTIME / 'c'}", f"-I{out}", str(main), str(out / "g.c"),
		 str(out / f"g_{facility}.c"), str(RUNTIME / "c" / "situ.c"),
		 "-o", str(tmp_path / "m")],
		capture_output=True, text=True)
	assert compiled.returncode == 0, compiled.stderr


PY_PROGRAM = r"""import sys
sys.path[:0] = [".", "/home/funk/src/situ/runtime/python"]
from situ_runtime import Message, ConstraintError
import g, g_converse, g_drive

def frame(msg, to, frm):
	b = bytearray(44)
	b[0:4] = msg.to_bytes(4, "big"); b[4:8] = to.to_bytes(4, "big"); b[20:24] = frm.to_bytes(4, "big")
	return g.frame.at(Message(b), 0)

t = g_converse.reply_to_table(4)
t.record(frame(7, 9, 0), 11, peer=3)
def no(f):
	try: f(); return False
	except ConstraintError: return True
assert no(lambda: t.take(frame(7, 0, 8).open_sealed(True), peer=3))
assert no(lambda: t.peek(frame(7, 0, 9).open_sealed(True), peer=4))
assert no(lambda: t.take(frame(7, 0, 9).open_sealed(True)))
assert t.take(frame(7, 0, 9).open_sealed(True), peer=3) == 11
sent = []
d = g_drive.reply_to_driver(sent.append, 2)
d.send(frame(7, 9, 0), b"x", 5, 0, peer=2)
assert no(lambda: d.on_message(frame(7, 0, 9).open_sealed(True)))
assert d.on_message(frame(7, 0, 9).open_sealed(True), peer=2) == 5
print("ok")
"""


def test_python_matches_through_the_gate_and_the_peer(
		tmp_path: Path) -> None:
	out = _build(tmp_path, "python")
	ran = subprocess.run(
		[sys.executable, "-c",
		 PY_PROGRAM.replace('"/home/funk/src/situ/runtime/python"',
		                    repr(str(RUNTIME / "python")))],
		cwd=out, capture_output=True, text=True, timeout=60)
	assert ran.returncode == 0 and ran.stdout.strip() == "ok", ran.stderr


CPP_PROGRAM = r"""#include <cstdio>
#include <cstring>
#include "g_converse.hpp"
#include "g_drive.hpp"

#define CHECK(c) do { if (!(c)) { std::printf("FAIL %d: %s\n", __LINE__, #c); fail = 1; } } while (0)

static void frame_bytes(std::uint8_t *out, std::uint32_t msg, std::uint32_t to, std::uint32_t from)
{
	std::memset(out, 0, 44u);
	for (unsigned b = 0; b < 4u; b++) {
		out[b]       = static_cast<std::uint8_t>(msg  >> (24u - 8u * b));
		out[4u + b]  = static_cast<std::uint8_t>(to   >> (24u - 8u * b));
		out[20u + b] = static_cast<std::uint8_t>(from >> (24u - 8u * b));
	}
}

struct sink final : ::situ::io {
	::situ::rt::err submit(const std::uint8_t *, std::uint32_t) noexcept override
	{ return ::situ::rt::err::ok; }
};

int main()
{
	int fail = 0;
	std::uint8_t req[44], r8[44], r9[44];
	frame_bytes(req, 7u, 9u, 0u);
	frame_bytes(r8, 7u, 0u, 8u);
	frame_bytes(r9, 7u, 0u, 9u);
	::situ::rt::message mq(req, 44u), m8(r8, 44u), m9(r9, 44u);
	::situ::frame q, v8, v9;
	CHECK(::situ::frame::at(mq, 0u, q) == ::situ::rt::err::ok);
	CHECK(::situ::frame::at(m8, 0u, v8) == ::situ::rt::err::ok);
	CHECK(::situ::frame::at(m9, 0u, v9) == ::situ::rt::err::ok);

	::situ::reply_to_table::slot keys[4];
	::situ::reply_to_table t(keys, 4u);
	CHECK(t.record_from(q, 3u, 11u) == ::situ::rt::err::ok);
	std::uint32_t id = 0u;
	(void)v8.with_sealed(true, [&](::situ::frame::sealed_gate g) {
		CHECK(t.take_from(g, 3u, id) == ::situ::rt::err::constraint);
	});
	(void)v9.with_sealed(true, [&](::situ::frame::sealed_gate g) {
		CHECK(t.peek_from(g, 4u, id) == ::situ::rt::err::constraint);
		CHECK(t.take(g, id) == ::situ::rt::err::constraint);
		CHECK(t.take_from(g, 3u, id) == ::situ::rt::err::ok && id == 11u);
	});

	sink s;
	::situ::reply_to_driver::slot ds[2];
	::situ::reply_to_driver d(ds, 2u, s, 100u, 1u);
	CHECK(d.send_from(q, 2u, req, 44u, 5u, 0u) == ::situ::rt::err::ok);
	(void)v9.with_sealed(true, [&](::situ::frame::sealed_gate g) {
		CHECK(d.on_message(g, id) == ::situ::rt::err::constraint);
		CHECK(d.on_message_from(g, 2u, id) == ::situ::rt::err::ok && id == 5u);
	});
	if (!fail) std::printf("ALL PASS\n");
	return fail;
}
"""


@pytest.mark.skipif(CXX is None or CC is None, reason="needs C and C++")
def test_cpp_matches_through_the_gate_and_the_peer(tmp_path: Path) -> None:
	out = _build(tmp_path, "cpp")
	(tmp_path / "gated.cpp").write_text(CPP_PROGRAM, encoding="ascii")
	runtime = subprocess.run(
		[CC or "cc", "-std=c11", "-c", str(RUNTIME / "c" / "situ.c"),
		 "-o", str(tmp_path / "situ.o")], capture_output=True, text=True)
	assert runtime.returncode == 0, runtime.stderr
	compiled = subprocess.run(
		[CXX or "c++", "-std=c++17", "-Wall", "-Wextra", "-Werror",
		 "-Wconversion", "-Wsign-conversion", f"-I{RUNTIME / 'c'}",
		 f"-I{RUNTIME / 'cpp'}", f"-I{out}", str(tmp_path / "gated.cpp"),
		 str(tmp_path / "situ.o"), "-o", str(tmp_path / "gated")],
		capture_output=True, text=True)
	assert compiled.returncode == 0, compiled.stderr
	ran = subprocess.run([str(tmp_path / "gated")], capture_output=True,
	                     text=True, timeout=60)
	assert ran.returncode == 0 and "ALL PASS" in ran.stdout, ran.stdout


RUST_PROGRAM = r"""#![deny(warnings)]
#![allow(unused_attributes)]
#[allow(dead_code)] mod situ_rt;
#[allow(dead_code)] mod g;
#[allow(dead_code)] mod g_converse;
#[allow(dead_code)] mod g_drive;
use g::*;
use g_converse::{ReplyToSlot as KeySlot, ReplyToTable};
use g_drive::{Io, ReplyToDriver, ReplyToSlot as DriveSlot};
use situ_rt::{Error, Result};

fn frame(msg: u32, to: u32, from: u32) -> [u8; 44] {
	let mut b = [0u8; 44];
	b[0..4].copy_from_slice(&msg.to_be_bytes());
	b[4..8].copy_from_slice(&to.to_be_bytes());
	b[20..24].copy_from_slice(&from.to_be_bytes());
	b
}

struct Sink;
impl Io for Sink {
	fn submit(&mut self, _data: &[u8]) -> Result<()> { Ok(()) }
}

fn main() {
	let req = frame(7, 9, 0);
	let rq = Frame::new(&req).unwrap();
	let mut slots = [KeySlot::default(); 4];
	let mut t = ReplyToTable::new(&mut slots);
	t.record_from(&rq, 3, 11).unwrap();
	let r8 = frame(7, 0, 8);
	let r9 = frame(7, 0, 9);
	let g8 = Frame::new(&r8).unwrap();
	let g9 = Frame::new(&r9).unwrap();
	let o8 = g8.open_sealed(true).unwrap();
	let o9 = g9.open_sealed(true).unwrap();
	assert!(matches!(t.take_from(&o8, 3), Err(Error::Constraint)));
	assert!(matches!(t.peek_from(&o9, 4), Err(Error::Constraint)));
	assert!(matches!(t.take(&o9), Err(Error::Constraint)));
	assert_eq!(t.take_from(&o9, 3).unwrap(), 11);

	let mut ds = [DriveSlot::default(); 2];
	let mut d = ReplyToDriver::new(&mut ds, Sink, 100, 1);
	d.send_from(&rq, 2, &req, 5, 0, None).unwrap();
	assert!(matches!(d.on_message(&o9), Err(Error::Constraint)));
	assert_eq!(d.on_message_from(&o9, 2).unwrap(), 5);
	println!("ok");
}
"""


@pytest.mark.skipif(RUSTC is None, reason="no rustc")
def test_rust_matches_through_the_gate_and_the_peer(tmp_path: Path) -> None:
	out = _build(tmp_path, "rust")
	crate = tmp_path / "crate"
	crate.mkdir()
	for name in ("g.rs", "g_converse.rs", "g_drive.rs"):
		(crate / name).write_text((out / name).read_text(encoding="ascii"),
		                          encoding="ascii")
	(crate / "situ_rt.rs").write_text(
		(RUNTIME / "rust" / "situ_rt.rs").read_text(encoding="ascii"),
		encoding="ascii")
	(crate / "main.rs").write_text(RUST_PROGRAM, encoding="ascii")
	compiled = subprocess.run(
		[RUSTC or "rustc", "--edition", "2021", "-o", str(tmp_path / "m"),
		 str(crate / "main.rs")], capture_output=True, text=True)
	assert compiled.returncode == 0, compiled.stderr
	ran = subprocess.run([str(tmp_path / "m")], capture_output=True,
	                     text=True, timeout=60)
	assert ran.returncode == 0 and ran.stdout.strip() == "ok", ran.stderr
