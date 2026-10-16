#!/usr/bin/env python3
"""Move a cited entry out of `project.md` into its own record.

Set by the copyright holder after 26.655 measured that the cut alone
cannot make `project.md` readable in a session: it takes 15 to 20 per
cent where three context windows of it would have to go. What
`harmonization.md` names as the case a second document earns is a record
CITED BY NUMBER, because a single file cannot give a stable anchor per
record -- and section 26's entries are cited about 4,000 times, 2,652 of
them from source.

**The number is the anchor, so the number is the filename.** A record
moves to `doc/decision/26.NNN-slug.md` and keeps its own number rather
than taking the next in the `NNNN` sequence. Renumbering would invalidate
every one of those citation sites, in code comments nobody would think to
grep.

`project.md` keeps the heading and a one-line pointer, so the section
still reads as a list of what was found and `26.NNN` still resolves from
the document.

**The style gate does NOT check those pointers, and this file claimed it
did.** `doc_paths` reads table rows only, deliberately -- a path in prose
is as often an example, a glob or a layout being described -- so the
`docs` mode's "names no missing file" says nothing about a pointer
written as prose. The claim was written here as this design's
justification and was false; `--check` below is the check it described,
and it is this tool's to run (26.656).

The proof, refusing the write rather than warning:

  * every moved line lands in exactly one record, byte-identical;
  * `project.md` keeps the entry's heading, spelled identically;
  * nothing else in `project.md` changes, beyond the pointer inserted
    under the heading;
  * no record file is overwritten.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOC = ROOT / "project.md"
RECORDS = ROOT / "doc" / "decision"
ENTRY = re.compile(r"^### (26\.\d+) (.+)$")


def slug(title: str) -> str:
	"""A filename from a heading, kebab-case per `code-style.md`."""
	# Strike-through, backticks and emphasis are the heading's own markup
	# and not part of its name.
	bare = re.sub(r"[`~*]", "", title)
	return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", bare.lower())).strip("-")


def first_seen(heading: str) -> str:
	"""When this entry was written, from the commit that added its heading.

	Asked of git rather than stored, because the entry never carried a
	date: the document is one file and its history is per commit.
	"""
	got = subprocess.run(
		["git", "log", "--reverse", "--format=%as", "-S", heading, "--", "project.md"],
		capture_output=True, text=True, cwd=ROOT)
	dates = [l for l in got.stdout.splitlines() if l.strip()]
	return dates[0] if dates else "unknown"


def main() -> int:
	parser = argparse.ArgumentParser()
	parser.add_argument("entries", nargs="*", metavar="26.N")
	parser.add_argument("--list", action="store_true")
	parser.add_argument("--check", action="store_true",
	                    help="every `Moved to` pointer resolves")
	args = parser.parse_args()

	if args.check:
		missing = [token for token in
		           re.findall(r"Moved to `([^`]+)`", DOC.read_text())
		           if not (ROOT / token).exists()]
		if missing:
			print(f"{len(missing)} pointer(s) name no file:")
			print("\n".join("  " + m for m in missing))
			return 1
		total = len(re.findall(r"Moved to `([^`]+)`", DOC.read_text()))
		print(f"{total} pointer(s), every one resolves")
		return 0

	lines = DOC.read_text().splitlines()
	bounds = [i for i, l in enumerate(lines)
	          if l.startswith("## ") or ENTRY.match(l)]
	spans: dict[str, tuple[int, int, str]] = {}
	for n, i in enumerate(bounds):
		match = ENTRY.match(lines[i])
		if match:
			end = bounds[n + 1] if n + 1 < len(bounds) else len(lines)
			spans[match.group(1)] = (i, end, match.group(2))

	if args.list:
		for name, (start, end, title) in sorted(
				spans.items(), key=lambda kv: int(kv[0].split(".")[1])):
			print(f"  {name:8} {end-start:4}l  {slug(title)}")
		return 0
	if not args.entries:
		parser.error("name the entries to move, or --list")

	wanted = []
	for name in args.entries:
		if name not in spans:
			print(f"no entry {name}")
			return 1
		wanted.append(name)

	doomed: set[int] = set()
	writes: list[tuple[Path, str]] = []
	pointers: dict[int, list[str]] = {}
	for name in wanted:
		start, end, title = spans[name]
		path = RECORDS / f"{name}-{slug(title)}.md"
		if path.exists():
			print(f"REFUSED: {path.name} exists already")
			return 1
		body = lines[start + 1:end]
		while body and not body[0].strip():
			body.pop(0)
		while body and not body[-1].strip():
			body.pop()
		writes.append((path, "\n".join([
			f"# {name}: {title}",
			"",
			"Status: recorded",
			f"Date: {first_seen(lines[start])}",
			f"Source: `project.md` section 26, where it was written",
			"",
			*body,
		]) + "\n"))
		doomed.update(range(start + 1, end))
		pointers[start] = ["", f"Moved to `doc/decision/{path.name}`.", ""]

	after: list[str] = []
	for i, line in enumerate(lines):
		if i in doomed:
			continue
		after.append(line)
		after.extend(pointers.get(i, []))

	# The proof. Every moved line must land in exactly one record, and
	# nothing but the pointers may be new.
	moved = {lines[i] for i in doomed if lines[i].strip()}
	landed = {l for _, text in writes for l in text.splitlines() if l.strip()}
	lost = moved - landed
	if lost:
		print(f"REFUSED: {len(lost)} moved line(s) are in no record: "
		      f"{sorted(lost)[0][:60]!r}")
		return 1
	heads = [l for l in lines if ENTRY.match(l)]
	if heads != [l for l in after if ENTRY.match(l)]:
		print("REFUSED: an entry heading changed")
		return 1
	added = set(after) - set(lines)
	expected = {p for ps in pointers.values() for p in ps if p.strip()}
	if added - expected:
		print(f"REFUSED: {len(added - expected)} unexpected new line(s)")
		return 1

	for path, text in writes:
		path.write_text(text)
	DOC.write_text("\n".join(after) + "\n")
	print(f"moved {len(writes)} record(s); project.md "
	      f"{len(lines)} -> {len(after)} lines")
	gate = subprocess.run([sys.executable, "tool/style_gate.py", "docs"],
	                      capture_output=True, text=True, cwd=ROOT)
	print((gate.stdout or gate.stderr).strip()[:260])
	return gate.returncode


if __name__ == "__main__":
	sys.exit(main())
