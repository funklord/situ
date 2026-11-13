"""Rung 6's deadlines are compared modulo 2^32 (26.1008).

Reported by fuzznet (`suggestion/fuzznet.md`, 2026-11-13) against the
generated DNS driver: `deadline_ms = now_ms + timeout_ms` wraps, and
`now_ms >= deadline_ms` then holds at once -- sent at `0xFFFFFFFF - 1000`,
an exchange with a 5000 ms wait spent both retries and expired one
millisecond later. A daemon passing milliseconds since boot meets it after
49.7 days. Rust's `+` panicked there instead, in a debug build.

The case here, in all four drivers: send 500 ms before the wrap with an
800 ms timeout, so the deadline is 299 after it. Steps before the deadline
neither resend nor expire, and `next` is the wrapped deadline; a step past
it resends.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from every_schema import ROOT
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
from test_relations import (COMPILER, HOST_CXX, POLICY, RUNTIME, RUSTC,
                            WARNINGS, analysed)

SENT = 0xFFFFFFFF - 500
DUE  = (SENT + 800) & 0xFFFFFFFF		# 299


@pytest.mark.skipif(COMPILER is None, reason = "no C compiler")
def test_c(tmp_path: Path) -> None:
	schema, resolved = analysed(POLICY)
	for name, text in generate_c(schema, resolved, "t").files().items():
		(tmp_path / name).write_text(text, encoding = "ascii")
	for emitted in (converse.generate(schema, resolved, "t"),
	                drive.generate(schema, resolved, "t")):
		for name, text in emitted.items():
			(tmp_path / name).write_text(text, encoding = "ascii")
	(tmp_path / "wrap.c").write_text(f"""#include <string.h>
#include "t_drive.h"

static uint32_t sent;

static situ_err_t count(void *ctx, const uint8_t *data, uint32_t len)
{{
	(void)ctx; (void)data; (void)len;
	sent++;
	return SITU_OK;
}}

int main(void)
{{
	uint8_t buf[17];
	situ_msg_t msg;
	situ_view_t view;
	situ_drive_response_to_slot_t slots[2];
	situ_conv_response_to_slot_t keys[2];
	situ_drive_response_to_t drive;
	situ_io_t io = {{ count, NULL }};
	uint32_t next = 0u, expired = 0u;

	memset(buf, 0, sizeof buf);
	situ_msg_init(&msg, buf, 17);
	if (situ_frame_view(&msg, 0u, &view) != SITU_OK) return 1;
	situ_drive_response_to_init(&drive, slots, keys, 2u, io, 800u, 3u);
	if (situ_drive_response_to_send(&drive, view, buf, 17u, 7u, {SENT}u)
	    != SITU_OK || sent != 1u) return 2;
	for (int i = 0; i < 3; i++) {{
		if (situ_drive_response_to_step(&drive, {SENT + 1}u, &next, &expired)
		    != SITU_OK || sent != 1u || expired != 0u || next != {DUE}u)
			return 3 + i;
	}}
	if (situ_drive_response_to_step(&drive, {DUE}u, &next, &expired)
	    != SITU_OK || sent != 2u || expired != 0u) return 6;
	return 0;
}}
""", encoding = "ascii")
	built = subprocess.run(
		[COMPILER or "cc", *WARNINGS, f"-I{tmp_path}", f"-I{RUNTIME}",
		 str(tmp_path / "wrap.c"), str(tmp_path / "t.c"),
		 str(RUNTIME / "situ.c"), "-o", str(tmp_path / "wrap")],
		capture_output = True, text = True)
	assert built.returncode == 0, built.stderr
	ran = subprocess.run([str(tmp_path / "wrap")], capture_output = True)
	assert ran.returncode == 0, f"check {ran.returncode} failed"


@pytest.mark.skipif(HOST_CXX is None, reason = "no C++ compiler")
def test_cpp(tmp_path: Path) -> None:
	schema, resolved = analysed(POLICY)
	for name, text in generate_cpp(schema, resolved, "t").files().items():
		(tmp_path / name).write_text(text, encoding = "ascii")
	for module in (relate_cpp, frame_cpp, converse_cpp, drive_cpp):
		for name, text in module.generate(schema, resolved, "t").items():
			(tmp_path / name).write_text(text, encoding = "ascii")
	(tmp_path / "main.cpp").write_text(f"""#include <cstdint>
#include <cstring>
#include "t_drive.hpp"

namespace {{
class counting_io : public ::situ::io {{
public:
	unsigned submissions = 0u;
	::situ::rt::err submit(const std::uint8_t *, std::uint32_t) noexcept override
	{{
		submissions++;
		return ::situ::rt::err::ok;
	}}
}};
}}

