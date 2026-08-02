"""What four backends answer for bytes nobody meant to send.

The suite has compared backends on *well-formed* buffers since the C++ one
landed: one program, one buffer, every field read through both headers. That
check is what makes "a schema means one thing" more than a slogan, and it has
one blind spot -- a message somebody chose to be hostile.

That blind spot cost something real. A member placed after a variable-length
region has an offset the message decides, and for a length the frame cannot
hold the four backends did four different things: C read out of bounds, C++
handed out a span pointing past the buffer, Rust panicked, Python clamped in
silence (26.27). Every one of those is a different answer to the same
question, and no test asked the question.

So this asks it, over `examples/packet` -- the schema with a data-driven
length, a sealed region and a tag placed after both. Two hundred pseudo-random
buffers, the same bytes to all four, and every answer compared: what the
plaintext fields read, whether the tag is reachable, what its first byte is,
and what `validate` says. The seed is fixed so a disagreement reproduces.

One schema, and that is worth stating plainly: this is not a general
differential fuzzer over every construct. It is the construct that broke, held
by the property that would have caught it on the day any one backend diverged.
"""

from __future__ import annotations

import random
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from every_schema import ROOT
from situc.cli import analyse
from situc.codegen.c import generate as generate_c
from situc.codegen.cpp import generate as generate_cpp
from situc.codegen.python import generate as generate_py
from situc.codegen.rust import generate as generate_rs
from situc.parser import parse

SCHEMA   = ROOT / "examples" / "packet" / "packet.situ"
RUNTIME  = ROOT / "runtime"
HOST_CC  = shutil.which("gcc") or shutil.which("cc")
HOST_CXX = shutil.which("g++") or shutil.which("clang++")
RUSTC    = shutil.which("rustc")

#: Enough buffers to reach the interesting lengths, few enough to build and run
#: in a second. The lengths straddle `SIZE_MIN`, so about half are refused
#: outright and the rest reach the accessors.
COUNT = 200
SEED  = 20260801

C_DRIVER = """\
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "unit.h"

int main(int argc, char **argv)
{
	uint8_t     raw[128];
	uint32_t    n = 0;
	situ_msg_t  msg;
	situ_view_t view;
	situ_view_t hdr;
	const uint8_t *tag;

	if (argc != 2) { return 2; }
	for (n = 0; argv[1][n * 2] != '\\0'; n++) {
		char pair[3] = { argv[1][n * 2], argv[1][n * 2 + 1], '\\0' };
		raw[n] = (uint8_t)strtoul(pair, NULL, 16);
	}

	situ_msg_init(&msg, raw, n);
	if (situ_packet_view(&msg, 0, n, &view) != SITU_OK) {
		printf("no-view\\n");
		return 0;
	}

	printf("hop %u\\n", situ_packet_hop_get(view));
	if (situ_packet_hdr_view(view, &hdr) == SITU_OK) {
		printf("version %u\\n", situ_header_version_get(hdr));
		printf("length %u\\n", situ_header_length_get(hdr));
	} else {
		printf("no-hdr\\n");
	}

	tag = situ_packet_tag_ptr(view);
	printf("tag %s\\n", tag == NULL ? "absent" : "present");
	if (tag != NULL) { printf("tag0 %u\\n", tag[0]); }
	printf("validate %d\\n", (int)situ_packet_validate(view));
	return 0;
}
"""

CPP_DRIVER = """\
#include <cstdio>
#include <cstdlib>
#include <cstring>

#include "unit.hpp"

int main(int argc, char **argv)
{
	std::uint8_t raw[128];
	std::uint32_t n = 0;

	if (argc != 2) { return 2; }
	for (n = 0; argv[1][n * 2] != '\\0'; n++) {
		char pair[3] = { argv[1][n * 2], argv[1][n * 2 + 1], '\\0' };
		raw[n] = static_cast<std::uint8_t>(std::strtoul(pair, nullptr, 16));
	}

	::situ::rt::message msg(raw, n);
	::situ::packet view;
	if (::situ::packet::at(msg, 0, n, view) != ::situ::rt::err::ok) {
		std::printf("no-view\\n");
		return 0;
	}

	std::printf("hop %u\\n", view.hop());
	std::printf("version %u\\n", view.hdr().version());
	std::printf("length %u\\n", view.hdr().length());

	const auto tag = view.tag();
	std::printf("tag %s\\n", tag.empty() ? "absent" : "present");
	if (!tag.empty()) { std::printf("tag0 %u\\n", tag[0]); }
	std::printf("validate %d\\n", static_cast<int>(view.validate()));
	return 0;
}
"""

RUST_DRIVER = """\
mod situ_rt;
mod unit;

fn main() {
	let hex: Vec<String> = std::env::args().collect();
	let raw: Vec<u8> = hex[1].as_bytes().chunks(2)
		.map(|pair| u8::from_str_radix(std::str::from_utf8(pair).unwrap(), 16)
			.unwrap())
		.collect();

	let view = match unit::Packet::new(&raw) {
		Ok(view) => view,
		Err(_)   => { println!("no-view"); return; }
	};

	println!("hop {}", view.hop());
	println!("version {}", view.hdr().version());
	println!("length {}", view.hdr().length());

	let tag = view.tag();
	println!("tag {}", if tag.is_empty() { "absent" } else { "present" });
	if !tag.is_empty() { println!("tag0 {}", tag[0]); }
	println!("validate {}", match view.validate() {
		Ok(())                        => 0,
		Err(situ_rt::Error::Bounds)     => 1,
		Err(situ_rt::Error::Constraint) => 2,
		Err(_)                          => 9,
	});
}
"""

