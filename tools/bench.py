"""What the offset cache is worth, in each of the four backends.

Section 26.30 records the second accessor family and what it bought, and until
this existed every number on that page was C's. The other three grew the family
after the measurements were taken, so the page claimed a cost in four languages
and had evidence in one -- the last claim on those pages with nothing behind
it.

What it measures is the half of `--materialize` that is not the run index. A
member placed after a delimited one has an offset nobody can read without
rescanning everything before it, so resolving the offsets one at a time is
quadratic in how many there are and the cache is that sum once.

Two cases, because the first is the one a reader assumes and it is not where
the win is:

  * `example/http`'s request line, three members, which is the example the
    emitters' own comments cite. The cache saves nothing there, and why is
    worth knowing: resolving the offsets scans the target, and then reading
    the members scans it again. Three members leaves the quadratic nothing to
    be quadratic about.
  * an eight-field record, which is the shape the recorded C number came from.
    No schema in this repository has eight delimited members, so this one is
    written here rather than taken from the tree -- eight is not a construct,
    it is a size, which is what makes it a benchmark input rather than a
    worked example.

Not a test, and deliberately not in the suite. A wall-clock number is a
property of the machine that took it, so a threshold here would either be loose
enough to hold nothing or tight enough to fail on somebody else's laptop. It is
a thing you run, and 26.30 says what ran it.

    python3 tools/bench.py [--iterations N]

A missing toolchain is reported rather than skipped: a table with a row quietly
absent reads as a backend that has no offset cache.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

ROOT    = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "runtime"


@dataclass(frozen=True)
class Case:
	"""One schema, one struct, and the bytes to read it over.

	`members` is in declaration order. The first sits at a static offset --
	nothing precedes it -- so it has no entry in the offset cache and is read
	the same way in both loops.
	"""

	title: str
	stem: str
	source: str
	struct: str
	members: tuple[str, ...]
	payload: bytes

	@property
	def pascal(self) -> str:
		"""The Rust backend's name for the struct: `record` -> `Record`."""
		return "".join(part[:1].upper() + part[1:]
		               for part in self.struct.split("_"))


#: A 1200-byte target, which is what makes the rescan visible: the cost of
#: reading `version` twice is the cost of scanning `target` twice.
REQUEST = b"GET " + b"a" * 1200 + b" HTTP/1.1\r\n"

#: Eight fields adding up to about the same, so the two cases differ in how
#: many members there are and as little else as possible.
FIELDS = "abcdefgh"


def record_source() -> str:
	"""Eight delimited members, which nothing in the tree has."""
	lines = [
		"target buffer;",
		"endian big;",
		"",
		"// Eight delimited members, written for a measurement rather than",
		"// taken from a protocol: the cost the offset cache removes grows",
		"// with the square of that count, and three cannot show it.",
		"struct record {",
	]
	for name in FIELDS[:-1]:
		lines.append(f'\tu8  {name}[]  until ",";')
	lines.append(f'\tu8  {FIELDS[-1]}[]  until "\\r\\n";')
	lines.extend(["}", ""])
	return "\n".join(lines)


CASES = (
	Case(
		title   = "example/http's request line, three members",
		stem    = "http",
		source  = (ROOT / "example" / "http" / "http.situ").read_text(
			encoding="ascii"),
		struct  = "request_line",
		members = ("method", "target", "version"),
		payload = REQUEST,
	),
	Case(
		title   = "an eight-field record, seven dynamic offsets",
		stem    = "record",
		source  = record_source(),
		struct  = "record",
		members = tuple(FIELDS),
		payload = b",".join(b"f" * 150 for _ in FIELDS) + b"\r\n",
	),
)


def literal(payload: bytes, opener: str) -> str:
	"""The bytes as a source literal all four languages read the same way.

	Hex escapes throughout rather than printable characters: C, C++, Rust and
	Python agree on `\\xNN`, and agreeing is worth more than legibility in a
	line nobody reads.
	"""
	return opener + "".join(f"\\x{byte:02x}" for byte in payload) + '"'


C_DRIVER = """\
#include <stdio.h>
#include <time.h>

#include "{stem}.h"

static const char payload[] = {payload};

static double ms(struct timespec a, struct timespec b)
{{
	return (double)(b.tv_sec - a.tv_sec) * 1000.0
	     + (double)(b.tv_nsec - a.tv_nsec) / 1000000.0;
}}

int main(void)
{{
	struct timespec t0, t1, t2;
	unsigned long long sink = 0;
	situ_view_t view;
	int i;

	view.base = (uint8_t *)payload;
	view.limit = (uint32_t)(sizeof payload - 1u);
	view.generation = 0;

	clock_gettime(CLOCK_MONOTONIC, &t0);
	for (i = 0; i < {n}; i++) {{
{per_call}
	}}
	clock_gettime(CLOCK_MONOTONIC, &t1);
	for (i = 0; i < {n}; i++) {{
		situ_{struct}_offsets_t at;
		situ_{struct}_offsets(view, &at);
{cached}
	}}
	clock_gettime(CLOCK_MONOTONIC, &t2);

	printf("%.1f %.1f %llu\\n", ms(t0, t1), ms(t1, t2), sink);
	return 0;
}}
"""

