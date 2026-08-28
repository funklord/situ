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

from situc import ast
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


# -- generated computation, not generated layout ------------------------------
#
# Every oracle above checks where the bytes are. This one checks what situ
# *computes* from them: `gen-derived` emits CRC implementations from a kernel
# description -- a 256-entry table it calculates rather than copies -- and
# situ's own property tests read that same description, so a table built from
# a mistranscribed polynomial would agree with them forever.
#
# This existed and was deleted on 2026-08-04 by 17724a0, a commit about network
# oracles whose message never mentions it. The imports it used stayed behind,
# so nothing went red: an unused import is not a failure, and the only outside
# check on any derived codec vanished under a green suite. That is what
# `test_every_polynomial_codec_is_checked_or_excused` exists to prevent a
# second time.
#
# Two kinds of evidence here, and they are not equally strong.
#
# `zlib.crc32` and `binascii.crc_hqx` are independent *implementations* that
# ship with Python, both old enough and used enough that disagreement means
# situ is wrong. That is the real oracle, and it reaches two codecs.
#
# A published check value -- what a CRC produces for "123456789" -- is weaker,
# and saying so matters: it comes from the same catalogue the kernel
# parameters were transcribed from, so it is not a second implementation and
# must not be counted as one. What it does catch is the transcription, which
# is the failure that actually happens: a wrong poly, init, xorout or reflect
# gives a different check value. It is the only outside evidence available for
# the five CRCs the standard library does not implement.
#
# Measured, so that the strength of both is known rather than assumed:
# flipping one bit of crc32's polynomial disagrees with zlib at 8 of the 9
# lengths below. The exception is the empty input, where the result is
# init ^ xorout and the table never runs.

CRC_CASES = (
	("crc32", "situ_crc32", ctypes.c_uint32,
	 lambda data: zlib.crc32(data)),
	("crc16_ccitt", "situ_crc16_ccitt", ctypes.c_uint16,
	 lambda data: binascii.crc_hqx(data, 0xFFFF)),
)

#: Each CRC's published check value: its output over the nine bytes
#: "123456789", which is how the catalogue identifies a parameter set.
CRC_CHECK_VALUES = {
	"crc8_smbus":   (ctypes.c_uint8,  0xF4),
	"crc16_ccitt":  (ctypes.c_uint16, 0x29B1),
	"crc16_modbus": (ctypes.c_uint16, 0x4B37),
	"crc24_ble":    (ctypes.c_uint32, 0xC25A56),
	"crc32":        (ctypes.c_uint32, 0xCBF43926),
	"crc32c":       (ctypes.c_uint32, 0xE3069283),
	"crc40_gsm":    (ctypes.c_uint64, 0xD4164FC646),
	"crc8_maxim":   (ctypes.c_uint8,  0xA1),
	"crc16_xmodem": (ctypes.c_uint16, 0x31C3),
	"crc16_kermit": (ctypes.c_uint16, 0x2189),
	"crc16_usb":    (ctypes.c_uint16, 0xB4C8),
	"crc32_bzip2":  (ctypes.c_uint32, 0xFC891918),
	"crc64_xz":     (ctypes.c_uint64, 0x995DC9BBDF1939FA),
}

#: A polynomial codec neither oracle reaches, and why. Being named here is a
#: decision a reader can see and argue with; being in none of the three is the
#: silence the guard below refuses.
CRC_UNCHECKED = {
	"reed_solomon_255_223": "a block code rather than a CRC -- it has no "
	                        "check value and its own encode/decode shape",
	"reed_solomon_64_56":   "as reed_solomon_255_223",
}


@pytest.fixture(scope="module")
def kernel_library(tmp_path_factory: pytest.TempPathFactory) -> ctypes.CDLL:
	"""Build the derived codecs of `std/kernels.situ` into a shared object.

	Built once for the module rather than once per case: the nine checks
	below all read the same standard kernels, and a compile each was most
	of what they cost.

	Skips loudly without a compiler rather than passing: a differential
	test that quietly becomes a no-op is worth less than no test at all.
	"""
	compiler = shutil.which("cc") or shutil.which("gcc")
	if compiler is None:
		pytest.skip("no C compiler; the CRC oracle did not run")

	tmp = tmp_path_factory.mktemp("kernels")

	kernels = ROOT / "std" / "kernels.situ"
	source  = Source(str(kernels), kernels.read_text(encoding="ascii"))
	parsed  = parse(source)
	solved  = resolve(parsed, solve(parsed))

	(tmp / "kernels.h").write_text(
		generate_c.generate(parsed, solved, "kernels").header, encoding="ascii")
	(tmp / "derived.c").write_text(
		generate_derived.generate(parsed, "kernels"), encoding="ascii")

	shared = tmp / "kernels.so"
	built  = subprocess.run(
		[compiler, "-O2", "-shared", "-fPIC",
		 "-I", str(ROOT / "runtime" / "c"), "-I", str(tmp),
		 str(tmp / "derived.c"), "-o", str(shared)],
		capture_output=True, text=True)
	assert built.returncode == 0, built.stderr

	return ctypes.CDLL(str(shared))


