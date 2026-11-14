"""Rung 2's write half: four builders, one message (26.858).

C has built since 26.610, Python since 26.855, and C++ and Rust since
26.858, each for the class `plain_build` states. A round trip through
one backend's own accessors cannot see a fault that backend's reader and
writer share, so this holds the four to each other: every struct the class
serves, in every corpus schema, built by all four from the same values,
and the bytes compared. A refusal has to be one all four make.
"""

from __future__ import annotations

import importlib
import itertools
import math
import random
import shutil
import subprocess
import sys
from collections.abc import Iterable, Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "test" / "unit"))

import fourway						# noqa: E402
from every_schema import ROOT as CORPUS_ROOT, load_schema	# noqa: E402

from situc.codegen import plain_build			# noqa: E402
from situc.codegen.plain_build import Unit			# noqa: E402
from situc.codegen.c.build import (ARG, LOCATED, ArmOf, BITS, CHOSEN, ENDED, MARKER, NESTED, REPEAT,	# noqa: E402
                                   RUN, SCALAR, SIZE, SPAN, TAG, VARINT, WIDE,
                                   Part)
from situc.codegen.c.names import bare_name, c_name, ident	# noqa: E402
from situc.codegen.cpp.build import _arm_type as cpp_arm_type	# noqa: E402
from situc.codegen.python.build import _arm_type as py_arm_type	# noqa: E402
from situc.codegen.rust.build import _arm_type as rust_arm_type	# noqa: E402
from situc.codegen.rust.build import arm_variant	# noqa: E402
from situc.codegen.cpp.edit import _scalar as cpp_scalar	# noqa: E402
from situc.codegen.cpp.names import class_name		# noqa: E402
from situc.codegen.python.emit import py_name		# noqa: E402
from situc.codegen.rust.edit import _scalar as rust_scalar	# noqa: E402
from situc.codegen.rust.emit import _ident		# noqa: E402
from situc import traverse				# noqa: E402
from situc.layout import BITS_PER_BYTE, Placement, solve	# noqa: E402
from situc.pack import pack				# noqa: E402
from situc.resolve import resolve			# noqa: E402
from test_walker_c import python_verdict		# noqa: E402
from walker.image import load				# noqa: E402
from walker import report				# noqa: E402
from walker.walk import acquire			# noqa: E402

RUNTIME = ROOT / "runtime"
#: Comfortably past the largest struct the class serves; asserted below.
ROOM = 512
#: A negative value, as the 64 bits C and Rust spell it.
MASK = (1 << 64) - 1
#: How deep nesting goes in the corpus, plus one.
PASSES = 5
#: Values tried on a unit before it counts as one nothing satisfies.
TRIES = 2000


def schemas() -> list[Path]:
	found = sorted([*CORPUS_ROOT.glob("example/*/*.situ"),
	                *CORPUS_ROOT.glob("test/schema/*.situ")])
	assert len(found) > 40, f"only {len(found)} schemas; the corpus moved"
	return found


def _raw(placement: Placement, index: int) -> int:
	"""The value's bytes as an unsigned number, ascending and not symmetric.

	Not zero and not all-ones: both survive a wrong byte order, which is the
	fault this exists to catch.
	"""
	width = (placement.size_bits or 8) // BITS_PER_BYTE
	return int.from_bytes(bytes((0x12 + index + at) & 0xFF
	                            for at in range(width)), "big")


def _signed(placement: Placement, raw: int) -> int:
	bits = placement.scalar.bits if placement.scalar else 8
	if placement.scalar and placement.scalar.signed and raw > 0 \
			and raw >> (bits - 1):
		return raw - (1 << bits)
	return raw


def _span_length(part: Part, parts: list[Part]) -> int:
	"""A length the run's size field can state exactly.

	Three bytes for a plain size; for arithmetic, the length the field's
	value 2 describes, so that `(n + 1) * 8 - 2` comes out whole rather than
	exercising only the refusal.
	"""
	for one in parts:
		if one.role == SIZE \
				and one.sizes is part.placement:
			if one.solved is not None:
				return max(one.solved.scale * 2 + one.solved.offset, 0)
	return 3


