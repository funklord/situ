"""A retransmission is told which exchange it is (26.1036).

fuzznet seals every datagram under a fresh nonce and the far end refuses a
replayed one, so a resend has to be resealed -- and `submit` was handed
bytes alone, with no way to say which exchange they were (fuzznet's gap 1,
`suggestion/fuzznet.md` 2026-11-13 and 2026-11-14). Each driver now takes
an optional `resend` that is told the exchange's id: C's `_on_resend`, a
virtual in C++'s `io` and a default method on Rust's `Io` that both fall
back to `submit`, and a keyword-only callable in Python. Without it a
retransmission is the same bytes through `submit`, as before.

The case, in all four: send exchange 7 at 0 with an 800 ms timeout; a step
at 800 retransmits it, and the hook is handed 7 and nothing reaches
`submit` a second time.
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


@pytest.mark.skipif(COMPILER is None, reason = "no C compiler")
def test_c(tmp_path: Path) -> None:
	schema, resolved = analysed(POLICY)
	for name, text in generate_c(schema, resolved, "t").files().items():
		(tmp_path / name).write_text(text, encoding = "ascii")
	for emitted in (converse.generate(schema, resolved, "t"),
	                drive.generate(schema, resolved, "t")):
		for name, text in emitted.items():
			(tmp_path / name).write_text(text, encoding = "ascii")
	(tmp_path / "resend.c").write_text("""#include <string.h>
#include "t_drive.h"

static uint32_t sent, resent, told;

static situ_err_t count(void *ctx, const uint8_t *data, uint32_t len)
{
	(void)ctx; (void)data; (void)len;
	sent++;
	return SITU_OK;
}

static situ_err_t again(void *ctx, uint32_t id, const uint8_t *data,
                        uint32_t len)
{
	(void)ctx; (void)data; (void)len;
	resent++;
	told = id;
	return SITU_OK;
}

int main(void)
{
	uint8_t buf[17];
	situ_msg_t msg;
	situ_view_t view;
	situ_drive_response_to_slot_t slots[2];
	situ_conv_response_to_slot_t keys[2];
	situ_drive_response_to_t drive;
	situ_io_t io = { count, NULL };
	uint32_t next = 0u, expired = 0u;

	memset(buf, 0, sizeof buf);
	situ_msg_init(&msg, buf, 17);
	if (situ_frame_view(&msg, 0u, &view) != SITU_OK) return 1;
	situ_drive_response_to_init(&drive, slots, keys, 2u, io, 800u, 3u);
	situ_drive_response_to_on_resend(&drive, again);
	if (situ_drive_response_to_send(&drive, view, buf, 17u, 7u, 0u)
	    != SITU_OK || sent != 1u) return 2;
	if (situ_drive_response_to_step(&drive, 800u, &next, &expired)
	    != SITU_OK || sent != 1u || resent != 1u || told != 7u) return 3;
	return 0;
}
""", encoding = "ascii")
	built = subprocess.run(
		[COMPILER or "cc", *WARNINGS, f"-I{tmp_path}", f"-I{RUNTIME}",
		 str(tmp_path / "resend.c"), str(tmp_path / "t.c"),
		 str(RUNTIME / "situ.c"), "-o", str(tmp_path / "resend")],
		capture_output = True, text = True)
	assert built.returncode == 0, built.stderr
	ran = subprocess.run([str(tmp_path / "resend")], capture_output = True)
	assert ran.returncode == 0, f"check {ran.returncode} failed"


@pytest.mark.skipif(HOST_CXX is None, reason = "no C++ compiler")
def test_cpp(tmp_path: Path) -> None:
	schema, resolved = analysed(POLICY)
	for name, text in generate_cpp(schema, resolved, "t").files().items():
		(tmp_path / name).write_text(text, encoding = "ascii")
	for module in (relate_cpp, frame_cpp, converse_cpp, drive_cpp):
		for name, text in module.generate(schema, resolved, "t").items():
			(tmp_path / name).write_text(text, encoding = "ascii")
	(tmp_path / "main.cpp").write_text("""#include <cstdint>
#include <cstring>
#include "t_drive.hpp"

namespace {
class resealing_io : public ::situ::io {
public:
	unsigned submissions = 0u, resends = 0u;
	std::uint32_t told = 0u;
	::situ::rt::err submit(const std::uint8_t *, std::uint32_t) noexcept override
	{
		submissions++;
		return ::situ::rt::err::ok;
	}
	::situ::rt::err resend(std::uint32_t id, const std::uint8_t *,
	                       std::uint32_t) noexcept override
	{
		resends++;
		told = id;
		return ::situ::rt::err::ok;
	}
};
}