def _crc(lib: ctypes.CDLL, name: str, ctype: type) -> object:
	"""One generated CRC, bound with its real signature."""
	fn = getattr(lib, f"situ_{name}")
	fn.restype  = ctype
	fn.argtypes = [ctypes.POINTER(ctypes.c_uint8), ctypes.c_uint32]
	return fn


@pytest.mark.parametrize(
	"name,symbol,ctype,independently", CRC_CASES,
	ids=[case[0] for case in CRC_CASES])
def test_a_generated_crc_matches_an_independent_implementation(
		name: str, symbol: str, ctype: type, independently: object,
		kernel_library: ctypes.CDLL) -> None:
	"""What situ computes, against what the standard library computes.

	The kernel description says width, polynomial, initial value and whether
	the input is reflected; situ turns that into a 256-entry table and a loop.
	A table generated from a wrong polynomial is wrong consistently, so situ's
	own tests -- which read the same description -- would agree with it.
	"""
	fn = _crc(kernel_library, name, ctype)

	# Lengths around the boundaries a table-driven CRC gets wrong: empty, a
	# single byte, and either side of a 256-byte table wrap.
	random.seed(20260804)
	for length in (0, 1, 2, 15, 64, 255, 256, 257, 1024):
		data = bytes(random.randrange(256) for _ in range(length))
		buf  = (ctypes.c_uint8 * max(1, length))(*data)

		assert fn(buf, length) == independently(data), (  # type: ignore[operator]
			f"{name}: situ and the standard library disagree at {length} bytes")


@pytest.mark.parametrize("name", sorted(CRC_CHECK_VALUES))
def test_a_generated_crc_produces_its_published_check_value(
		name: str, kernel_library: ctypes.CDLL) -> None:
	"""Every CRC here against the value its catalogue entry publishes.

	This is the transcription check, and it is the only outside evidence for
	the five the standard library does not implement. A parameter copied
	wrongly out of the catalogue -- the polynomial, the initial value, the
	final xor, the reflection -- lands on a different check value.
	"""
	ctype, expected = CRC_CHECK_VALUES[name]
	fn = _crc(kernel_library, name, ctype)

	data = b"123456789"
	buf  = (ctypes.c_uint8 * len(data))(*data)
	got  = fn(buf, len(data))                             # type: ignore[operator]

	assert got == expected, (
		f"{name}: situ computes {got:#x} over \"123456789\" where the "
		f"catalogue publishes {expected:#x}, so a kernel parameter in "
		f"std/kernels.situ does not say what it was meant to say")


def test_every_polynomial_codec_is_checked_or_excused() -> None:
	"""No generated CRC joins the standard kernels unchecked and unremarked.

	The section above was deleted once without anybody noticing, because
	nothing asserted that it was still there. This reads the schema rather
	than a list of its own, so a tenth polynomial codec is covered the moment
	it is added -- and deleting the cases fails here rather than quietly.
	"""
	kernels = ROOT / "std" / "kernels.situ"
	parsed  = parse(Source(str(kernels), kernels.read_text(encoding="ascii")))

	polynomial = {codec.name for codec in parsed.codecs()
	              if codec.kernel is not None
	              and codec.kernel.family is ast.KernelFamily.POLYNOMIAL}
	assert polynomial, (
		"no polynomial codec found in std/kernels.situ -- this guard is "
		"reading the wrong thing, and an empty set passes exactly as loudly "
		"as a real one")
	assert CRC_CASES, "CRC_CASES is empty, so no generated CRC is checked"

	covered = ({case[0] for case in CRC_CASES} | set(CRC_CHECK_VALUES)
	           | set(CRC_UNCHECKED))
	missing = polynomial - covered
	assert not missing, (
		f"{sorted(missing)}: a generated CRC that nothing outside this "
		f"project checks. Add it to CRC_CASES if the standard library "
		f"implements it, to CRC_CHECK_VALUES with its published check value, "
		f"or to CRC_UNCHECKED with the reason it can have neither")


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


def test_the_oracles_scratch_goes_away(tmp_path: Path) -> None:
	"""A run must leave no scratch directory behind, and the count is the
	assertion rather than the presence of a cleanup call.

	`oracles.py` used `tempfile.mkdtemp` with no `rmtree`, no `finally` and
	no `TemporaryDirectory`, and the three entry points that take no
	`tmp_path` to hand down leaked one directory each per run: 1591 of them
	accumulated over a fortnight, under a green suite. A cleanup that
	removes nothing looks exactly like one with nothing to remove, so what
	is checked here is what the run *left*, not what the source appears to
	do.

	A subprocess, because the directory goes when the interpreter does:
	inside one run the scratch is still legitimately present. The child
	prints the path it made and exits; a path that still exists after that
	is a leak.
	"""
	child = tmp_path / "child.py"
	child.write_text(
		"import sys\n"
		f"sys.path.insert(0, {str(Path(__file__).parent)!r})\n"
		"import oracles\n"
		"print(oracles._scratch('oracle-'))\n",
		encoding="ascii")

	ran = subprocess.run([sys.executable, str(child)],
	                     capture_output=True, text=True, timeout=120)
	assert ran.returncode == 0, ran.stderr

	made = Path(ran.stdout.strip())
	assert made.name.startswith("oracle-"), ran.stdout
	assert not made.exists(), (
		f"{made} outlived the process that made it: the scratch leaks")
