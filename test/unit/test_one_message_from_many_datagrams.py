"""One message reassembled from many datagrams (26.1123).

fuzznet reassembles in 648 lines of its own; its review fixed the rules
this table follows. A stride, not a total: every piece but the last is
one stride long, the last no longer, and the stride is set by the first
piece that is NOT the last -- a last piece arriving first is held at the
slot's end until one does, since a stride taken from it would let whoever
sends the final piece choose the layout. The bound is checked by
division. `missing` answers capped ranges. A piece behind a sealed region
is taken through its gate.
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

ROOT     = Path(__file__).resolve().parents[2]
RUNTIME  = ROOT / "runtime" / "c"
COMPILER = shutil.which("cc") or shutil.which("gcc")

SCHEMA = r"""target buffer;
endian big;

struct head {
	u8   sender[8];
	u32  msg;
	u16  index [max = chunks - 1];
	u16  chunks;
}

struct piece_frame {
	head h;
	u16  len;
	u8   body[len];
}

relation same_message(first: piece_frame, later: piece_frame) {
	must later.h.msg    == first.h.msg;
	must later.h.chunks == first.h.chunks;
	must later.h.sender == first.h.sender;
}

reassemble pieces(f: piece_frame) by same_message [
	index = f.h.index, count = f.h.chunks, piece = f.body,
	messages = 4, bytes = 4096, timeout = 3000, unit = ms];
"""

SEALED = r"""target buffer;
endian big;
codec aead {
	tag_bytes = 16;
	nonce_bytes = 12;
	granularity = byte;
	length_preserving;
	seekable;
	authenticated;
	invertible;
	deterministic;
}
impl aead extern "test_aead";
struct head { u32 msg; u16 index [max = chunks - 1]; u16 chunks; u16 len; }
struct sframe {
	authenticated { head h; u8 nonce[12]; }
	sealed(aead, nonce = nonce) { u8 payload[h.len]; }
	tag u8[16];
}
require verify_gated(sframe.sealed);
relation same_message(first: sframe, later: sframe) {
	must later.h.msg == first.h.msg;
	must later.h.chunks == first.h.chunks;
}
reassemble pieces(f: sframe) by same_message [
	index = f.h.index, count = f.h.chunks, piece = f.sealed.payload,
	messages = 2, bytes = 256, timeout = 100];
"""

PROGRAM = r"""#include <stdio.h>
#include <string.h>
#include "r_reassemble.h"

#define CHECK(c) do { if (!(c)) { printf("FAIL %d: %s\n", __LINE__, #c); fail = 1; } } while (0)

static uint8_t buf[64];
static situ_msg_t msg;

/* sender[8] msg u32 index u16 chunks u16 len u16 body */
static situ_view_t frame(uint8_t who, uint32_t id, uint16_t index, uint16_t chunks,
                         const char *body, uint16_t len)
{
	situ_view_t v;
	memset(buf, 0, sizeof buf);
	memset(buf, who, 8);
	buf[8] = (uint8_t)(id >> 24); buf[9] = (uint8_t)(id >> 16);
	buf[10] = (uint8_t)(id >> 8); buf[11] = (uint8_t)id;
	buf[12] = (uint8_t)(index >> 8); buf[13] = (uint8_t)index;
	buf[14] = (uint8_t)(chunks >> 8); buf[15] = (uint8_t)chunks;
	buf[16] = (uint8_t)(len >> 8); buf[17] = (uint8_t)len;
	memcpy(buf + 18, body, len);
	situ_msg_init(&msg, buf, 18u + len);
	(void)situ_piece_frame_view(&msg, 0u, 18u + len, &v);
	return v;
}

static situ_pieces_t t;