def _run_bytes(length: int, index: int) -> bytes:
	return bytes((0x41 + index + at) & 0xFF for at in range(length))


Given = list[tuple["Part", "int | bytes | list[int]"]]


@dataclass(frozen=True)
class Picked(Unit):
	"""One combination of a builder's arms.

	The builder takes each arm as a value, so nothing generated grows with
	the combinations (26.881). What is tried does, so the product is taken
	only where it is small: radiotap's is 2^24, and this comparison would
	have outlived the gate that ran it (26.889).
	"""

	picks: tuple[tuple[Part, ArmOf], ...] = ()


#: Every combination up to this many, and past it a covering set.
ALL_COMBINATIONS = 64


def _covering(chosen: list[Part]) -> Iterable[tuple[ArmOf, ...]]:
	"""Every arm seen, and enough mixes that their order is exercised.

	Each arm of each variant beside the first arm of the rest, the first and
	last arm of all, and a fixed sample. Seeded, because four backends have to
	be handed the same list, and a failure has to come back.
	"""
	arms   = [part.arms for part in chosen]
	first  = tuple(one[0] for one in arms)
	yield first
	yield tuple(one[-1] for one in arms)
	for at, held in enumerate(arms):
		for arm in held[1:]:
			yield (*first[:at], arm, *first[at + 1:])
	picked = random.Random(len(arms))
	for _ in range(ALL_COMBINATIONS):
		yield tuple(picked.choice(one) for one in arms)


def _combinations(units: list[Unit]) -> list[Unit]:
	"""Each unit, once per combination of the arms it takes as values."""
	found: list[Unit] = []
	for unit in units:
		chosen = [part for part in unit.parts if part.role == CHOSEN]
		if not chosen:
			found.append(unit)
			continue
		count = math.prod(len(part.arms) for part in chosen)
		tried = (itertools.product(*(part.arms for part in chosen))
		         if count <= ALL_COMBINATIONS else _covering(chosen))
		found.extend(Picked(unit.struct, unit.parts, unit.suffix,
		                    tuple(zip(chosen, arms))) for arms in tried)
	return found


def _picked(unit: Unit) -> dict[int, ArmOf]:
	return {id(part): arm for part, arm in getattr(unit, "picks", ())}


def _flat(unit: Unit) -> list[Part]:
	"""The parts this combination writes, each arm in its variant's place."""
	picked = _picked(unit)
	found: list[Part] = []
	for part in unit.parts:
		if part.role != CHOSEN:
			found.append(part)
			continue
		arm = picked[id(part)]
		found.extend(one for one in (arm.tag, arm.part) if one is not None)
	return found


def _called(unit: Unit, values: list[tuple[Part, list[str]]],
		call: Callable[[Part, ArmOf, list[str]], str]) -> list[str]:
	"""A call's arguments, each arm's made into its value by `call`."""
	said: dict[int, list[str]] = {}
	for part, spelt in values:
		said.setdefault(id(part), []).extend(spelt)
	picked = _picked(unit)
	found: list[str] = []
	for part in unit.parts:
		if part.role != CHOSEN:
			found.extend(said.get(id(part), []))
			continue
		arm = picked[id(part)]
		found.append(call(part, arm, [
			line for one in (arm.tag, arm.part) if one is not None
			for line in said.get(id(one), [])]))
	return found


