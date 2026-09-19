"""`until` consumes its delimiter and `before` does not -- inside an arm too.

All four backends had this right for a delimited member at the top level
and spelled it as `until` inside a variant arm, so every member after the
variant was read one byte late (26.444). One schema, two spellings, and
only the arm was wrong -- which is what made it findable at all.

What is asserted here is the PAIR rather than either half. A test pinning
`after == 44` would pass against a backend that had learned to read
`before` correctly and forgotten `until`, and the defect being guarded
against is precisely a second implementation of a decision already made.
`edges.situ` carries both structs so the corpus differential covers them
on every commit; this asks with chosen bytes, and asks the two together.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from every_schema import ROOT
from fourway import COMPLETE, answers, build

EDGES = ROOT / "test" / "schema" / "edges.situ"

#: `,` and `\n`, which the two structs delimit on. The separator is what
#: `after` reads when it is NOT consumed, so it is the assertion's subject
#: rather than an incidental byte.
COMMA, NEWLINE = 0x2C, 0x0A


def _member(text: str, struct: str, name: str) -> str:
	seen = False
	for line in text.splitlines():
		if line.startswith("-- "):
			seen = line == f"-- {struct}"
		elif seen and line.startswith(f"{name} "):
			return line.split()[-1]
	return "absent"


def _length(text: str, struct: str, name: str) -> str:
	"""A delimited member's reported CONTENT length.

	The driver renders one as `held_line ok=1 len=2`, so the last token is
	the length only for a scalar. Reading it as one gave `len=2 == "2"`
	and a failure that looked like a disagreement about the arm.
	"""
	seen = False
	for line in text.splitlines():
		if line.startswith("-- "):
			seen = line == f"-- {struct}"
		elif seen and line.startswith(f"{name} "):
			for token in line.split():
				if token.startswith("len="):
					return token[4:]
	return "absent"


@pytest.mark.skipif(not COMPLETE, reason="needs all four toolchains")
def test_a_separator_in_an_arm_is_left_for_the_member_after_it(
		tmp_path: Path) -> None:
	"""The pair, over one schema, in all four backends.

	`separated_arm` delimits on `,` with `before`, so the comma belongs to
	neither side and `after` reads it. `delimited_arm` delimits on `\\n`
	with `until`, so the newline is the arm's own last byte and `after`
	reads what follows it. Same two bytes of payload either way; the only
	difference is which member owns the delimiter.
	"""
	command = build(tmp_path, EDGES)
	assert command, "edges.situ built no drivers"

	for name, cmd in command.items():
		separated = answers(cmd, b"\x01ab,Z", tmp_path)
		delimited = answers(cmd, b"\x01ab\nZ", tmp_path)

		# `before`: the arm stops at the comma and does not take it.
		assert _member(separated, "separated_arm", "after") \
			== str(COMMA), (name, separated)
		# `until`: the arm takes the newline, so `after` is the Z past it.
		assert _member(delimited, "delimited_arm", "after") \
			== str(ord("Z")), (name, delimited)


@pytest.mark.skipif(not COMPLETE, reason="needs all four toolchains")
def test_the_two_arms_disagree_about_the_delimiter_by_exactly_one_byte(
		tmp_path: Path) -> None:
	"""The control for the test above, which is the arms' own lengths.

	Reading `after` correctly is consistent with an arm span that is right
	and with one that is wrong in a way the next member happens to absorb.
	The arm's own reported length cannot be absorbed: two bytes of content
	either way, and the delimiter counted by one of them and not the
	other.
	"""
	command = build(tmp_path, EDGES)

	for name, cmd in command.items():
		separated = answers(cmd, b"\x01ab,Z", tmp_path)
		delimited = answers(cmd, b"\x01ab\nZ", tmp_path)

		assert _length(separated, "separated_arm", "held_line") \
			== "2", (name, separated)
		assert _length(delimited, "delimited_arm", "held_line") \
			== "2", (name, delimited)
