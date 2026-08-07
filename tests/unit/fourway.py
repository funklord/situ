"""Build one schema four times, with a driver for each, and ask them all.

The harness `test_backends_agree_under_random_bytes` was written around, moved
out of it when a second caller appeared: `test_composed_schemas` generates
schemas nobody wrote and asks them the same question. Two copies of this would
be two things that have to agree about what "the same question" is, which is
the mistake `situc/codegen/differ.py` exists to avoid one level down.

What lives here is the mechanical part -- compile four, run four, diff -- and
nothing about *which* schemas. That belongs to the callers.
"""

from __future__ import annotations

import random
import shutil
import subprocess
import sys
from pathlib import Path

from every_schema import ROOT
from situc.cli import analyse
from situc.codegen import differ
from situc.codegen.c import generate as generate_c
from situc.codegen.cpp import generate as generate_cpp
from situc.codegen.python import generate as generate_py
from situc.codegen.rust import generate as generate_rs
from situc.parser import parse

RUNTIME  = ROOT / "runtime"
HOST_CC  = shutil.which("gcc") or shutil.which("cc")
HOST_CXX = shutil.which("g++") or shutil.which("clang++")
RUSTC    = shutil.which("rustc")

#: Whether every toolchain a four-way comparison needs is here.
COMPLETE = HOST_CC is not None and HOST_CXX is not None and RUSTC is not None

#: The largest frame in the tree is about a kilobyte; a buffer twice that
#: reaches every minimum without making the drivers slow.
LONGEST = 1200

#: And a short one, which is the interesting length rather than the cheap one.
#: A declared length can only exceed the frame it sits in when the frame is
#: small, so a kilobyte of noise asks the question with the answer already
#: filled in. Three quarters of the buffers are drawn from here.
SHORTEST = 64

#: What a buffer is made of. Uniform noise was the only alphabet, and it is
#: the one that reaches the least: a member framed on `" "` or `"\r\n"` finds
#: its delimiter about once in a hundred bytes under it, so `examples/http`
#: and `examples/smtp` were compared almost entirely on the path where nothing
#: parses at all. Text-shaped bytes reach the parse; digits reach the number.
ALPHABETS = (
	None,					# uniform over 0..255
	bytes(range(0x20, 0x7f)) + b"\r\n\t",	# printable text and its framing
	b"0123456789 \r\n:.-",			# digits and the delimiters
	b"\x00\x01\x7f\x80\xff 0123456789",	# edge bytes among text
	# Text that is *terminated* and not ASCII, which no other alphabet
	# reaches often enough to matter. `validate` returns on the first thing
	# wrong with a member, so an encoding check on a delimited member is
	# only reachable through a buffer whose delimiter is present -- and the
	# alphabets above put a high byte and a delimiter in the same buffer
	# rarely enough that five unchecked `[encoding = ascii]` declarations
	# sat in `http` and `smtp` without a single draw noticing.
	b"GET / HTTP1.\r\n:\xc3\xa9\x80\xff",
)


def draw(rng: random.Random) -> bytes:
	"""One buffer: an alphabet, a length, and nothing else.

	Both choices come off the same seeded generator, so the sequence is the
	schema's regardless of which alphabet a buffer lands on and a
	disagreement still reproduces from the seed alone.
	"""
	alphabet = ALPHABETS[rng.randrange(len(ALPHABETS))]
	length   = (rng.randrange(0, LONGEST) if rng.randrange(4) == 0
	            else rng.randrange(0, SHORTEST))

	if alphabet is None:
		return bytes(rng.randrange(256) for _ in range(length))
	return bytes(alphabet[rng.randrange(len(alphabet))]
	             for _ in range(length))


class BuildFailed(Exception):
	"""A backend emitted something its own compiler will not take.

	Raised rather than asserted, because one caller wants the failure to fail
	the test and the other wants to collect it and carry on sweeping.
	"""

	def __init__(self, target: str, message: str) -> None:
		super().__init__(f"{target}: {message}")
		self.target  = target
		self.message = message