def _given(parts: list[Part], inner: dict[str, list[bytes]]) -> Given:
	"""What the caller hands each part that takes anything.

	`inner` is whole messages of the structs a NESTED part holds. A struct
	with none falls back to one byte, which `required` refuses in all four.
	"""
	found: Given = []
	for at, part in enumerate(parts):
		if part.role == BITS and plain_build.takes_value(part):
			# Small enough for its field: a wider value is the refusal, and
			# one of those would say nothing about where the bits go.
			width = part.placement.size_bits or 1
			found.append((part, (0x5 + at) & ((1 << width) - 1) or 1))
		elif part.role == SCALAR and part.radix:
			found.append((part, 5 + at))	# small enough for its digits
		elif part.role == SCALAR:
			found.append((part, _raw(part.placement, at)))
		elif part.role == MARKER:
			# The little-endian literal: a writer ignoring the marker
			# writes big, so this is the value that tells them apart.
			found.append((part, part.little[0]))
		elif part.role == ARG:
			# A byte order handed down is the same choice as a marker's,
			# and the little literal tells the four apart the same way.
			found.append((part, part.little[0] if part.little
			              else _raw(part.placement, at)))
		elif part.role == WIDE:
			found.append((part, [0x12 + at + one
			                     for one in range(part.bytes or 2)]))
		elif part.role == VARINT and plain_build.takes_value(part):
			# Past one byte, so the continuation bit is exercised.
			found.append((part, 0x1234 + at))
		elif part.role == RUN:
			found.append((part, _run_bytes(part.bytes, at)))
		elif part.role == SPAN:
			found.append((part, _run_bytes(_span_length(part, parts), at)))
		elif part.role == LOCATED:
			# Where the offset already written puts it; its length is the
			# one a nested header states, which a generic value rarely is,
			# so the explicit cases are 26.1025's own test.
			found.append((part, _run_bytes(4, at)))
		elif part.role == NESTED:
			found.append((part, inner.get(part.placement.type_name or "",
			                              [b"\x00"])[0]))
		elif part.role == TAG and part.opens:
			found.append((part, inner.get(part.placement.tag_prefix or "",
			                              [b"\x00"])[0]))
		elif part.role == ENDED:
			# What needs escaping, where the schema escapes: a run of
			# ordinary bytes would never exercise it.
			escape = part.placement.delimiter_escape
			found.append((part, _run_bytes(3, at) if escape is None else
			              b"a" + part.delimiter + bytes([escape]) + b"z"))
		elif part.role == REPEAT:
			element = inner.get(part.placement.type_name or "", [b"\x00"])[0]
			found.append((part, element * (part.digits or 2)))
	return found


def _varied(given: Given, inner: dict[str, list[bytes]],
		rng: random.Random) -> Given:
	"""Another set of values: small numbers, and the other inner messages.

	Rules, enums and arm tags refuse the fixed values, so most of a schema
	would otherwise be compared only on all four saying no.
	"""
	found: Given = []
	for part, value in given:
		if part.role == NESTED:
			value = rng.choice(inner.get(part.placement.type_name or "",
			                             [value]))
		elif part.role == REPEAT:
			held  = inner.get(part.placement.type_name or "", [b"\x00"])
			value = b"".join(rng.choice(held) for _ in range(
				part.digits or rng.randrange(4)))
		elif isinstance(value, int):
			bits  = (part.placement.scalar.bits if part.radix
			         and part.placement.scalar
			         else part.placement.size_bits or 8)
			value = rng.choice((*range(9), 16, 32, 64, 128, 255,
			                    0x1234)) & ((1 << bits) - 1)
		found.append((part, value))
	return found


def _array(name: str, value: bytes, kind: str) -> str:
	items = ", ".join(f"{one:#x}" for one in value) or "0"
	return f"static const {kind} {name}[] = {{{items}}};"


def _label(unit: Unit) -> str:
	picks = "+".join(arm.name for _, arm in getattr(unit, "picks", ()))
	return f"{unit.struct.name}.{picks or unit.suffix}"