int main(void)
{
	int fail = 0;
	const uint8_t *out = NULL;
	uint32_t len = 0;
	situ_pieces_init(&t);

	/* In order: two of the stride, then a shorter last. */
	CHECK(situ_pieces_add(&t, frame(1, 7, 0, 3, "abcd", 4), 0u, &out, &len) == SITU_ERR_TRUNCATED);
	CHECK(situ_pieces_add(&t, frame(1, 7, 0, 3, "abcd", 4), 0u, &out, &len) == SITU_ERR_TRUNCATED);
	CHECK(situ_pieces_add(&t, frame(1, 7, 1, 3, "efgh", 4), 0u, &out, &len) == SITU_ERR_TRUNCATED);
	CHECK(situ_pieces_add(&t, frame(1, 7, 2, 3, "ij", 2), 0u, &out, &len) == SITU_OK);
	CHECK(len == 10u && memcmp(out, "abcdefghij", 10) == 0);

	/* The last first: parked, then placed once a stride arrives. */
	CHECK(situ_pieces_add(&t, frame(2, 8, 2, 3, "IJ", 2), 0u, &out, &len) == SITU_ERR_TRUNCATED);
	CHECK(situ_pieces_add(&t, frame(2, 8, 1, 3, "EFGH", 4), 0u, &out, &len) == SITU_ERR_TRUNCATED);
	CHECK(situ_pieces_add(&t, frame(2, 8, 0, 3, "ABCD", 4), 0u, &out, &len) == SITU_OK);
	CHECK(len == 10u && memcmp(out, "ABCDEFGHIJ", 10) == 0);

	/* One piece alone. */
	CHECK(situ_pieces_add(&t, frame(3, 9, 0, 1, "solo", 4), 0u, &out, &len) == SITU_OK);
	CHECK(len == 4u && memcmp(out, "solo", 4) == 0);

	/* Refusals. */
	CHECK(situ_pieces_add(&t, frame(4, 1, 3, 3, "abcd", 4), 0u, &out, &len) == SITU_ERR_CONSTRAINT);
	CHECK(situ_pieces_add(&t, frame(4, 1, 0, 3, "abcd", 4), 0u, &out, &len) == SITU_ERR_TRUNCATED);
	CHECK(situ_pieces_add(&t, frame(4, 1, 1, 3, "abc", 3), 0u, &out, &len) == SITU_ERR_CONSTRAINT);
	CHECK(situ_pieces_add(&t, frame(4, 1, 2, 3, "abcde", 5), 0u, &out, &len) == SITU_ERR_CONSTRAINT);

	/* Missing, as ranges: of ten, 0 1 5 held. */
	situ_pieces_init(&t);
	CHECK(situ_pieces_add(&t, frame(5, 2, 0, 10, "aa", 2), 0u, &out, &len) == SITU_ERR_TRUNCATED);
	CHECK(situ_pieces_add(&t, frame(5, 2, 1, 10, "bb", 2), 0u, &out, &len) == SITU_ERR_TRUNCATED);
	CHECK(situ_pieces_add(&t, frame(5, 2, 5, 10, "ff", 2), 0u, &out, &len) == SITU_ERR_TRUNCATED);
	uint32_t first[8], count[8];
	uint8_t key[14];
	memset(key, 5, 8);
	(void)key;
	uint8_t k[14];
	memcpy(k, t.slot[0].key, sizeof k);
	uint32_t n = situ_pieces_missing(&t, k, first, count, 8u, 100u);
	CHECK(n == 2u && first[0] == 2u && count[0] == 3u && first[1] == 6u && count[1] == 4u);
	n = situ_pieces_missing(&t, k, first, count, 8u, 2u);
	CHECK(n == 4u && first[0] == 2u && count[0] == 2u && first[1] == 4u && count[1] == 1u
	      && first[2] == 6u && count[2] == 2u && first[3] == 8u && count[3] == 2u);

	/* Expiry, by the wrapping clock. */
	CHECK(situ_pieces_expire(&t, 2999u) == 0u);
	CHECK(situ_pieces_expire(&t, 3000u) == 1u);
	CHECK(situ_pieces_missing(&t, k, first, count, 8u, 2u) == 0u);

	/* Past the capacity: 1024 a slot, so two of 600 is refused. */
	static char big[600];
	memset(big, 'x', sizeof big);
	CHECK(situ_pieces_add(&t, frame(6, 3, 0, 2, big, 40), 0u, &out, &len) == SITU_ERR_TRUNCATED);
	situ_pieces_init(&t);
	CHECK(situ_pieces_add(&t, frame(6, 3, 0, 30, "abcdefghijklmnopqrstuvwxyzabcdefghij", 36), 0u, &out, &len) == SITU_ERR_BOUNDS);

	/* Every slot taken: the fifth message is refused, not evicted. */
	situ_pieces_init(&t);
	for (uint8_t who = 0; who < 4; who++) {
		CHECK(situ_pieces_add(&t, frame(who, 1, 0, 2, "ab", 2), 0u, &out, &len) == SITU_ERR_TRUNCATED);
	}
	CHECK(situ_pieces_add(&t, frame(9, 1, 0, 2, "ab", 2), 0u, &out, &len) == SITU_ERR_BOUNDS);

	if (!fail) printf("ALL PASS\n");
	return fail;
}
"""

RANDOM = r"""#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "r_reassemble.h"

