"""`image_endian`'s fourth value, and why its silence is correct (26.601).

`std/image.situ` declares `unset`, `big`, `little` and `native`;
`walker/image.py` named three of them. That is the shape 26.597 was: a
reader's copy short of the schema's enum, which a consumer then writes
correct code over without ever learning what it is missing.

Here the missing value is load-bearing by ABSENCE. `walk._order` falls
through to big for anything that is not `LITTLE` or `NATIVE`, so a
placement carrying `UNSET` reads big-endian -- and the packer writes
`UNSET` 76 times across the corpus, including in `keystore`, whose own
`endian` is little. Nothing was wrong, and nothing on this side said why
-- `situ_walk.c` had reasoned it out for its own narrower build, where it
declines such a read by name:

	 6   `marker_governed`       `_order` asks the marker first
	 2   the markers THEMSELVES  read big because they decide the order
	 3   sub-byte scalars        5, 7 and 5 bits: bit_order governs
	33   byte runs               read as bytes either way
	29   regions, variants, a tlv run -- spans, not values
	 3   pads                    RESERVED, and `_scalars` never probes one

The first version of that table said 73 and 3, which was the same sweep
read less carefully -- the 3 were only the MULTI-BYTE scalars among the
marker-governed six. A count taken to justify a claim is the one nobody
re-checks, so it is broken out rather than totalled.

So this module asserts the PARTITION rather than the absence. "The packer
never writes unset" is false, and "it does not matter" is a claim about
every placement in a corpus that grows -- what is checkable is that each
one falls in one of those two cells, so a placement arriving in neither
fails as a message addressed to whoever made the packer write it.

The last test pins the consequence, which is what makes the partition
worth having: an unset, unmarked, multi-byte scalar really does read
big-endian, so the cells are not a stylistic preference.
"""

from __future__ import annotations

import dataclasses
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "test" / "unit"))

import pytest						# noqa: E402
from situc import ast				# noqa: E402
from situc import pack as packer			# noqa: E402
from situc import parser				# noqa: E402
from situc.diagnostics import Source			# noqa: E402
from situc.layout import solve				# noqa: E402
from situc.resolve import resolve			# noqa: E402
from walker import report				# noqa: E402
from walker.image import BIG, LITTLE, NATIVE, UNSET, load	# noqa: E402
from walker.walk import View, is_run			# noqa: E402
from walker.walk import _order				# noqa: E402
from every_schema import SCHEMAS, ids			# noqa: E402


def packed(path: Path) -> bytes:
	parsed   = parser.parse(Source(str(path), path.read_text(encoding="utf-8")))
	resolved = resolve(parsed, solve(parsed))
	blob, _  = packer.pack(parsed, resolved, metadata=True)
	return blob


def orders_a_value(image: object, index: int) -> bool:
	"""Whether byte order can change what this placement reads.

	`walk.is_run` decides whether it holds several values rather than a
	second reading here, which is the reason that function was extracted
	(26.578). A pad is RESERVED and `_scalars` says in as many words that
	it never probes one; a region, a variant and a tlv run are spans; and
	anything one byte wide or narrower has no byte order -- the three
	sub-byte cases in the corpus are 5-, 7- and 5-bit CRCs, which
	`bit_order` governs instead.

	**A MARKER is excluded, and that one is a design fact rather than a
	triviality.** It is read as a scalar -- big-endian, always -- because
	it is what decides the order and so cannot be read in the order it is
	about. Including it here would fail this partition on tiff's
	`byte_order` while nothing was wrong.
	"""
	placement = image.placements[index]			# type: ignore[attr-defined]
	if placement.kind != report.FIELD:
		return False
	if is_run(image, index):				# type: ignore[arg-type]
		return False
	return (placement.size_bits or 0) > 8


def test_the_readers_endian_table_matches_the_schemas_enum() -> None:
	"""The gate that stops this recurring, as `image_kind` has one. A copy
	short of the schema is not a defect anybody trips over -- the value it
	omits simply goes unmentioned -- so only a comparison can see it."""
	schema = parser.parse(Source(
		"std/image.situ",
		(ROOT / "std" / "image.situ").read_text(encoding="utf-8")))
	declared = [decl for decl in schema.enums()
	            if decl.name == "image_endian"]
	assert len(declared) == 1

	theirs = {}
	for member in declared[0].members:
		assert isinstance(member.value, ast.IntLiteral), member.name
		theirs[member.name] = member.value.value
	assert theirs == {"unset": UNSET, "big": BIG,
	                  "little": LITTLE, "native": NATIVE}


@pytest.mark.parametrize("path", SCHEMAS, ids=ids(SCHEMAS))
def test_every_unset_placement_falls_in_one_of_the_two_cells(
		path: Path) -> None:
	"""A placement whose byte order is unset must have no byte order
	question to answer -- either nothing to order, or a marker that
	answers before `_order` reaches its fall-through."""
	image = load(packed(path))
	for index in range(len(image.structs)):
		for member in image.members(image.structs[index]):
			placement = image.placements[member]
			if placement.endian != UNSET:
				continue
			assert (placement.marker_governed
			        or not orders_a_value(image, member)), (
				f"{path.name}:{image.struct_name(index)}:"
				f"{report._local(image, member)} carries endian unset, "
				f"orders a value, and no marker governs it -- so it reads "
				f"big-endian whatever its schema says")


def test_both_cells_are_occupied_by_the_corpus() -> None:
	"""A partition over an empty cell is a claim rather than a guarantee.
	If either empties, this fails and says which -- and an empty
	`marker_governed` cell would mean the harder half had stopped being
	exercised."""
	seen = set()
	for path in SCHEMAS:
		image = load(packed(path))
		for index in range(len(image.structs)):
			for member in image.members(image.structs[index]):
				if image.placements[member].endian != UNSET:
					continue
				seen.add("marker" if image.placements[member].marker_governed
				         else "nothing-to-order")
	assert seen == {"marker", "nothing-to-order"}, sorted(seen)


def test_an_unset_unmarked_scalar_really_does_read_big() -> None:
	"""The consequence the partition exists to keep unreachable.

	Built by replacing one placement's own fields rather than by finding
	such a case, because the packer does not produce one -- which is the
	whole finding. If `_order` ever stopped falling through to big, the
	partition above would go on passing while the hazard it guards had
	gone, so the fall-through is pinned here beside it.
	"""
	image_bytes = packed(ROOT / "example" / "keystore" / "keystore.situ")
	image = load(image_bytes)
	little = [index for index in range(len(image.placements))
	          if image.placements[index].endian == LITTLE]
	assert little, "keystore declares `endian little` and should have one"

	index = little[0]
	# `marker_governed` is derived from the placement's flags rather than
	# being a field, so it is ASSERTED rather than forced: keystore carries
	# no marker at all, which makes every placement in it unmarked and this
	# the honest fixture instead of a hand-cleared flag bit.
	assert not image.placements[index].marker_governed
	image.placements[index] = dataclasses.replace(
		image.placements[index], endian = UNSET)
	view = View(image, b"\x00" * 64, 0, 0, 64)
	assert _order(view, index) == "big"