def _c_driver(stem: str, cases: list[tuple[Unit, Given]]) -> str:
	lines = ["#include <stdio.h>", f'#include "{stem}_build.h"',
	         f"static uint8_t buf[{ROOM}];", "static uint32_t n;",
	         "static void show(const char *name, situ_err_t e) {",
	         '\tprintf("%s ", name);',
	         '\tif (e != SITU_OK) { puts("refused"); return; }',
	         '\tfor (uint32_t i = 0; i < n; i++) printf("%02x", buf[i]);',
	         '\tputs("");', "}"]
	calls = []
	for index, (unit, given) in enumerate(cases):
		spelt: list[tuple[Part, list[str]]] = []
		for at, (part, value) in enumerate(given):
			if isinstance(value, int):
				spelt.append((part, [f"{value & MASK:#x}ull"]))
				continue
			if isinstance(value, list):
				name = f"run_{index}_{at}"
				lines.append(_array(name, bytes(value),
				                    f"uint{part.bit_at * 8}_t"))
				spelt.append((part, [name, *([] if part.bytes
				                             else [f"{len(value)}u"])]))
				continue
			name = f"run_{index}_{at}"
			lines.append(_array(name, value, "uint8_t"))
			spelt.append((part, [name, *(
				[f"{len(value)}u"]
				if part.role in (*plain_build.MEASURED, TAG, LOCATED)
				 else [])]))

		def arm_value(part: Part, arm: ArmOf, args: list[str],
				unit: Unit = unit) -> str:
			name = traverse.local_name(unit.struct, part.placement)
			return (f'{ident("situ", unit.struct.name, name, "arm", arm.name)}'
			        f'({", ".join(args)})')

		values = _called(unit, spelt, arm_value)
		calls.append(f'\tshow("{_label(unit)}", '
		             f'{ident("situ", unit.struct.name, "build", unit.suffix)}'
		             f'(buf, {ROOM}u, {", ".join([*values, "&n"])}));')
	return "\n".join([*lines, "int main(void) {", *calls, "\treturn 0;",
	                  "}", ""])


def _cpp_driver(stem: str, cases: list[tuple[Unit, Given]]) -> str:
	lines = ["#include <cstdio>", f'#include "{stem}_build.hpp"',
	         f"static std::uint8_t buf[{ROOM}];", "static std::uint32_t n;",
	         "static void show(const char *name, ::situ::rt::err e) {",
	         '\tstd::printf("%s ", name);',
	         "\tif (e != ::situ::rt::err::ok) {",
	         '\t\tstd::puts("refused");', "\t\treturn;", "\t}",
	         '\tfor (std::uint32_t i = 0; i < n; i++) '
	         'std::printf("%02x", buf[i]);',
	         '\tstd::puts("");', "}"]
	calls = []
	for index, (unit, given) in enumerate(cases):
		spelt: list[tuple[Part, list[str]]] = []
		for at, (part, value) in enumerate(given):
			if isinstance(value, int):
				kind = ("std::uint64_t" if part.role == VARINT and not part.minimal
				        else "std::int64_t" if part.role == VARINT
				        else cpp_scalar(part.placement))
				spelt.append((part, [f"static_cast<{kind}>"
				                     f"({value & MASK:#x}ull)"]))
				continue
			if isinstance(value, list):
				name = f"run_{index}_{at}"
				lines.append(_array(name, bytes(value),
				                    cpp_scalar(part.placement)))
				spelt.append((part, [name, *([] if part.bytes
				                             else [f"{len(value)}u"])]))
				continue
			name = f"run_{index}_{at}"
			lines.append(_array(name, value, "std::uint8_t"))
			spelt.append((part, [name, *(
				[f"{len(value)}u"]
				if part.role in (*plain_build.MEASURED, TAG, LOCATED)
				 else [])]))

		def arm_value(part: Part, arm: ArmOf, args: list[str],
				unit: Unit = unit) -> str:
			return (f"::situ_build::{cpp_arm_type(unit.struct, part)}::"
			        f'{bare_name(arm.name)}({", ".join(args)})')

		values = _called(unit, spelt, arm_value)
		held = class_name(unit.struct)
		if unit.suffix:
			held = f"{held}_{c_name(unit.suffix)}"
		calls.append(f'\tshow("{_label(unit)}", '
		             f"::situ_build::{held}"
		             f'(buf, {ROOM}u, {", ".join([*values, "n"])}));')
	return "\n".join([*lines, "int main() {", *calls, "}", ""])