static uint8_t buf[64];
static situ_msg_t msg;
static situ_view_t frame(uint32_t id, uint16_t index, uint16_t chunks, const uint8_t *body, uint16_t len)
{
	situ_view_t v;
	memset(buf, 0, sizeof buf);
	buf[8] = (uint8_t)(id >> 24); buf[9] = (uint8_t)(id >> 16); buf[10] = (uint8_t)(id >> 8); buf[11] = (uint8_t)id;
	buf[12] = (uint8_t)(index >> 8); buf[13] = (uint8_t)index;
	buf[14] = (uint8_t)(chunks >> 8); buf[15] = (uint8_t)chunks;
	buf[16] = (uint8_t)(len >> 8); buf[17] = (uint8_t)len;
	memcpy(buf + 18, body, len);
	situ_msg_init(&msg, buf, 18u + len);
	(void)situ_piece_frame_view(&msg, 0u, 18u + len, &v);
	return v;
}
static situ_pieces_t t;
int main(void)
{
	srand(1);
	for (uint32_t round = 0; round < 3000; round++) {
		uint16_t count = (uint16_t)(1 + rand() % 20), stride = (uint16_t)(1 + rand() % 40);
		uint16_t last = (uint16_t)(1 + rand() % stride);
		uint8_t whole[1024];
		uint32_t total = (uint32_t)(count - 1) * stride + last;
		for (uint32_t i = 0; i < total; i++) whole[i] = (uint8_t)rand();
		uint16_t order[60]; uint32_t n = 0;
		for (uint16_t i = 0; i < count; i++) order[n++] = i;
		for (uint32_t d = 0; d < 5u; d++) order[n++] = (uint16_t)(rand() % count);
		for (uint32_t i = n - 1; i > 0; i--) { uint32_t j = (uint32_t)rand() % (i + 1); uint16_t x = order[i]; order[i] = order[j]; order[j] = x; }
		situ_pieces_init(&t);
		const uint8_t *out = NULL; uint32_t got = 0; int done = 0;
		for (uint32_t i = 0; i < n && !done; i++) {
			uint16_t ix = order[i];
			uint16_t len = ix == count - 1 ? last : stride;
			situ_err_t e = situ_pieces_add(&t, frame(round, ix, count, whole + (uint32_t)ix * stride, len), 0u, &out, &got);
			if (e == SITU_OK) done = 1;
			else if (e != SITU_ERR_TRUNCATED) { printf("round %u: unexpected %d (count %u stride %u)\n", round, (int)e, count, stride); return 1; }
		}
		if (!done || got != total || memcmp(out, whole, total) != 0) { printf("round %u: wrong message\n", round); return 1; }
	}
	printf("rounds ok\n");
	return 0;
}
"""

SEALED_PROGRAM = r"""#include <stdio.h>
#include <string.h>
#include "s_reassemble.h"
static uint8_t buf[64];
static situ_pieces_t t;
static situ_err_t add(uint16_t ix, uint16_t n, const char *p, uint16_t len, const uint8_t **out, uint32_t *got)
{
	situ_msg_t m; situ_view_t v; situ_sframe_sealed_t g;
	memset(buf, 0, sizeof buf);
	buf[3] = 7; buf[4] = (uint8_t)(ix >> 8); buf[5] = (uint8_t)ix; buf[7] = (uint8_t)n; buf[9] = (uint8_t)len;
	memcpy(buf + 22, p, len);
	situ_msg_init(&m, buf, 22u + len + 16u);
	if (situ_sframe_view(&m, 0u, 22u + len + 16u, &v) != SITU_OK) return SITU_ERR_BOUNDS;
	(void)situ_sframe_sealed_open(v, 1, &g);
	return situ_pieces_add(&t, g, 0u, out, got);
}
int main(void)
{
	const uint8_t *out; uint32_t got;
	situ_pieces_init(&t);
	int ok = add(1, 2, "yz", 2, &out, &got) == SITU_ERR_TRUNCATED
	      && add(0, 2, "wxyz", 4, &out, &got) == SITU_OK && got == 6u && memcmp(out, "wxyzyz", 6) == 0;
	puts(ok ? "sealed ok" : "sealed FAIL");
	return !ok;
}
"""


def _refused(source: str) -> str:
	with pytest.raises(SituError) as caught:
		check(parse_text(source))
	return str(caught.value)


def test_every_bound_is_stated() -> None:
	assert "states no `messages`" in _refused(
		SCHEMA.replace("messages = 4, ", ""))
	assert "states no `timeout`" in _refused(
		SCHEMA.replace("timeout = 3000, ", ""))


def test_the_relation_is_one_of_this_schema() -> None:
	assert "not a relation of this schema" in _refused(
		SCHEMA.replace("by same_message", "by nothing"))


def test_a_place_is_a_field_of_the_datagram() -> None:
	assert "names a field of `f`" in _refused(
		SCHEMA.replace("index = f.h.index", "index = 3"))


def _run(tmp_path: Path, schema: str, program: str, name: str,
		sanitize: bool = False) -> str:
	(tmp_path / f"{name}.situ").write_text(schema, encoding="ascii")
	out = tmp_path / name
	done = subprocess.run(
		[sys.executable, "-m", "situc", "build", str(tmp_path / f"{name}.situ"),
		 "--target", "c", "--layer", "converse", "--out", str(out)],
		cwd=ROOT, capture_output=True, text=True)
	assert done.returncode == 0, done.stderr
	(tmp_path / f"{name}_t.c").write_text(program, encoding="ascii")
	flags = (["-g", "-fsanitize=address,undefined",
	          "-fno-sanitize-recover=all"] if sanitize else [])
	compiled = subprocess.run(
		[COMPILER or "cc", "-std=c11", "-Wall", "-Wextra", "-Werror",
		 "-Wconversion", "-Wsign-conversion", *flags, f"-I{RUNTIME}",
		 f"-I{out}", str(tmp_path / f"{name}_t.c"), str(out / f"{name}.c"),
		 str(RUNTIME / "situ.c"), "-o", str(tmp_path / f"{name}_t")],
		capture_output=True, text=True)
	assert compiled.returncode == 0, compiled.stderr
	ran = subprocess.run([str(tmp_path / f"{name}_t")], capture_output=True,
	                     text=True, timeout=120)
	return ran.stdout


@pytest.mark.skipif(COMPILER is None, reason="needs a C compiler")
def test_pieces_in_any_order_make_the_message(tmp_path: Path) -> None:
	"""In order, the last first, one alone, duplicates, every refusal,
	missing ranges capped and not, expiry by the wrapping clock, the
	capacity and a full table refusing rather than evicting."""
	assert "ALL PASS" in _run(tmp_path, SCHEMA, PROGRAM, "r", sanitize=True)


@pytest.mark.skipif(COMPILER is None, reason="needs a C compiler")
def test_shuffled_and_duplicated_pieces_always_reassemble(
		tmp_path: Path) -> None:
	assert "rounds ok" in _run(tmp_path, SCHEMA, RANDOM, "r", sanitize=True)


@pytest.mark.skipif(COMPILER is None, reason="needs a C compiler")
def test_a_sealed_piece_is_taken_through_its_gate(tmp_path: Path) -> None:
	assert "sealed ok" in _run(tmp_path, SEALED, SEALED_PROGRAM, "s")


def test_every_backend_writes_the_table(tmp_path: Path) -> None:
	(tmp_path / "r.situ").write_text(SCHEMA, encoding="ascii")
	for target, name in (("c", "r_reassemble.h"), ("cpp", "r_reassemble.hpp"),
	                     ("python", "r_reassemble.py"),
	                     ("rust", "r_reassemble.rs")):
		done = subprocess.run(
			[sys.executable, "-m", "situc", "build", str(tmp_path / "r.situ"),
			 "--target", target, "--layer", "converse", "--out",
			 str(tmp_path / target)], cwd=ROOT, capture_output=True, text=True)
		assert done.returncode == 0, done.stderr
		assert (tmp_path / target / name).exists(), (target, done.stderr)
		assert "no reassembly table" not in done.stderr, done.stderr


def test_its_words_mean_nothing_on_a_member() -> None:
	"""`[count = 2]` on a member was accepted and read by nothing, as
	replay's `[window = 3]` had been since 26.1120: each is refused now,
	naming the declaration it belongs to."""
	for said in ("count = 2", "window = 3", 'media = "x/y"'):
		reason = _refused(f"target buffer;\nstruct s {{ u8 a [{said}]; }}\n")
		assert "means nothing here" in reason, (said, reason)


PYTHON = r"""
import random, sys
sys.path[:0] = [".", RUNTIME]
from situ_runtime import Message, ConstraintError, BoundsError
import r, r_reassemble, r_converse

