"""Schemas nobody wrote, on every commit.

`tests/unit/compose.py` enumerates the compositions of constructs this
language admits -- a driver, a form, an element, what precedes it, where it
sits -- and `tools/sweep.py` runs as much of that space as you ask for. This
runs a fixed sample of it, so the method that found 26.47 through 26.49 keeps
running without anybody choosing what to try.

**`KNOWN` is empty, and that is the claim.** It was not: the first sweep found
six distinct causes behind two thirds of the cells it touched, and this file
carried their names for as long as they stood. The list is checked both ways --
a cell not in it must pass, and a cell in it must still fail -- so an entry
cannot quietly outlive its defect, and the day the last one was fixed this
test said so by failing.

A cell that fails here is a real finding: a traceback out of the compiler,
generated code that will not build, or four backends that build and disagree.
A *refusal* is a pass -- most of the composition space is illegal and a
diagnostic is the right answer to all of it. `python3 tools/sweep.py --only
<fragment>` reproduces one; `--all` walks the whole space.
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

#: Cells that do not pass today, and what each dies of. The next defect this
#: sweep finds goes here with its symptom while it is being fixed, and the
#: test above holds the entry to being true in both directions -- so an entry
#: cannot outlive its defect, and the day the last one is fixed this test
#: says so by failing.
#:
#: Empty again. The thirteen that arrived with the versioning axis were one
#: question rather than thirteen defects, and 26.73 answered it: a versioned
#: member the message places clamps like every other dynamically placed
#: scalar. The day it was answered this test said so by failing on entries
#: that had started passing, which is the mechanism working.
KNOWN: dict[str, str] = {}


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
