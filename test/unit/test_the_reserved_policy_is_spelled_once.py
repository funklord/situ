"""The reserved policy names live in `traverse` and nowhere else.

Section 8.8's policies are two frozensets -- `RESERVED_STATED` for the
ones a check enforces, `RESERVED_OPEN` for the ones whose bits are
carried -- and two functions read them: `reserved_policy`, which says
what the policy IS, and `reserved_constraint`, which says what `validate`
must CHECK and so folds the open pair into None.

**Both existed already; the second had four copies.** The C++, Python and
Rust emitters and the C check builder each held one, and nine further
sites spelled a set out as a tuple. They were byte-identical when this
was written, measured -- so nothing had drifted, and that is the point:
the cost of a duplicated schema rule is not today's disagreement but the
next policy, which `RESERVED_STATED` picks up in one line and a literal
does not. An unrecognised policy falls through to `must_be_zero`, so a
field the schema says must hold ones would be zero-filled by a writer
and checked for zero by a validator (26.658).

This asks for the shape rather than the count: that no module outside
`traverse` spells a policy set, and that the two functions agree on every
name in them.
"""

from __future__ import annotations

import re
from pathlib import Path

from situc import ast
from situc.traverse import (RESERVED_OPEN, RESERVED_STATED,
                            reserved_constraint, reserved_policy)

ROOT = Path(__file__).resolve().parents[2]

#: A policy SET written out, in either order. Not a single name: `unknown`
#: and `preserve` are ordinary words with 61 uses across the tree, most of
#: them nothing to do with section 8.8, and a check keyed on those would
#: report the tree's vocabulary rather than this rule's copies.
SPELLED = re.compile(
	r'\(\s*"(?:must_be_zero|must_be_one|preserve|unknown)"\s*,\s*'
	r'"(?:must_be_zero|must_be_one|preserve|unknown)"\s*\)')


def test_no_module_outside_traverse_spells_a_policy_set() -> None:
	found = []
	for path in sorted((ROOT / "situc").rglob("*.py")):
		if path.name == "traverse.py":
			continue
		for number, line in enumerate(
				path.read_text(encoding="utf-8").splitlines(), start=1):
			if SPELLED.search(line):
				found.append(f"{path.relative_to(ROOT)}:{number}: {line.strip()}")
	assert not found, ("a policy set is spelled outside `traverse`:\n  "
	                   + "\n  ".join(found[:8]))


def test_the_two_policy_questions_agree() -> None:
	"""`reserved_constraint` is `reserved_policy` with the open pair as None.

	Asserted as a RELATIONSHIP rather than by listing the answers, so a
	policy added to either set is covered without touching this file.
	"""
	assert RESERVED_STATED and RESERVED_OPEN
	assert not (RESERVED_STATED & RESERVED_OPEN), "a policy in both sets"

	for name in sorted(RESERVED_STATED | RESERVED_OPEN):
		attrs = (ast.Attr(None, name, ()),)		# type: ignore[arg-type]
		assert reserved_policy(attrs) == name
		assert reserved_constraint(attrs) == (name if name in RESERVED_STATED
		                                      else None)

	# No attribute at all is the documented default, and it is a STATED
	# policy: every ignored bit is a malleability surface, so the safe
	# option is the silent one.
	assert reserved_policy(()) == "must_be_zero"
	assert reserved_constraint(()) == "must_be_zero"
	assert reserved_policy(()) in RESERVED_STATED
