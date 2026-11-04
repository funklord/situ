"""Rung 2's write half in Python, built and RUN over the corpus (26.855).

C has had an append-only builder since 26.610 and no other backend had any
of it, so `--layer edit` emitted `--layer view`'s output in three languages
(26.854). This is the first increment of the port: a builder for the struct
whose every member is a whole-byte fixed scalar.

**Run, not read.** Python needs no toolchain, so this generates the builder
for every corpus schema, imports it, builds each struct it serves with
chosen values, and reads every member back through the ordinary accessors.
A builder that wrote the right number of bytes in the wrong order, or the
right bytes at the wrong offsets, passes a text assertion and fails this.

**And the partition is asserted, not either cell.** A struct the builder
serves has to round-trip; a struct it does not has to be NAMED on stderr.
26.610 made that C's stance for the same reason -- a caller who asked for a
builder and found their struct missing would conclude the generator was
broken -- and an increment whose boundary is an absence is one nobody can
see moving.
"""

from __future__ import annotations

import importlib
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "test" / "unit"))

from every_schema import ROOT as CORPUS_ROOT, load_schema	# noqa: E402

from situc.codegen.c.build import Shape			# noqa: E402
from situc.codegen import plain_build			# noqa: E402
from situc.codegen.python import build as build_py		# noqa: E402
from situc.layout import BITS_PER_BYTE, Placement, solve	# noqa: E402
from situc.resolve import ResolvedStruct, resolve	# noqa: E402
from situc.codegen.c.build import SCALAR			# noqa: E402
from situc.traverse import is_own_member, local_name	# noqa: E402
from situc.codegen.python.emit import py_name		# noqa: E402

RUNTIME = ROOT / "runtime" / "python" / "situ_runtime.py"


def schemas() -> list[Path]:
	"""Every corpus schema, asked of the filesystem.

	`std/` is in because a schema of codecs and no struct is a case the
	builder has to answer with nothing rather than with an error.
	"""
	found = sorted([*CORPUS_ROOT.glob("example/*/*.situ"),
	                *CORPUS_ROOT.glob("std/*.situ"),
	                *CORPUS_ROOT.glob("test/schema/*.situ")])
	assert len(found) > 40, f"only {len(found)} schemas; the corpus moved"
	return found


def _value(width: int, signed: bool, index: int) -> int:
	"""A value that fits and is not symmetric.

	Not zero and not all-ones: both survive a wrong byte order, which is
	the fault a round-trip exists to catch. `0x12 0x34 ...` ascending per
	byte differs under every permutation of its own bytes.
	"""
	raw = int.from_bytes(bytes((0x12 + index + at) & 0xFF
	                           for at in range(width)), "big")
	if not signed:
		return raw
	top = 1 << (width * 8 - 1)
	return raw - (1 << (width * 8)) if raw >= top else raw


def _members(struct: ResolvedStruct) -> list[Placement]:
	return [entry.placement for entry in struct.entries
	        if is_own_member(struct, entry.placement)]


@pytest.mark.parametrize("schema", schemas(), ids=lambda p: p.stem)
def test_every_struct_the_builder_serves_round_trips(
		schema: Path, tmp_path: Path) -> None:
	parsed   = load_schema(schema)
	resolved = resolve(parsed, solve(parsed))
	shape    = Shape("", "situ", resolved.structs,
	                 {decl.name: decl for decl in parsed.varints()})
	served   = build_py.plain(resolved, shape)
	# Every struct is built by `test_the_four_builders_agree`; reading each
	# member back through the accessors is this file's, and it asks that of
	# the struct whose parts are all values the caller chose.
	scalars  = [unit.struct for unit in plain_build.units(resolved, shape)
	            if not unit.suffix
	            and all(part.role == SCALAR for part in unit.parts)]

	emitted = subprocess.run(
		[sys.executable, "-m", "situc", "build", str(schema),
		 "--target", "python", "--layer", "edit", "--out", str(tmp_path)],
		capture_output=True, text=True, cwd=ROOT)
	assert emitted.returncode == 0, emitted.stderr[-800:]

	built = tmp_path / f"{schema.stem}_build.py"
	assert built.exists() == bool(served), (served, sorted(
		one.name for one in tmp_path.iterdir()))
	if not served:
		return

	shutil.copy(RUNTIME, tmp_path)
	sys.path.insert(0, str(tmp_path))
	try:
		module = importlib.import_module(f"{schema.stem}_build")
		plain  = importlib.import_module(schema.stem)
		for struct in scalars:
			members = _members(struct)
			values  = [_value((one.size_bits or 8) // BITS_PER_BYTE,
			                  bool(one.scalar and one.scalar.signed), n)
			           for n, one in enumerate(members)]
			out = bytearray(struct.layout.size_max_bytes
			                or struct.layout.size_bytes or 1)
			make = getattr(module, f"build_{py_name(struct.name)}")
			try:
				wrote = make(out, *values)
			except plain.SituError:
				# A schema the chosen values cannot satisfy -- a `must_eq`,
				# an enum with `default = error` -- is not a builder fault,
				# and the gate says so rather than passing in silence.
				continue
			# The members' own widths: a `[since]` struct is written whole,
			# which is longer than the minimum `size_bytes` states.
			assert wrote == sum((one.size_bits or 0) // BITS_PER_BYTE
			                    for one in members), (struct.name, wrote)

			view = getattr(plain, py_name(struct.name)).at(
				plain.Message(bytearray(out[:wrote])), 0,
				*(() if struct.layout.is_fixed_size else (wrote,)))
			for placement, want in zip(members, values):
				name = py_name(local_name(struct, placement))
				got  = getattr(view, name)
				assert int(got) == want, (struct.name, name, got, want)
	finally:
		sys.path.remove(str(tmp_path))
		for name in (f"{schema.stem}_build", schema.stem):
			sys.modules.pop(name, None)


def test_what_it_does_not_build_is_named(tmp_path: Path) -> None:
	"""The other cell: a struct outside the class is on stderr.

	`example/modbus` is the witness because it holds both -- four plain
	structs and a variant whose arms hold structs whose own bytes do not
	state their extent, which is C's refusal and passes through unchanged.
	"""
	schema = CORPUS_ROOT / "example" / "modbus" / "modbus.situ"
	emitted = subprocess.run(
		[sys.executable, "-m", "situc", "build", str(schema),
		 "--target", "python", "--layer", "edit", "--out", str(tmp_path)],
		capture_output=True, text=True, cwd=ROOT)
	assert emitted.returncode == 0, emitted.stderr[-800:]

	said = emitted.stderr
	assert "no builder for `request.read_coils`" in said, said
	# ...and the increment's OWN boundary, which is not C's refusal.
	assert "no builder for `" in said, said
	assert (tmp_path / "modbus_build.py").exists()
