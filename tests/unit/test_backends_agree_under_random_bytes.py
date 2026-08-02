"""What four backends answer for bytes nobody meant to send.

The suite has compared backends on *well-formed* buffers since the C++ one
landed: one program, one buffer, every field read through both headers. That
check is what makes "a schema means one thing" more than a slogan, and it has
one blind spot -- a message somebody chose to be hostile.

That blind spot cost something twice. A member placed after a variable-length
region has an offset the message decides, and for a length the frame cannot
hold the four did four different things: C read out of bounds, C++ handed out a
span past the buffer, Rust panicked, Python clamped in silence. And a frame
shorter than a struct's minimum was a view in two backends and an error in the
other two, which is the check section 20.2 says every constant-offset access
below it depends on (26.27).

So this asks the other question, over every schema in the repository. The
drivers are generated (`situc/codegen/differ.py`) from the same layout the
accessors come from, so what is asked of one backend is asked of all four:
pseudo-random buffers in, one canonical listing out, diffed. The seed is fixed
so a disagreement reproduces.

*Which* pseudo-random buffers turned out to be most of the question. They were
uniform bytes of uniform length, which is one distribution and the least
searching one: a text protocol never parsed under it, and a frame small enough
for a declared length to overrun it was rare. Drawing from four alphabets and
mostly short lengths -- the same number of buffers, differently spread -- found
four disagreements the first time it ran, one of them a generated accessor
handing a caller fifty-five bytes out of a five-byte frame (26.35).

What is *not* asked is written down in that module: a subset of member kinds,
because a probe that is spelled wrong in one language reports a disagreement
that is not there. The subset is the thing to grow.
"""

from __future__ import annotations

import random
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from every_schema import ROOT, SCHEMAS, ids
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

#: Buffers per schema. Enough to reach past the acquiring bounds check on the
#: bigger frames, few enough that four processes per buffer stay quick.
COUNT = 48
SEED  = 20260801

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
	assert compiled.returncode == 0, compiled.stderr
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
	assert compiled.returncode == 0, compiled.stderr
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
	assert compiled.returncode == 0, compiled.stderr
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
	result = subprocess.run([*command, packet.hex()], capture_output=True,
	                        text=True, cwd=cwd)
	assert result.returncode == 0, f"{command[0]}: {result.stderr}"
	return result.stdout


@pytest.mark.skipif(
	HOST_CC is None or HOST_CXX is None or RUSTC is None,
	reason="needs all four toolchains")
@pytest.mark.parametrize("schema", SCHEMAS, ids=ids(SCHEMAS))
def test_the_four_agree_about_bytes_nobody_meant_to_send(
		schema: Path, tmp_path: Path) -> None:
	command = build(tmp_path, schema)
	if not command:
		pytest.skip("no struct a driver can acquire")

	rng     = random.Random(SEED)
	reached = 0

	for _ in range(COUNT):
		packet = draw(rng)
		given  = {name: answers(argv, packet, tmp_path)
		          for name, argv in command.items()}

		if "no-view" not in given["c"]:
			reached += 1

		assert len(set(given.values())) == 1, (
			f"{schema.name}: the four disagree about a "
			f"{len(packet)}-byte buffer:\n  {packet.hex()}\n"
			+ "\n".join(f"-- {name}\n{text}" for name, text in given.items()))

	# A run where every buffer was refused at acquisition would pass while
	# testing nothing, which is the failure mode of a random-input test.
	assert reached >= 1, f"{schema.name}: no buffer reached an accessor"
