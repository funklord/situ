"""The three vector targets added at 26.829, built and run over the corpus.

`gen-tests` reached C alone until this run, and section 1 names four
languages. The C suite is built and run by `make test-c`; these three were
verified once, by hand, which is how a target starts rotting -- a generator
nothing exercises looks exactly like one that works.

**The compile is the gate that matters here and the unit test in
`test_cli.py` says so.** That one checks each target names every case,
keyed on the shared case symbol, and cannot tell a call from a definition:
with the C++ emitter cut to one case every symbol still appeared, because
`main` dispatches to all of them. What catches a dropped body is the
compiler -- `error: 'header_minimum' was not declared in this scope` -- so
this is where that is caught.

Python has no compiler, which is why its suite is the one that found a real
fault the first time it ran: `at()` takes a length for a variable struct and
13 of the corpus's 27 vector files raised `TypeError` for want of it. For
Python, running IS the compile.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
RUSTC = shutil.which("rustc")
CXX = shutil.which("g++") or shutil.which("c++")


def vector_schemas() -> list[Path]:
	"""Every schema with a `.vectors` beside it, asked of the filesystem.

	`test/generated/Makefile` derives its own list the same way and says
	why: a hand-kept list is a second answer to a question the filesystem
	answers, and the two drift in the direction nobody notices -- a corpus
	file that is never run looks exactly like one that passes.
	"""
	found = sorted(
		path.with_suffix(".situ")
		for path in [*ROOT.glob("example/*/*.vectors"),
		             *ROOT.glob("test/schema/*.vectors")]
		if path.with_suffix(".situ").exists())
	assert len(found) > 20, f"only {len(found)} schemas with vectors; the corpus moved"
	return found


def emit(schema: Path, target: str, out: Path) -> None:
	out.mkdir(parents=True, exist_ok=True)
	for command in (
		["gen-tests", str(schema), str(schema.with_suffix(".vectors")),
		 "--target", target, "--out", str(out)],
		["build", str(schema), "--target", target, "--out", str(out)],
	):
		done = subprocess.run(
			[sys.executable, "-m", "situc", *command],
			capture_output=True, text=True, cwd=ROOT)
		assert done.returncode == 0, f"{command[0]} {target}: {done.stderr[-700:]}"


@pytest.mark.parametrize("schema", vector_schemas(), ids=lambda p: p.stem)
def test_the_python_vector_suite_runs(schema: Path, tmp_path: Path) -> None:
	"""Python first, because it is the one with no compiler: running the
	suite is the only thing that executes the generated accessors at all."""
	emit(schema, "python", tmp_path)
	shutil.copy(ROOT / "runtime" / "python" / "situ_runtime.py", tmp_path)

	done = subprocess.run(
		[sys.executable, "-m", "unittest", f"{schema.stem}_vectors"],
		capture_output=True, text=True, cwd=tmp_path)
	assert done.returncode == 0, done.stderr[-2000:]
	# A suite that ran nothing exits 0 as loudly as one that ran everything.
	assert "Ran 0 tests" not in done.stderr, done.stderr


@pytest.mark.skipif(RUSTC is None, reason="needs rustc")
@pytest.mark.parametrize("schema", vector_schemas(), ids=lambda p: p.stem)
def test_the_rust_vector_suite_builds_and_runs(
	schema: Path, tmp_path: Path,
) -> None:
	emit(schema, "rust", tmp_path)
	# `#![no_std]` stripped, as `fourway` strips it: a test crate links the
	# standard library and the runtime is written to be embeddable without.
	(tmp_path / "situ_rt.rs").write_text(
		(ROOT / "runtime" / "rust" / "situ_rt.rs")
		.read_text(encoding="ascii").replace("#![no_std]\n", ""),
		encoding="ascii")

	assert RUSTC is not None
	built = subprocess.run(
		[RUSTC, "--edition", "2021", "-O", "-A", "warnings", "--test",
		 f"{schema.stem}_vectors.rs", "-o", "vectors"],
		capture_output=True, text=True, cwd=tmp_path)
	assert built.returncode == 0, built.stderr[-2000:]

	done = subprocess.run([str(tmp_path / "vectors")],
	                      capture_output=True, text=True, cwd=tmp_path)
	assert done.returncode == 0, done.stdout[-2000:]

	# The count is READ and not matched. This was `"0 passed" not in
	# stdout`, which `20 passed` contains -- so the guard against a
	# vacuous pass would have failed any crate whose test count ended in a
	# zero, and passed only because none did. Found when the same line,
	# copied into the checks gate below, met seven schemas that do
	# (26.845).
	ran = re.search(r"test result: \w+\. (\d+) passed", done.stdout)
	assert ran is not None, done.stdout
	assert int(ran.group(1)) > 0, done.stdout


@pytest.mark.skipif(CXX is None, reason="needs g++")
@pytest.mark.parametrize("schema", vector_schemas(), ids=lambda p: p.stem)
def test_the_cpp_vector_suite_builds_and_runs(
	schema: Path, tmp_path: Path,
) -> None:
	emit(schema, "cpp", tmp_path)

	assert CXX is not None
	built = subprocess.run(
		[CXX, "-std=c++17", "-O1", "-I.",
		 f"-I{ROOT / 'runtime' / 'c'}", f"-I{ROOT / 'runtime' / 'cpp'}",
		 f"{schema.stem}_vectors.cpp", str(ROOT / "runtime" / "c" / "situ.c"),
		 "-o", "vectors"],
		capture_output=True, text=True, cwd=tmp_path)
	assert built.returncode == 0, built.stderr[-2000:]

	done = subprocess.run([str(tmp_path / "vectors")],
	                      capture_output=True, text=True, cwd=tmp_path)
	assert done.returncode == 0, done.stdout[-2000:]
	# The suite prints its own verdict, and a run of nothing is not a pass.
	assert "0 of 0 vector(s) failed" not in done.stdout, done.stdout


def check_schemas() -> list[Path]:
	"""Every corpus schema, since `gen-checks` needs no vectors beside it.

	Asked of the filesystem for `vector_schemas`' reason. `std/` is left out:
	those declare codecs and no struct, so there is no accessor to hold to
	anything.
	"""
	found = sorted([*ROOT.glob("example/*/*.situ"),
	                *ROOT.glob("test/schema/*.situ")])
	assert len(found) > 30, f"only {len(found)} schemas; the corpus moved"
	return found


@pytest.mark.parametrize("schema", check_schemas(), ids=lambda p: p.stem)
def test_the_python_fuzz_harness_drives_the_accessors(
	schema: Path, tmp_path: Path,
) -> None:
	"""`gen-fuzz --target python`, driven with random bytes and RUN.

	**No `skipif` on a fuzzer's absence**, which is the point of the
	target's shape: `one_input` takes no dependency and a stdin `main`
	drives it, so this repository's own gates run the harness rather than
	skip where `atheris` is not installed. That split is `--target c`'s,
	whose `SITU_FUZZ_STANDALONE` main exists "so the harness can be built
	and run without a fuzzing engine. This is what keeps it from rotting
	between runs" (26.846).

	**And the reads are counted, because a harness that crashes on nothing
	and a harness that does nothing look identical from outside.** `sink`
	is where every field read goes, so patching it counts them: a run that
	performed none would be a harness that took a view and asked it
	nothing, which is the vacuous pass this file guards against
	everywhere else.
	"""
	out = tmp_path / "gen"
	out.mkdir(parents=True, exist_ok=True)
	emitted = subprocess.run(
		[sys.executable, "-m", "situc", "gen-fuzz", str(schema),
		 "--target", "python", "--out", str(out)],
		capture_output=True, text=True, cwd=ROOT)
	if emitted.returncode != 0:
		if "parameter" in emitted.stderr:
			pytest.skip("gen-fuzz declines a schema taking an argument")
		raise AssertionError(emitted.stderr[-700:])

	built = subprocess.run(
		[sys.executable, "-m", "situc", "build", str(schema),
		 "--target", "python", "--out", str(out)],
		capture_output=True, text=True, cwd=ROOT)
	assert built.returncode == 0, built.stderr[-700:]
	shutil.copy(ROOT / "runtime" / "python" / "situ_runtime.py", out)

	# **The inputs are sized from the harness, not from a round number.**
	# Written with `rng.randrange(0, 80)` first, and `cpio` read nothing:
	# its struct needs 110 bytes, so every input fell short of the guard
	# and the harness returned before touching an accessor. A fuzz driver
	# whose inputs cannot reach the minimum is a driver that tests the
	# length check and nothing else.
	harness = (out / f"{schema.stem}_fuzz.py").read_text(encoding="ascii")
	widest = max((int(one) for one in
	              re.findall(r"if len\(data\) < (\d+):", harness)),
	             default=16)

	driver = (
		"import importlib, random\n"
		f"mod = importlib.import_module('{schema.stem}_fuzz')\n"
		"reads = 0\n"
		"real = mod.sink\n"
		"def counting(value):\n"
		"    global reads\n"
		"    reads += 1\n"
		"    return real(value)\n"
		"mod.sink = counting\n"
		"rng = random.Random(20261103)\n"
		"for _ in range(300):\n"
		f"    n = rng.randrange(0, {widest * 2 + 8})\n"
		"    mod.one_input(bytes(rng.randrange(256) for _ in range(n)))\n"
		"print(reads)\n")
	done = subprocess.run([sys.executable, "-c", driver],
	                      capture_output=True, text=True, cwd=out)
	assert done.returncode == 0, done.stderr[-2000:]

	# **Only where the harness has a read to perform.** A schema whose
	# structs carry no plain scalar field has `pass` where the reads would
	# be -- `example/protobuf` is a bare `tlv` region and `example/slip` is
	# framing -- so zero is the true answer there and asserting otherwise
	# would be demanding a getter the schema does not describe.
	reads = int(done.stdout.strip().splitlines()[-1])
	if "\t\tsink(view." in harness:
		assert reads > 0, "the harness took views and read nothing"


@pytest.mark.skipif(RUSTC is None, reason="needs rustc")
@pytest.mark.parametrize("schema", check_schemas(), ids=lambda p: p.stem)
def test_the_rust_checks_suite_builds_and_runs(
	schema: Path, tmp_path: Path,
) -> None:
	"""`gen-checks --target rust`: seven of the nine kinds, built and run.

	The fourth backend of the four, and the compile is again the gate that
	matters: `rustc --test` is what catches a body that does not build, and
	it caught two things here that no review would have (26.845).

	No `--layer frame`: the framing kind is one of the two this target
	declines, so nothing imports a frame module.
	"""
	out = tmp_path / "gen"
	out.mkdir(parents=True, exist_ok=True)
	emitted = subprocess.run(
		[sys.executable, "-m", "situc", "gen-checks", str(schema),
		 "--target", "rust", "--out", str(out)],
		capture_output=True, text=True, cwd=ROOT)
	if emitted.returncode != 0:
		if "parameter" in emitted.stderr:
			pytest.skip("gen-checks declines a schema taking an argument")
		raise AssertionError(emitted.stderr[-700:])

	built = subprocess.run(
		[sys.executable, "-m", "situc", "build", str(schema),
		 "--target", "rust", "--out", str(out)],
		capture_output=True, text=True, cwd=ROOT)
	assert built.returncode == 0, built.stderr[-700:]

	# `#![no_std]` stripped, as the vector crates strip it: a test crate
	# links the standard library and the runtime is written to be
	# embeddable without.
	(out / "situ_rt.rs").write_text(
		(ROOT / "runtime" / "rust" / "situ_rt.rs")
		.read_text(encoding="ascii").replace("#![no_std]\n", ""),
		encoding="ascii")

	assert RUSTC is not None
	compiled = subprocess.run(
		[RUSTC, "--edition", "2021", "-A", "warnings", "--test",
		 f"{schema.stem}_checks.rs", "-o", "checks"],
		capture_output=True, text=True, cwd=out)
	assert compiled.returncode == 0, compiled.stderr[-2000:]

	done = subprocess.run([str(out / "checks")], capture_output=True,
	                      text=True, cwd=out)
	assert done.returncode == 0, done.stdout[-2000:]

	# A crate of no tests passes as loudly as one that checked everything,
	# so the count is read rather than matched. **Written as
	# `"0 passed" not in stdout` first, and it failed for seven schemas**
	# -- `20 passed` contains `0 passed`, so a guard whose whole purpose is
	# to catch a vacuous pass was itself answering about the wrong thing.
	# The same shape as the kinds it is guarding, one level out.
	ran = re.search(r"test result: \w+\. (\d+) passed", done.stdout)
	assert ran is not None, done.stdout
	assert int(ran.group(1)) > 0, done.stdout


@pytest.mark.skipif(CXX is None, reason="needs g++")
@pytest.mark.parametrize("schema", check_schemas(), ids=lambda p: p.stem)
def test_the_cpp_checks_suite_builds_and_runs(
	schema: Path, tmp_path: Path,
) -> None:
	"""`gen-checks --target cpp`: eight of the nine kinds, compiled and run.

	**The compile is the gate that matters here**, which is what the
	vectors tests above say of themselves: the unit tests in `test_cli.py`
	check that the emitter names what it should, and cannot tell a call
	from a definition. What catches a body that does not build is g++.

	`--layer frame` as well, because the framing kind includes
	`<name>_frame.hpp` where it emitted one -- and a suite that fails to
	COMPILE has asked none of its checks, which is the worst place to fail.

	A schema `gen-checks` declines -- one taking a `parameter`, which the
	`refuse_parameters` family all decline -- is skipped rather than
	failed, the refusal being the generator working.
	"""
	out = tmp_path / "gen"
	out.mkdir(parents=True, exist_ok=True)
	emitted = subprocess.run(
		[sys.executable, "-m", "situc", "gen-checks", str(schema),
		 "--target", "cpp", "--out", str(out)],
		capture_output=True, text=True, cwd=ROOT)
	if emitted.returncode != 0:
		if "parameter" in emitted.stderr:
			pytest.skip("gen-checks declines a schema taking an argument")
		raise AssertionError(emitted.stderr[-700:])

	built = subprocess.run(
		[sys.executable, "-m", "situc", "build", str(schema),
		 "--target", "cpp", "--layer", "frame", "--out", str(out)],
		capture_output=True, text=True, cwd=ROOT)
	assert built.returncode == 0, built.stderr[-700:]

	assert CXX is not None
	compiled = subprocess.run(
		[CXX, "-std=c++17", "-O1", "-I.", f"-I{ROOT / 'runtime' / 'c'}",
		 f"-I{ROOT / 'runtime' / 'cpp'}", f"{schema.stem}_checks.cpp",
		 str(ROOT / "runtime" / "c" / "situ.c"), "-o", "checks"],
		capture_output=True, text=True, cwd=out)
	assert compiled.returncode == 0, compiled.stderr[-2000:]

	done = subprocess.run([str(out / "checks")], capture_output=True,
	                      text=True, cwd=out)
	assert done.returncode == 0, done.stdout[-2000:]
	# A suite of nothing exits 0 as loudly as one that checked everything.
	assert "0 of 0 check(s) failed" not in done.stdout, done.stdout


@pytest.mark.parametrize("schema", check_schemas(), ids=lambda p: p.stem)
def test_the_python_checks_suite_runs(schema: Path, tmp_path: Path) -> None:
	"""`gen-checks --target python`, the first of the nine kinds `--target c`
	emits (26.831): a buffer one byte short of a struct's minimum must not
	yield a view.

	Python has no compiler, so running the suite is the only thing that
	executes the generated accessors -- and this is the check section 12.3
	calls the single one everything after it trusts, so a backend that got
	it wrong would have every other guarantee resting on nothing.

	A schema `gen-checks` declines -- one taking a `parameter`, which the
	`refuse_parameters` family all decline -- is skipped rather than failed,
	because the refusal is the generator working.
	"""
	out = tmp_path / "gen"
	out.mkdir(parents=True, exist_ok=True)
	emitted = subprocess.run(
		[sys.executable, "-m", "situc", "gen-checks", str(schema),
		 "--target", "python", "--out", str(out)],
		capture_output=True, text=True, cwd=ROOT)
	if emitted.returncode != 0:
		if "parameter" in emitted.stderr:
			pytest.skip("gen-checks declines a schema taking an argument")
		raise AssertionError(emitted.stderr[-700:])

	# **`--layer frame` as well**, because the ninth kind's checks drive the
	# stream reader and the suite imports `<name>_frame` where it emitted
	# one. The frame layer is a separate artifact, so a suite built without
	# it would fail on IMPORT rather than on a check -- the worst place for
	# a suite to fail, since nothing it was meant to assert gets asked.
	built = subprocess.run(
		[sys.executable, "-m", "situc", "build", str(schema),
		 "--target", "python", "--layer", "frame", "--out", str(out)],
		capture_output=True, text=True, cwd=ROOT)
	assert built.returncode == 0, built.stderr[-700:]
	shutil.copy(ROOT / "runtime" / "python" / "situ_runtime.py", out)

	done = subprocess.run(
		[sys.executable, "-m", "unittest", f"{schema.stem}_checks"],
		capture_output=True, text=True, cwd=out)
	assert done.returncode == 0, done.stderr[-2000:]
	# A suite that ran nothing exits 0 as loudly as one that ran everything.
	assert "Ran 0 tests" not in done.stderr, done.stderr