CPP_DRIVER = """\
#include <chrono>
#include <cstdio>

#include "{stem}.hpp"

static const char payload[] = {payload};

int main()
{{
	const situ::{struct} view{{ situ_view_t{{
		(std::uint8_t *)payload, sizeof payload - 1u, 0 }} }};
	unsigned long long sink = 0;

	const auto t0 = std::chrono::steady_clock::now();
	for (int i = 0; i < {n}; i++) {{
{per_call}
	}}
	const auto t1 = std::chrono::steady_clock::now();
	for (int i = 0; i < {n}; i++) {{
		situ::{struct}::offsets at;
		view.resolve_offsets(at);
{cached}
	}}
	const auto t2 = std::chrono::steady_clock::now();

	using ms = std::chrono::duration<double, std::milli>;
	std::printf("%.1f %.1f %llu\\n", ms(t1 - t0).count(), ms(t2 - t1).count(),
	            sink);
	return 0;
}}
"""

RUST_DRIVER = """\
mod situ_rt;
mod unit;

use std::time::Instant;

static PAYLOAD: &[u8] = {payload};

fn main() {{
	let view = unit::{pascal}::new(PAYLOAD).unwrap();
	let mut sink: u64 = 0;

	let t0 = Instant::now();
	for _ in 0..{n} {{
{per_call}
	}}
	let per_call = t0.elapsed();

	let t1 = Instant::now();
	for _ in 0..{n} {{
		let at = view.resolve_offsets();
{cached}
	}}
	let cached = t1.elapsed();

	println!("{{:.1}} {{:.1}} {{}}", per_call.as_secs_f64() * 1000.0,
	         cached.as_secs_f64() * 1000.0, sink);
}}
"""

PYTHON_DRIVER = """\
import time

import situ_runtime
import {stem} as generated

payload = bytearray({payload})
msg     = situ_runtime.Message(payload)
view    = generated.{struct}(msg, 0, len(payload))
sink    = 0

t0 = time.perf_counter()
for _ in range({n}):
{per_call}
t1 = time.perf_counter()
for _ in range({n}):
	at = view.resolve_offsets()
{cached}
t2 = time.perf_counter()

print("%.1f %.1f %d" % ((t1 - t0) * 1000.0, (t2 - t1) * 1000.0, sink))
"""


def reads(case: Case, per_call: str, cached: str, indent: str) -> tuple[str, str]:
	"""The two loop bodies: every member read the slow way, then the fast one.

	Both read every member, because the question is what one pass over a
	message costs rather than what one accessor costs. The first member is
	read identically in both -- its offset is a constant either way -- so what
	the difference isolates is the rescan behind the others.
	"""
	return (
		"\n".join(indent + per_call.format(member=name)
		          for name in case.members),
		"\n".join(indent + (per_call if index == 0 else cached)
		          .format(member=name)
		          for index, name in enumerate(case.members)),
	)


def run(command: list[str], cwd: Path) -> str:
	"""A build or a driver, with the failure readable.

	`check=True` alone reports an exit status and swallows the diagnostic,
	which for a compiler is the whole of the message.
	"""
	result = subprocess.run(command, cwd=cwd, capture_output=True, text=True)
	if result.returncode != 0:
		raise SystemExit(f"{command[0]} failed:\n{result.stderr}")
	return result.stdout


def generate(case: Case, out: Path, target: str) -> None:
	"""The accessors, through the CLI a user would run.

	`--materialize` is the whole point: the offset cache is off by default,
	being memory the caller did not ask for (decision 0022).
	"""
	schema = out / f"{case.stem}.situ"
	schema.write_text(case.source, encoding="ascii")
	run([sys.executable, "-m", "situc", "build", "--target", target,
	     "--materialize", "--out", str(out), str(schema)], ROOT)


#: How many times each driver runs. The fastest is kept, per column: a
#: measurement is a lower bound the machine got in the way of, and a mean over
#: a noisy laptop measures the laptop. Three runs moved C's eight-field number
#: from 168ms one run and 278ms the next to within a few percent of itself.
REPEATS = 3


def timings(command: list[str], cwd: Path) -> tuple[float, float]:
	"""Run a built driver, and keep the fastest of its runs per column."""
	taken = []
	for _ in range(REPEATS):
		per_call, cached, _sink = run(command, cwd).split()
		taken.append((float(per_call), float(cached)))

	return min(one for one, _ in taken), min(other for _, other in taken)


