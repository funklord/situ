#!/usr/bin/env python3
"""Cut narration out of `project.md`, and prove nothing else moved.

26.608 measured the problem and 26.610 recorded the authorised pass: keep
each entry's rule and its load-bearing number, drop the narration, one
commit per pass rather than by attrition. This is the tool that performs
it, and it exists because `CLAUDE.md` requires a mechanical change to
carry a proof -- state what must not change, check it, refuse to write
when the check fails.

**Deletion only, by whole paragraph.** Nothing is rewritten and nothing
is added, which is what makes the proof mechanical: every retained line
is byte-identical to a line of the original, in the original order. A
compressed paraphrase would read better and could not be checked, and
what recovers a removed paragraph is `git log -S` on its own words --
the property the `evidence.md` cut relied on, which is why each pass is
one commit.

Usage:

	python3 tool/cut.py --list 26.144        # paragraphs, numbered
	python3 tool/cut.py --drop 26.144:3,5-7  # delete those, with the proof
	python3 tool/cut.py --check              # the proof alone

The invariants, each refusing the write rather than warning:

  * every heading survives, spelled identically and in order -- an entry
    is cited by number from elsewhere, and a lost heading is a dangling
    reference somebody else holds;
  * every `N.NNN` cross-reference still names a heading that exists;
  * every retained line is a line of the original, in order (deletion
    only, proved by a subsequence check rather than asserted);
  * the file still parses as the same entry structure.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

DOC = Path(__file__).resolve().parent.parent / "project.md"
HEADING = re.compile(r"^#{1,4} ")
ENTRY = re.compile(r"^#{2,4} (\d+(?:\.\d+)?)\s")
#: A reference to an entry by number, as the prose spells it: `26.144`,
#: optionally in backticks. Bounded so `26.14` does not match `26.144`.
REFERENCE = re.compile(r"(?<![\w.])(\d{1,2}\.\d{1,3})(?![\w.])")


def paragraphs(lines: list[str], start: int, end: int) -> list[tuple[int, int]]:
	"""The entry body's paragraphs, as half-open line ranges.

	Blank-line separated, with a `####` subheading its own paragraph so
	that dropping a subsection means naming the heading and its prose
	rather than leaving a heading over nothing.
	"""
	found: list[tuple[int, int]] = []
	at = start + 1
	while at < end:
		if not lines[at].strip():
			at += 1
			continue
		stop = at + 1
		if HEADING.match(lines[at]):
			found.append((at, stop))
			at = stop
			continue
		while stop < end and lines[stop].strip() and not HEADING.match(lines[stop]):
			stop += 1
		found.append((at, stop))
		at = stop
	return found


def entries(lines: list[str]) -> dict[str, tuple[int, int]]:
	"""Each numbered entry, as a half-open line range.

	An entry ends at the next NUMBERED heading or section, not at the next
	heading of any level: its `####` subheadings are part of it, and the
	narration this cut is for mostly sits under one. Ending at any heading
	made that prose unaddressable -- measured on 26.654, which listed four
	paragraphs of nineteen lines because its first subheading cut the body
	short.
	"""
	bounds = [i for i, line in enumerate(lines)
	          if line.startswith("## ") or ENTRY.match(line)]
	found = {}
	for n, i in enumerate(bounds):
		match = ENTRY.match(lines[i])
		if match:
			found[match.group(1)] = (i, bounds[n + 1] if n + 1 < len(bounds)
			                         else len(lines))
	return found


def _is_subsequence(kept: list[str], original: list[str]) -> str | None:
	"""Which retained line is not a line of the original, in order."""
	at = 0
	for line in kept:
		while at < len(original) and original[at] != line:
			at += 1
		if at == len(original):
			return line
		at += 1
	return None


def check(before: list[str], after: list[str]) -> list[str]:
	"""What the cut broke. Empty is the proof."""
	broken = []

	was = [l for l in before if HEADING.match(l)]
	now = [l for l in after if HEADING.match(l)]
	if was != now:
		lost = [l for l in was if l not in now]
		broken.append(f"{len(was) - len(now)} heading(s) changed or lost: "
		              + "; ".join(l[:60] for l in lost[:4]))

	known = set(entries(after))
	text  = "\n".join(after)
	named = {m.group(1) for m in REFERENCE.finditer(text)}
	# Only references that looked resolvable BEFORE: the document cites
	# section numbers of other documents too, and those were never ours.
	resolved_before = {m.group(1) for m in REFERENCE.finditer("\n".join(before))
	                   if m.group(1) in set(entries(before))}
	dangling = sorted((named & resolved_before) - known)
	if dangling:
		broken.append(f"{len(dangling)} reference(s) now name no heading: "
		              + ", ".join(dangling[:8]))

	stray = _is_subsequence(after, before)
	if stray is not None:
		broken.append(f"a retained line is not an original line, in order: "
		              f"{stray[:70]!r}")
	return broken


def main() -> int:
	parser = argparse.ArgumentParser()
	parser.add_argument("--list", metavar="ENTRY")
	parser.add_argument("--drop", metavar="ENTRY:RANGES", action="append",
	                    default=[])
	parser.add_argument("--check", action="store_true")
	args = parser.parse_args()

	before = DOC.read_text().splitlines()

	if args.list:
		found = entries(before)
		if args.list not in found:
			print(f"no entry {args.list}")
			return 1
		start, end = found[args.list]
		print(f"{before[start]}   [{end - start} lines]")
		for n, (at, stop) in enumerate(paragraphs(before, start, end), start=1):
			head = " ".join(" ".join(before[at:stop]).split())
			print(f"  {n:3} [{stop - at:2}l] {head[:96]}")
		return 0

	if args.check:
		broken = check(before, before)
		print("\n".join(broken) if broken else "the document is self-consistent")
		return 1 if broken else 0

	if not args.drop:
		parser.error("one of --list, --drop or --check")

	found = entries(before)
	doomed: set[int] = set()
	for spec in args.drop:
		name, _, ranges = spec.partition(":")
		if name not in found:
			print(f"no entry {name}")
			return 1
		start, end = found[name]
		paras = paragraphs(before, start, end)
		for piece in ranges.split(","):
			lo, _, hi = piece.partition("-")
			for n in range(int(lo), int(hi or lo) + 1):
				if not 1 <= n <= len(paras):
					print(f"{name} has no paragraph {n}")
					return 1
				at, stop = paras[n - 1]
				doomed.update(range(at, stop))

	after = [l for i, l in enumerate(before) if i not in doomed]
	# Two blank lines in a row where a paragraph went: a blank line is not
	# a paragraph, so collapsing them is not a rewrite of any line.
	squeezed: list[str] = []
	for line in after:
		if not line.strip() and squeezed and not squeezed[-1].strip():
			continue
		squeezed.append(line)

	broken = check(before, squeezed)
	if broken:
		print("REFUSED, nothing written:")
		print("\n".join("  " + b for b in broken))
		return 1

	DOC.write_text("\n".join(squeezed) + "\n")
	print(f"dropped {len(before) - len(squeezed)} lines "
	      f"({len(before)} -> {len(squeezed)})")
	gate = subprocess.run([sys.executable, "tool/style_gate.py", "docs"],
	                      capture_output=True, text=True,
	                      cwd=DOC.parent)
	print((gate.stdout or gate.stderr).strip()[:300])
	return gate.returncode


if __name__ == "__main__":
	sys.exit(main())
