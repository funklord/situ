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

import sys
from pathlib import Path

import pytest

from every_schema import ROOT
from oracles import DRIVERS, ORACLES, Oracle, have

from situc.codegen import python as generate_py
from situc.diagnostics import Source
from situc.layout import solve
from situc.parser import parse
from situc.resolve import resolve


def build_module(schema: Path, tmp: Path) -> object:
	"""The Python accessors for one schema, importable."""
	source   = Source(str(schema), schema.read_text(encoding="ascii"))
	parsed   = parse(source)
	resolved = resolve(parsed, solve(parsed))
	emitted  = generate_py.generate(parsed, resolved, schema.stem)

	(tmp / f"{schema.stem}.py").write_text(emitted.module, encoding="ascii")
	runtime = ROOT / "runtime" / "python" / "situ_runtime.py"
	(tmp / "situ_runtime.py").write_text(runtime.read_text(encoding="ascii"),
	                                     encoding="ascii")

	sys.path.insert(0, str(tmp))
	try:
		import importlib
		for stale in (schema.stem, "situ_runtime"):
			sys.modules.pop(stale, None)
		return importlib.import_module(schema.stem)
	finally:
		sys.path.remove(str(tmp))


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

	assert ours == theirs, (
		f"{oracle.name}: situ and `{oracle.tool}` disagree about bytes "
		f"`{oracle.tool}` wrote.\n"
		f"  situ:            {ours}\n"
		f"  {oracle.tool}:   {theirs}\n"
		f"\n{oracle.why}")


def test_the_oracle_notices_a_schema_that_lies(tmp_path: Path) -> None:
	"""Break the schema on purpose and watch the oracle go red.

	A differential test that cannot fail is worth nothing, and it is exactly
	the kind that looks fine: the corpus is real, the comparison runs, and it
	would keep passing if `situ_read` returned the oracle's own answer. So
	this swaps `width` and `height` in a copy of the BMP schema -- the corpus
	is 7x5 precisely so the two are distinguishable -- regenerates, and
	requires disagreement.

	`suggestions/apt-emerge.md` asked for this in general terms: break the
	generator deliberately and confirm the generated suite goes red, because
	generated tests are produced in bulk, all look alike, and nobody reads the
	hundredth one.
	"""
	oracle = next(o for o in ORACLES if o.name == "bmp")
	if not have(oracle.tool):
		pytest.skip(f"no `{oracle.tool}` on PATH")

	corpus, independently, through_situ = DRIVERS["bmp"]

	honest = oracle.schema.read_text(encoding="ascii")
	lying  = honest.replace("\ti32          width;\n\ti32          height;",
	                        "\ti32          height;\n\ti32          width;")
	assert lying != honest, "the BMP schema no longer has the two fields to swap"

	broken = tmp_path / "bmp.situ"
	broken.write_text(lying, encoding="ascii")

	image  = corpus(tmp_path)
	theirs = independently(image, tmp_path)
	ours   = through_situ(build_module(broken, tmp_path), image)

	assert ours != theirs, (
		"the oracle agreed with a schema whose width and height are swapped, "
		"so it is not comparing what it claims to compare")


def test_the_corpus_is_not_this_project_s_opinion() -> None:
	"""The point of the whole file, asserted rather than trusted.

	If a corpus function ever starts returning bytes written here instead of
	bytes the third-party tool wrote, the test above keeps passing and stops
	meaning anything -- it would be comparing a schema against a vector again,
	with extra steps.
	"""
	import inspect

	import oracles

	for oracle in ORACLES:
		corpus = DRIVERS[oracle.name][0]
		body   = inspect.getsource(corpus)
		assert "subprocess" in body or "_run(" in body, (
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