def frame(who, mid, ix, n, body):
	b = bytearray(18 + len(body))
	b[0:8] = bytes([who]) * 8
	b[8:12] = mid.to_bytes(4, "big")
	b[12:14] = ix.to_bytes(2, "big")
	b[14:16] = n.to_bytes(2, "big")
	b[16:18] = len(body).to_bytes(2, "big")
	b[18:] = body
	return r.piece_frame.at(Message(b), 0, len(b))

def refused(kind, *args):
	try:
		t.add(frame(*args), 0)
	except kind:
		return True
	return False

t = r_reassemble.pieces_table()
assert t.add(frame(2, 8, 2, 3, b"IJ"), 0) is None
assert t.add(frame(2, 8, 1, 3, b"EFGH"), 0) is None
assert t.add(frame(2, 8, 1, 3, b"EFGH"), 0) is None
assert t.add(frame(2, 8, 0, 3, b"ABCD"), 0) == b"ABCDEFGHIJ"
assert refused(ConstraintError, 4, 1, 3, 3, b"abcd")
t.add(frame(4, 1, 0, 3, b"abcd"), 0)
assert refused(ConstraintError, 4, 1, 1, 3, b"abc")
assert refused(ConstraintError, 4, 1, 2, 3, b"abcde")
t = r_reassemble.pieces_table()
for ix in (0, 1, 5):
	t.add(frame(5, 2, ix, 10, b"aa"), 0)