def _rust_driver(stem: str, cases: list[tuple[Unit, Given]]) -> str:
	lines = ["mod situ_rt;", "#[allow(dead_code)]", f"mod {stem};",
	         "#[allow(dead_code)]", f"mod {stem}_build;",
	         "fn show(name: &str, buf: &[u8], "
	         "got: situ_rt::Result<usize>) {",
	         '\tprint!("{} ", name);',
	         "\tmatch got {",
	         "\t\tOk(n) => {",
	         '\t\t\tfor b in &buf[..n] { print!("{:02x}", b); }',
	         "\t\t\tprintln!();", "\t\t}",
	         '\t\tErr(_) => println!("refused"),', "\t}", "}",
	         "fn main() {", f"\tlet mut buf = [0u8; {ROOM}];"]
	for unit, given in cases:
		spelt: list[tuple[Part, list[str]]] = []
		for part, value in given:
			if isinstance(value, int):
				kind = ("u64" if part.role == VARINT and not part.minimal
				        else "i64" if part.role == VARINT
				        else rust_scalar(part.placement))
				spelt.append((part, [f"{value & MASK:#x}u64 as {kind}"]))
				continue
			if isinstance(value, list):
				kind  = rust_scalar(part.placement)
				items = ", ".join(f"{one:#x}{kind}" for one in value)
				spelt.append((part, [f"&[{items}]"]))
				continue
			items = ", ".join(f"{one:#x}u8" for one in value)
			spelt.append((part, [f"&[{items}]"]))

		def arm_value(part: Part, arm: ArmOf, args: list[str],
				unit: Unit = unit) -> str:
			made = (f"{stem}_build::{rust_arm_type(unit.struct, part)}"
			        f"::{arm_variant(arm.name)}")
			return f'{made}({", ".join(args)})' if args else made

		values = _called(unit, spelt, arm_value)
		name = _ident(unit.struct.name).removeprefix("r#")
		if unit.suffix:
			name = f"{name}_{_ident(unit.suffix).removeprefix('r#')}"
		lines.extend([f"\tlet got = {stem}_build::build_{name}"
		              f'({", ".join(["&mut buf", *values])});',
		              f'\tshow("{_label(unit)}", &buf, got);'])
	return "\n".join([*lines, "}", ""])


def _build_one(module: object, base: object, unit: Unit,
		given: Given) -> str:
	"""One unit through Python's builder: hex, or `refused`."""
	held: dict[int, object] = {
		id(part): _signed(part.placement, value)
		if isinstance(value, int) else value for part, value in given}
	picked = _picked(unit)
	values: list[object] = []
	for part in unit.parts:
		if part.role != CHOSEN:
			if id(part) in held:
				values.append(held[id(part)])
			continue
		arm  = picked[id(part)]
		made = getattr(getattr(module, py_arm_type(unit.struct, part)),
		               py_name(arm.name))
		values.append(made(*(held[id(one)] for one in (arm.tag, arm.part)
		                     if one is not None)))
	buf  = bytearray(ROOM)
	name = py_name(unit.struct.name)
	if unit.suffix:
		name = f"{name}_{py_name(unit.suffix)}"
	try:
		wrote = getattr(module, f"build_{name}")(buf, *values)
	except base.SituError:		# type: ignore[attr-defined]
		return "refused"
	return bytes(buf[:wrote]).hex()


def _cases(module: object, base: object,
		units: list[Unit]) -> list[tuple[Unit, Given, str]]:
	"""Each unit with the fixed values and, if those are refused, some it
	takes.

	Python goes first because a NESTED part wants a whole message of its
	inner struct, and a builder is what makes one. The passes are the
	nesting depth: each one builds what the last one's bytes made possible.
	The others are then handed the same bytes, so it is the outer message
	they are compared on.
	"""
	rng = random.Random(0)
	inner: dict[str, list[bytes]] = {}
	taken: dict[str, Given] = {}
	for _ in range(PASSES):
		for unit in units:
			if _label(unit) in taken:
				continue
			given = _given(_flat(unit), inner)
			for _ in range(TRIES):
				made = _build_one(module, base, unit, given)
				if made != "refused":
					taken[_label(unit)] = given
					inner.setdefault(unit.struct.name, []).append(
						bytes.fromhex(made))
					break
				given = _varied(given, inner, rng)
	found: list[tuple[Unit, Given, str]] = []
	for unit in units:
		given = _given(_flat(unit), inner)
		found.append((unit, given, _build_one(module, base, unit, given)))
		if _label(unit) in taken:
			given = taken[_label(unit)]
			found.append((unit, given, _build_one(module, base, unit, given)))
	return found


