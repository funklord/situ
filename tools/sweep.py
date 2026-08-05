"""Every composition of constructs this language admits, through four backends.

26.47 through 26.49 were found by writing schemas nobody wrote: two constructs
that compose badly, put next to each other, generated four times and executed.
Twenty defects, one hand-written schema at a time. This runs that method over
the space instead of over whatever occurred to somebody -- `tests/unit/compose`
enumerates it, `tests/unit/sweep` runs one cell, and this walks as much of it
as you ask for and reports what came back.

    python3 tools/sweep.py                 # a sample, seeded and reproducible
    python3 tools/sweep.py --all           # every cell; minutes, not seconds
    python3 tools/sweep.py --limit 200 --seed 7
    python3 tools/sweep.py --only sealed   # cells whose name contains this
    python3 tools/sweep.py --verbose       # print the refusals too
    python3 tools/sweep.py --all --shard 0/6   # one slice, for running six
                                               # of these at once

Sharding is strided rather than blocked: the cells are enumerated in a fixed
order, so a block of them shares an axis value, and one shard would compile
every `sealed` cell while another compiled every `frame` one. Six shards over
the whole space take about as long as a `make test` rather than about an hour.

Not part of `make test`, for the reason `tools/bench.py` is not: the whole
space is a long run, and what belongs in CI is a fixed sample of it, which
`tests/unit/test_composed_schemas.py` is.

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
sys.path.insert(0, str(ROOT / "tests" / "unit"))

from compose import Case, cases				# noqa: E402
from probe import Outcome, run				# noqa: E402


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
