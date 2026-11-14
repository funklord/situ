"""A builder writes a byte run its expression sizes, and checks it after
(26.1016).

RTP's `padding[padded_flag * last]` multiplies two fields, the second
being the packet's own last byte, so no field can be solved for the
caller's bytes -- and none needs to be: the caller states the flag and the
bytes, and once the message is whole the reader's `_len` says how long they
have to be. A disagreement is refused. ID3's synchsafe frame size and
QUIC's `length - (number_length + 1)` are the same shape and were refused
the same way. All four now (the other three since 26.1019).

The work also found the counted value array refusing a null pointer at a
count of zero: RTP's CSRC list, which is empty in nearly every packet.

Held to RFC 3550 section 5.1, ID3v2.4.0 structure section 4, and RFC 9000
section 17.2.2.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from test_an_arm_sized_by_its_argument_is_written import (COMPILER, ROOT,
                                                          _built, _run)

HOST_CXX = shutil.which("g++") or shutil.which("c++")
RUSTC    = shutil.which("rustc")


@pytest.mark.skipif(COMPILER is None, reason = "needs a C compiler")
def test_an_rtp_packet_with_and_without_padding(tmp_path: Path) -> None:
	"""V=2, PT 96, sequence 1, timestamp 160, SSRC 0x12345678, no CSRC and
	no extension, payload `abcd`; then the same padded by three bytes, the
	last of which counts the padding including itself."""
	_run(tmp_path, _built(tmp_path, "rtp"), "rtp", """
	static const uint8_t plain[16] = {
		0x80, 0x60, 0, 1, 0, 0, 0, 0xa0, 0x12, 0x34, 0x56, 0x78,
		'a', 'b', 'c', 'd' };
	static const uint8_t pad3[3] = { 0, 0, 3 }, pad2[3] = { 0, 0, 2 },
	                     pad5[3] = { 0, 0, 5 };
	static const uint8_t payload[4] = { 'a', 'b', 'c', 'd' };
	uint8_t  buf[64];
	uint32_t wrote = 0;

	if (situ_rtp_build_none(buf, sizeof buf, 2u, 0u, 0u, 96u, 1u, 160u,
	                        0x12345678u, NULL, 0u, NULL, 0u, payload, 4u,
	                        NULL, 0u, &wrote) != SITU_OK
	    || wrote != 16u || memcmp(buf, plain, 16u) != 0) {
		return 1;
	}
	if (situ_rtp_build_none(buf, sizeof buf, 2u, 1u, 0u, 96u, 1u, 160u,
	                        0x12345678u, NULL, 0u, NULL, 0u, payload, 4u,
	                        pad3, 3u, &wrote) != SITU_OK
	    || wrote != 19u || buf[0] != 0xa0
	    || memcmp(buf + 1, plain + 1, 15u) != 0
	    || memcmp(buf + 16, pad3, 3u) != 0) {
		return 2;
	}
	/* A last byte saying two over three bytes of padding. */
	if (situ_rtp_build_none(buf, sizeof buf, 2u, 1u, 0u, 96u, 1u, 160u,
	                        0x12345678u, NULL, 0u, NULL, 0u, payload, 4u,
	                        pad2, 3u, &wrote) != SITU_ERR_CONSTRAINT) {
		return 3;
	}
	/* And saying five: the reader's length stops at the bytes there are,
	 * so this one is caught as the message running short. */
	if (situ_rtp_build_none(buf, sizeof buf, 2u, 1u, 0u, 96u, 1u, 160u,
	                        0x12345678u, NULL, 0u, NULL, 0u, payload, 4u,
	                        pad5, 3u, &wrote) != SITU_ERR_CONSTRAINT) {
		return 6;
	}
	/* Padding with P clear: the reader would read none. */
	if (situ_rtp_build_none(buf, sizeof buf, 2u, 0u, 0u, 96u, 1u, 160u,
	                        0x12345678u, NULL, 0u, NULL, 0u, payload, 4u,
	                        pad3, 3u, &wrote) != SITU_ERR_CONSTRAINT) {
		return 4;
	}
	/* A count of one with no array is still refused. */
	if (situ_rtp_build_none(buf, sizeof buf, 2u, 0u, 0u, 96u, 1u, 160u,
	                        0x12345678u, NULL, 1u, NULL, 0u, payload, 4u,
	                        NULL, 0u, &wrote) != SITU_ERR_BOUNDS) {
		return 5;
	}
