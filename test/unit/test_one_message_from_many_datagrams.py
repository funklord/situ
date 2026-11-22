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
static situ_pieces_store_t store;

int main(void)
{
	int fail = 0;
	const uint8_t *out = NULL;
	uint32_t len = 0;
	situ_pieces_init(&t, &store);

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
	situ_pieces_init(&t, &store);
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
	situ_pieces_init(&t, &store);
	CHECK(situ_pieces_add(&t, frame(6, 3, 0, 30, "abcdefghijklmnopqrstuvwxyzabcdefghij", 36), 0u, &out, &len) == SITU_ERR_BOUNDS);

	/* Every slot taken: the fifth message is refused, not evicted. */
	situ_pieces_init(&t, &store);
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
static situ_pieces_store_t store;
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
		situ_pieces_init(&t, &store);
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
static situ_pieces_store_t store;
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
	situ_pieces_init(&t, &store);
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


def test_a_per_cap_is_stated_whole_and_of_the_key(tmp_path: Path) -> None:
	"""`per` and `per_max` together, `per` a field of the datagram and
	`per_max` at most `messages` (fuzznet, inbox 20261122T1804Z); and
	`per` a part of the relation's key, since the table holds keys and a
	cap by anything else would need a second value kept per slot."""
	capped = SCHEMA.replace("messages = 4", "per = f.h.sender, per_max = 1, "
	                        "messages = 4")
	assert "stated together" in _refused(
		capped.replace(", per_max = 1", ""))
	assert "names a field of `f`" in _refused(
		capped.replace("per = f.h.sender", "per = 3"))
	assert "from 1 to `messages`" in _refused(
		capped.replace("per_max = 1", "per_max = 9"))
	(tmp_path / "r.situ").write_text(
		capped.replace("per = f.h.sender", "per = f.h.index"),
		encoding="ascii")
	built = subprocess.run(
		[sys.executable, "-m", "situc", "build", str(tmp_path / "r.situ"),
		 "--target", "c", "--layer", "converse", "--out", str(tmp_path)],
		cwd=ROOT, capture_output=True, text=True)
	assert "no part of `same_message`'s key" in built.stderr, built.stderr


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


#: The relation without the count in its key: nothing then makes the
#: pieces of one message agree on how many there are, but the table.
NO_COUNT = SCHEMA.replace("\tmust later.h.chunks == first.h.chunks;\n", "")

NO_COUNT_C = r"""
#include <stdio.h>
#include <string.h>
#include "r_reassemble.h"
static situ_pieces_t t;
static situ_pieces_store_t store;
static uint8_t buf[64];
static int add(uint16_t ix, uint16_t n, const char *body, const uint8_t **msg,
               uint32_t *length)
{
	const uint16_t len = (uint16_t)strlen(body);
	memset(buf, 7, 8);
	memset(buf + 8, 0, 4);
	buf[11] = 1;
	buf[12] = (uint8_t)(ix >> 8); buf[13] = (uint8_t)ix;
	buf[14] = (uint8_t)(n >> 8); buf[15] = (uint8_t)n;
	buf[16] = 0; buf[17] = (uint8_t)len;
	memcpy(buf + 18, body, len);
	situ_msg_t m;
	situ_view_t v;
	situ_msg_init(&m, buf, 18u + len);
	if (situ_piece_frame_view(&m, 0u, 18u + len, &v) != SITU_OK
	    || situ_piece_frame_validate(v) != SITU_OK)
		return -1;
	return (int)situ_pieces_add(&t, v, 0u, msg, length);
}
int main(void)
{
	const uint8_t *msg = NULL;
	uint32_t length = 0u;
	situ_pieces_init(&t, &store);
	int a = add(0, 2, "ABCD", &msg, &length);
	int b = add(1000, 1024, "WXYZ", &msg, &length);
	int c = add(1, 2, "EF", &msg, &length);
	printf("%d %d %d %.*s\n", a == SITU_ERR_TRUNCATED, b == SITU_ERR_CONSTRAINT,
	       c == SITU_OK, (int)length, (const char *)msg);
	return 0;
}
"""


@pytest.mark.skipif(COMPILER is None, reason="no C compiler")
def test_a_piece_saying_another_count_is_refused(tmp_path: Path) -> None:
	"""Where the key leaves the count out, a later piece may say a larger
	one; its index, checked against its own count, put it at `index *
	stride`, past the slot -- a write half a megabyte beyond the table in
	C, a panic in Rust, a KeyError in Python. Every piece of a message
	says its count, and one that does not is malformed."""
	(tmp_path / "r.situ").write_text(NO_COUNT, encoding="ascii")
	runtime = ROOT / "runtime"
	for target in ("c", "python", "rust", "cpp"):
		done = subprocess.run(
			[sys.executable, "-m", "situc", "build", str(tmp_path / "r.situ"),
			 "--target", target, "--layer", "converse", "--out",
			 str(tmp_path / target)], cwd=ROOT, capture_output=True, text=True)
		assert done.returncode == 0, done.stderr
	(tmp_path / "t.c").write_text(NO_COUNT_C, encoding="ascii")
	built = subprocess.run(
		[COMPILER or "cc", "-std=c11", "-g", "-fsanitize=address,undefined",
		 "-fno-sanitize-recover=all", f"-I{runtime / 'c'}",
		 f"-I{tmp_path / 'c'}", str(tmp_path / "t.c"),
		 str(tmp_path / "c" / "r.c"), str(runtime / "c" / "situ.c"), "-o",
		 str(tmp_path / "t")], capture_output=True, text=True)
	assert built.returncode == 0, built.stderr
	ran = subprocess.run([str(tmp_path / "t")], capture_output=True, text=True,
	                     timeout=60)
	assert ran.stdout.strip() == "1 1 1 ABCDEF", ran.stdout + ran.stderr

	script = PYTHON.split("t = r_reassemble.pieces_table()")[0] + r'''
t = r_reassemble.pieces_table()
assert t.add(frame(7, 1, 0, 2, b"ABCD"), 0) is None
assert refused(ConstraintError, 7, 1, 1000, 1024, b"WXYZ")
assert t.add(frame(7, 1, 1, 2, b"EF"), 0) == b"ABCDEF"
print("python ok")
'''
	ran = subprocess.run(
		[sys.executable, "-c", script.replace(
			"RUNTIME", repr(str(runtime / "python")))],
		cwd=tmp_path / "python", capture_output=True, text=True, timeout=60)
	assert ran.stdout.strip() == "python ok", ran.stderr

	if RUSTC is not None:
		out = tmp_path / "rust"
		(out / "situ_rt.rs").write_text(
			(runtime / "rust" / "situ_rt.rs").read_text(encoding="ascii"),
			encoding="ascii")
		main = RUST.split("fn main() {")[0] + r"""fn main() {
	let mut t = Box::new(PiecesTable::new());
	assert!(add(&mut t, &frame(7, 1, 0, 2, b"ABCD")).unwrap().is_none());
	assert!(matches!(add(&mut t, &frame(7, 1, 1000, 1024, b"WXYZ")),
	                 Err(situ_rt::Error::Constraint)));
	assert_eq!(add(&mut t, &frame(7, 1, 1, 2, b"EF")).unwrap().unwrap(), b"ABCDEF");
	let _ = SameMessageTable::key;
	println!("rust ok");
}
"""
		(out / "main.rs").write_text(main, encoding="ascii")
		compiled = subprocess.run(
			[RUSTC, "--edition", "2021", "-o", str(tmp_path / "m"),
			 str(out / "main.rs")], capture_output=True, text=True)
		assert compiled.returncode == 0, compiled.stderr
		ran = subprocess.run([str(tmp_path / "m")], capture_output=True,
		                     text=True, timeout=60)
		assert ran.stdout.strip() == "rust ok", ran.stderr

	if CXX is not None:
		program = CPP.split("int main()")[0] + r"""int main()
{
	::situ::rt::const_bytes out;
	auto *t = new ::situ::pieces_table();
	auto a = frame(7, 1, 0, 2, text("ABCD"), 4);
	auto b = frame(7, 1, 1000, 1024, text("WXYZ"), 4);
	auto c = frame(7, 1, 1, 2, text("EF"), 2);
	const bool ok = add(*t, a, out) == ::situ::rt::err::truncated
	    && add(*t, b, out) == ::situ::rt::err::constraint
	    && add(*t, c, out) == ::situ::rt::err::ok && out.size() == 6u
	    && std::memcmp(out.data(), "ABCDEF", 6) == 0;
	delete t;
	std::printf("%s\n", ok ? "cpp ok" : "cpp wrong");
	return 0;
}
"""
		(tmp_path / "t.cpp").write_text(
			program.replace('#include "r_converse.hpp"\n', ""), encoding="ascii")
		compiled = subprocess.run(
			[CXX, "-std=c++17", "-g", "-fsanitize=address,undefined",
			 f"-I{runtime / 'c'}", f"-I{runtime / 'cpp'}",
			 f"-I{tmp_path / 'cpp'}", str(tmp_path / "t.cpp"),
			 str(runtime / "c" / "situ.c"), "-o", str(tmp_path / "tc")],
			capture_output=True, text=True)
		assert compiled.returncode == 0, compiled.stderr
		ran = subprocess.run([str(tmp_path / "tc")], capture_output=True,
		                     text=True, timeout=60)
		assert ran.stdout.strip() == "cpp ok", ran.stdout + ran.stderr


#: Two slots of 32 bytes, so a stream of pieces fills, overflows and
#: expires them; keyed without the count, so the count check is reached.
SMALL = NO_COUNT.replace("messages = 4, bytes = 4096", "messages = 2, bytes = 64")

#: Every backend reads the same records -- a two-byte length, a four-byte
#: clock, the frame -- and says for each: what `add` answered (and the
#: message where it completed one), the ranges `missing` gives for every
#: key the stream uses, and how many `expire` dropped.
DIFF_C = r"""
#include <stdio.h>
#include <string.h>
#include "r_reassemble.h"
#include "r_converse.h"
static situ_pieces_t t;
static situ_pieces_store_t store;
static uint8_t buf[70000];
int main(void)
{
	uint8_t head[6];
	situ_pieces_init(&t, &store);
	while (fread(head, 1, 6, stdin) == 6) {
		const uint32_t len = (uint32_t)head[0] << 8 | head[1];
		const uint32_t now = (uint32_t)head[2] << 24 | (uint32_t)head[3] << 16
		                     | (uint32_t)head[4] << 8 | head[5];
		if (fread(buf, 1, len, stdin) != len)
			return 2;
		situ_msg_t m;
		situ_view_t v;
		const uint8_t *msg = NULL;
		uint32_t length = 0u;
		situ_msg_init(&m, buf, len);
		situ_err_t e = situ_piece_frame_view(&m, 0u, len, &v);
		if (e == SITU_OK)
			e = situ_piece_frame_validate(v);
		if (e != SITU_OK)
			return 3;
		e = situ_pieces_add(&t, v, now, &msg, &length);
		printf("A %c", e == SITU_OK ? 'O' : e == SITU_ERR_TRUNCATED ? 'T'
		       : e == SITU_ERR_CONSTRAINT ? 'C' : e == SITU_ERR_BOUNDS ? 'B' : '?');
		if (e == SITU_OK) {
			putchar(' ');
			for (uint32_t i = 0u; i < length; i++)
				printf("%02x", msg[i]);
		}
		putchar('\n');
		for (unsigned who = 1u; who <= 3u; who++) {
			for (unsigned mid = 1u; mid <= 2u; mid++) {
				uint8_t sender[8], key[12];
				uint32_t first[4], count[4];
				memset(sender, (int)who, 8);
				situ_conv_same_message_key(mid, sender, key);
				const uint32_t n = situ_pieces_missing(&t, key, first, count, 4u, 2u);
				printf("M");
				for (uint32_t k = 0u; k < n; k++)
					printf(" %u:%u", first[k], count[k]);
				putchar('\n');
			}
		}
		printf("E %u\n", situ_pieces_expire(&t, now));
		if (now % 5u == 0u) {
			uint8_t sender[8], key[12];
			memset(sender, (int)(now % 3u + 1u), 8);
			situ_conv_same_message_key(now % 2u + 1u, sender, key);
			printf("F %u\n", situ_pieces_forget(&t, key));
		} else {
			printf("F -\n");
		}
	}
	return 0;
}
"""

DIFF_PY = r"""
import sys
sys.path[:0] = [".", RUNTIME]
from situ_runtime import Message, ConstraintError, BoundsError
import r, r_reassemble, r_converse
data = sys.stdin.buffer.read()
t = r_reassemble.pieces_table()
out = []
at = 0
while at + 6 <= len(data):
	n = int.from_bytes(data[at:at + 2], "big")
	now = int.from_bytes(data[at + 2:at + 6], "big")
	frame = data[at + 6:at + 6 + n]
	at += 6 + n
	view = r.piece_frame.at(Message(bytearray(frame)), 0, n)
	try:
		got = t.add(view, now)
		out.append("A T" if got is None else "A O " + got.hex())
	except ConstraintError:
		out.append("A C")
	except BoundsError:
		out.append("A B")
	for who in (1, 2, 3):
		for mid in (1, 2):
			key = r_converse.same_message_table.key(mid, bytes([who]) * 8)
			out.append("M" + "".join(f" {f}:{c}" for f, c in t.missing(key, 4, 2)))
	out.append(f"E {t.expire(now)}")
	if now % 5 == 0:
		key = r_converse.same_message_table.key(now % 2 + 1,
		                                        bytes([now % 3 + 1]) * 8)
		out.append(f"F {t.forget(key)}")
	else:
		out.append("F -")
print("\n".join(out))
"""

DIFF_RS = r"""
#![allow(unused_attributes)]
#[allow(dead_code)] mod situ_rt;
#[allow(dead_code)] mod r;
#[allow(dead_code)] mod r_converse;
#[allow(dead_code)] mod r_reassemble;
use std::io::{Read, Write};
fn main() {
	let mut data = Vec::new();
	std::io::stdin().read_to_end(&mut data).unwrap();
	let mut t = Box::new(r_reassemble::PiecesTable::new());
	let mut out = String::new();
	let mut at = 0usize;
	while at + 6 <= data.len() {
		let n = (data[at] as usize) << 8 | data[at + 1] as usize;
		let now = u32::from_be_bytes([data[at + 2], data[at + 3], data[at + 4], data[at + 5]]);
		let frame = data[at + 6..at + 6 + n].to_vec();
		at += 6 + n;
		let v = r::PieceFrame::new(&frame).unwrap();
		match t.add(&v, now) {
			Ok(Some(m)) => {
				out.push_str("A O ");
				for b in m { out.push_str(&format!("{:02x}", b)); }
				out.push('\n');
			}
			Ok(None) => out.push_str("A T\n"),
			Err(situ_rt::Error::Constraint) => out.push_str("A C\n"),
			Err(situ_rt::Error::Bounds) => out.push_str("A B\n"),
			Err(_) => out.push_str("A ?\n"),
		}
		for who in 1u8..=3 {
			for mid in 1u64..=2 {
				let key = r_converse::SameMessageTable::key(mid, &[who; 8]);
				let (mut first, mut count) = ([0u32; 4], [0u32; 4]);
				let k = t.missing(&key, &mut first, &mut count, 2);
				out.push('M');
				for i in 0..k { out.push_str(&format!(" {}:{}", first[i], count[i])); }
				out.push('\n');
			}
		}
		out.push_str(&format!("E {}\n", t.expire(now)));
		if now % 5 == 0 {
			let key = r_converse::SameMessageTable::key((now % 2 + 1) as u64,
				&[(now % 3 + 1) as u8; 8]);
			out.push_str(&format!("F {}\n", t.forget(&key) as u32));
		} else {
			out.push_str("F -\n");
		}
	}
	std::io::stdout().write_all(out.as_bytes()).unwrap();
}
"""

DIFF_CPP = r"""
#include <cstdio>
#include <cstring>
#include <vector>
#include "r_reassemble.hpp"
#include "r_converse.hpp"
int main()
{
	std::vector<std::uint8_t> data;
	int c;
	while ((c = std::getchar()) != EOF)
		data.push_back(static_cast<std::uint8_t>(c));
	auto *t = new ::situ::pieces_table();
	std::size_t at = 0;
	while (at + 6u <= data.size()) {
		const std::uint32_t n = static_cast<std::uint32_t>(data[at] << 8 | data[at + 1]);
		const std::uint32_t now = static_cast<std::uint32_t>(data[at + 2]) << 24
		    | static_cast<std::uint32_t>(data[at + 3]) << 16
		    | static_cast<std::uint32_t>(data[at + 4]) << 8 | data[at + 5];
		std::vector<std::uint8_t> frame(data.begin() + static_cast<long>(at + 6),
		                                data.begin() + static_cast<long>(at + 6 + n));
		at += 6u + n;
		::situ::rt::message m(frame.data(), n);
		::situ::piece_frame v;
		if (::situ::piece_frame::at(m, 0u, n, v) != ::situ::rt::err::ok)
			return 3;
		::situ::rt::const_bytes out;
		const ::situ::rt::err e = t->add(v, now, out);
		std::printf("A %c", e == ::situ::rt::err::ok ? 'O'
		            : e == ::situ::rt::err::truncated ? 'T'
		            : e == ::situ::rt::err::constraint ? 'C'
		            : e == ::situ::rt::err::bounds ? 'B' : '?');
		if (e == ::situ::rt::err::ok) {
			std::putchar(' ');
			for (std::size_t i = 0; i < out.size(); i++)
				std::printf("%02x", out[i]);
		}
		std::putchar('\n');
		for (unsigned who = 1u; who <= 3u; who++) {
			for (unsigned mid = 1u; mid <= 2u; mid++) {
				std::uint8_t sender[8], key[12];
				std::uint32_t first[4], count[4];
				std::memset(sender, static_cast<int>(who), 8);
				::situ::same_message_table::key(mid, sender, key);
				const std::uint32_t k = t->missing(key, first, count, 4u, 2u);
				std::printf("M");
				for (std::uint32_t i = 0u; i < k; i++)
					std::printf(" %u:%u", first[i], count[i]);
				std::putchar('\n');
			}
		}
		std::printf("E %u\n", t->expire(now));
		if (now % 5u == 0u) {
			std::uint8_t sender[8], key[12];
			std::memset(sender, static_cast<int>(now % 3u + 1u), 8);
			::situ::same_message_table::key(now % 2u + 1u, sender, key);
			std::printf("F %u\n", t->forget(key));
		} else {
			std::printf("F -\n");
		}
	}
	delete t;
	return 0;
}
"""


#: The C answers of the plain and the capped runs, in that order, so the
#: second can show the cap changed something.
_CAPPED_AND_NOT: list[str] = []

#: SMALL with every bound doubled, for a table given SMALL's at init.
BOUNDED = SMALL.replace("messages = 2", "messages = 4").replace(
	"bytes = 64", "bytes = 256").replace("timeout = 3000", "timeout = 6000")

#: Where each driver makes its table, and the same at SMALL's bounds.
_AT_INIT = {
	"c": ("situ_pieces_init(&t, &store);",
	      "if (situ_pieces_init_in(&t, 2u, 32u, 3000u, store.slot,"
	      " store.bytes, store.marks) != SITU_OK) return 2;"),
	"python": ("t = r_reassemble.pieces_table()",
	           "t = r_reassemble.pieces_table(2, 32, 3000)"),
	"rust": ("Box::new(r_reassemble::PiecesTable::new())",
	         "Box::new(r_reassemble::PiecesTable::with_bounds(2, 32, 3000)"
	         ".unwrap())"),
	"cpp": ("auto *t = new ::situ::pieces_table();",
	        "auto *t = new ::situ::pieces_table();\n\tif (!::situ::rt::ok("
	        "t->bounds(2u, 32u, 3000u))) return 2;"),
}

#: The plain run's C answers, which the bounded run must repeat.
_PLAIN: list[str] = []


def _driver(source: str, backend: str, schema: str) -> str:
	if schema is not BOUNDED:
		return source
	old, new = _AT_INIT[backend]
	assert source.count(old) == 1, (backend, old)
	return source.replace(old, new)


def _adversarial_stream(seed: int, steps: int,
		expiry: bool = False) -> bytes:
	"""Pieces meant to collide: three senders, two message numbers, counts
	that disagree with their message's, lengths off the stride and empty
	ones, a count past a slot, and a clock starting just short of 2^32 so
	it wraps under the deadlines."""
	import random
	rnd = random.Random(seed)
	plans: dict[tuple[int, int], tuple[int, int]] = {}
	now = 0xFFFFFFFF - 20000
	out = b""
	for _ in range(steps):
		who, mid = rnd.randint(1, 3), rnd.randint(1, 2)
		count, stride = plans.setdefault((who, mid), (rnd.choice(
			[1, 2, 3, 4, 5, 6]), rnd.randint(1, 10)))
		if rnd.random() < 0.05:
			plans.pop((who, mid))
		# The schema holds `index` under `chunks`, so a piece past its own
		# count never reaches a table; one past its MESSAGE's count does.
		if rnd.random() < 0.1:
			count = rnd.choice([1, 7, 40, count + 1])
		index = rnd.randrange(count)
		length = (stride if index < count - 1 else rnd.randint(0, stride))
		if rnd.random() < 0.1:
			length = rnd.randint(0, 14)
		body = bytes(rnd.getrandbits(8) for _ in range(length))
		now = (now + rnd.choice([0, 0, 1, 50, 400, 1200])) & 0xFFFFFFFF
		# A piece's own deadline: sometimes past already, sometimes before
		# the table's timeout, sometimes after it, across the wrap.
		until = ((now + rnd.choice([-5, 0, 3, 200, 1000, 2500, 9000]))
		         & 0xFFFFFFFF).to_bytes(4, "big") if expiry else b""
		frame = (bytes([who]) * 8 + mid.to_bytes(4, "big")
		         + index.to_bytes(2, "big") + count.to_bytes(2, "big")
		         + until + length.to_bytes(2, "big") + body)
		out += len(frame).to_bytes(2, "big") + now.to_bytes(4, "big") + frame
	return out


@pytest.mark.skipif(COMPILER is None or CXX is None or RUSTC is None,
                    reason="needs C, C++ and Rust")
@pytest.mark.parametrize("schema", [
	SMALL,
	# `per` (fuzznet, inbox 20261122T1804Z): one sender holds one message
	# at a time, its DoS bound.
	SMALL.replace("messages = 2", "per = f.h.sender, per_max = 1, "
	              "messages = 2"),
	# `expires` (the same message): a piece's own deadline bounds its
	# slot's, and a piece already past it is refused.
	SMALL.replace("\tu16  chunks;\n", "\tu16  chunks;\n\tu32  until;\n")
	     .replace("messages = 2", "expires = f.h.until, messages = 2"),
	# Bounds at init (the same message, item 5): twice the schema's
	# numbers as maxima, SMALL's given at init -- the answers must be
	# SMALL's plain run's, byte for byte.
	BOUNDED,
], ids=["plain", "per", "expires", "bounded"])
def test_four_tables_answer_alike_under_a_hostile_stream(tmp_path: Path,
		schema: str) -> None:
	"""The four tables were written from one set of rules and held to
	valid, shuffled messages; a piece saying another count got past all
	four alike. So: one stream of colliding, malformed and late pieces,
	every answer of every backend compared -- what `add` said and gave,
	what `missing` lists for every key, what `expire` dropped, and what
	`forget` found -- with a `per` cap and without."""
	(tmp_path / "r.situ").write_text(schema, encoding="ascii")
	runtime = ROOT / "runtime"
	for target in ("c", "python", "rust", "cpp"):
		done = subprocess.run(
			[sys.executable, "-m", "situc", "build", str(tmp_path / "r.situ"),
			 "--target", target, "--layer", "converse", "--out",
			 str(tmp_path / target)], cwd=ROOT, capture_output=True, text=True)
		assert done.returncode == 0, done.stderr
	stream = _adversarial_stream(1, 3000, expiry="expires =" in schema)
	said: dict[str, str] = {}

	(tmp_path / "d.c").write_text(_driver(DIFF_C, "c", schema),
	                              encoding="ascii")
	built = subprocess.run(
		[COMPILER or "cc", "-std=c11", "-g", "-fsanitize=address,undefined",
		 "-fno-sanitize-recover=all", f"-I{runtime / 'c'}",
		 f"-I{tmp_path / 'c'}", str(tmp_path / "d.c"),
		 str(tmp_path / "c" / "r.c"), str(runtime / "c" / "situ.c"), "-o",
		 str(tmp_path / "dc")], capture_output=True, text=True)
	assert built.returncode == 0, built.stderr
	(tmp_path / "d.cpp").write_text(_driver(DIFF_CPP, "cpp", schema),
	                                encoding="ascii")
	built = subprocess.run(
		[CXX or "c++", "-std=c++17", "-g", "-fsanitize=address,undefined",
		 "-fno-sanitize-recover=all", f"-I{runtime / 'c'}",
		 f"-I{runtime / 'cpp'}", f"-I{tmp_path / 'cpp'}",
		 str(tmp_path / "d.cpp"), str(runtime / "c" / "situ.c"), "-o",
		 str(tmp_path / "dcpp")], capture_output=True, text=True)
	assert built.returncode == 0, built.stderr
	out = tmp_path / "rust"
	(out / "situ_rt.rs").write_text(
		(runtime / "rust" / "situ_rt.rs").read_text(encoding="ascii"),
		encoding="ascii")
	(out / "main.rs").write_text(_driver(DIFF_RS, "rust", schema),
	                             encoding="ascii")
	built = subprocess.run(
		[RUSTC or "rustc", "--edition", "2021", "-o", str(tmp_path / "drs"),
		 str(out / "main.rs")], capture_output=True, text=True)
	assert built.returncode == 0, built.stderr

	for name, command, where in (
			("c", [str(tmp_path / "dc")], tmp_path),
			("cpp", [str(tmp_path / "dcpp")], tmp_path),
			("rust", [str(tmp_path / "drs")], tmp_path),
			("python", [sys.executable, "-c", _driver(DIFF_PY, "python",
			                                         schema).replace(
				"RUNTIME", repr(str(runtime / "python")))], tmp_path / "python")):
		ran = subprocess.run(command, input=stream, cwd=where,
		                     capture_output=True, timeout=120)
		assert ran.returncode == 0, (name, ran.stderr[-2000:])
		said[name] = ran.stdout.decode("ascii")

	lines = said["c"].splitlines()
	assert len(lines) == 3000 * 9
	# `forget` (fuzznet, inbox 20261122T1804Z): a held message dropped by
	# its key, and a key held by nothing, both met in the stream.
	assert "F 1" in lines and "F 0" in lines
	kinds = {line[:3] for line in lines if line.startswith("A ")}
	assert kinds == {"A O", "A T", "A C", "A B"}, kinds
	assert any(line.startswith("E ") and line != "E 0" for line in lines)
	if schema is SMALL:
		_PLAIN.append(said["c"])
	if schema is BOUNDED and _PLAIN:
		assert said["c"] == _PLAIN[0], "the bounds given at init are not SMALL's"
	if "per_max" in schema:
		# The cap bites: a second message from one sender is refused where
		# a free slot would have taken it. Measured against the uncapped
		# table's answers over the same stream, which it must change.
		_CAPPED_AND_NOT.append(said["c"])
	elif schema is SMALL:
		_CAPPED_AND_NOT.insert(0, said["c"])
	if len(_CAPPED_AND_NOT) == 2:
		assert _CAPPED_AND_NOT[0] != _CAPPED_AND_NOT[1]
	for name in ("cpp", "rust", "python"):
		other = said[name].splitlines()
		first = next((at for at, (a, b) in enumerate(zip(lines, other))
		              if a != b), None)
		assert first is None and len(other) == len(lines), \
			(name, first, lines[first - 8:first + 1] if first else None,
			 other[first - 8:first + 1] if first else None)


EXPIRING = SCHEMA.replace("\tu16  chunks;\n", "\tu16  chunks;\n\tu32  until;\n") \
	.replace("messages = 4", "expires = f.h.until, messages = 4")

EXPIRING_PY = r"""
import sys
sys.path[:0] = [".", RUNTIME]
from situ_runtime import Message, BoundsError
import r, r_reassemble

def frame(ix, n, until, body):
	b = (bytes([7]) * 8 + (1).to_bytes(4, "big") + ix.to_bytes(2, "big")
	     + n.to_bytes(2, "big") + until.to_bytes(4, "big")
	     + len(body).to_bytes(2, "big") + body)
	return r.piece_frame.at(Message(bytearray(b)), 0, len(b))

t = r_reassemble.pieces_table()
try:
	t.add(frame(0, 2, 100, b"ab"), 100)
	raise SystemExit("a piece at its own deadline was taken")
except BoundsError:
	pass
assert t.add(frame(0, 2, 150, b"ab"), 100) is None
# Its deadline, 150, is the slot's: earlier than 100 + the timeout.
assert t.expire(149) == 0 and t.expire(150) == 1
# A later piece stating an earlier deadline pulls the slot's in: held
# from 200 with the table's timeout it would go at 3200, and goes at 300.
assert t.add(frame(0, 3, 5000, b"ab"), 200) is None
assert t.add(frame(1, 3, 300, b"cd"), 210) is None
assert t.expire(299) == 0 and t.expire(300) == 1
print("python ok")
"""


def test_a_piece_s_own_deadline_bounds_its_slot(tmp_path: Path) -> None:
	"""`expires` (fuzznet, inbox 20261122T1804Z): a piece already at its
	deadline is refused, and a slot's deadline is the earliest any of its
	pieces states, so it expires before the table's own timeout. The four
	tables agree on it in the hostile stream; this says what it is."""
	(tmp_path / "r.situ").write_text(EXPIRING, encoding="ascii")
	out = tmp_path / "py"
	done = subprocess.run(
		[sys.executable, "-m", "situc", "build", str(tmp_path / "r.situ"),
		 "--target", "python", "--layer", "converse", "--out", str(out)],
		cwd=ROOT, capture_output=True, text=True)
	assert done.returncode == 0, done.stderr
	ran = subprocess.run(
		[sys.executable, "-c", EXPIRING_PY.replace(
			"RUNTIME", repr(str(ROOT / "runtime" / "python")))],
		cwd=out, capture_output=True, text=True, timeout=120)
	assert ran.stdout.strip() == "python ok", ran.stderr


PARTS_C = PROGRAM.split("static situ_pieces_t t;")[0] + r"""
static situ_pieces_t t;
static situ_pieces_store_t store;

int main(void)
{
	int fail = 0;
	const uint8_t *out = NULL;
	uint32_t len = 0;
	uint8_t key[14], sender[8];
	situ_pieces_init(&t, &store);
	memset(sender, 7, sizeof sender);
	situ_conv_same_message_key(1u, 3u, sender, key);
	/* The first piece opened by the caller, the second read by the
	 * table, the third opened again: one message, keyed alike. */
	CHECK(situ_pieces_add_parts(&t, key, 0u, 3u, (const uint8_t *)"ab", 2u,
	                            10u, &out, &len) == SITU_ERR_TRUNCATED);
	situ_view_t v = frame(7, 1u, 1u, 3u, "cd", 2u);
	CHECK(situ_pieces_add(&t, v, 11u, &out, &len) == SITU_ERR_TRUNCATED);
	CHECK(situ_pieces_add_parts(&t, key, 2u, 3u, (const uint8_t *)"e", 1u,
	                            12u, &out, &len) == SITU_OK);
	CHECK(len == 5u && memcmp(out, "abcde", 5u) == 0);
	/* What `_add` refuses, `_add_parts` refuses the same. */
	CHECK(situ_pieces_add_parts(&t, key, 3u, 3u, (const uint8_t *)"x", 1u,
	                            13u, &out, &len) == SITU_ERR_CONSTRAINT);
	/* Bounds at init: the schema's are maxima, and zero is no bound. */
	CHECK(situ_pieces_init_in(&t, 5u, 1024u, 3000u, store.slot, store.bytes,
	                          store.marks) == SITU_ERR_BOUNDS);
	CHECK(situ_pieces_init_in(&t, 4u, 1025u, 3000u, store.slot, store.bytes,
	                          store.marks) == SITU_ERR_BOUNDS);
	CHECK(situ_pieces_init_in(&t, 4u, 1024u, 0u, store.slot, store.bytes,
	                          store.marks) == SITU_ERR_BOUNDS);
	/* One slot of eight bytes, in the caller's own small memory. */
	{
		situ_pieces_slot_t one[1];
		uint8_t bytes[SITU_PIECES_BYTES_FOR(1u, 8u)];
		uint8_t marks[SITU_PIECES_MARKS_FOR(1u, 8u)];
		CHECK(situ_pieces_init_in(&t, 1u, 8u, 100u, one, bytes, marks) == SITU_OK);
		CHECK(situ_pieces_add_parts(&t, key, 0u, 2u, (const uint8_t *)"abcd",
		                            4u, 20u, &out, &len) == SITU_ERR_TRUNCATED);
		key[0] ^= 1u;
		CHECK(situ_pieces_add_parts(&t, key, 0u, 2u, (const uint8_t *)"abcd",
		                            4u, 21u, &out, &len) == SITU_ERR_BOUNDS);
		key[0] ^= 1u;
		CHECK(situ_pieces_add_parts(&t, key, 1u, 2u, (const uint8_t *)"efghi",
		                            5u, 22u, &out, &len) == SITU_ERR_CONSTRAINT);
		CHECK(situ_pieces_add_parts(&t, key, 1u, 2u, (const uint8_t *)"ef",
		                            2u, 22u, &out, &len) == SITU_OK);
		CHECK(len == 6u && memcmp(out, "abcdef", 6u) == 0);
		CHECK(situ_pieces_add_parts(&t, key, 0u, 2u, (const uint8_t *)"abcde",
		                            5u, 30u, &out, &len) == SITU_ERR_BOUNDS);
		CHECK(situ_pieces_add_parts(&t, key, 0u, 1u, (const uint8_t *)"x",
		                            1u, 40u, &out, &len) == SITU_OK);
		CHECK(situ_pieces_expire(&t, 140u) == 0u);
	}
	printf(fail ? "FAILED\n" : "PARTS PASS\n");
	return fail;
}
"""


PARTS_PY = r"""
import sys
sys.path[:0] = [".", RUNTIME]
from situ_runtime import Message, BoundsError, ConstraintError
import r, r_converse, r_reassemble

def frame(ix, n, body):
	b = (bytes([7]) * 8 + (1).to_bytes(4, "big") + ix.to_bytes(2, "big")
	     + n.to_bytes(2, "big") + len(body).to_bytes(2, "big") + body)
	return r.piece_frame.at(Message(bytearray(b)), 0, len(b))

key = r_converse.same_message_table.key(1, 3, bytes([7]) * 8)
t = r_reassemble.pieces_table()
assert t.add_parts(key, 0, 3, b"ab", 10) is None
assert t.add(frame(1, 3, b"cd"), 11) is None
assert t.add_parts(key, 2, 3, b"e", 12) == b"abcde"
try:
	t.add_parts(key, 3, 3, b"x", 13)
	raise SystemExit("an index past its count was taken")
except ConstraintError:
	pass
for bounds in ((5, 1024, 3000), (4, 1025, 3000), (4, 1024, 0)):
	try:
		r_reassemble.pieces_table(*bounds)
		raise SystemExit(f"bounds {bounds} were taken")
	except BoundsError:
		pass
small = r_reassemble.pieces_table(1, 8, 100)
assert small.add_parts(key, 0, 2, b"abcd", 20) is None
try:
	small.add_parts(key[:-1] + b"\x00", 0, 2, b"abcd", 21)
	raise SystemExit("a second message took the one slot")
except BoundsError:
	pass
assert small.add_parts(key, 1, 2, b"ef", 22) == b"abcdef"
try:
	small.add_parts(key, 0, 2, b"abcde", 30)
	raise SystemExit("a message past eight bytes was taken")
except BoundsError:
	pass
print("python ok")
"""


def test_a_piece_opened_by_the_caller_joins_its_message(tmp_path: Path) -> None:
	"""`_add_parts` (fuzznet, inbox 20261122T1804Z): a caller that opens a
	piece itself hands the table the key, index, count and bytes. Pieces
	of one message through `_add` and `_add_parts` meet in one slot, so
	the conversation table's key is the reassembly table's."""
	assert "PARTS PASS" in _run(tmp_path, SCHEMA, PARTS_C.replace(
		'#include "r_reassemble.h"',
		'#include "r_converse.h"\n#include "r_reassemble.h"'), "r",
		sanitize=True)
	out = tmp_path / "py"
	done = subprocess.run(
		[sys.executable, "-m", "situc", "build", str(tmp_path / "r.situ"),
		 "--target", "python", "--layer", "converse", "--out", str(out)],
		cwd=ROOT, capture_output=True, text=True)
	assert done.returncode == 0, done.stderr
	ran = subprocess.run(
		[sys.executable, "-c", PARTS_PY.replace(
			"RUNTIME", repr(str(ROOT / "runtime" / "python")))],
		cwd=out, capture_output=True, text=True, timeout=120)
	assert ran.stdout.strip() == "python ok", ran.stderr
