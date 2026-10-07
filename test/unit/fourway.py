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
#: its delimiter about once in a hundred bytes under it, so `example/http`
#: and `example/smtp` were compared almost entirely on the path where nothing
#: parses at all. Text-shaped bytes reach the parse; digits reach the number.
ALPHABETS = (
	None,					# uniform over 0..255
	bytes(range(0x20, 0x7f)) + b"\r\n\t",	# printable text and its framing
	# `eE+` as well as the digits: a scaled text number's exponent is
	# unreachable without them, so `12.5e3` could not be drawn and the
	# exponent half of 0056 was compared by nobody.
	b"0123456789 \r\n:.-eE+",		# digits, exponents, delimiters
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


#: Where a schema states a value the data must carry, and what it must be.
#:
#: A drawn buffer essentially never satisfies one. Measured over 2400
#: buffers from 200 seeds: **zero** begin with a TIFF byte-order marker, so
#: `test_the_walker_agrees_with_the_compiled_backends[tiff]` compared the
#: walker and the four backends on the big-endian branch only -- and agreed
#: there by accident while the walker read every little-endian field
#: byte-swapped (26.576). The branch behind a magic is unreachable by
#: random bytes, which is `evidence.md`'s rule about agreement needing a
#: case that would disagree, with the case never run.
#:
#: 66 such sites across the corpus, counted: 56 `must_eq`, 6 pinned runs
#: and 4 markers, every one at a static offset.
def magics(parsed: object, resolved: object,
		rng: random.Random) -> list[tuple[int, bytes]]:
	"""Each `(offset, bytes)` a schema says the data must carry.

	Static offsets only. A member the data places is where a *drawn* value
	says, so planting at a computed offset would need the walk this is here
	to test -- and a magic at a dynamic offset is a shape no schema here
	has.

	A marker is drawn BETWEEN its two literals rather than fixed at one, so
	the branch a format exists for is reached about half the time and the
	other branch keeps its coverage. That is what makes this a widening of
	the draw rather than a swap of one blind spot for another.
	"""
	orders = {decl.name: decl for decl in parsed.markers()}  # type: ignore[attr-defined]
	planted: list[tuple[int, bytes]] = []
	chosen: dict[str, str] = {}

	# A variant names its discriminant by the member's own name, so the
	# placement has to be looked up rather than carried.
	by_name = {entry.placement.name: entry.placement
	           for struct in resolved.structs.values()	# type: ignore[attr-defined]
	           for entry in struct.entries}

	for struct in resolved.structs.values():            # type: ignore[attr-defined]
		for entry in struct.entries:
			placement = entry.placement
			if placement.offset_bits is None:
				continue
			at = placement.offset_bits // 8
			width = (placement.size_bits or 0) // 8

			# The marker first, and in declaration order, because an
			# integer magic governed by one is written in the order the
			# marker states. The marker MEMBER carries `kind == "marker"`
			# and `marker is None` -- it is what governs rather than what
			# is governed -- which the first version of this keyed the
			# wrong way round and planted nothing for TIFF.
			if placement.kind == "marker":
				decl = orders.get(placement.name)
				if decl is None:
					continue
				side = chosen.setdefault(
					decl.name, "little" if rng.randrange(2) else "big")
				held = decl.little if side == "little" else decl.big
				# The literals are AST nodes, not numbers.
				value = getattr(held, "value", held)
				# Big-endian whatever it says: a marker cannot be written
				# in the order it is about, which is the same reason the
				# walker and the generated C both read it `be`.
				planted.append((at, int(value).to_bytes(width or 2, "big")))
				continue

			if placement.pinned_runs:
				planted.append((at, placement.pinned_runs[0]))
				continue

			# A VARIANT'S DISCRIMINANT, which is not a magic and gates a
			# branch exactly as one does (26.585). keystore is the case: its
			# `params` switches on `version` with `default: error`, so every
			# drawn buffer refuses at the variant and the walk stops -- and
			# the members after it, including everything that places the
			# sealed region and the tag, were compared by nobody. A
			# RecursionError lived behind it.
			#
			# Needing BOTH a magic and a declared discriminant is why
			# 26.576's planted draw did not reach it either: it writes the
			# magics and a discriminant is a value the schema enumerates
			# rather than one it pins.
			#
			# One case at random, like the marker above, so the arms share
			# the draws and `default: error` keeps its own coverage from the
			# twelve unplanted buffers.
			# WHOLE BYTES ONLY, and this is the half the first version got
			# wrong. A discriminant may be sub-byte -- id3 switches on
			# `extended_header`, one bit, and dnsname on a two-bit `form` --
			# and planting means writing bytes, so `size_bits // 8` is zero
			# and `to_bytes(0, ...)` raised OverflowError on both. Writing a
			# whole byte there would clobber the neighbours sharing it,
			# which is worse than not planting: the draw would stop being a
			# draw of the schema.
			#
			# Read-modify-write on the byte is possible and needs the bit
			# order and the bit offset, which is the walk this harness
			# exists to test. Skipped by name instead, so a bit-wide
			# discriminant keeps the coverage the twelve unplanted buffers
			# give it.
			if placement.arm_cases and placement.discriminant:
				cases = [arm.value for arm in placement.arm_cases
				         if arm.value is not None]
				chose = by_name.get(placement.discriminant)
				if cases and chose is not None \
						and chose.offset_bits is not None \
						and chose.size_bits \
						and chose.size_bits % 8 == 0 \
						and chose.offset_bits % 8 == 0:
					order = (chose.endian.value
					         if chose.endian is not None else "big")
					if order == "native":
						order = sys.byteorder
					planted.append((
						chose.offset_bits // 8,
						int(rng.choice(cases)).to_bytes(
							chose.size_bits // 8,
							"little" if order == "little" else "big")))
				continue

			for attr in placement.attrs:
				if attr.name != "must_eq" or attr.value is None:
					continue
				held = getattr(attr.value, "value", None)
				if isinstance(held, str):
					planted.append((at, held.encode("latin-1")))
				elif isinstance(held, int) and width:
					order = (placement.endian.value
					         if placement.endian is not None
					         else chosen.get(placement.marker or "", "big"))
					if order == "native":
						order = sys.byteorder
					little = order == "little"
					planted.append((at, held.to_bytes(
						width, "little" if little else "big")))
				break

	return planted


def planted(rng: random.Random, parsed: object, resolved: object,
		buffer: bytes) -> bytes:
	"""`buffer` with every static magic the schema states written into it.

	Additive: the unplanted draws are untouched and keep every verdict they
	had, so this can only reach cases nothing reached before.
	"""
	held = bytearray(buffer)
	for at, value in magics(parsed, resolved, rng):
		if at + len(value) <= len(held):
			held[at:at + len(value)] = value
	return bytes(held)


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

	# An empty answer means this schema has nothing a driver can acquire --
	# `std/codecs.situ` declares signatures and no structs -- and returning
	# an empty command map is how the callers pass such a schema over.
	#
	# It does NOT mean "everything was skipped". The differ holds a struct
	# that takes a `parameter` out of the comparison, and it refuses rather
	# than returning an empty list when that takes the last one, precisely
	# so this line cannot turn an emptied harness into a schema the sweep
	# reports as swept (26.402). The guard has to live there, because a
	# schema in that state never reaches `differ.generate` from here.
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