key = r_converse.same_message_table.key(2, 10, bytes([5]) * 8)
assert t.missing(key, 8, 100) == [(2, 3), (6, 4)]
assert t.missing(key, 8, 2) == [(2, 2), (4, 1), (6, 2), (8, 2)]
assert t.expire(2999) == 0 and t.expire(3000) == 1
t = r_reassemble.pieces_table()
for who in range(4):
	t.add(frame(who, 1, 0, 2, b"ab"), 0)
assert refused(BoundsError, 9, 1, 0, 2, b"ab")
random.seed(1)
for round in range(500):
	n, st = random.randint(1, 20), random.randint(1, 40)
	last = random.randint(1, st)
	whole = bytes(random.getrandbits(8) for _ in range((n - 1) * st + last))
	order = list(range(n)) + [random.randrange(n) for _ in range(5)]
	random.shuffle(order)
	t = r_reassemble.pieces_table()
	got = None
	for ix in order:
		body = whole[ix * st:ix * st + (last if ix == n - 1 else st)]
		got = t.add(frame(1, round, ix, n, body), 0)
		if got is not None:
			break
	assert got == whole, round
print("python ok")
"""


def test_python_reassembles_alike(tmp_path: Path) -> None:
	"""The same table in Python: C's rules, `add` answering the message or
	None and raising where C refuses."""
	(tmp_path / "r.situ").write_text(SCHEMA, encoding="ascii")
	out = tmp_path / "py"
	done = subprocess.run(
		[sys.executable, "-m", "situc", "build", str(tmp_path / "r.situ"),
		 "--target", "python", "--layer", "converse", "--out", str(out)],
		cwd=ROOT, capture_output=True, text=True)
	assert done.returncode == 0, done.stderr
	ran = subprocess.run(
		[sys.executable, "-c", PYTHON.replace(
			"RUNTIME", repr(str(ROOT / "runtime" / "python")))],
		cwd=out, capture_output=True, text=True, timeout=120)
	assert ran.stdout.strip() == "python ok", ran.stderr


RUSTC = shutil.which("rustc")

RUST = r"""#![deny(warnings)]
#![allow(unused_attributes)]
#[allow(dead_code)] mod situ_rt;
#[allow(dead_code)] mod r;
#[allow(dead_code)] mod r_converse;
#[allow(dead_code)] mod r_reassemble;
use r::*;
use r_reassemble::PiecesTable;
use r_converse::SameMessageTable;
fn frame(who: u8, mid: u32, ix: u16, n: u16, body: &[u8]) -> Vec<u8> {
	let mut b = vec![who; 8];
	b.extend_from_slice(&mid.to_be_bytes());
	b.extend_from_slice(&ix.to_be_bytes());
	b.extend_from_slice(&n.to_be_bytes());
	b.extend_from_slice(&(body.len() as u16).to_be_bytes());
	b.extend_from_slice(body);
	b
}
fn add(t: &mut PiecesTable, b: &[u8]) -> situ_rt::Result<Option<Vec<u8>>> {
	let v = PieceFrame::new(b).unwrap();
	t.add(&v, 0).map(|o| o.map(|s| s.to_vec()))
}
fn main() {
	let mut t = Box::new(PiecesTable::new());
	assert!(add(&mut t, &frame(2, 8, 2, 3, b"IJ")).unwrap().is_none());
	assert!(add(&mut t, &frame(2, 8, 1, 3, b"EFGH")).unwrap().is_none());
	assert_eq!(add(&mut t, &frame(2, 8, 0, 3, b"ABCD")).unwrap().unwrap(), b"ABCDEFGHIJ");
	assert!(add(&mut t, &frame(4, 1, 3, 3, b"abcd")).is_err());
	let mut t = Box::new(PiecesTable::new());
	for ix in [0u16, 1, 5] { add(&mut t, &frame(5, 2, ix, 10, b"aa")).unwrap(); }
	let key = SameMessageTable::key(2, 10, &[5u8; 8]);
	let (mut first, mut count) = ([0u32; 8], [0u32; 8]);
	let n = t.missing(&key, &mut first, &mut count, 2);
	assert_eq!(n, 4);
	assert_eq!((first[0], count[0], first[3], count[3]), (2, 2, 8, 2));
	assert_eq!(t.expire(2999), 0);
	assert_eq!(t.expire(3000), 1);
	let mut seed = 1u64;
	let mut rnd = |m: u64| { seed = seed.wrapping_mul(6364136223846793005).wrapping_add(1442695040888963407); (seed >> 33) % m };
	for round in 0..500u32 {
		let n = 1 + rnd(20) as u16; let st = 1 + rnd(40) as usize; let last = 1 + rnd(st as u64) as usize;
		let total = (n as usize - 1) * st + last;
		let whole: Vec<u8> = (0..total).map(|_| rnd(256) as u8).collect();
		let mut order: Vec<u16> = (0..n).collect();
		for _ in 0..5 { order.push(rnd(n as u64) as u16); }
		for i in (1..order.len()).rev() { let j = rnd(i as u64 + 1) as usize; order.swap(i, j); }
		let mut t = Box::new(PiecesTable::new());
		let mut got = None;
		for ix in order {
			let at = ix as usize * st;
			let len = if ix == n - 1 { last } else { st };
			if let Some(m) = add(&mut t, &frame(1, round, ix, n, &whole[at..at + len])).unwrap() { got = Some(m); break; }
		}
		assert_eq!(got.unwrap(), whole, "round {}", round);
	}
	println!("rust ok");
}
"""


@pytest.mark.skipif(RUSTC is None, reason="no rustc")
def test_rust_reassembles_alike(tmp_path: Path) -> None:
	"""The same table in Rust, `no_std` and allocating nothing: `add`
	answers `Ok(Some(message))`, `Ok(None)` or the refusal."""
	(tmp_path / "r.situ").write_text(SCHEMA, encoding="ascii")
	out = tmp_path / "rs"
	done = subprocess.run(
		[sys.executable, "-m", "situc", "build", str(tmp_path / "r.situ"),
		 "--target", "rust", "--layer", "converse", "--out", str(out)],
		cwd=ROOT, capture_output=True, text=True)
	assert done.returncode == 0, done.stderr
	(out / "situ_rt.rs").write_text(
		(ROOT / "runtime" / "rust" / "situ_rt.rs").read_text(encoding="ascii"),
		encoding="ascii")
	(out / "main.rs").write_text(RUST, encoding="ascii")
	compiled = subprocess.run(
		[RUSTC or "rustc", "--edition", "2021", "-o", str(tmp_path / "m"),
		 str(out / "main.rs")], capture_output=True, text=True)
	assert compiled.returncode == 0, compiled.stderr
	ran = subprocess.run([str(tmp_path / "m")], capture_output=True,
	                     text=True, timeout=120)
	assert ran.stdout.strip() == "rust ok", ran.stderr


CXX = shutil.which("g++") or shutil.which("c++")

CPP = r"""#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <vector>
#include "r_reassemble.hpp"
#include "r_converse.hpp"

