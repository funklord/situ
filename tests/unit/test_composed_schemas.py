"""Schemas nobody wrote, on every commit.

`tests/unit/compose.py` enumerates the compositions of constructs this
language admits -- a driver, a form, an element, what precedes it, where it
sits -- and `tools/sweep.py` runs as much of that space as you ask for. This
runs a fixed sample of it, so the method that found 26.47 through 26.49 keeps
running without anybody choosing what to try.

**The failing cells are named rather than hidden.** The space is not clean:
the first sweep found six distinct causes behind two thirds of the cells it
touched, and this repository does not have a way to say "these are known" that
does not rot. So the list below is both halves of the claim -- a cell not in
it must pass, and a cell in it must still fail. Fixing one fails this test,
which is the point: the list is a measurement, and a measurement that quietly
kept a stale entry would be back to a comment.

Each entry carries what it fails with, so a reader can tell six causes from
twenty-four symptoms. `python3 tools/sweep.py --only <fragment>` reproduces
one.
"""

from __future__ import annotations

import random
from pathlib import Path

import pytest

from compose import Case, cases
from fourway import COMPLETE
from probe import run

#: Which cells. Fixed, because a sample that moved would make a failure
#: something nobody can reproduce tomorrow, and because the known list below
#: is only meaningful against a known sample.
SEED  = 20260804
COUNT = 24

#: Cells that do not pass today, and what each dies of. Six causes:
#:
#:   * an offset function named for a member inside a region or an arm that
#:     nothing emits -- `implicit declaration`;
#:   * `offset is dynamic`, which is a backend asking a placement for a
#:     constant offset it has not got: the crash 26.49 fixed in three places
#:     and did not finish;
#:   * a Rust panic on an index into a slice whose length the message chose;
#:   * `too few arguments`, a text driver's value helper called as the plain
#:     getter;
#:   * `None` reaching generated C++ as an identifier, which is a Python
#:     value formatted into a template;
#:   * a span function named for a run inside a region that nothing emits.
KNOWN: dict[str, str] = {
	"nested-arith-i32-after-nothing-in-sealed":             "too few arguments",
	"nested-arith-u16-after-nothing-in-sealed":             "too few arguments",
	"nested-arith-vrec-after-nothing-in-nested":            "implicit declaration",
	"nested-count-vrec-after-delim-in-arm":                 "implicit declaration",
	"nested-remaining-vrec-after-nothing-in-authenticated": "implicit declaration",
	"packed-arith-vrec-after-delim-in-arm":                 "offset is dynamic",
	"packed-arith-vrec-after-nothing-in-arm":               "implicit declaration",
	"text-arith-u16-after-nothing-in-authenticated":        "too few arguments",
	"text-arith-u8-after-bytes-in-frame":                   "too few arguments",
	"u16-arith-i32-after-delim-in-arm":                     "implicit declaration",
	"u16-count-vrec-after-bytes-in-sealed":                 "implicit declaration",
	"u16-count-vrec-after-delim-in-arm":                    "implicit declaration",
	"u8-count-rec-after-bytes-in-nested":                   "rust panic",
	"varint-arith-u16-after-bytes-in-arm":                  "implicit declaration",
	"varint-remaining-vrec-after-delim-in-sealed":          "implicit declaration",
}


def sample() -> list[Case]:
	return random.Random(SEED).sample(cases(), COUNT)


@pytest.mark.skipif(not COMPLETE, reason="needs all four toolchains")
@pytest.mark.parametrize("case", sample(), ids=lambda case: case.name)
def test_a_composed_schema_builds_and_agrees(case: Case,
		tmp_path: Path) -> None:
	"""One composition, through the compiler and then through four backends.

	A refusal is a pass: most of this space is illegal and a diagnostic is the
	right answer to all of it. What is not a pass is a traceback, generated
	code that will not build, or four backends that build and disagree.
	"""
	outcome = run(case, tmp_path, seed=SEED)

	if case.name in KNOWN:
		assert not outcome.ok, (
			f"{case.name} passes now -- it is listed as failing with "
			f"`{KNOWN[case.name]}`. Take it out of KNOWN.")
		return

	assert outcome.ok, (
		f"{case.name}: {outcome.kind}\n{outcome.detail}")


def test_every_known_failure_is_in_the_sample() -> None:
	"""A name that is not drawn is a name nothing checks.

	The list above is only self-policing for the cells the sample reaches; one
	that drifted out of it would sit there forever saying something nobody
	asks about, which is the shelf life invariant 11 warns about.
	"""
	drawn = {case.name for case in sample()}
	assert set(KNOWN) <= drawn, sorted(set(KNOWN) - drawn)