PYTHON_DRIVER = """\
import sys

import situ_runtime
import unit

raw  = bytearray(bytes.fromhex(sys.argv[1]))
msg  = situ_runtime.Message(raw)

try:
	view = unit.packet.at(msg, 0, len(raw))
except situ_runtime.BoundsError:
	print("no-view")
	sys.exit(0)

print("hop %d" % view.hop)
print("version %d" % view.hdr.version)
print("length %d" % view.hdr.length)

tag = view.tag
print("tag %s" % ("absent" if len(tag) == 0 else "present"))
if len(tag) != 0:
	print("tag0 %d" % tag[0])

try:
	view.validate()
	print("validate 0")
except situ_runtime.BoundsError:
	print("validate 1")
except situ_runtime.ConstraintError:
	print("validate 2")
except situ_runtime.SituError:
	print("validate 9")
"""


def build_all(tmp_path: Path) -> dict[str, list[str]]:
	"""Generate `examples/packet` four times and build a driver for each.

	The drivers print the same lines in the same order, so a disagreement is a
	diff rather than an interpretation. `validate` is reported as the C error
	number in every one of them, which is what the other three runtimes say
	they mirror (20.2).
	"""
	source, resolved, _ = analyse(SCHEMA)
	parsed  = parse(source)
	command: dict[str, list[str]] = {}

	# -- C ---------------------------------------------------------------
	built = generate_c(parsed, resolved, "unit")
	for name, text in built.files().items():
		(tmp_path / name).write_text(text, encoding="ascii")
	(tmp_path / "c_driver.c").write_text(C_DRIVER, encoding="ascii")
	assert subprocess.run(
		[HOST_CC or "cc", "-std=c11", "-O1", f"-I{RUNTIME / 'c'}",
		 f"-I{tmp_path}", str(tmp_path / "c_driver.c"),
		 str(tmp_path / "unit.c"), str(RUNTIME / "c" / "situ.c"),
		 "-o", str(tmp_path / "c_probe")],
		capture_output=True, text=True).returncode == 0
	command["c"] = [str(tmp_path / "c_probe")]

	# -- C++ -------------------------------------------------------------
	(tmp_path / "unit.hpp").write_text(
		generate_cpp(parsed, resolved, "unit").header, encoding="ascii")
	(tmp_path / "cpp_driver.cpp").write_text(CPP_DRIVER, encoding="ascii")
	assert subprocess.run(
		[HOST_CXX or "g++", "-std=c++17", "-O1", f"-I{RUNTIME / 'c'}",
		 f"-I{RUNTIME / 'cpp'}", f"-I{tmp_path}",
		 str(tmp_path / "cpp_driver.cpp"), str(RUNTIME / "c" / "situ.c"),
		 "-o", str(tmp_path / "cpp_probe")],
		capture_output=True, text=True).returncode == 0
	command["cpp"] = [str(tmp_path / "cpp_probe")]

	# -- Rust ------------------------------------------------------------
	src = tmp_path / "src"
	src.mkdir(exist_ok=True)
	(src / "situ_rt.rs").write_text(
		(RUNTIME / "rust" / "situ_rt.rs").read_text(encoding="ascii")
		.replace("#![no_std]\n", ""), encoding="ascii")
	(src / "unit.rs").write_text(
		generate_rs(parsed, resolved, "unit").module, encoding="ascii")
	(src / "main.rs").write_text(RUST_DRIVER, encoding="ascii")
	assert RUSTC is not None
	assert subprocess.run(
		[RUSTC, "--edition", "2021", "-O", str(src / "main.rs"),
		 "-o", str(tmp_path / "rs_probe")],
		capture_output=True, text=True, cwd=tmp_path).returncode == 0
	command["rust"] = [str(tmp_path / "rs_probe")]

	# -- Python ----------------------------------------------------------
	(tmp_path / "situ_runtime.py").write_text(
		(RUNTIME / "python" / "situ_runtime.py").read_text(encoding="ascii"),
		encoding="ascii")
	(tmp_path / "unit.py").write_text(
		generate_py(parsed, resolved, "unit").module, encoding="ascii")
	(tmp_path / "py_driver.py").write_text(PYTHON_DRIVER, encoding="ascii")
	command["python"] = [sys.executable, str(tmp_path / "py_driver.py")]

	return command


def answers(command: list[str], packet: bytes, cwd: Path) -> str:
	result = subprocess.run([*command, packet.hex()], capture_output=True,
	                        text=True, cwd=cwd)
	assert result.returncode == 0, f"{command[0]}: {result.stderr}"
	return result.stdout


@pytest.mark.skipif(
	HOST_CC is None or HOST_CXX is None or RUSTC is None,
	reason="needs all four toolchains")
def test_the_four_agree_about_bytes_nobody_meant_to_send(
		tmp_path: Path) -> None:
	command = build_all(tmp_path)
	rng     = random.Random(SEED)

	reached = 0
	for _ in range(COUNT):
		packet = bytes(rng.randrange(256)
		               for _ in range(rng.randrange(0, 90)))
		given  = {name: answers(argv, packet, tmp_path)
		          for name, argv in command.items()}

		if given["c"] != "no-view\n":
			reached += 1

		assert len(set(given.values())) == 1, (
			f"the four disagree about {packet.hex()}:\n"
			+ "\n".join(f"-- {name}\n{text}" for name, text in given.items()))

	# A run where every buffer was refused at acquisition would pass while
	# testing nothing, which is the failure mode of a random-input test.
	assert reached >= COUNT // 8, \
		f"only {reached} of {COUNT} buffers reached an accessor"