static std::vector<std::uint8_t> frame(std::uint8_t who, std::uint32_t mid,
		std::uint16_t ix, std::uint16_t n, const std::uint8_t *body,
		std::uint16_t len)
{
	std::vector<std::uint8_t> b(18u + len, 0u);
	std::memset(b.data(), who, 8);
	for (unsigned i = 0; i < 4u; i++)
		b[8u + i] = static_cast<std::uint8_t>(mid >> (24u - 8u * i));
	b[12] = static_cast<std::uint8_t>(ix >> 8);
	b[13] = static_cast<std::uint8_t>(ix);
	b[14] = static_cast<std::uint8_t>(n >> 8);
	b[15] = static_cast<std::uint8_t>(n);
	b[16] = static_cast<std::uint8_t>(len >> 8);
	b[17] = static_cast<std::uint8_t>(len);
	if (len != 0u)
		std::memcpy(b.data() + 18, body, len);
	return b;
}

static ::situ::rt::err add(::situ::pieces_table &t,
		std::vector<std::uint8_t> &b, ::situ::rt::const_bytes &out)
{
	::situ::rt::message m(b.data(), static_cast<std::uint32_t>(b.size()));
	::situ::piece_frame v;
	if (::situ::piece_frame::at(m, 0u, static_cast<std::uint32_t>(b.size()),
	                            v) != ::situ::rt::err::ok)
		return ::situ::rt::err::bounds;
	return t.add(v, 0u, out);
}

