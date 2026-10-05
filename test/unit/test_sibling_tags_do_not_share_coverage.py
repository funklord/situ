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


# ---------------------------------------------------------------------------
# The struct's own line, which 26.519 left reading the other way
# ---------------------------------------------------------------------------

def _struct_auth(text: str, struct: str) -> tuple[str, ...] | None:
	"""The tags a struct's OWN line names, or None where it names none."""
	from situc.capability import Axis
	from situc.resolve import resolve

	schema   = parse_text(PREAMBLE + text)
	resolved = resolve(schema, solve(schema))
	held     = dict(resolved.structs[struct].vector.values)[Axis.AUTH]
	return held.params if held.base == "Covered" else None


def test_a_tag_covering_all_but_itself_still_names_the_struct() -> None:
	"""The case the literal rule would have destroyed.

	A tag never covers its own bytes -- `resolve_coverage` discards it,
	because coverage means "writing these bytes leaves that tag stale" and
	that is false of the bytes the tag is written into. So no tag covers
	ALL of a struct it sits in, and requiring that empties the axis:
	measured, 22 corpus struct lines read Covered and the literal rule
	keeps a tag on none of them, `ipv4_header` among the losses.

	With the tag's own bytes exempt, a header whose checksum covers the
	rest of it still says so, which is the whole use of the line.
	"""
	assert _struct_auth(
		SELF_SIGNED.format(name="a", field="x"), "a") == ("sig",)


DEEP = (
	"struct leaf {\n"
	"\tauthenticated body { u8 x; }\n"
	"\tchecksum u8 sig[4] covers(body);\n"
	"}\n"
	"struct mid {\n"
	"\tauthenticated body { leaf held; }\n"
	"\tchecksum u8 sig[4] covers(body);\n"
	"}\n"
)


def test_a_nested_tag_of_the_same_name_is_not_exempted_too() -> None:
	"""The bug the flat fixtures could not see.

	Finding a tag's own placement by dotted suffix -- `path.endswith("." +
	tag)` -- also matches a nested member's, so for a tag called `sig` the
	exemption swallowed `mid.held.sig` as well as `mid.sig`. One placement
	too many was excused, `mid`'s own `sig` fell one short of covering
	everything but itself, and `mid` named no tag.

	`mid` is nine bytes: a five-byte body holding `leaf` whole, and four
	bytes of its own signature. `sig` covers the body, which is every byte
	that is not `sig`, so it names the struct. The flat cases passed
	throughout and this one was not asserted -- a rule keyed on a name
	needs a fixture where two members share it.
	"""
	assert _struct_auth(DEEP, "mid") == ("sig",)
	assert _struct_auth(DEEP, "leaf") == ("sig",)


def test_two_independently_signed_children_name_no_tag() -> None:
	"""fuzznet's shape. Their `card.hop` read `Covered(signature,
	hop.signature)` over 179 bytes of which `hop.signature` covers 115 --
	two tags that between them reach every member and neither of which
	reaches all of it."""
	assert _struct_auth(
		SELF_SIGNED.format(name="a", field="x")
		+ SELF_SIGNED.format(name="b", field="y")
		+ "struct outer { u8 plain; a first; b second; }\n", "outer") is None


def test_a_tag_over_part_of_a_struct_names_nothing() -> None:
	"""One tag, one child, and a sibling it does not cover.

	The narrowing is not about how MANY tags there are -- that reading
	would keep this one, since there is only one candidate. It is about
	whether the tag reaches every byte, and `plain` is outside the region.
	"""
	assert _struct_auth(
		SELF_SIGNED.format(name="a", field="x")
		+ "struct outer { u8 plain; a first; }\n", "outer") is None


def test_every_struct_line_naming_a_tag_is_covered_by_it_throughout() -> None:
	"""The quantifier, over the corpus, derived rather than enumerated.

	A test naming ipv4 and tcp would pass while a seventh schema's line
	over-claimed. This asks the property of every struct in the tree: if
	the line names a tag, that tag is on every member's `covered_by` bar
	the tag's own placement.
	"""
	from situc.capability import Axis
	from situc.resolve import resolve

	from every_schema import SCHEMAS, load_schema

	named = 0
	for path in SCHEMAS:
		parsed   = load_schema(path)
		resolved = resolve(parsed, solve(parsed))
		for name, struct in resolved.structs.items():
			held = dict(struct.vector.values)[Axis.AUTH]
			if held.base != "Covered":
				continue
			placements = struct.layout.placements
			for tag in held.params:
				named += 1
				missing = [held_one.path for held_one in placements
				           if tag not in held_one.covered_by
				           and not held_one.path.endswith("." + tag)]
				assert not missing, (
					f"{path.name}: `{name}` names {tag} and it does not "
					f"cover {missing[:3]}")

	# Not vacuous: the corpus really does carry these, and a rule that
	# named nothing anywhere would satisfy the loop above silently.
	assert named >= 9, f"only {named} struct lines name a tag"
