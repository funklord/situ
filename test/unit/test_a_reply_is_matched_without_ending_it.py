"""A reply is matched without ending its exchange (26.1009).

Asked for by fuzznet (`suggestion/fuzznet.md`, 2026-11-13, gap 3): a reply
there is many datagrams, and a late piece of a settled message is dropped
BEFORE reassembly, so the check runs once a piece. `on_message` (C's and
the converse table's `match`, Rust's and Python's `take`) ends the exchange,
so it could be asked only at the end. `peek` answers the same question and
leaves it pending, in every driver and every converse table.

Each driver here: `peek` names the id twice over, `on_message` still
matches afterwards, and `peek` is refused once it has.
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
	(tmp_path / "peek.c").write_text("""#include <string.h>
#include "t_drive.h"

static situ_err_t count(void *ctx, const uint8_t *data, uint32_t len)
{
	(void)ctx; (void)data; (void)len;
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
	uint32_t id = 0u;

	memset(buf, 0, sizeof buf);
	situ_msg_init(&msg, buf, 17);
	if (situ_frame_view(&msg, 0u, &view) != SITU_OK) return 1;
	situ_drive_response_to_init(&drive, slots, keys, 2u, io, 800u, 3u);
	if (situ_drive_response_to_send(&drive, view, buf, 17u, 7u, 0u)
	    != SITU_OK) return 2;
	for (int i = 0; i < 2; i++) {
		if (situ_drive_response_to_peek(&drive, view, &id) != SITU_OK
		    || id != 7u) return 3;
	}
	if (situ_drive_response_to_on_message(&drive, view, &id) != SITU_OK
	    || id != 7u) return 4;
	if (situ_drive_response_to_peek(&drive, view, &id)
	    != SITU_ERR_CONSTRAINT) return 5;
	return 0;
}
""", encoding = "ascii")
	built = subprocess.run(
		[COMPILER or "cc", *WARNINGS, f"-I{tmp_path}", f"-I{RUNTIME}",
		 str(tmp_path / "peek.c"), str(tmp_path / "t.c"),
		 str(RUNTIME / "situ.c"), "-o", str(tmp_path / "peek")],
		capture_output = True, text = True)
	assert built.returncode == 0, built.stderr
	ran = subprocess.run([str(tmp_path / "peek")], capture_output = True)
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
class quiet_io : public ::situ::io {
public:
	::situ::rt::err submit(const std::uint8_t *, std::uint32_t) noexcept override
	{
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
	quiet_io sink;
	::situ::response_to_driver::slot slots[2];
	::situ::response_to_driver driver(slots, 2u, sink, 800u, 3u);
	if (driver.send(view, bytes, ::situ::frame::size_bytes, 7u, 0u)
	    != ::situ::rt::err::ok) return 2;
	std::uint32_t id = 0u;
	for (int i = 0; i < 2; i++) {
		if (driver.peek(view, id) != ::situ::rt::err::ok || id != 7u) return 3;
	}
	if (driver.on_message(view, id) != ::situ::rt::err::ok || id != 7u) return 4;
	if (driver.peek(view, id) != ::situ::rt::err::constraint) return 5;
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

struct Quiet;

impl t_drive::Io for Quiet {
	fn submit(&mut self, _data: &[u8]) -> Result<()> {
		Ok(())
	}
}

fn main() {
	let bytes = [0u8; t::Frame::SIZE];
	let query = t::Frame::new(&bytes).expect("a view");
	let mut slots = [t_drive::ResponseToSlot::default(); 2];
	let mut driver = t_drive::ResponseToDriver::new(&mut slots, Quiet, 800, 3);
	driver.send(&query, &bytes, 7, 0).expect("a slot");
	assert_eq!(driver.peek(&query).ok(), Some(7));
	assert_eq!(driver.peek(&query).ok(), Some(7));
	assert_eq!(driver.on_message(&query).ok(), Some(7));
	assert!(driver.peek(&query).is_err());
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
from situ_runtime import ConstraintError, Message

buf = bytearray(t.frame.SIZE_BYTES)
view = t.frame(Message(buf), 0, len(buf))
driver = t_drive.response_to_driver(lambda data: None, 2, 800, 3)
driver.send(view, bytes(buf), 7, 0)
assert driver.peek(view) == 7
assert driver.peek(view) == 7
assert driver.on_message(view) == 7
try:
	driver.peek(view)
except ConstraintError:
	pass
else:
	raise AssertionError("peek answered for an exchange that is over")
""", encoding = "ascii")
	ran = subprocess.run(["python3", str(tmp_path / "run.py")],
	                     capture_output = True, text = True, cwd = tmp_path)
	assert ran.returncode == 0, ran.stdout + ran.stderr