static const std::uint8_t *text(const char *s)
{
	return reinterpret_cast<const std::uint8_t *>(s);
}

int main()
{
	int fail = 0;
	::situ::rt::const_bytes out;
	auto *t = new ::situ::pieces_table();
	auto f1 = frame(2, 8, 2, 3, text("IJ"), 2);
	auto f2 = frame(2, 8, 1, 3, text("EFGH"), 4);
	auto f3 = frame(2, 8, 0, 3, text("ABCD"), 4);
	if (add(*t, f1, out) != ::situ::rt::err::truncated) fail |= 1;
	if (add(*t, f2, out) != ::situ::rt::err::truncated) fail |= 2;
	if (add(*t, f3, out) != ::situ::rt::err::ok || out.size() != 10u
	    || std::memcmp(out.data(), "ABCDEFGHIJ", 10) != 0) fail |= 4;
	auto bad = frame(4, 1, 3, 3, text("abcd"), 4);
	if (add(*t, bad, out) != ::situ::rt::err::constraint) fail |= 64;
	delete t;

	t = new ::situ::pieces_table();
	static const std::uint16_t held[] = {0u, 1u, 5u};
	for (std::uint16_t ix : held) {
		auto f = frame(5, 2, ix, 10, text("aa"), 2);
		(void)add(*t, f, out);
	}
	std::uint8_t sender[8];
	std::memset(sender, 5, 8);
	std::uint8_t key[14];
	::situ::same_message_table::key(2u, 10u, sender, key);
	std::uint32_t first[8], count[8];
	std::uint32_t n = t->missing(key, first, count, 8u, 2u);
	if (n != 4u || first[0] != 2u || count[0] != 2u || first[3] != 8u
	    || count[3] != 2u) fail |= 8;
	if (t->expire(2999u) != 0u || t->expire(3000u) != 1u) fail |= 16;
	delete t;

	std::srand(1);
	for (std::uint32_t round = 0; round < 500u; round++) {
		std::uint16_t cnt = static_cast<std::uint16_t>(1 + std::rand() % 20);
		std::uint16_t st = static_cast<std::uint16_t>(1 + std::rand() % 40);
		std::uint16_t last = static_cast<std::uint16_t>(1 + std::rand() % st);
		std::vector<std::uint8_t> whole(
			static_cast<std::size_t>((cnt - 1) * st + last));
		for (auto &x : whole) x = static_cast<std::uint8_t>(std::rand());
		std::vector<std::uint16_t> order;
		for (std::uint16_t i = 0; i < cnt; i++) order.push_back(i);
		for (int d = 0; d < 5; d++)
			order.push_back(static_cast<std::uint16_t>(std::rand() % cnt));
		for (std::size_t i = order.size() - 1; i > 0; i--)
			std::swap(order[i],
			          order[static_cast<std::size_t>(std::rand()) % (i + 1)]);
		t = new ::situ::pieces_table();
		bool done = false;
		for (auto ix : order) {
			std::uint16_t len = ix == cnt - 1 ? last : st;
			auto f = frame(1, round, ix, cnt,
			               whole.data() + static_cast<std::size_t>(ix) * st, len);
			::situ::rt::err e = add(*t, f, out);
			if (e == ::situ::rt::err::ok) {
				done = out.size() == whole.size()
				       && std::memcmp(out.data(), whole.data(), whole.size()) == 0;
				break;
			}
			if (e != ::situ::rt::err::truncated) break;
		}
		delete t;
		if (!done) { fail |= 32; break; }
	}
	std::printf("fail=%d\n", fail);
	return fail;
}
"""


@pytest.mark.skipif(CXX is None or COMPILER is None, reason="needs C and C++")
def test_cpp_reassembles_alike(tmp_path: Path) -> None:
	"""The same table in C++, allocating nothing: `add` answers `err::ok`
	with the message, `err::truncated` while more is wanted, or the
	refusal. Its relation's key reads three parts through one nested
	struct, which the C++ conversation header could not compile until each
	nested view was named after its binding."""
	(tmp_path / "r.situ").write_text(SCHEMA, encoding="ascii")
	out = tmp_path / "cpp"
	done = subprocess.run(
		[sys.executable, "-m", "situc", "build", str(tmp_path / "r.situ"),
		 "--target", "cpp", "--layer", "drive", "--out", str(out)],
		cwd=ROOT, capture_output=True, text=True)
	assert done.returncode == 0, done.stderr
	(tmp_path / "t.cpp").write_text(CPP, encoding="ascii")
	runtime = ROOT / "runtime"
	compiled = subprocess.run(
		[CXX or "c++", "-std=c++17", "-Wall", "-Wextra", "-Werror",
		 "-Wconversion", "-Wsign-conversion", f"-I{runtime / 'c'}",
		 f"-I{runtime / 'cpp'}", f"-I{out}", str(tmp_path / "t.cpp"),
		 str(runtime / "c" / "situ.c"), "-o", str(tmp_path / "t")],
		capture_output=True, text=True)
	assert compiled.returncode == 0, compiled.stderr
	ran = subprocess.run([str(tmp_path / "t")], capture_output=True,
	                     text=True, timeout=120)
	assert ran.stdout.strip() == "fail=0", ran.stdout


def _frames(*pieces: tuple[int, int, int, int, bytes]) -> bytes:
	"""The harness's input after its selector byte: per frame a two-byte
	length, a clock step, and the frame."""
	out = b""
	for who, mid, index, count, body in pieces:
		frame = (bytes([who]) * 8 + mid.to_bytes(4, "big")
		         + index.to_bytes(2, "big") + count.to_bytes(2, "big")
		         + len(body).to_bytes(2, "big") + body)
		out += len(frame).to_bytes(2, "big") + b"\x01" + frame
	return out


@pytest.mark.skipif(COMPILER is None, reason="no C compiler")
def test_the_fuzz_harness_reassembles(tmp_path: Path) -> None:
	"""`gen-fuzz` gives the table a harness: frames out of a fuzzer's
	bytes, the first frame's key copied into the later ones so pieces can
	meet. Held to completing a message from pieces with three different
	keys, through a copy printing what the table hands back. Building it
	found the relation harness calling a variable struct's view without
	its length, which did not compile; this fixture's relation is over
	one."""
	(tmp_path / "r.situ").write_text(SCHEMA, encoding="ascii")
	out = tmp_path / "c"
	for command in (["build", "--layer", "converse"], ["gen-fuzz"]):
		done = subprocess.run(
			[sys.executable, "-m", "situc", command[0], str(tmp_path / "r.situ"),
			 *command[1:], "--out", str(out)],
			cwd=ROOT, capture_output=True, text=True)
		assert done.returncode == 0, done.stderr
	harness = (out / "r_fuzz.c").read_text(encoding="ascii")
	assert "fuzz_ra_pieces" in harness
	sink = "\t\t\tsitu_fuzz_sink(length == 0u ? 0u : message[length - 1u]);"
	assert harness.count(sink) == 1
	(out / "probe.c").write_text(harness.replace(
		sink, '\t\t\tprintf("done %.*s\\n", (int)length, '
		      "(const char *)message);"), encoding="ascii")
	runtime = ROOT / "runtime" / "c"
	built = subprocess.run(
		[COMPILER or "cc", "-std=c11", "-Wall", "-Wextra", "-Werror",
		 "-Wconversion", "-Wsign-conversion", "-include", "stdio.h", "-g",
		 "-fsanitize=address,undefined", "-fno-sanitize-recover=all",
		 "-DSITU_FUZZ_STANDALONE", f"-I{runtime}", f"-I{out}",
		 str(out / "probe.c"), str(out / "r.c"), str(out / "r_relate.c"),
		 str(runtime / "situ.c"), "-o", str(tmp_path / "probe")],
		capture_output=True, text=True)
	assert built.returncode == 0, built.stderr
	frames = _frames((7, 9, 2, 3, b"IJ"), (1, 4, 0, 3, b"ABCD"),
	                 (2, 5, 1, 3, b"EFGH"))
	said = []
	for selector in range(harness.count("\tcase ")):
		ran = subprocess.run([str(tmp_path / "probe")],
		                     input=bytes([selector]) + frames,
		                     capture_output=True, timeout=60)
		assert ran.returncode == 0, ran.stderr
		said.append(ran.stdout)
	assert said.count(b"done ABCDEFGHIJ\n") == 1, said


def test_the_fuzz_harness_declines_a_sealed_piece(tmp_path: Path) -> None:
	(tmp_path / "s.situ").write_text(SEALED, encoding="ascii")
	done = subprocess.run(
		[sys.executable, "-m", "situc", "gen-fuzz", str(tmp_path / "s.situ"),
		 "--out", str(tmp_path)], cwd=ROOT, capture_output=True, text=True)
	assert done.returncode == 0, done.stderr
	harness = (tmp_path / "s_fuzz.c").read_text(encoding="ascii")
	assert "No harness for reassembly pieces: its piece is behind a sealed" \
		in harness
	assert "fuzz_ra_" not in harness
