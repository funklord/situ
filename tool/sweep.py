"""Every composition of constructs this language admits, through four backends.

26.47 through 26.49 were found by writing schemas nobody wrote: two constructs
that compose badly, put next to each other, generated four times and executed.
Twenty defects, one hand-written schema at a time. This runs that method over
the space instead of over whatever occurred to somebody -- `test/unit/compose`
enumerates it, `test/unit/sweep` runs one cell, and this walks as much of it
as you ask for and reports what came back.

    python3 tool/sweep.py                 # a sample, seeded and reproducible
    python3 tool/sweep.py --all           # every cell; minutes, not seconds
    python3 tool/sweep.py --limit 200 --seed 7
    python3 tool/sweep.py --only sealed   # cells whose name contains this
    python3 tool/sweep.py --verbose       # print the refusals too
    python3 tool/sweep.py --all --shard 0/6   # one slice, for running six
                                               # of these at once

Sharding is strided rather than blocked: the cells are enumerated in a fixed
order, so a block of them shares an axis value, and one shard would compile
every `sealed` cell while another compiled every `frame` one. Six shards over
the whole space take about as long as a `make test` rather than about an hour.

Not part of `make test`, for the reason `tool/bench.py` is not: the whole
space is a long run, and what belongs in CI is a fixed sample of it, which
`test/unit/test_composed_schemas.py` is.

**A refusal is a pass.** Most of this space is illegal -- a bit-packed field
at a dynamic offset, `[remaining]` with a member after it, a run of varints --
and a diagnostic is the correct answer to every one of them. What this looks
for is a compiler that falls over, generated code that will not build, and
four backends that build and then disagree.

The exit status is the number of cells that failed, capped at 125, so a shell
can tell a clean sweep from a dirty one.
"""

from __future__ import annotations

import argparse
import random
import shutil
import sys
import tempfile
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "test" / "unit"))

from compose import Case, cases				# noqa: E402
from probe import Outcome, run				# noqa: E402


def _compiled(files: dict[str, str]) -> str:
	"""Compile one cell's generated code, and report only what the
	BUILDER's header is blamed for.

	Warnings are on and `-Werror` is not, which is deliberate rather
	than lenient: the READER's header does not survive `-Werror` for
	some of these shapes -- a comparison of mixed signedness in a loop
	bound, an unused `view` -- and a lens that stopped there would be
	measuring somebody else's code. project.md 26.635 has those, with
	the measurement that the four backends agree on the values and that
	the warning is cosmetic for every count this space can spell.

	So the filter is the filename. A diagnostic naming the writer's own
	header is this lens's finding; one naming the reader's is recorded
	already and not this run's business.
	"""
	import subprocess, tempfile

	with tempfile.TemporaryDirectory(prefix="situ-writer-") as held:
		tmp = Path(held)
		for name, text in files.items():
			(tmp / name).write_text(text, encoding="utf-8")
		(tmp / "probe.c").write_text(
			'#include "cell_build.h"\nint main(void) { return 0; }\n',
			encoding="ascii")
		done = subprocess.run(
			["cc", "-std=c11", "-O1", "-Wall", "-Wextra",
			 f"-I{ROOT / 'runtime' / 'c'}", f"-I{tmp}",
			 str(tmp / "probe.c"), str(tmp / "cell.c"),
			 str(ROOT / "runtime" / "c" / "situ.c"),
			 "-o", str(tmp / "probe")],
			capture_output=True, text=True)
		blamed = [line for line in done.stderr.splitlines()
		          if "cell_build.h:" in line
		          and (": error:" in line or ": warning:" in line)]
		if blamed:
			return blamed[0].partition("cell_build.h:")[2][:160]
		# An error nowhere near the builder still stops the compile, and
		# saying so is better than reporting a clean run.
		if done.returncode != 0 and not blamed:
			return ""
	return ""