def _reads_back(plain: object, base: object, unit: Unit, given: Given,
		made: str) -> None:
	"""A message a builder wrote is one the reader takes whole.

	Agreement between four writers says nothing if all four are wrong
	together, so the reader is asked what extent the bytes state.
	"""
	held = getattr(plain, py_name(unit.struct.name))
	if made == "refused" or not hasattr(held, "required"):
		return
	message = bytes.fromhex(made)
	# The struct's own arguments, as the reader's keywords: the values the
	# builder was handed for them.
	handed = {traverse.argument_name(part.placement): value for part, value in given
	          if part.role == ARG}
	try:
		said = held.required(message, **handed)
	except base.TruncatedError:		# type: ignore[attr-defined]
		# A run nothing but the frame ends asks for more: `required` frames
		# a stream, and netlink's attributes end where the message does.
		assert any(part.role == REPEAT and not part.digits
		           and not part.delimiter
		           and not plain_build.walked_at_size(part, _flat(unit))
		           for part in _flat(unit)), (_label(unit), made)
		return
	assert said == len(message), (_label(unit), made)


def _python(tmp: Path, stem: str, units: list[Unit],
		extra: list[tuple[Unit, Given]]
		) -> tuple[str, list[tuple[Unit, Given]]]:
	"""Python's answers, and the cases the others are given."""
	shutil.copy(RUNTIME / "python" / "situ_runtime.py", tmp)
	sys.path.insert(0, str(tmp))
	# `example/json` is named for a module pytest already holds.
	shadowed = sys.modules.pop(stem, None)
	try:
		module = importlib.import_module(f"{stem}_build")
		base   = importlib.import_module("situ_runtime")
		plain  = importlib.import_module(stem)
		found  = [*_cases(module, base, units),
		          *((unit, given, _build_one(module, base, unit, given))
		            for unit, given in extra)]
		for unit, given, made in found:
			_reads_back(plain, base, unit, given, made)
		return ("".join(f"{_label(unit)} {made}\n"
		                for unit, _, made in found),
		        [(unit, given) for unit, given, _ in found])
	finally:
		sys.path.remove(str(tmp))
		for name in (f"{stem}_build", stem, "situ_runtime"):
			sys.modules.pop(name, None)
		if shadowed is not None:
			sys.modules[stem] = shadowed


def _emit(schema: Path, target: str, out: Path) -> None:
	done = subprocess.run(
		[sys.executable, "-m", "situc", "build", str(schema), "--target",
		 target, "--layer", "edit", "--out", str(out)],
		capture_output=True, text=True, cwd=ROOT)
	assert done.returncode == 0, done.stderr[-800:]


def _derived(schema: Path, out: Path) -> list[str]:
	"""A codec's C implementation, which `build` declares and does not hold."""
	done = subprocess.run(
		[sys.executable, "-m", "situc", "gen-derived", str(schema),
		 "--out", str(out)], capture_output=True, text=True, cwd=ROOT)
	assert done.returncode == 0, done.stderr[-800:]
	made = out / f"{schema.stem}_derived.c"
	return [made.name] if made.exists() else []


def _run(argv: list[str], cwd: Path) -> str:
	done = subprocess.run(argv, capture_output=True, text=True, cwd=cwd)
	assert done.returncode == 0, done.stderr[-1500:]
	return done.stdout


def _the_walker_takes_it(schema: Path, python: str,
		cases: list[tuple[Unit, Given]]) -> None:
	"""What four writers built is valid to the reader that shares no code
	with them. Python's walker alone: C's costs a compile per message.

	A struct's own arguments are the values its builder was handed, in
	declaration order, which is the order its ARG parts are in."""
	parsed = load_schema(schema)
	blob   = pack(parsed, resolve(parsed, solve(parsed)), metadata=True)[0]
	image  = load(blob)
	names  = image.struct_names
	for line, (_, given) in zip(python.splitlines(), cases, strict=True):
		label, made = line.rsplit(" ", 1)
		if made == "refused":
			continue
		shape  = names.index(label.split(".")[0])
		handed = [value for part, value in given
		          if part.role == ARG and isinstance(value, int)]
		if not handed:
			assert python_verdict(blob, bytes.fromhex(made), shape) == "0", \
				(label, made)
			continue
		view = acquire(image, bytes.fromhex(made), shape, handed)
		assert report._validate(image, view, shape) == 0, (label, made)