def bench_c(case: Case, out: Path, iterations: int) -> tuple[float, float]:
	generate(case, out, "c")
	slow, fast = reads(
		case,
		f"sink += situ_{case.struct}_{{member}}_len(view);",
		f"sink += situ_{case.struct}_{{member}}_len_from(view, at.{{member}});",
		"\t\t")
	(out / "driver.c").write_text(
		C_DRIVER.format(n=iterations, stem=case.stem, struct=case.struct,
		                payload=literal(case.payload, '"'),
		                per_call=slow, cached=fast),
		encoding="ascii")

	compiler = shutil.which("gcc") or shutil.which("cc")
	assert compiler is not None
	# `clock_gettime` is POSIX and `-std=c11` hides it. The driver is a
	# measuring instrument rather than generated code, and asks for the clock.
	run([compiler, "-O2", "-std=c11", "-D_POSIX_C_SOURCE=199309L",
	     f"-I{RUNTIME / 'c'}", f"-I{out}", str(out / "driver.c"),
	     str(out / f"{case.stem}.c"), str(RUNTIME / "c" / "situ.c"),
	     "-o", str(out / "bench")], out)
	return timings([str(out / "bench")], out)


def bench_cpp(case: Case, out: Path, iterations: int) -> tuple[float, float]:
	generate(case, out, "cpp")
	slow, fast = reads(case, "sink += view.{member}_len();",
	                   "sink += view.{member}_len_from(at.{member});", "\t\t")
	(out / "driver.cpp").write_text(
		CPP_DRIVER.format(n=iterations, stem=case.stem, struct=case.struct,
		                  payload=literal(case.payload, '"'),
		                  per_call=slow, cached=fast),
		encoding="ascii")

	compiler = shutil.which("g++") or shutil.which("clang++")
	assert compiler is not None
	run([compiler, "-O2", "-std=c++17", f"-I{RUNTIME / 'c'}",
	     f"-I{RUNTIME / 'cpp'}", f"-I{out}", str(out / "driver.cpp"),
	     str(RUNTIME / "c" / "situ.c"), "-o", str(out / "bench")], out)
	return timings([str(out / "bench")], out)


def bench_rust(case: Case, out: Path, iterations: int) -> tuple[float, float]:
	generate(case, out, "rust")
	(out / "unit.rs").write_text(
		(out / f"{case.stem}.rs").read_text(encoding="ascii"), encoding="ascii")
	# The runtime is `no_std` and a driver is not: the same strip the suite
	# does to compile it against `std`.
	(out / "situ_rt.rs").write_text(
		(RUNTIME / "rust" / "situ_rt.rs").read_text(encoding="ascii")
		.replace("#![no_std]\n", ""), encoding="ascii")

	slow, fast = reads(case, "sink += view.{member}_len() as u64;",
	                   "sink += view.{member}_len_from(at.{member}) as u64;",
	                   "\t\t")
	(out / "driver.rs").write_text(
		RUST_DRIVER.format(n=iterations, pascal=case.pascal,
		                   payload=literal(case.payload, 'b"'),
		                   per_call=slow, cached=fast),
		encoding="ascii")

	rustc = shutil.which("rustc")
	assert rustc is not None
	run([rustc, "-O", "--edition", "2021", str(out / "driver.rs"),
	     "-o", str(out / "bench")], out)
	return timings([str(out / "bench")], out)


def bench_python(case: Case, out: Path,
		iterations: int) -> tuple[float, float]:
	generate(case, out, "python")
	(out / "situ_runtime.py").write_text(
		(RUNTIME / "python" / "situ_runtime.py").read_text(encoding="ascii"),
		encoding="ascii")

	slow, fast = reads(case, "sink += view.{member}_len",
	                   'sink += view.{member}_len_from(at["{member}"])', "\t")
	(out / "driver.py").write_text(
		PYTHON_DRIVER.format(n=iterations, stem=case.stem, struct=case.struct,
		                     payload=literal(case.payload, 'b"'),
		                     per_call=slow, cached=fast),
		encoding="ascii")

	return timings([sys.executable, str(out / "driver.py")], out)


BACKENDS = (
	("C",      bench_c,      ("gcc", "cc")),
	("C++",    bench_cpp,    ("g++", "clang++")),
	("Rust",   bench_rust,   ("rustc",)),
	("Python", bench_python, ()),
)


def main() -> int:
	parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
	# The default is what 26.30's table was taken with: a reader re-deriving
	# those numbers should not have to guess the workload as well as the
	# machine.
	parser.add_argument("--iterations", type=int, default=100000,
	                    help="reads of the whole struct per measurement")
	args = parser.parse_args()

	with tempfile.TemporaryDirectory() as raw:
		work = Path(raw)
		for case in CASES:
			print(f"\n**{case.title}**: {args.iterations} reads of every "
			      f"member, over {len(case.payload)} bytes\n")
			print("| | per-call offsets | offset cache | |")
			print("|---|---|---|---|")

			for name, bench, tools in BACKENDS:
				if tools and not any(shutil.which(tool) for tool in tools):
					print(f"| {name} | -- | -- | no {tools[0]} |")
					continue

				out = work / f"{case.stem}-{name.lower()}"
				out.mkdir()
				per_call, cached = bench(case, out, args.iterations)
				print(f"| {name} | {per_call:.0f} ms | {cached:.0f} ms | "
				      f"{per_call / cached:.1f}x |")

	return 0


if __name__ == "__main__":
	sys.exit(main())
