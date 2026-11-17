"""A second resend schedule, once part of the reply is held.

fuzznet measured a 960 KB fetch at 20 % loss: 600 ms and three resends in a
3 s wait took 24.5-31.5 s, 300 ms and eight took 8.0-8.5 s -- but a fixed
300 ms resends whole replies on any link slower than that. So it resends at
300 ms, up to 8 times, only once SOME PIECE of the reply has arrived (then
the path works, and what is missing was lost rather than slow), keeping
600 ms and 3 while nothing has: 8.5 s. `[resend_part_ms = 300, retries_part
= 8]` beside 26.1039's three says it, the spelling fuzznet proposed and the
holder approved.

The consumer says when part is held -- reassembly is the consumer's -- by
`part_held(id, now)`. From then the next resend is due 300 ms after the
last send, and 8 bounds every resend, the one already sent included, as
fuzznet's one counter does.

The schedule, in all four drivers: sent at 0, resent at 600, part held at
700, resent every 300 ms from 900 to 2700 -- eight resends -- and expired at
exactly 3000.
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


SCHEMA = POLICY.replace(
	"[timeout_ms = 800, retries = 3]",
	"[timeout_ms = 3000, retries = 3, resend_ms = 600,"
	" resend_part_ms = 300, retries_part = 8]")

#: ("step", now, sent so far, next wake or None, expired) or ("part", now).
EVENTS: list[tuple[object, ...]] = [
	("step", 599, 1, 600, 0), ("step", 600, 2, 1200, 0),
	("part", 700),
	("step", 899, 2, 900, 0), ("step", 900, 3, 1200, 0),
	("step", 1200, 4, 1500, 0), ("step", 1500, 5, 1800, 0),
	("step", 1800, 6, 2100, 0), ("step", 2100, 7, 2400, 0),
	("step", 2400, 8, 2700, 0), ("step", 2700, 9, 3000, 0),
	("step", 2999, 9, 3000, 0), ("step", 3000, 9, None, 1),
]


@pytest.mark.skipif(COMPILER is None, reason = "no C compiler")
def test_c(tmp_path: Path) -> None:
	assert SCHEMA != POLICY
	schema, resolved = analysed(SCHEMA)
	for name, text in generate_c(schema, resolved, "t").files().items():
		(tmp_path / name).write_text(text, encoding = "ascii")
	for emitted in (converse.generate(schema, resolved, "t"),
	                drive.generate(schema, resolved, "t")):
		for name, text in emitted.items():
			(tmp_path / name).write_text(text, encoding = "ascii")
	checks = ""
	for at, event in enumerate(EVENTS):
		if event[0] == "part":
			checks += (f"\tif (situ_drive_response_to_part_held(&drive, 7u,"
			           f" {event[1]}u) != SITU_OK) return {10 + at};\n")
			continue
		_, now, sent, wake, gone = event
		checks += (
			f"\tif (situ_drive_response_to_step(&drive, {now}u, &next, &expired)"
			f" != {'SITU_OK' if wake is not None else 'SITU_ERR_TRUNCATED'}"
			f" || sent != {sent}u || expired != {gone}u"
			+ (f" || next != {wake}u" if wake is not None else "")
			+ f") return {10 + at};\n")
	(tmp_path / "sched.c").write_text(f"""#include <string.h>
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
	situ_drive_response_to_init(&drive, slots, keys, 2u, io, 3000u, 3u, 600u,
	                            300u, 8u);
	if (situ_drive_response_to_send(&drive, view, buf, 17u, 7u, 0u)
	    != SITU_OK || sent != 1u) return 2;
	if (situ_drive_response_to_part_held(&drive, 8u, 0u)
	    != SITU_ERR_CONSTRAINT) return 3;
{{checks}}	return 0;
}}
""".replace("{checks}", checks), encoding = "ascii")
	built = subprocess.run(
		[COMPILER or "cc", *WARNINGS, f"-I{tmp_path}", f"-I{RUNTIME}",
		 str(tmp_path / "sched.c"), str(tmp_path / "t.c"),
		 str(RUNTIME / "situ.c"), "-o", str(tmp_path / "sched")],
		capture_output = True, text = True)
	assert built.returncode == 0, built.stderr
	ran = subprocess.run([str(tmp_path / "sched")], capture_output = True)
	assert ran.returncode == 0, f"check {ran.returncode} failed"


@pytest.mark.skipif(HOST_CXX is None, reason = "no C++ compiler")
def test_cpp(tmp_path: Path) -> None:
	schema, resolved = analysed(SCHEMA)
	for name, text in generate_cpp(schema, resolved, "t").files().items():
		(tmp_path / name).write_text(text, encoding = "ascii")
	for module in (relate_cpp, frame_cpp, converse_cpp, drive_cpp):
		for name, text in module.generate(schema, resolved, "t").items():
			(tmp_path / name).write_text(text, encoding = "ascii")
	checks = ""
	for at, event in enumerate(EVENTS):
		if event[0] == "part":
			checks += (f"\t\tif (driver.part_held(7u, {event[1]}u) != "
			           f"::situ::rt::err::ok) return {10 + at};\n")
			continue
		_, now, sent, wake, gone = event
		checks += (
			f"\t\tif (driver.step({now}u, next, expired) != "
			f"{'::situ::rt::err::ok' if wake is not None else '::situ::rt::err::truncated'}"
			f" || sink.submissions != {sent}u || expired != {gone}u"
			+ (f" || next != {wake}u" if wake is not None else "")
			+ f") return {10 + at};\n")
	(tmp_path / "main.cpp").write_text("""#include <cstdint>
