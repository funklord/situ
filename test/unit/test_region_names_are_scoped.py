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


# -- the guard a collision could silence (26.529) ----------------------------


SUB_BYTE = """target buffer;
endian big;

codec crc5_usb {
	kernel = polynomial(width = 5, poly = 0x05, init = 0x1F, xorout = 0x1F,
	                    reflect);
}
impl crc5_usb derived;

struct filler {
	authenticated body { u8 pad; }
	checksum u8 fsig[1] covers(body) is crc5_usb;
}

struct wrong_order [allow_straddle, bit_order = msb_first] {
	authenticated body {
		u7  address;
		u4  endpoint;
	}
	u5           gap;
FILLER
	checksum u5  crc covers(body) is crc5_usb;
	u3           tail;
}
"""


def _refusal(text: str) -> str:
	from situc.diagnostics import SituError
	schema = parse_text(text)
	try:
		resolve(schema, solve(schema))
	except SituError as exc:
		return str(exc)
	return ""


def test_the_sub_byte_bit_order_guard_fires_at_all() -> None:
	"""The control, and it is the whole reason the next test means
	anything: a guard that cannot fire is silenced by everything.

	This one had NO test before 26.529 -- `grep 'covers .* bits'` over
	`test/` found nothing -- which is why it could stop firing without
	anybody noticing. It exists because USB's token computed a CRC over
	three of its own check bits, stored it, and then refused its own
	message.
	"""
	assert "reads them the other way round" in _refusal(
		SUB_BYTE.replace("FILLER\n", ""))


def test_a_same_named_region_next_door_does_not_silence_it() -> None:
	"""The fault. `_check_bit_coverage_direction` skips a coverage that
	is whole bytes, so the span decides whether it applies at all.

	Gathering by bare name took `wrong_order.nested.body` as well and
	widened the span from 11 bits to 24 -- byte-aligned, so the guard
	skipped and a schema the compiler should refuse built clean. A
	member with nothing to do with the tag turned the check off.
	"""
	assert "reads them the other way round" in _refusal(
		SUB_BYTE.replace("FILLER", "\tfiller       nested;"))


# -- the other direction: a valid schema refused (26.530) --------------------


TRANSFORM = """target buffer;
endian big;

codec masking {
	granularity = byte;
	length_preserving;
	seekable;
	invertible;
	deterministic;
}
impl masking extern "app_header_mask";

codec summing { kernel = ones_complement(width = 16); }
impl summing derived;

struct inner {
	authenticated body { u16 first; }
	checksum u8 isig[2] covers(body) is summing;
}

struct outer {
	u16    first;
	coded  pn(masking) covers(first) { u16 number; }
NESTED
}
"""

AMBIGUOUS = """target buffer;
endian big;

codec masking {
	granularity = byte;
	length_preserving;
	seekable;
	invertible;
	deterministic;
}
impl masking extern "app_header_mask";

codec summing { kernel = ones_complement(width = 16); }
impl summing derived;

struct must_say {
	authenticated body { u16 first; }
	coded  pn(masking) covers(first) { u16 number; }
	checksum u8 sig[2] covers(body) is summing;
}
"""


def test_tag_order_is_demanded_when_it_is_genuinely_ambiguous() -> None:
	"""The control. `pn` transforms bytes this struct's own tag covers,
	so 14.1b says the schema has to state the order -- and the wrong
	choice is undetectable at run time, which is why it is an error.

	Without this, the test below passes just as well against a guard
	that has stopped firing altogether.
	"""
	assert "does not say in which order" in _refusal(AMBIGUOUS)


def test_a_nested_structs_field_does_not_invent_the_ambiguity() -> None:
	"""`_transform_covers` gathered by bare name over every placement,
	with a depth test that admitted exactly two levels -- so a member
	called `first` inside a nested struct came along, and the tags
	gathered with it were that struct's.

	`outer.first` is covered by nothing, so `pn` needs no `tag_order`.
	The compiler refused the schema anyway, saying `pn` transforms bytes
	`nested.isig` covers. It does not: those bytes are inside `nested`.

	A message naming a tag from another struct is the tell, and it is
	the same tell the eleven faults before this one left.
	"""
	assert _refusal(TRANSFORM.replace("NESTED\n", "")) == "", \
		"the schema without the nested member must compile"
	assert _refusal(TRANSFORM.replace("NESTED", "\tinner  nested;")) == "", \
		"a nested struct reusing a field name invented an ambiguity"
