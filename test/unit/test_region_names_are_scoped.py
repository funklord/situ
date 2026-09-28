"""Every lookup that answers "which region does this name mean" (26.528).

`Placement.regions` and `Placement.tag_covers` both hold BARE region
names, so a name is only an answer together with the struct it was
declared in. Ten faults in one family came from asking without that
scope, and they were spread across four modules -- the capability map,
the packed image, the advisor and the code generators -- with the last
one emitting a checksum over twice the declared bytes in all four
backends.

**The corpus is the detector, and it had to be taught how.** Three
sweeps ran over these schemas and found nothing while each found a real
fault beside them: every schema here was written by somebody naming
regions distinctly, so the one property this family needs in order to
show up was the property the corpus was curated not to have.
`edges.situ` now carries `outer_body`, a struct whose own region shares
a name with one its member's type declares, and the assertions below
read it FROM THERE rather than from a copy -- a private fixture would
go on passing after somebody removed the construct it stands for.

**What this does not catch**, pinned here so nobody quotes it for more
than it does: a NEW consumer that asks by bare name and that nothing
else observes. The four sites below are the ones whose answers are
reachable from outside; a fifth added tomorrow is covered only once
something asserts on it. The durable half is the schema, not this file
-- it is in the corpus every backend compiles and every sweep reads.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from situc import advise, pack as packer, traverse
from situc.layout import solve
from situc.parser import parse_text
from situc.resolve import resolve
from situc.traverse import own_entries

from every_schema import ROOT, SCHEMAS, ids

from walker import image as image_reader

EDGES = ROOT / "test" / "schema" / "edges.situ"

#: The construct this file exists for, by name so its removal is loud.
COLLIDING = "outer_body"
NESTED = "outer_body.nested"


@pytest.fixture(scope="module")
def edges() -> tuple[object, object]:
	schema = parse_text(EDGES.read_text())
	return schema, resolve(schema, solve(schema))


def test_the_corpus_still_carries_the_colliding_construct(edges) -> None:  # type: ignore[no-untyped-def]
	"""First, because every assertion below is vacuous without it.

	A gate over a construct that has been deleted passes exactly as
	loudly as one over a construct that is correct.
	"""
	_, resolved = edges
	held = resolved.structs[COLLIDING]

	# The kinds spelled out rather than imported from `pack`, so this file
	# can be run against a tree that predates the constant. A test that
	# names a symbol its own change introduced raises AttributeError
	# against the old code, which is a crash where a control should be a
	# message, and proves only that the symbol is new.
	regions = [entry.placement.path for entry in held.entries
	           if entry.placement.kind in ("sealed", "coded", "authenticated")]
	assert f"{COLLIDING}.body" in regions
	assert f"{NESTED}.body" in regions, \
		"the nested struct no longer shares the region name, so nothing here tests anything"


def test_coverage_stops_at_the_region_it_names(edges) -> None:  # type: ignore[no-untyped-def]
	"""`layout.resolve_coverage`. The outer tag covers its own region
	only; the nested struct's members answer to their own tag.

	And the legitimate nesting has to survive: `signed_whole.piece` IS
	inside `whole_body`, so its members carry both tags. Under-claiming
	is the worse direction and is what the first repair here did.
	"""
	_, resolved = edges
	by_path = {entry.placement.path: entry.placement
	           for entry in resolved.structs[COLLIDING].entries}

	assert by_path[f"{COLLIDING}.outer_value"].covered_by == ("outer_sum",)
	assert by_path[f"{NESTED}.inner_value"].covered_by == ("nested.inner_sum",), \
		"the outer tag claimed authentication for a nested struct's bytes"

	whole = {entry.placement.path: entry.placement
	         for entry in resolved.structs["signed_whole"].entries}
	assert whole["signed_whole.piece.a"].covered_by \
		== ("piece.part_sig", "whole_sig"), "real nested coverage was lost"


def test_the_covered_run_stops_there_too(edges) -> None:  # type: ignore[no-untyped-def]
	"""`traverse.covered_run`, which every backend turns into a span.

	The two regions are adjacent, so an unscoped run is not refused --
	it comes back four bytes long and the generated checksum runs over
	twice what the schema declares.
	"""
	_, resolved = edges
	held = resolved.structs[COLLIDING]
	tag = next(entry.placement for entry in own_entries(held)
	           if entry.placement.path == f"{COLLIDING}.outer_sum")

	run = traverse.covered_run(held, tag)
	assert run is not None
	first, last = run
	assert (first.path, last.path) == (f"{COLLIDING}.body", f"{COLLIDING}.body")
	assert first.offset_bits is not None and last.offset_bits is not None
	assert (last.offset_bits + last.size_bits) - first.offset_bits == 16


def test_the_advisors_extent_stops_there_too(edges) -> None:  # type: ignore[no-untyped-def]
	"""`advise._covered_bytes`, which is a suggestion's whole claim."""
	_, resolved = edges
	held = resolved.structs[COLLIDING]
	tag = next(entry.placement for entry in own_entries(held)
	           if entry.placement.path == f"{COLLIDING}.outer_sum")

	assert advise._covered_bytes(held, tag) == 2


def test_the_image_gives_each_region_its_own_members(edges) -> None:  # type: ignore[no-untyped-def]
	"""`pack._region_owners`, which decides what a gate protects."""
	schema, resolved = edges
	blob, _ = packer.pack(schema, resolved, metadata=True)
	image = image_reader.load(blob)

	owners = {image.name_of(i): image.name_of(owner)
	          for i, owner in image.region_owner.items()}
	assert owners[f"{COLLIDING}.outer_value"] == f"{COLLIDING}.body"
	# The nested region owns ITSELF rather than the first row in the image
	# whose path ends `.body`, which is what it was recorded as before
	# 26.525. Its interior is not a row here -- a nested struct's members
	# arrive only when something wants them -- so this is the assertion the
	# image can carry, and it is the one that moved.
	assert owners[f"{NESTED}.body"] == f"{NESTED}.body"


@pytest.mark.parametrize("path", SCHEMAS, ids=ids(SCHEMAS))
def test_the_two_derivations_of_coverage_agree(path: Path) -> None:
	"""The corpus-wide half: `layout` and `traverse` must not diverge.

	Which regions a tag covers is worked out twice from the same bare
	names -- once by `layout.resolve_coverage` into `covered_by`, and
	once by `traverse.covered_regions` for the span a backend emits. A
	region the second names must say so in the first.

	**This catches divergence and not shared error, and the difference
	matters enough to write down.** Before 26.527 and 26.528 both
	derivations were wrong in the same direction, so they agreed and
	this would have passed. What it defends is the state after: two
	modules deriving one fact, where a regression in either alone now
	shows up as a disagreement rather than as a quiet wrong answer in
	both. The tests above are what catch a shared error, because their
	expected values were read off the schema rather than out of the
	code.
	"""
	schema = parse_text(path.read_text())
	resolved = resolve(schema, solve(schema))

	for name, held in resolved.structs.items():
		for entry in own_entries(held):
			tag = entry.placement
			if tag.kind not in ("tag", "checksum") or not tag.tag_covers:
				continue
			local = tag.path[len(name) + 1:]
			for region in traverse.covered_regions(held, tag):
				assert local in region.covered_by, \
					(f"{tag.path} names {region.path} as covered, and "
					 f"{region.path} says {region.covered_by}")