""")


@pytest.mark.skipif(COMPILER is None, reason = "needs a C compiler")
def test_an_id3_text_frame(tmp_path: Path) -> None:
	"""TIT2, three bytes of content -- encoding 3 (UTF-8) and `Hi` -- with
	a synchsafe size of 3 and no flags; a size of 4 over them refused."""
	_run(tmp_path, _built(tmp_path, "id3"), "id3", """
	static const uint8_t content[3] = { 3, 'H', 'i' };
	static const uint8_t want[13] = {
		'T', 'I', 'T', '2', 0, 0, 0, 3, 0, 0, 3, 'H', 'i' };
	uint8_t  buf[32];
	uint32_t wrote = 0;

	if (situ_id3_frame_build(buf, sizeof buf, 'T', 'I', 'T', '2',
	                         0u, 0u, 0u, 3u, 0u, 0u, 0u, 0u, 0u, 0u, 0u, 0u,
	                         content, 3u, &wrote) != SITU_OK
	    || wrote != 13u || memcmp(buf, want, 13u) != 0) {
		return 1;
	}
	if (situ_id3_frame_build(buf, sizeof buf, 'T', 'I', 'T', '2',
	                         0u, 0u, 0u, 4u, 0u, 0u, 0u, 0u, 0u, 0u, 0u, 0u,
	                         content, 3u, &wrote) != SITU_ERR_CONSTRAINT) {
		return 2;
	}
""")


@pytest.mark.skipif(COMPILER is None, reason = "needs a C compiler")
def test_a_quic_initial(tmp_path: Path) -> None:
	"""Version 1, an eight-byte destination ID, no source ID or token, a
	one-byte packet number and sixteen bytes of payload: Length is 17, one
	varint byte. Length 18 over the same bytes refused."""
	_run(tmp_path, _built(tmp_path, "quic"), "quic", """
	static const uint8_t dcid[8] = { 1, 2, 3, 4, 5, 6, 7, 8 };
	static const uint8_t number[1] = { 0x2a };
	uint8_t  payload[16];
	uint8_t  want[34];
	uint8_t  buf[64];
	uint32_t wrote = 0;

	memset(payload, 0x55, sizeof payload);
	want[0] = 0xc0;
	want[1] = 0; want[2] = 0; want[3] = 0; want[4] = 1;
	want[5] = 8;
	memcpy(want + 6, dcid, 8u);
	want[14] = 0; want[15] = 0; want[16] = 17; want[17] = 0x2a;
	memcpy(want + 18, payload, 16u);
	if (situ_initial_build(buf, sizeof buf, 1u, 1u, 0u, 0u, 1u, dcid, 8u,
	                       NULL, 0u, NULL, 0u, 17u, number, 1u,
	                       payload, 16u, &wrote) != SITU_OK
	    || wrote != 34u || memcmp(buf, want, 34u) != 0) {
		return 1;
	}
	if (situ_initial_build(buf, sizeof buf, 1u, 1u, 0u, 0u, 1u, dcid, 8u,
	                       NULL, 0u, NULL, 0u, 18u, number, 1u,
	                       payload, 16u, &wrote) != SITU_ERR_CONSTRAINT) {
		return 2;
	}
""")




#: RTP's five cases: (P, padding, what comes back). `constraint` is the
#: caller's length disagreeing with their bytes, however it is caught.
PLAIN  = "80600001000000a012345678" + b"abcd".hex()
PADDED = "a" + PLAIN[1:] + "000003"
CASES  = [(0, b"", PLAIN), (1, b"\0\0\3", PADDED),
          (1, b"\0\0\2", "constraint"), (1, b"\0\0\5", "constraint"),
          (0, b"\0\0\3", "constraint")]


def _said(lines: str) -> None:
	assert lines.split("\n")[:len(CASES)] == [want for _, _, want in CASES], \
		lines


def _bytes(value: bytes) -> str:
	return ", ".join(f"0x{one:02x}" for one in value) or "0"


def test_python_checks_it_too(tmp_path: Path) -> None:
	"""26.1019: they built from C's plan and wrote the span unchecked --
	Python returned a packet for a last byte of 2 over three bytes of
	padding, and for padding with P clear. Each checks it as C does."""
	_emitted(tmp_path, "python")
	(tmp_path / "situ_runtime.py").write_text(
		(ROOT / "runtime" / "python" / "situ_runtime.py").read_text(
			encoding = "ascii"), encoding = "ascii")
	(tmp_path / "run.py").write_text(f"""import sys
sys.path.insert(0, ".")
import rtp_build
from situ_runtime import ConstraintError
for flag, pad in {[(flag, pad) for flag, pad, _ in CASES]!r}:
	out = bytearray(64)
	try:
		n = rtp_build.build_rtp_none(out, 2, flag, 0, 96, 1, 160, 0x12345678,
		                             [], b"", b"abcd", pad)
		print(bytes(out[:n]).hex())
	except ConstraintError:
		print("constraint")