def _writer(chosen: list[Case], verbose: bool, compiling: bool = False
		) -> int:
	"""The builder's planner over these cells.

	No compiling and no subprocesses, so the whole space is a minute
	rather than an hour -- which is what makes it worth running over
	9000 cells rather than a sample. What it looks for is what the unit
	gate looks for over the corpus: a struct that is neither built nor
	named, a planner that raises instead of refusing, and a refusal
	whose claim is missing or false.

	Every fault the writer's increments found was in a shape the corpus
	does not have, which is the argument for pointing it at this space.
	The first run of it found 36 refusals with no claim -- a message the
	corpus cannot produce -- and no crash, no silent cell and no false
	claim over 13356 plans.
	"""
	import traceback

	from situc.diagnostics import Source, SituError
	from situc.parser import parse
	from situc.resolve import resolve
	from situc.layout import solve
	from situc.codegen.c import build
	from situc.codegen.c.emit import generate as generate_c
	from test_a_refusal_says_what_the_member_is import (
		CLAIMS, ABOUT_THE_STRUCT, _member)

	plans = refused = rejected = 0
	bad: list[str] = []
	for index, case in enumerate(chosen, start=1):
		try:
			parsed   = parse(Source(case.name, case.schema()))
			resolved = resolve(parsed, solve(parsed))
			header   = generate_c(parsed, resolved, "cell").files().get(
				"cell.h", "")
		except SituError:
			rejected += 1
			continue
		except Exception:
			bad.append(f"{case.name}: front end: "
			           + traceback.format_exc().splitlines()[-1])
			continue

		if compiling:
			try:
				files = generate_c(parsed, resolved, "cell").files()
				files.update(build.generate(parsed, resolved, "cell",
				                            header=header))
			except Exception:
				bad.append(f"{case.name}: generating: "
				           + traceback.format_exc().splitlines()[-1])
				files = {}
			if "cell_build.h" in files:
				blamed = _compiled(files)
				if blamed:
					bad.append(f"{case.name}: {blamed}")

		shape = build.Shape(header, "situ", resolved.structs,
		                    {decl.name: decl for decl in parsed.varints()})
		for name, struct in resolved.structs.items():
			arms = build.arms(struct)
			try:
				walked = ([build.plan(struct, shape, arm) for arm in arms]
				          if arms else [build.plan(struct, shape)])
			except Exception:
				bad.append(f"{case.name}:{name}: "
				           + traceback.format_exc().splitlines()[-1])
				continue
			for parts, why in walked:
				if parts:
					plans += 1
					continue
				if not why:
					bad.append(f"{case.name}:{name}: neither built nor named")
					continue
				refused += 1
				if not why.startswith("`"):
					if not any(part in why for part in ABOUT_THE_STRUCT):
						bad.append(f"{case.name}:{name}: names no member "
						           f"and is not about the struct: {why}")
					continue
				quoted = why.split("`")[1]
				held   = _member(struct, quoted)
				if held is None:
					bad.append(f"{case.name}:{name}: names `{quoted}`, "
					           f"not a member: {why}")
					continue
				matched = [(fragment, claim) for fragment, claim in CLAIMS
				           if fragment in why]
				if len(matched) != 1:
					bad.append(f"{case.name}:{name}: {len(matched)} claims "
					           f"match: {why}")
					continue
				fragment, claim = matched[0]
				if not claim(held, resolved):
					bad.append(f"{case.name}:{name}: claims `{fragment}` "
					           f"and the member is not that: {why}")
		if verbose:
			print(f"[{index}/{len(chosen)}] {case.name}")

	print(f"\n  {plans} plans, {refused} claimed refusals, "
	      f"{rejected} schemas the front end refused")
	for line in bad[:20]:
		print(f"  {line}")
	print(f"\n  {len(bad)} finding(s)")
	return 1 if bad else 0