#include <cstring>
#include "t_drive.hpp"

namespace {
class counting_io : public ::situ::io {
public:
	unsigned submissions = 0u;
	::situ::rt::err submit(const std::uint8_t *, std::uint32_t) noexcept override
	{
		submissions++;
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
	counting_io sink;
	::situ::response_to_driver::slot slots[2];
	::situ::response_to_driver driver(slots, 2u, sink, 3000u, 3u, 600u, 300u, 8u);
	if (driver.send(view, bytes, ::situ::frame::size_bytes, 7u, 0u)
	    != ::situ::rt::err::ok) return 2;
	if (driver.part_held(8u, 0u) != ::situ::rt::err::constraint) return 3;
	{
		std::uint32_t next = 0u, expired = 0u;
CHECKS	}
	return 0;
}
""".replace("CHECKS", checks), encoding = "ascii")
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
	schema, resolved = analysed(SCHEMA)
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
	checks = ""
	for event in EVENTS:
		if event[0] == "part":
			checks += f"\tdriver.part_held(7, {event[1]}).expect(\"held\");\n"
			continue
		_, now, sent, wake, gone = event
		checks += (
			f"\tlet (next, expired) = driver.step({now});\n"
			f"\tassert_eq!(next, {f'Some({wake})' if wake is not None else 'None'});\n"
			f"\tassert_eq!(expired, {gone});\n"
			f"\tassert_eq!(seen.get(), {sent});\n")
	(src / "main.rs").write_text("""pub mod situ_rt;
pub mod t;
pub mod t_converse;
pub mod t_drive;
pub mod t_frame;
pub mod t_relate;

use situ_rt::Result;

struct Counting {
	submissions: std::rc::Rc<std::cell::Cell<u32>>,
}

impl t_drive::Io for Counting {
	fn submit(&mut self, _data: &[u8]) -> Result<()> {
		self.submissions.set(self.submissions.get() + 1);
		Ok(())
	}
}

fn main() {
	let bytes = [0u8; t::Frame::SIZE];
	let query = t::Frame::new(&bytes).expect("a view");
	let seen = std::rc::Rc::new(std::cell::Cell::new(0u32));
	let mut slots = [t_drive::ResponseToSlot::default(); 2];
	let mut driver = t_drive::ResponseToDriver::new(
		&mut slots, Counting { submissions: seen.clone() }, 3000, 3, 600, 300, 8);
	driver.send(&query, &bytes, 7, 0).expect("a slot");
	assert!(driver.part_held(8, 0).is_err());
CHECKS}
""".replace("CHECKS", checks), encoding = "ascii")
	built = subprocess.run(
		[RUSTC or "rustc", "--edition", "2021", "-D", "warnings",
		 "-A", "dead_code", "-C", "overflow-checks=on",
		 str(src / "main.rs"), "-o", str(tmp_path / "run")],
		capture_output = True, text = True, cwd = tmp_path)
	assert built.returncode == 0, built.stderr
	ran = subprocess.run([str(tmp_path / "run")], capture_output = True,
	                     text = True)
	assert ran.returncode == 0, ran.stderr


def test_python(tmp_path: Path) -> None:
	schema, resolved = analysed(SCHEMA)
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
from situ_runtime import ConstraintError, Message

sent = []
buf = bytearray(t.frame.SIZE_BYTES)
view = t.frame(Message(buf), 0, len(buf))
driver = t_drive.response_to_driver(sent.append, 2, 3000, 3, 600, 300, 8)
driver.send(view, bytes(buf), 7, 0)
try:
	driver.part_held(8, 0)
	raise SystemExit("an unknown exchange was accepted")
except ConstraintError:
	pass
for event in {{EVENTS!r}}:
	if event[0] == "part":
		driver.part_held(7, event[1])
		continue
	_, now, count, wake, gone = event
	got = driver.step(now)
	assert got == (wake, gone) and len(sent) == count, (now, got, len(sent))
""".replace("{EVENTS!r}", repr(EVENTS)), encoding = "ascii")
	ran = subprocess.run(["python3", str(tmp_path / "run.py")],
	                     capture_output = True, text = True, cwd = tmp_path)
	assert ran.returncode == 0, ran.stdout + ran.stderr


@pytest.mark.parametrize("attrs", [
	"[timeout_ms = 800, retries = 3]",
	"[timeout_ms = 3000, retries = 3, resend_ms = 600]",
	"[timeout_ms = 3000, retries = 3, resend_ms = 600,"
	" resend_part_ms = 300, retries_part = 8]",
])
def test_the_python_driver_type_checks(tmp_path: Path, attrs: str) -> None:
	"""All three schedules. The table held `list[object]`, so every read was
	an `int(object)` under a `type: ignore`, three of them naming the wrong
	code: the driver failed `mypy --strict` as generated, before this
	schedule existed. A typed record needs no ignore at all."""
	if subprocess.run(["python3", "-m", "mypy", "--version"],
	                  capture_output = True).returncode != 0:
		pytest.skip("no mypy")
	schema, resolved = analysed(
		POLICY.replace("[timeout_ms = 800, retries = 3]", attrs))
	for name, text in generate_py(schema, resolved, "t").files().items():
		(tmp_path / name).write_text(text, encoding = "ascii")
	for module in (relate_py, converse_py, drive_py):
		for name, text in module.generate(schema, resolved, "t").items():
			(tmp_path / name).write_text(text, encoding = "ascii")
	(tmp_path / "situ_runtime.py").write_text(
		(ROOT / "runtime" / "python" / "situ_runtime.py").read_text(
			encoding = "ascii"), encoding = "ascii")
	assert "type: ignore" not in (tmp_path / "t_drive.py").read_text(
		encoding = "ascii")
	checked = subprocess.run(
		["python3", "-m", "mypy", "--strict", "--no-pretty",
		 "--no-error-summary", "t_drive.py"],
		capture_output = True, text = True, cwd = tmp_path)
	assert checked.returncode == 0, checked.stdout + checked.stderr


def test_without_a_part_schedule_nothing_changes() -> None:
	"""The driver 26.1039 generates is the one generated without the two."""
	plain = POLICY.replace("[timeout_ms = 800, retries = 3]",
	                       "[timeout_ms = 3000, retries = 3, resend_ms = 600]")
	schema, resolved = analysed(plain)
	for text in drive.generate(schema, resolved, "t").values():
		assert "part" not in text.replace("partial", ""), text


@pytest.mark.parametrize("attrs,said", [
	("[timeout_ms = 3000, retries = 3, resend_part_ms = 300,"
	 " retries_part = 8]", "a part schedule with no `resend_ms`"),
	("[timeout_ms = 3000, retries = 3, resend_ms = 600,"
	 " resend_part_ms = 300]", "states half a part schedule"),
	("[timeout_ms = 3000, retries = 3, resend_ms = 600,"
	 " resend_part_ms = 3000, retries_part = 8]",
	 "`resend_part_ms` is 3000 and the whole exchange waits 3000"),
	("[timeout_ms = 3000, retries = 3, resend_ms = 600,"
	 " resend_part_ms = 300, retries_part = 0]",
	 "`retries_part` needs a positive number"),
	("[retries_part = 8]", "`retries_part` with no retransmission policy"),
])
def test_a_part_schedule_that_cannot_be_kept_is_refused(attrs: str,
		said: str) -> None:
	"""Whole, beside `resend_ms`, and able to resend inside the total."""
	with pytest.raises(Exception) as refused:
		analysed(POLICY.replace("[timeout_ms = 800, retries = 3]", attrs))
	assert said in str(refused.value), refused.value