""", encoding = "ascii")
	ran = subprocess.run([sys.executable, "-I", "run.py"], cwd = tmp_path,
	                     capture_output = True, text = True)
	assert ran.returncode == 0, ran.stderr
	_said(ran.stdout)


@pytest.mark.skipif(HOST_CXX is None, reason = "needs a C++ compiler")
def test_cpp_checks_it_too(tmp_path: Path) -> None:
	"""And C++, whose counted value array also refused an empty CSRC list
	given as a null pointer, as C's did."""
	_emitted(tmp_path, "cpp")
	calls = "".join(f"""
	{{
		static const std::uint8_t pad[] = {{ {_bytes(pad)} }};
		show(::situ_build::rtp_none(buf, sizeof buf, 2u, {flag}u, 0u, 96u, 1u,
		     160u, 0x12345678u, nullptr, 0u, nullptr, 0u, payload, 4u, pad,
		     {len(pad)}u, n));
	}}""" for flag, pad, _ in CASES)
	(tmp_path / "main.cpp").write_text(f"""#include <cstdio>
#include "rtp_build.hpp"
static std::uint8_t buf[64];
static std::uint32_t n;
static void show(::situ::rt::err e)
{{
	if (e == ::situ::rt::err::constraint) {{
		std::puts("constraint");
		return;
	}}
	if (e != ::situ::rt::err::ok) {{
		std::puts("other");
		return;
	}}
	for (std::uint32_t i = 0; i < n; i++) std::printf("%02x", buf[i]);
	std::puts("");
}}
int main()
{{
	static const std::uint8_t payload[4] = {{ 'a', 'b', 'c', 'd' }};
{calls}
	return 0;
}}
""", encoding = "ascii")
	built = subprocess.run(
		[HOST_CXX or "c++", "-std=c++17", "-Wall", "-Wextra", "-Werror",
		 f"-I{ROOT / 'runtime' / 'c'}", f"-I{ROOT / 'runtime' / 'cpp'}",
		 f"-I{tmp_path}", str(tmp_path / "main.cpp"),
		 str(ROOT / "runtime" / "c" / "situ.c"), "-o", str(tmp_path / "run")],
		capture_output = True, text = True)
	assert built.returncode == 0, built.stderr
	ran = subprocess.run([str(tmp_path / "run")], capture_output = True,
	                     text = True)
	assert ran.returncode == 0, ran.stderr
	_said(ran.stdout)


@pytest.mark.skipif(RUSTC is None, reason = "needs rustc")
def test_rust_checks_it_too(tmp_path: Path) -> None:
	_emitted(tmp_path, "rust")
	(tmp_path / "situ_rt.rs").write_text(
		(ROOT / "runtime" / "rust" / "situ_rt.rs").read_text(
			encoding = "ascii").replace("#![no_std]\n", ""),
		encoding = "ascii")
	calls = "".join(f"""
	let got = rtp_build::build_rtp_none(&mut buf, 2, {flag}, 0, 96, 1, 160,
		0x12345678, &[], &[], b"abcd", &[{_bytes(pad) if pad else ''}]);
	show(&buf, got);"""
		for flag, pad, _ in CASES)
	(tmp_path / "main.rs").write_text(f"""mod situ_rt;
#[allow(dead_code)]
mod rtp;
#[allow(dead_code)]
mod rtp_build;
fn show(buf: &[u8], got: situ_rt::Result<usize>) {{
	match got {{
		Ok(n) => {{
			for b in &buf[..n] {{ print!("{{:02x}}", b); }}
			println!();
		}}
		Err(situ_rt::Error::Constraint) => println!("constraint"),
		Err(_) => println!("other"),
	}}
}}
fn main() {{
	let mut buf = [0u8; 64];{calls}
}}
""", encoding = "ascii")
	built = subprocess.run(
		[RUSTC or "rustc", "--edition", "2021", "-D", "warnings",
		 "-A", "dead_code", "main.rs", "-o", "run"],
		cwd = tmp_path, capture_output = True, text = True)
	assert built.returncode == 0, built.stderr
	ran = subprocess.run([str(tmp_path / "run")], capture_output = True,
	                     text = True)
	assert ran.returncode == 0, ran.stderr
	_said(ran.stdout)


def _emitted(out: Path, target: str) -> None:
	done = subprocess.run(
		[sys.executable, "-m", "situc", "build",
		 str(ROOT / "example" / "rtp" / "rtp.situ"), "--target", target,
		 "--layer", "edit", "--out", str(out)],
		cwd = ROOT, capture_output = True, text = True)
	assert done.returncode == 0, done.stderr
	assert "no builder for `rtp.none`" not in done.stderr, done.stderr