def four_say(schema: Path, tmp_path: Path, units: list[Unit],
		extra: list[tuple[Unit, Given]]) -> str:
	"""What all four write, one line a case; a disagreement fails here.

	`extra` is cases of the caller's, after the ones found for `units`.
	"""
	stem  = schema.stem
	units = _combinations(units)
	dirs  = {target: tmp_path / target
	        for target in ("c", "cpp", "rust", "python")}
	for target, out in dirs.items():
		out.mkdir()
		_emit(schema, target, out)

	python, cases = _python(dirs["python"], stem, units, extra)

	(dirs["c"] / "main.c").write_text(_c_driver(stem, cases),
	                                  encoding="ascii")
	assert fourway.HOST_CC is not None and fourway.HOST_CXX is not None
	derived = _derived(schema, dirs["c"])
	_run([fourway.HOST_CC, "-std=c11", "-w", f"-I{RUNTIME / 'c'}", "-I.",
	      "main.c", f"{stem}.c", *derived, str(RUNTIME / "c" / "situ.c"),
	      "-o", "probe"], dirs["c"])

	(dirs["cpp"] / "main.cpp").write_text(_cpp_driver(stem, cases),
	                                      encoding="ascii")
	# A codec is the C implementation in every language that links one, so
	# C++ takes the object C's compiler made rather than a copy of its own.
	objects: list[str] = []
	if derived:
		_run([fourway.HOST_CC, "-std=c11", "-w", f"-I{RUNTIME / 'c'}", "-I.",
		      "-c", *derived, "-o", str(dirs["cpp"] / "derived.o")],
		     dirs["c"])
		objects.append("derived.o")
	_run([fourway.HOST_CXX, "-std=c++17", "-Wall", "-Wextra", "-Werror",
	      f"-I{RUNTIME / 'c'}", f"-I{RUNTIME / 'cpp'}", "-I.", "main.cpp",
	      *objects, str(RUNTIME / "c" / "situ.c"), "-o", "probe"],
	     dirs["cpp"])

	shutil.copy(RUNTIME / "rust" / "situ_rt.rs", dirs["rust"])
	rt = dirs["rust"] / "situ_rt.rs"
	rt.write_text(rt.read_text(encoding="ascii").replace("#![no_std]\n", ""),
	              encoding="ascii")
	(dirs["rust"] / "main.rs").write_text(_rust_driver(stem, cases),
	                                      encoding="ascii")
	assert fourway.RUSTC is not None
	# Warnings denied, as a consumer under `-D warnings` builds them: an
	# unused import in a builder of nothing but a coded region, and six
	# builders' worth of parentheses and constant comparisons, compiled
	# here for months under `-A warnings` (26.1001). Dead code alone is
	# allowed -- a probe calls one builder of a module that has many.
	_run([fourway.RUSTC, "--edition", "2021", "-D", "warnings",
	      "-A", "dead_code", "main.rs", "-o", "probe"], dirs["rust"])

	said = {"python": python,
	        **{target: _run([str(dirs[target] / "probe")], dirs[target])
	           for target in ("c", "cpp", "rust")}}
	assert said["python"].count("\n") == len(cases)
	assert len(set(said.values())) == 1, said
	_the_walker_takes_it(schema, python, cases)
	return python


@pytest.mark.skipif(not fourway.COMPLETE, reason="needs gcc, g++ and rustc")
@pytest.mark.parametrize("schema", schemas(), ids=lambda p: p.stem)
def test_four_builders_write_one_message(schema: Path, tmp_path: Path) -> None:
	parsed   = load_schema(schema)
	resolved = resolve(parsed, solve(parsed))
	units    = plain_build.units(resolved, plain_build.shape(parsed, resolved))
	if not units:
		pytest.skip("no struct here is in the class")
	four_say(schema, tmp_path, units, [])
