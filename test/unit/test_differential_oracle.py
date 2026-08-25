"""situ's accessors against somebody else's implementation of the same format.

Every other test of the generated code compares situ against situ: four
backends against each other (which finds disagreement but not shared error),
the accessors against the capability map, the accessors against arbitrary
bytes. All of them are downstream of one schema written by one person reading
one specification.

This is the one that is not. `oracles.py` has the argument in full; the short
version is that a hand-authored vector and a misread specification fail in the
same direction and agree forever, and an independent implementation does not.

Skips loudly when a tool is absent -- a differential test that quietly becomes
a no-op is worth less than no test, because the suite still reports green.
`test_the_report_names_what_did_not_run` is what makes the skip visible.
"""

from __future__ import annotations

import binascii
import ctypes
import importlib
import itertools
import random
import shutil
import subprocess
import sys
import zlib
from pathlib import Path

import pytest

from every_schema import ROOT
from oracles import DRIVERS, LIES, ORACLES, Oracle, have

from situc.codegen import c as generate_c
from situc.codegen import python as generate_py
from situc.codegen.c import derived as generate_derived
from situc.diagnostics import Source
from situc.layout import solve
from situc.parser import parse
from situc.resolve import resolve


#: Each built module gets a directory of its own, counted rather than named
#: after the schema.
_BUILDS = itertools.count()


def build_module(schema: Path, tmp: Path) -> object:
	"""The Python accessors for one schema, importable.

	Every build lands in a fresh directory and is imported under a fresh
	name. Two versions of one schema in one process is exactly what the
	honest/lying pair below is, and sharing a directory means the second
	import can be served from the first's cached bytecode -- the module
	stays stale, the comparison agrees, and the test that exists to prove a
	mutation is noticed reports that it was not. That cost an hour of
	believing a false alarm, which is cheaper than believing a false green.
	"""
	source   = Source(str(schema), schema.read_text(encoding="ascii"))
	parsed   = parse(source)
	resolved = resolve(parsed, solve(parsed))
	emitted  = generate_py.generate(parsed, resolved, schema.stem)

	where = tmp / f"build{next(_BUILDS)}"
	where.mkdir()
	(where / f"{schema.stem}.py").write_text(emitted.module, encoding="ascii")
	runtime = ROOT / "runtime" / "python" / "situ_runtime.py"
	(where / "situ_runtime.py").write_text(runtime.read_text(encoding="ascii"),
	                                       encoding="ascii")

	sys.path.insert(0, str(where))
	try:
		importlib.invalidate_caches()
		for stale in (schema.stem, "situ_runtime"):
			sys.modules.pop(stale, None)
		return importlib.import_module(schema.stem)
	finally:
		sys.path.remove(str(where))


@pytest.mark.parametrize("oracle", ORACLES, ids=[o.name for o in ORACLES])
def test_situ_agrees_with_an_independent_implementation(
		oracle: Oracle, tmp_path: Path) -> None:
	if not have(oracle.tool):
		pytest.skip(f"no `{oracle.tool}` on PATH; "
		            f"the {oracle.name} oracle did not run")

	corpus, independently, through_situ = DRIVERS[oracle.name]

	bytes_ = corpus(tmp_path)
	assert bytes_, f"{oracle.name}: the oracle produced an empty corpus"

	theirs = independently(bytes_, tmp_path)
	ours   = through_situ(build_module(oracle.schema, tmp_path), bytes_)

	# An empty comparison passes and means nothing. It is reachable here: the
	# network oracles drop frames tshark could not fully dissect, and randpkt
	# truncates, so a bad `-b` could filter every frame away.
	assert theirs, f"{oracle.name}: `{oracle.tool}` reported nothing to compare"
	assert ours, f"{oracle.name}: situ read nothing to compare"

	assert ours == theirs, (
		f"{oracle.name}: situ and `{oracle.tool}` disagree about bytes "
		f"`{oracle.tool}` wrote.\n"
		f"  situ:            {ours}\n"
		f"  {oracle.tool}:   {theirs}\n"
		f"\n{oracle.why}")


@pytest.mark.parametrize(
	"oracle", [o for o in ORACLES if o.name in LIES],
	ids=[o.name for o in ORACLES if o.name in LIES])
def test_each_oracle_notices_a_schema_that_lies(
		oracle: Oracle, tmp_path: Path) -> None:
	"""Every oracle, not just one: break its schema and require a red result.

	Two adjacent members are swapped, so the fields all still exist and only
	their offsets move. Anything genuinely reading bytes notices; anything
	that had quietly stopped comparing does not.
	"""
	if not have(oracle.tool):
		pytest.skip(f"no `{oracle.tool}` on PATH")

	corpus, independently, through_situ = DRIVERS[oracle.name]
	honest_text, lying_text = LIES[oracle.name]

	honest = oracle.schema.read_text(encoding="ascii")
	assert honest_text in honest, (
		f"{oracle.name}: the schema no longer contains the members this "
		f"test swaps; the mutation in LIES needs updating")

	broken = tmp_path / oracle.schema.name
	broken.write_text(honest.replace(honest_text, lying_text), encoding="ascii")

	bytes_ = corpus(tmp_path)
	theirs = independently(bytes_, tmp_path)

	# Refusing to read at all counts as noticing: a swap can move a member
	# past the frame, and an accessor that reports that is doing its job.
	# What must not happen is agreement.
	try:
		ours = through_situ(build_module(broken, tmp_path), bytes_)
	except Exception:                                  # noqa: BLE001
		return

	assert ours != theirs, (
		f"{oracle.name}: the oracle agreed with a schema whose members are "
		f"swapped, so it is not comparing what it claims to compare")


def test_the_corpus_is_not_this_project_s_opinion() -> None:
	"""The point of the whole file, asserted rather than trusted.

	If a corpus function ever starts returning bytes written here instead of
	bytes the third-party tool wrote, the test above keeps passing and stops
	meaning anything -- it would be comparing a schema against a vector again,
	with extra steps.
	"""
	import inspect

	import oracles

	# The ways a corpus may legitimately be produced: run a tool, delegate to
	# a helper that runs one, or call a third-party library. Anything else is
	# bytes chosen in this file, which is the thing that must not happen.
	#
	# `_pymodbus(` is here because Modbus's independent implementation is a
	# library rather than a command. The rule is "not written here", not
	# "spawned a process".
	elsewhere = ("subprocess", "_run(", "_randpkt(", "_pymodbus(",
	             "_paho_packets(")

	for oracle in ORACLES:
		corpus = DRIVERS[oracle.name][0]
		body   = inspect.getsource(corpus)
		assert any(mark in body for mark in elsewhere), (
			f"{oracle.name}: the corpus is not produced by `{oracle.tool}`")
	assert oracles.__doc__ is not None


def test_the_report_names_what_did_not_run(
		capsys: pytest.CaptureFixture[str]) -> None:
	"""A skipped oracle is a fact about this machine, and it is printed.

	`working-practice.md`: a passing check is not evidence until you know it
	checked something. A differential suite where every oracle skipped reports
	exactly as green as one where they all ran, so the count goes to stdout
	where `-s` and a CI log will carry it.
	"""
	ran     = [o.name for o in ORACLES if have(o.tool)]
	skipped = [f"{o.name} (no `{o.tool}`)" for o in ORACLES if not have(o.tool)]

	print(f"\ndifferential oracles: {len(ran)} ran"
	      f"{', ' + ', '.join(ran) if ran else ''}")
	if skipped:
		print(f"                      {len(skipped)} skipped: "
		      f"{', '.join(skipped)}")

	assert ran or skipped
