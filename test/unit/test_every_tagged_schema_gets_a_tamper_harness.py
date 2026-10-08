"""The two generators whose output is a CHECK, swept over the corpus
(26.593).

`gen-fuzz` emits a libFuzzer entry point per schema and `make fuzz` runs
them. `gen-tamper` emits a harness that flips every byte of a message and
asserts the tag refuses exactly the covered ones -- which is the claim an
`authenticated` region makes, so a schema whose harness quietly stopped
being emitted would lose the only check that its coverage is what it says.

`test_tamper.py` compiles and runs one, with two deliberate liars as
controls, and that is the half that proves the harness WORKS. What nothing
asked is which schemas get one. Both populations are measured here:

	fuzz     45 of 45 schemas
	tamper   4 of 45 -- dtls, keystore, packet, edges

The tamper four are exactly the schemas declaring a `tag`. A checksum is
not one: udp and icmp carry `checksum` members and get no harness, which
is right -- flipping a byte under a CRC proves nothing about
authentication, and 14.1 puts a cryptographic tag in a different place
from a sum.

ASKED OF THE SCHEMA, not of the generator. Which structs declare a tag
comes from `resolved`, so a generator that stopped emitting fails here
rather than agreeing with itself about what it should have emitted.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "test" / "unit"))

import pytest					# noqa: E402
from every_schema import SCHEMAS, ids, load_schema	# noqa: E402
from situc.codegen.c import fuzz, tamper		# noqa: E402
from situc.layout import solve			# noqa: E402
from situc import ast					# noqa: E402
from situc.resolve import ResolvedSchema, resolve	# noqa: E402

#: The schemas that declare a `tag`, and so the ones a tamper harness is
#: owed. Named rather than counted: a count tells the next reader a number
#: moved and a list tells them which.
TAGGED = {"dtls", "keystore", "packet", "edges"}


def _resolved(path: Path) -> tuple[ast.Schema, ResolvedSchema]:
	parsed = load_schema(path)
	return parsed, resolve(parsed, solve(parsed))


@pytest.mark.parametrize("path", SCHEMAS, ids=ids(SCHEMAS))
def test_every_schema_gets_a_fuzz_harness(path: Path) -> None:
	"""All 45, because a harness is what `make fuzz` runs.

	A schema with no harness is one nobody fuzzes, and the two saturating
	helpers in `runtime/c` were both found by fuzzing -- `situ_remaining_u32`
	and `situ_advance_u32`, each with its own note saying so, and the second
	"three seconds into the first run that was fuzzing rather than eight
	random inputs".
	"""
	parsed, resolved = _resolved(path)
	text = fuzz.generate(parsed, resolved, path.stem)

	assert text and text.strip(), f"{path.stem} gets no fuzz harness"
	# WITH THE PAREN, because a substring assertion passes for a longer
	# name: renaming the entry point to `LLVMFuzzerTestOneInputX` left
	# `"LLVMFuzzerTestOneInput" in text` true and this test green, which is
	# a check whose pass includes the failure it was written for.
	assert "LLVMFuzzerTestOneInput(" in text, (
		f"{path.stem}'s harness has no libFuzzer entry point, so `make "
		f"fuzz` would build it and run nothing")


def test_a_tamper_harness_goes_to_exactly_the_tagged_schemas() -> None:
	"""The POPULATION, in both directions.

	A schema that gains a tag and gets no harness loses a check nobody will
	miss, and one that gets a harness without a tag has a harness with
	nothing to flip against. Both fail here, and the message names the
	schema rather than the count.
	"""
	owed: set[str] = set()
	given: set[str] = set()

	for path in SCHEMAS:
		parsed, resolved = _resolved(path)
		if any(entry.placement.kind == "tag"
		       for struct in resolved.structs.values()
		       for entry in struct.entries):
			owed.add(path.stem)

		files = tamper.generate(parsed, resolved, path.stem)
		if any(text and text.strip() for text in files.values()):
			given.add(path.stem)

	assert owed == TAGGED, (
		f"the schemas declaring a tag have changed: {sorted(owed)} against "
		f"{sorted(TAGGED)}. A new one is owed a tamper harness, and this "
		f"list is where that is decided")
	assert given == owed, (
		f"owed a tamper harness but got none: {sorted(owed - given)}; "
		f"given one with no tag: {sorted(given - owed)}")


def test_a_checksum_is_not_a_tag_for_this_purpose() -> None:
	"""udp and icmp carry a `checksum` and are deliberately not in the set.

	Pinned because the distinction is the kind a later reader would
	"fix": flipping a byte under a CRC proves nothing about
	authentication, since anyone who can flip the byte can recompute the
	sum. 14.1 puts a cryptographic tag with a different owner from a sum,
	and this is that line showing up in what gets generated.
	"""
	for name in ("udp", "icmp"):
		path = next(one for one in SCHEMAS if one.stem == name)
		parsed, resolved = _resolved(path)

		sums = [entry.placement.name
		        for struct in resolved.structs.values()
		        for entry in struct.entries
		        if entry.placement.kind == "checksum"]
		assert sums, f"{name} no longer carries a checksum at all"
		assert name not in TAGGED

		files = tamper.generate(parsed, resolved, name)
		assert not any(text and text.strip() for text in files.values()), (
			f"{name} has a checksum and no tag, and got a tamper harness")