int main()
{
	std::uint8_t bytes[::situ::frame::size_bytes];
	std::memset(bytes, 0, sizeof bytes);
	::situ::rt::message owner(bytes, ::situ::frame::size_bytes);
	::situ::frame view;
	if (::situ::frame::at(owner, 0, view) != ::situ::rt::err::ok) return 1;
	resealing_io sink;
	::situ::response_to_driver::slot slots[2];
	::situ::response_to_driver driver(slots, 2u, sink, 800u, 3u);
	if (driver.send(view, bytes, ::situ::frame::size_bytes, 7u, 0u)
	    != ::situ::rt::err::ok) return 2;
	std::uint32_t next = 0u, expired = 0u;
	if (driver.step(800u, next, expired) != ::situ::rt::err::ok
	    || sink.submissions != 1u || sink.resends != 1u || sink.told != 7u)
		return 3;
	return 0;
}
""", encoding = "ascii")
	built = subprocess.run(
		[HOST_CXX or "c++", "-std=c++17", "-O1", "-Wall", "-Wextra", "-Werror",
		 f"-I{tmp_path}", f"-I{ROOT / 'runtime' / 'cpp'}", f"-I{RUNTIME}",
		 str(tmp_path / "main.cpp"), str(RUNTIME / "situ.c"),
		 "-o", str(tmp_path / "run")],
		capture_output = True, text = True)
	assert built.returncode == 0, built.stderr
	ran = subprocess.run([str(tmp_path / "run")], capture_output = True)
	assert ran.returncode == 0, f"check {ran.returncode} failed"


@pytest.mark.skipif(RUSTC is None, reason = "no rustc")
def test_rust(tmp_path: Path) -> None:
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
	(src / "main.rs").write_text("""pub mod situ_rt;
pub mod t;
pub mod t_converse;
pub mod t_drive;
pub mod t_frame;
pub mod t_relate;

use situ_rt::Result;
use std::cell::Cell;
use std::rc::Rc;

struct Resealing {
	submissions: Rc<Cell<u32>>,
	told: Rc<Cell<u32>>,
}

impl t_drive::Io for Resealing {
	fn submit(&mut self, _data: &[u8]) -> Result<()> {
		self.submissions.set(self.submissions.get() + 1);
		Ok(())
	}
	fn resend(&mut self, id: u32, _data: &[u8]) -> Result<()> {
		self.told.set(id);
		Ok(())
	}
}

fn main() {
	let bytes = [0u8; t::Frame::SIZE];
	let query = t::Frame::new(&bytes).expect("a view");
	let seen = Rc::new(Cell::new(0u32));
	let told = Rc::new(Cell::new(0u32));
	let mut slots = [t_drive::ResponseToSlot::default(); 2];
	let mut driver = t_drive::ResponseToDriver::new(
		&mut slots, Resealing { submissions: seen.clone(), told: told.clone() },
		800, 3);
	driver.send(&query, &bytes, 7, 0).expect("a slot");
	let (_, expired) = driver.step(800);
	assert_eq!(expired, 0);
	assert_eq!(seen.get(), 1);
	assert_eq!(told.get(), 7);
}
""", encoding = "ascii")
	built = subprocess.run(
		[RUSTC or "rustc", "--edition", "2021", "-A", "warnings",
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
	(tmp_path / "run.py").write_text("""import sys
sys.path.insert(0, ".")
import t
import t_drive
from situ_runtime import Message

sent, resent = [], []
buf = bytearray(t.frame.SIZE_BYTES)
view = t.frame(Message(buf), 0, len(buf))
driver = t_drive.response_to_driver(sent.append, 2, 800, 3,
                                    resend=lambda id, data: resent.append(id))
driver.send(view, bytes(buf), 7, 0)
assert driver.step(800)[1] == 0
assert len(sent) == 1, sent
assert resent == [7], resent
plain = t_drive.response_to_driver(sent.append, 2, 800, 3)
plain.send(view, bytes(buf), 8, 0)
plain.step(800)
assert len(sent) == 3, sent
""", encoding = "ascii")
	ran = subprocess.run(["python3", str(tmp_path / "run.py")],
	                     capture_output = True, text = True, cwd = tmp_path)
	assert ran.returncode == 0, ran.stdout + ran.stderr