int main()
{{
	std::uint8_t bytes[::situ::frame::size_bytes];
	std::memset(bytes, 0, sizeof bytes);
	::situ::rt::message owner(bytes, ::situ::frame::size_bytes);
	::situ::frame view;
	if (::situ::frame::at(owner, 0, view) != ::situ::rt::err::ok) return 1;
	counting_io sink;
	::situ::response_to_driver::slot slots[2];
	::situ::response_to_driver driver(slots, 2u, sink, 800u, 3u);
	if (driver.send(view, bytes, ::situ::frame::size_bytes, 7u, {SENT}u)
	    != ::situ::rt::err::ok) return 2;
	std::uint32_t next = 0u, expired = 0u;
	for (int i = 0; i < 3; i++) {{
		if (driver.step({SENT + 1}u, next, expired) != ::situ::rt::err::ok
		    || sink.submissions != 1u || expired != 0u || next != {DUE}u)
			return 3 + i;
	}}
	if (driver.step({DUE}u, next, expired) != ::situ::rt::err::ok
	    || sink.submissions != 2u) return 6;
	return 0;
}}
""", encoding = "ascii")
	built = subprocess.run(
		[HOST_CXX or "c++", "-std=c++17", "-O1", f"-I{tmp_path}",
		 f"-I{ROOT / 'runtime' / 'cpp'}", f"-I{RUNTIME}",
		 str(tmp_path / "main.cpp"), str(RUNTIME / "situ.c"),
		 "-o", str(tmp_path / "run")],
		capture_output = True, text = True)
	assert built.returncode == 0, built.stderr
	ran = subprocess.run([str(tmp_path / "run")], capture_output = True)
	assert ran.returncode == 0, f"check {ran.returncode} failed"


@pytest.mark.skipif(RUSTC is None, reason = "no rustc")
def test_rust(tmp_path: Path) -> None:
	"""Built without optimisation, so overflow checks are on: the `+` that
	computed a deadline panicked at the wrap."""
	schema, resolved = analysed(POLICY)
	src = tmp_path / "src"
	src.mkdir()
	(src / "situ_rt.rs").write_text(
		(ROOT / "runtime" / "rust" / "situ_rt.rs")
		.read_text(encoding = "ascii").replace("#![no_std]\n", ""),
		encoding = "ascii")
	(src / "t.rs").write_text(
		generate_rs(schema, resolved, "t").module, encoding = "ascii")
	for module in (relate_rs, frame_rs, converse_rs, drive_rs):
		for name, text in module.generate(schema, resolved, "t").items():
			(src / name).write_text(text, encoding = "ascii")
	(src / "main.rs").write_text(f"""pub mod situ_rt;
pub mod t;
pub mod t_converse;
pub mod t_drive;
pub mod t_frame;
pub mod t_relate;

use situ_rt::Result;

struct Counting {{
	submissions: std::rc::Rc<std::cell::Cell<u32>>,
}}

impl t_drive::Io for Counting {{
	fn submit(&mut self, _data: &[u8]) -> Result<()> {{
		self.submissions.set(self.submissions.get() + 1);
		Ok(())
	}}
}}

fn main() {{
	let bytes = [0u8; t::Frame::SIZE];
	let query = t::Frame::new(&bytes).expect("a view");
	let seen = std::rc::Rc::new(std::cell::Cell::new(0u32));
	let mut slots = [t_drive::ResponseToSlot::default(); 2];
	let mut driver = t_drive::ResponseToDriver::new(
		&mut slots, Counting {{ submissions: seen.clone() }}, 800, 3);
	driver.send(&query, &bytes, 7, {SENT}).expect("a slot");
	for _ in 0..3 {{
		let (next, expired) = driver.step({SENT + 1});
		assert_eq!(next, Some({DUE}));
		assert_eq!(expired, 0);
		assert_eq!(seen.get(), 1);
	}}
	let (_, expired) = driver.step({DUE});
	assert_eq!(expired, 0);
	assert_eq!(seen.get(), 2);
}}
""", encoding = "ascii")
	built = subprocess.run(
		[RUSTC or "rustc", "--edition", "2021", "-A", "warnings",
		 "-C", "debug-assertions=on", "-C", "overflow-checks=on",
		 str(src / "main.rs"), "-o", str(tmp_path / "run")],
		capture_output = True, text = True, cwd = tmp_path)
	assert built.returncode == 0, built.stderr
	ran = subprocess.run([str(tmp_path / "run")], capture_output = True,
	                     text = True)
	assert ran.returncode == 0, ran.stderr


def test_python(tmp_path: Path) -> None:
	schema, resolved = analysed(POLICY)
	for name, text in generate_py(schema, resolved, "t").files().items():
		(tmp_path / name).write_text(text, encoding = "ascii")
	for module in (relate_py, converse_py, drive_py):
		for name, text in module.generate(schema, resolved, "t").items():
			(tmp_path / name).write_text(text, encoding = "ascii")
	(tmp_path / "situ_runtime.py").write_text(
		(ROOT / "runtime" / "python" / "situ_runtime.py").read_text(
			encoding = "ascii"), encoding = "ascii")
	(tmp_path / "run.py").write_text(f"""import sys
sys.path.insert(0, ".")
import t
import t_drive
from situ_runtime import Message

sent = []
buf = bytearray(t.frame.SIZE_BYTES)
view = t.frame(Message(buf), 0, len(buf))
driver = t_drive.response_to_driver(sent.append, 2, 800, 3)
driver.send(view, bytes(buf), 7, {SENT})
for _ in range(3):
	assert driver.step({SENT + 1}) == ({DUE}, 0), driver.step({SENT + 1})
	assert len(sent) == 1
assert driver.step({DUE})[1] == 0
assert len(sent) == 2
""", encoding = "ascii")
	ran = subprocess.run(["python3", str(tmp_path / "run.py")],
	                     capture_output = True, text = True, cwd = tmp_path)
	assert ran.returncode == 0, ran.stdout + ran.stderr
