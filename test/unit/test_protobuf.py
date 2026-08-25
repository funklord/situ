"""The protobuf conformance test (project.md section 9.7).

This is the gate on phase 7, and it is a test of the compiler rather than a
feature of it. Protobuf is close to the worst case on every capability axis, so
if situ can describe it faithfully and then report exactly why each capability
is weak, the lattice is sound. If it cannot, the lattice is decorative.

Section 9.7 names five independent causes of non-canonicity and requires
`situc explain` to enumerate all of them with source locations. Each is checked
by name below, because "five causes" passing while one of them is wrong would
be worse than failing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from situc import requirements
from situc.capability import Axis, Value
from situc.diagnostics import Source
from situc.layout import solve
from situc.parser import parse
from situc.propagate import Weakening
from situc.resolve import ResolvedSchema, resolve

SCHEMA = Path(__file__).resolve().parents[2] / "example" / "protobuf" / "protobuf.situ"


@pytest.fixture(scope="module")
def resolved() -> ResolvedSchema:
	source = Source(str(SCHEMA), SCHEMA.read_text(encoding="ascii"))
	schema = parse(source)
	return resolve(schema, solve(schema))


def canonical_blame(resolved: ResolvedSchema) -> list[Weakening]:
	entry = resolved.find("proto_message.fields")
	assert entry is not None
	return entry.blame(Axis.CANONICAL)


# -- the expected capability outcome ----------------------------------------


def test_the_region_is_unbounded(resolved: ResolvedSchema) -> None:
	entry = resolved.find("proto_message.fields")
	assert entry is not None
	assert entry.vector.get(Axis.SIZE) == Value("Unbounded")


def test_the_region_starts_where_it_starts(resolved: ResolvedSchema) -> None:
	"""The one thing protobuf does keep, and situ says so."""
	entry = resolved.find("proto_message.fields")
	assert entry is not None
	assert entry.vector.get(Axis.OFFSET) == Value("AbsoluteStatic", ("0x00",))


def test_access_is_sequential(resolved: ResolvedSchema) -> None:
	entry = resolved.find("proto_message.fields")
	assert entry is not None
	assert entry.vector.get(Axis.ACCESS) == Value("Sequential")


def test_addresses_are_unstable(resolved: ResolvedSchema) -> None:
	entry = resolved.find("proto_message.fields")
	assert entry is not None
	assert entry.vector.get(Axis.ADDRESS) == Value("Unstable")


def test_the_region_is_not_canonical(resolved: ResolvedSchema) -> None:
	entry = resolved.find("proto_message.fields")
	assert entry is not None
	assert entry.vector.get(Axis.CANONICAL) == Value("NonCanonical")


# -- the five causes --------------------------------------------------------


EXPECTED_CAUSES = {
	# "non-minimal varint encodings accepted (pb_varint has no `minimal`)"
	"tlv-non-minimal-tag",
	# "duplicate_tags = allowed with no ordering rule"
	"tlv-unordered-duplicates",
	# "unknown = preserve"
	"tlv-unknown-preserve",
	# "field order is unconstrained"
	"tlv-unordered-items",
	# "packed and unpacked repeated encodings both legal"
	"tlv-packed-and-unpacked",
}


def test_all_five_causes_are_reported(resolved: ResolvedSchema) -> None:
	"""Section 9.7 names five, and every one has to be found by name.

	Counting five while one of them is the wrong cause would be worse than
	reporting four.
	"""
	found = {weakening.rule.name for weakening in canonical_blame(resolved)}
	assert found == EXPECTED_CAUSES


def test_the_causes_are_independent(resolved: ResolvedSchema) -> None:
	"""Five separate weakenings, not one reported five times."""
	assert len(canonical_blame(resolved)) == len(EXPECTED_CAUSES)


@pytest.mark.parametrize("cause", sorted(EXPECTED_CAUSES))
def test_each_cause_carries_a_source_location(resolved: ResolvedSchema,
		cause: str) -> None:
	"""Section 9.7 asks for source locations, not just a list of reasons."""
	weakening = next(w for w in canonical_blame(resolved) if w.rule.name == cause)
	line, column = weakening.span.source.locate(weakening.span.start)

	assert weakening.span.source.path.endswith("protobuf.situ")
	assert line > 0 and column > 0
	assert "tlv" in weakening.span.text()


@pytest.mark.parametrize("cause", sorted(EXPECTED_CAUSES))
def test_each_cause_explains_itself(resolved: ResolvedSchema, cause: str) -> None:
	"""A cause with no explanation would be a diagnostic without a root cause,
	which section 26 invariant 3 calls a bug."""
	weakening = next(w for w in canonical_blame(resolved) if w.rule.name == cause)
	assert weakening.rule.construct
	assert weakening.effect.because
	assert weakening.rule.remedy


def test_the_non_minimal_cause_names_the_tag_type(resolved: ResolvedSchema) -> None:
	weakening = next(w for w in canonical_blame(resolved)
	                 if w.rule.name == "tlv-non-minimal-tag")
	assert "more than one encoding" in weakening.effect.because
	assert "minimal" in weakening.rule.remedy


def test_the_packed_cause_describes_both_encodings(resolved: ResolvedSchema) -> None:
	weakening = next(w for w in canonical_blame(resolved)
	                 if w.rule.name == "tlv-packed-and-unpacked")
	assert "several scalar items" in weakening.effect.because
	assert "one length-prefixed item" in weakening.effect.because


# -- the requirements the schema states -------------------------------------


def test_the_schema_discharges(resolved: ResolvedSchema) -> None:
	"""The asserts fail and the require passes, which is the schema's point:
	every failing assertion is a true statement about protobuf."""
	source   = Source(str(SCHEMA), SCHEMA.read_text(encoding="ascii"))
	schema   = parse(source)
	outcomes = requirements.discharge(schema, resolved)

	satisfied: dict[str, list[bool]] = {
		outcome.requirement.kind.value: [] for outcome in outcomes}
	for outcome in outcomes:
		satisfied[outcome.requirement.kind.value].append(outcome.satisfied)

	assert satisfied["assert"] == [False, False, False]
	assert satisfied["require"] == [True]
