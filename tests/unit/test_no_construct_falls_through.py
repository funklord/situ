"""No construct in this repository's schemas reaches a backend's fallthrough.

Every backend ends its member dispatch with a note saying it does not handle
this member -- "not in the static subset yet" in C++ and Rust, "not emitted by
this backend yet" in Python. That note is the honest thing to emit for a
construct nobody has written yet. It is a bad thing to emit for one that is in
`examples/`, because a reader takes it for a language limit and designs around
it.

Six constructs reached it silently and were found one at a time by a human:
`tlv` regions, `indexed` regions, endian markers, tags, fixed-width text
numbers and varint fields. Each had a `Member` kind added *after* the gap was
noticed. In four of the six the note was the better half of the problem -- C,
which does not ask the shared classifier, answered on its own and answered
wrongly, so the same schema was refused in three backends and silently
misread in the fourth.

This is the check that would have caught all six the day they landed, and the
one that caught `packet.tag` coming back after the tag machinery was recorded
as done. It is deliberately dumb: generate everything, grep for the note.

Section 0's rule, applied to itself -- prefer adding a check to adding a
promise.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from situc.codegen.cpp import generate as generate_cpp
from situc.codegen.python import generate as generate_py
from situc.codegen.rust import generate as generate_rs
from situc.layout import solve
from situc.parser import parse
from situc.diagnostics import Source
from situc.resolve import resolve

from every_schema import ROOT, SCHEMAS, ids

#: What each backend says when its dispatch runs out.
FALLTHROUGH = re.compile(
	r"([A-Za-z_][\w.]*): (?:not in the static subset yet"
	r"|not emitted by this backend yet)")

#: Constructs allowed to reach it, with why. Every entry here is a gap 26.31
#: names, and the list is short so that adding to it is something somebody has
#: to argue for rather than something that happens.
#:
#: Empty, and that is the point: it was not empty while this file was written.
EXEMPT: dict[str, str] = {}


def emitted(path: Path) -> dict[str, str]:
	source   = Source(str(path), path.read_text(encoding="ascii"))
	schema   = parse(source)
	resolved = resolve(schema, solve(schema))
	name     = path.stem

	return {
		"cpp":    generate_cpp(schema, resolved, name).header,
		"python": generate_py(schema, resolved, name).module,
		"rust":   generate_rs(schema, resolved, name).module,
	}


@pytest.mark.parametrize("path", SCHEMAS, ids=ids(SCHEMAS))
def test_no_member_reaches_a_backends_fallthrough(path: Path) -> None:
	found: list[str] = []

	for backend, text in emitted(path).items():
		for member in FALLTHROUGH.findall(text):
			if member not in EXEMPT:
				found.append(f"{backend}: {member}")

	assert not found, (
		f"{path.name} has members no backend handles, and the note a reader "
		f"sees says the language does not support them:\n  "
		+ "\n  ".join(sorted(found))
		+ "\n\nEither emit the construct, or say what it is where the note is "
		"-- a variant and a sealed region both have accessors that are not "
		"their own, and both say so rather than falling through."
	)


def test_the_exemptions_are_still_needed() -> None:
	"""An exemption for a construct that is now emitted is a note nobody reads
	claiming a limit that is not there. Invariant 11, one level up: this list
	asserts an absence, so it has a shelf life too."""
	seen: set[str] = set()

	for path in SCHEMAS:
		for text in emitted(path).values():
			seen.update(FALLTHROUGH.findall(text))

	stale = sorted(set(EXEMPT) - seen)
	assert not stale, (
		f"exempted but no longer falling through: {stale}. Remove them from "
		f"EXEMPT -- the construct is emitted now."
	)


def test_the_pattern_matches_what_the_backends_actually_write() -> None:
	"""A regex that matches nothing would make this file pass forever.

	Checked against the emitters' own source rather than against generated
	output, because the fallthrough is now hard to reach on purpose -- there is
	no construct left in the language that lands on it, which is the outcome
	this file exists to keep. So the thing to pin is the wording each backend
	writes, which is where the regex was copied from.
	"""
	for backend in ("cpp", "python", "rust"):
		source = (ROOT / "situc" / "codegen" / backend / "emit.py").read_text(
			encoding="ascii")
		notes  = [line for line in source.splitlines()
		          if "not in the static subset yet" in line
		          or "not emitted by this backend yet" in line]

		assert notes, f"{backend} has no fallthrough note; this file greps for one"
		assert any(FALLTHROUGH.search(
			line.replace("{placement.path}", "s.x")) for line in notes), (
			f"{backend}'s fallthrough note no longer matches the pattern this "
			f"file greps for:\n  " + "\n  ".join(notes))
