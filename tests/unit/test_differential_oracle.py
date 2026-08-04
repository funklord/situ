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
import random
import shutil
import subprocess
import sys
import zlib
from pathlib import Path

import pytest

from every_schema import ROOT
from oracles import DRIVERS, ORACLES, Oracle, have

from situc.codegen import c as generate_c
from situc.codegen import python as generate_py
from situc.codegen.c import derived as generate_derived
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


# -- generated computation, not generated layout ------------------------------
#
# Every oracle above checks where the bytes are. This one checks what situ
# *computes* from them: `gen-derived` emits CRC implementations from a kernel
# description -- a table it calculates rather than copies -- and until now the
# only thing that had ever checked one was situ's own property tests.
#
# `zlib.crc32` and `binascii.crc_hqx` are independent implementations that ship
# with Python, and both are old enough and used enough that disagreement means
# situ is wrong.

CRC_CASES = (
	("crc32", "situ_crc32", ctypes.c_uint32,
	 lambda data: zlib.crc32(data)),
	("crc16_ccitt", "situ_crc16_ccitt", ctypes.c_uint16,
	 lambda data: binascii.crc_hqx(data, 0xFFFF)),
)


@pytest.mark.parametrize(
	"name,symbol,ctype,independently", CRC_CASES,
	ids=[case[0] for case in CRC_CASES])
def test_a_generated_crc_matches_an_independent_implementation(
		name: str, symbol: str, ctype: type, independently: object,
		tmp_path: Path) -> None:
	"""What situ computes, against what the standard library computes.

	The kernel description says width, polynomial, initial value and whether
	the input is reflected; situ turns that into a 256-entry table and a loop.
	Nothing outside this project had ever checked the result, and a table
	generated from a wrong polynomial is a table that is wrong consistently --
	so situ's own tests, which use the same description, would agree with it.
	"""
	compiler = shutil.which("cc") or shutil.which("gcc")
	if compiler is None:
		pytest.skip("no C compiler; the CRC oracle did not run")

	kernels = ROOT / "std" / "kernels.situ"
	source  = Source(str(kernels), kernels.read_text(encoding="ascii"))
	parsed  = parse(source)
	solved  = resolve(parsed, solve(parsed))

	(tmp_path / "kernels.h").write_text(
		generate_c.generate(parsed, solved, "kernels").header, encoding="ascii")
	(tmp_path / "derived.c").write_text(
		generate_derived.generate(parsed, "kernels"), encoding="ascii")

	shared = tmp_path / "kernels.so"
	built  = subprocess.run(
		[compiler, "-O2", "-shared", "-fPIC",
		 "-I", str(ROOT / "runtime" / "c"), "-I", str(tmp_path),
		 str(tmp_path / "derived.c"), "-o", str(shared)],
		capture_output=True, text=True)
	assert built.returncode == 0, built.stderr

	lib = ctypes.CDLL(str(shared))
	fn  = getattr(lib, symbol)
	fn.restype  = ctype
	fn.argtypes = [ctypes.POINTER(ctypes.c_uint8), ctypes.c_uint32]

	# Lengths chosen around the shift boundaries a table-driven CRC gets wrong:
	# empty, single byte, and either side of a 256-byte table wrap.
	random.seed(20260804)
	for length in (0, 1, 2, 15, 64, 255, 256, 257, 1024):
		data = bytes(random.randrange(256) for _ in range(length))
		buf  = (ctypes.c_uint8 * max(1, length))(*data)

		assert fn(buf, length) == independently(data), (      # type: ignore[operator]
			f"{name}: situ and the standard library disagree at {length} bytes")


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