def build(tmp_path: Path, schema: Path) -> dict[str, list[str]]:
	"""Generate one schema four times, with a driver for each, and build them.

	Returns the command to run each driver, or an empty mapping where the
	schema has nothing a driver can acquire -- `std/codecs.situ` declares
	signatures and no structs at all.
	"""
	source, resolved, _ = analyse(schema)
	parsed = parse(source)

	if not differ.structs_of(resolved):
		return {}

	command: dict[str, list[str]] = {}

	# -- C ---------------------------------------------------------------
	built = generate_c(parsed, resolved, "unit")
	for name, text in built.files().items():
		(tmp_path / name).write_text(text, encoding="ascii")
	(tmp_path / "c_driver.c").write_text(
		differ.generate(parsed, resolved, "c"), encoding="ascii")
	compiled = subprocess.run(
		[HOST_CC or "cc", "-std=c11", "-O1", f"-I{RUNTIME / 'c'}",
		 f"-I{tmp_path}", str(tmp_path / "c_driver.c"),
		 str(tmp_path / "unit.c"), str(RUNTIME / "c" / "situ.c"),
		 "-o", str(tmp_path / "c_probe")],
		capture_output=True, text=True)
	if compiled.returncode != 0:
		raise BuildFailed("c", compiled.stderr)
	command["c"] = [str(tmp_path / "c_probe")]

	# -- C++ -------------------------------------------------------------
	(tmp_path / "unit.hpp").write_text(
		generate_cpp(parsed, resolved, "unit").header, encoding="ascii")
	(tmp_path / "cpp_driver.cpp").write_text(
		differ.generate(parsed, resolved, "cpp"), encoding="ascii")
	compiled = subprocess.run(
		[HOST_CXX or "g++", "-std=c++17", "-O1", f"-I{RUNTIME / 'c'}",
		 f"-I{RUNTIME / 'cpp'}", f"-I{tmp_path}",
		 str(tmp_path / "cpp_driver.cpp"), str(RUNTIME / "c" / "situ.c"),
		 "-o", str(tmp_path / "cpp_probe")],
		capture_output=True, text=True)
	if compiled.returncode != 0:
		raise BuildFailed("cpp", compiled.stderr)
	command["cpp"] = [str(tmp_path / "cpp_probe")]

	# -- Rust ------------------------------------------------------------
	src = tmp_path / "src"
	src.mkdir(exist_ok=True)
	(src / "situ_rt.rs").write_text(
		(RUNTIME / "rust" / "situ_rt.rs").read_text(encoding="ascii")
		.replace("#![no_std]\n", ""), encoding="ascii")
	(src / "unit.rs").write_text(
		generate_rs(parsed, resolved, "unit").module, encoding="ascii")
	(src / "main.rs").write_text(
		differ.generate(parsed, resolved, "rust"), encoding="ascii")
	assert RUSTC is not None
	compiled = subprocess.run(
		[RUSTC, "--edition", "2021", "-O", "-A", "warnings",
		 str(src / "main.rs"), "-o", str(tmp_path / "rs_probe")],
		capture_output=True, text=True, cwd=tmp_path)
	if compiled.returncode != 0:
		raise BuildFailed("rust", compiled.stderr)
	command["rust"] = [str(tmp_path / "rs_probe")]

	# -- Python ----------------------------------------------------------
	(tmp_path / "situ_runtime.py").write_text(
		(RUNTIME / "python" / "situ_runtime.py").read_text(encoding="ascii"),
		encoding="ascii")
	(tmp_path / "unit.py").write_text(
		generate_py(parsed, resolved, "unit").module, encoding="ascii")
	(tmp_path / "py_driver.py").write_text(
		differ.generate(parsed, resolved, "python"), encoding="ascii")
	command["python"] = [sys.executable, str(tmp_path / "py_driver.py")]

	return command


def answers(command: list[str], packet: bytes, cwd: Path) -> str:
	"""What one backend says about one buffer.

	A driver that dies is a failure of the same kind a disagreement is -- a
	Rust panic and a C++ segfault have both been that -- so the exit status is
	part of the answer.
	"""
	result = subprocess.run([*command, packet.hex()], capture_output=True,
	                        text=True, cwd=cwd)
	if result.returncode != 0:
		raise BuildFailed(command[0], result.stderr)
	return result.stdout