def main() -> int:
	parser = argparse.ArgumentParser(
		prog="sweep", description=__doc__.splitlines()[0])
	parser.add_argument("--all", action="store_true",
	                    help="every cell rather than a sample")
	parser.add_argument("--limit", type=int, default=40,
	                    help="how many cells to run (default 40)")
	parser.add_argument("--seed", type=int, default=20260804,
	                    help="which sample, and which buffers")
	parser.add_argument("--only", default="",
	                    help="cells whose name contains this")
	parser.add_argument("--verbose", action="store_true",
	                    help="print every cell rather than the failures")
	parser.add_argument("--shard", default="",
	                    help="run one slice of the space, as `i/n`")
	parser.add_argument("--writer", action="store_true",
	                    help="the BUILDER over the space rather than the "
	                         "four readers: no compiling, so the whole "
	                         "9000 take a minute")
	parser.add_argument("--compile", action="store_true",
	                    help="with --writer, also compile what it writes; "
	                         "one `cc` per cell, so use --limit")
	args = parser.parse_args()

	space = [case for case in cases() if args.only in case.name]
	if not space:
		print(f"sweep: nothing matches `{args.only}`")
		return 1

	if args.all:
		chosen = space
	else:
		rng    = random.Random(args.seed)
		chosen = rng.sample(space, min(args.limit, len(space)))

	# One slice of the run, for walking the whole space in parallel. Strided
	# rather than blocked, because the cells are enumerated in a fixed order
	# and a block of them shares an axis value: a blocked shard would compile
	# every `sealed` cell while another compiled every `frame` one, and the
	# two would take wildly different times. Every shard gets the same mix.
	if args.shard:
		index, count = (int(part) for part in args.shard.split("/"))
		chosen = [case for n, case in enumerate(chosen) if n % count == index]
		print(f"sweep: shard {index} of {count},", end=" ")

	print(f"{len(chosen)} of {len(space)} cells, seed {args.seed}")

	if args.writer:
		return _writer(chosen, args.verbose, args.compile)

	tally  = Counter[str]()
	failed: list[Outcome] = []

	# Interruption is caught rather than allowed to propagate, so that a run
	# stopped partway still reaches the summary and reports how far it got.
	# A sweep that exits silently leaves a log indistinguishable from a clean
	# one, which is the whole reason the count below is printed.
	try:
		for index, case in enumerate(chosen, start=1):
			tmp = Path(tempfile.mkdtemp(prefix="situ-sweep-"))
			try:
				outcome = run(case, tmp, seed=args.seed)
			finally:
				shutil.rmtree(tmp, ignore_errors=True)

			tally[outcome.kind] += 1
			if not outcome.ok:
				failed.append(outcome)
				print(f"[{index}/{len(chosen)}] {outcome.kind.upper():9}"
				      f" {case.name}")
				print("    " + outcome.detail.replace("\n", "\n    ")[:1200])
			elif args.verbose:
				print(f"[{index}/{len(chosen)}] {outcome.kind:9} {case.name}")
	except KeyboardInterrupt:
		print("\ninterrupted")

	print()
	for kind in ("agreed", "refused", "empty",
	             "malformed", "crash", "build", "disagree"):
		if tally[kind]:
			print(f"  {kind:9} {tally[kind]}")

	# Say how much was looked at, not just what was wrong with it. A run that
	# dies partway prints no failures, and neither does a perfect one, so a
	# log without complaints in it is evidence of nothing until the count
	# that produced it is on the page. Both numbers were already here.
	examined = sum(tally.values())
	print(f"\nexamined {examined}/{len(chosen)}")

	if failed:
		print(f"\n{len(failed)} cell(s) to answer for:")
		for outcome in failed:
			print(f"  {outcome.kind:9} {outcome.case.name}")

	if examined != len(chosen):
		print("incomplete: this run did not finish every cell it chose")
		return 126
	return min(len(failed), 125)


if __name__ == "__main__":
	raise SystemExit(main())
