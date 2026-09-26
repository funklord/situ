"""A tag covers the region it names, and not a sibling's region of the
same name (14.1, 14.2).

`covered_by` says which tags these bytes leave stale, and the map renders
it as `auth=Covered(...)`. Both readings make the union unsafe in the same
direction: a byte reported as authenticated by a tag that does not cover
it is a claim the format does not make.

`regions` records the NAMES of the regions a member sits inside, and a
nested struct brings its own along unqualified -- so two siblings that
each hold an `authenticated body` both say `body`. The tags were collected
into a dict keyed on that name, which merged them: `outer.first.x` read
`Covered(first.sig, second.sig)` where `second.sig` covers nothing of
`first`.

Reported by fuzznet from their provisioning card, which is a hop and a
prekey side by side, each self-signed; their committed wire signature had
carried two unqualified `signature covers:` lines listing both structs'
fields since the card was converted (26.519).

**Five shapes, and each is the others' control.** Four were already
correct, which is what makes the fifth a scoping fault rather than a
blanket over-claim -- and what would have caught the first repair, which
tested containment over paths and took coverage away from everything.
"""

from __future__ import annotations

import pytest

from situc.layout import solve
from situc.parser import parse_text

PREAMBLE = "target buffer;\nendian big;\n\n"

SELF_SIGNED = (
	"struct {name} {{\n"
	"\tauthenticated body {{ u8 {field}; }}\n"
	"\tchecksum u8 sig[4] covers(body);\n"
	"}}\n"
)


def _covered(text: str, struct: str) -> dict[str, tuple[str, ...]]:
	"""Every placement's `covered_by`, keyed on its path within the struct."""
	layout = solve(parse_text(PREAMBLE + text))
	return {held.path[len(struct) + 1:]: held.covered_by
	        for held in layout.structs[struct].placements}


def test_two_self_signed_siblings_keep_their_tags_apart() -> None:
	"""The reported case. Each member is covered by its own struct's tag."""
	held = _covered(
		SELF_SIGNED.format(name="a", field="x")
		+ SELF_SIGNED.format(name="b", field="y")
		+ "struct outer { u8 plain; a first; b second; }\n", "outer")

	assert held["first.x"] == ("first.sig",)
	assert held["second.y"] == ("second.sig",)
	# The regions too, not only the leaves: both carried the union.
	assert held["first.body"] == ("first.sig",)
	assert held["second.body"] == ("second.sig",)
	# And a member outside every region stays uncovered, which is what says
	# the fault was the union and not coverage leaking generally.
	assert held["plain"] == ()


def test_the_union_grows_with_the_siblings() -> None:
	"""Three, because two cannot tell a swap from a union.

	With two siblings a bug that returned the OTHER tag looks identical to
	one that returns both. The third member separates them, and it is how
	the union was shown to be over all siblings rather than a two-way
	confusion.
	"""
	held = _covered(
		SELF_SIGNED.format(name="a", field="x")
		+ SELF_SIGNED.format(name="b", field="y")
		+ SELF_SIGNED.format(name="c", field="z")
		+ "struct outer { u8 plain; a first; b second; c third; }\n", "outer")

	assert held["first.x"] == ("first.sig",)
	assert held["second.y"] == ("second.sig",)
	assert held["third.z"] == ("third.sig",)


def test_one_self_signed_member_was_always_right() -> None:
	"""fuzznet's own control, and the case that says a tagless parent is
	not the cause: `outer` declares no region and no tag here either."""
	held = _covered(
		SELF_SIGNED.format(name="a", field="x")
		+ "struct outer { u8 plain; a first; }\n", "outer")

	assert held["first.x"] == ("first.sig",)
	assert held["plain"] == ()


def test_nesting_still_accumulates_every_enclosing_tag() -> None:
	"""Three deep, each level self-signed: containment is real coverage.

	The innermost field IS inside all three regions, so all three tags
	stale when it is written, and innermost-first is the order generated
	code must recompute in. This is the case a fix keyed on siblinghood
	alone would break, and the one the containment repair got right for
	the wrong reason.
	"""
	held = _covered(
		"struct leaf {\n"
		"\tauthenticated body { u8 x; }\n"
		"\tchecksum u8 sig[4] covers(body);\n"
		"}\n"
		"struct mid {\n"
		"\tauthenticated body { leaf held; }\n"
		"\tchecksum u8 sig[4] covers(body);\n"
		"}\n"
		"struct top {\n"
		"\tauthenticated body { mid held; }\n"
		"\tchecksum u8 sig[4] covers(body);\n"
		"}\n", "top")

	assert held["held.held.x"] == ("held.held.sig", "held.sig", "sig")
	assert held["held.held.body"] == ("held.held.sig", "held.sig", "sig")
	# Narrower as it goes out, and `top.body` is covered by top's tag only.
	assert held["held.body"] == ("held.sig", "sig")
	assert held["body"] == ("sig",)


def test_two_tags_in_one_struct_were_always_right() -> None:
	"""Siblings without the nesting: two regions of one struct, two tags.

	Correct before the fix and after it, which is what says the fault was
	not "a struct with two tags" -- the collection could already tell two
	regions apart when their names differed.
	"""
	held = _covered(
		"struct two {\n"
		"\tauthenticated p { u8 a; }\n"
		"\tchecksum u8 sig_p[4] covers(p);\n"
		"\tauthenticated q { u8 b; }\n"
		"\tchecksum u8 sig_q[4] covers(q);\n"
		"}\n", "two")

	assert held["a"] == ("sig_p",)
	assert held["b"] == ("sig_q",)
