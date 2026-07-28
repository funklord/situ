"""The struct walk every backend and artifact shares.

This existed as five copies before it existed as one function, and the copies
were not identical -- which is the argument for the module. With backends
planned for C++, Rust and Python (section 20.1), the rules below are the ones
each of them would otherwise get slightly wrong in its own way.
"""

from __future__ import annotations

import pytest

from pathlib import Path

from situc.ast import Schema
from situc.layout import solve
from situc.parser import parse_text
from situc.resolve import ResolvedStruct, resolve
from situc.traverse import (
	Check, Member, byte_span, classify, classify_check, container_bits,
	is_own_member, local_name, obligation, obligations, own_members, span_bits,
)

ROOT = Path(__file__).resolve().parents[2]

PREAMBLE = "target buffer;\nendian big;\nbit_order msb_first;\n"


def structs(body: str) -> dict[str, ResolvedStruct]:
	schema = parse_text(PREAMBLE + body)
	return resolve(schema, solve(schema)).structs


def names(body: str, struct: str) -> list[str]:
	held = structs(body)[struct]
	return [local_name(held, placement) for placement in own_members(held)]


def test_a_nested_structs_fields_belong_to_the_nested_struct() -> None:
	"""They appear in the parent's entries under a dotted path, and a walk that
	takes them places the parent's own members twice."""
	body = """struct inner { u16 a; u16 b; }
	struct outer { u8 tag; inner nested; u8 tail; }
	"""

	assert names(body, "outer") == ["tag", "nested", "tail"]
	assert names(body, "inner") == ["a", "b"]


def test_an_authenticated_region_is_not_a_member() -> None:
	"""It names bytes its members already own and consumes none itself. Counting
	it puts everything after it one region too far along -- which was a real bug,
	fixed once and then found again in each copy of the walk."""
	body = """struct h { u8 v; u8 w; }
	struct s {
		u8   hop;
		authenticated { h hdr; u8 nonce[12] [nonce]; }
		tag  u8[16];
	}
	"""

	walked = names(body, "s")
	assert "authenticated" not in walked
	assert walked == ["hop", "hdr", "nonce", "tag"]


def test_array_elements_are_not_members() -> None:
	"""`recs[]` describes every element at once, so it has no bytes of its own."""
	body = """struct r { u32 id; }
	struct s { u8 n; r recs[4]; }
	"""

	assert names(body, "s") == ["n", "recs"]


def test_members_partition_the_struct() -> None:
	"""The property the walk exists to have: every byte is claimed once. An
	offset arithmetic bug shows up here before it shows up in emitted code."""
	body = "struct s { u8 a; u16 b; u32 c; u8 d; }"
	held = structs(body)["s"]

	at = 0
	for placement in own_members(held):
		assert placement.offset_bits == at, f"{placement.path} is not contiguous"
		at += placement.size_bits

	assert at == held.layout.size_bytes * 8


# -- byte spans --------------------------------------------------------------


@pytest.mark.parametrize("body,field,want", [
	("struct s { u8 a; }",                        "s.a", (0, 1)),
	("struct s { u8 a; u16 b; }",                 "s.b", (1, 2)),
	("struct s { u4 a; u4 b; }",                  "s.a", (0, 1)),
	("struct s { u4 a; u4 b; }",                  "s.b", (0, 1)),
])
def test_byte_span_is_the_bytes_touched(body: str, field: str,
		want: tuple[int, int]) -> None:
	"""Not the placement's own width. A four-bit field is zero bytes wide if you
	divide, and dividing is what dropped every bit-packed field out of the
	Wireshark dissector."""
	held  = structs(body)["s"]
	found = next(p for p in held.entries if p.placement.path == field)

	assert byte_span(found.placement) == want


def test_a_straddling_field_spans_both_its_bytes() -> None:
	"""Thirteen bits starting three bits in: two bytes, not one."""
	body = "struct s [allow_straddle] { bit a; bit b; bit c; u13 d; }"
	held = structs(body)["s"]
	found = next(p for p in held.entries if p.placement.path == "s.d")

	assert byte_span(found.placement) == (0, 2)
	assert span_bits(found.placement) == 16


def test_container_widths_belong_to_the_backend() -> None:
	"""Wireshark has a 24-bit field type and C does not. One hardcoded list
	silently gave the dissector the C answer for a three-byte scalar."""
	held  = structs("struct s { u24 a; }")["s"]
	found = next(p for p in held.entries if p.placement.path == "s.a")

	assert container_bits(found.placement, (8, 16, 24, 32, 64)) == 24
	assert container_bits(found.placement, (8, 16, 32, 64)) == 32


def test_a_placement_with_no_extent_has_no_span() -> None:
	held  = structs("struct s { u8 a; u8 rest[remaining]; }")["s"]
	found = next(p for p in held.entries if p.placement.path == "s.rest")

	assert byte_span(found.placement) is None
	assert span_bits(found.placement) is None
	assert container_bits(found.placement, (8, 16, 32, 64)) is None


def test_is_own_member_agrees_with_own_members() -> None:
	"""They are two views of one rule, and a backend will reach for either."""
	body = """struct inner { u16 a; }
	struct outer { u8 tag; inner nested; }
	"""
	held = structs(body)["outer"]

	assert [p for p in (e.placement for e in held.entries)
	        if is_own_member(held, p)] == own_members(held)


# -- the dispatch order ------------------------------------------------------


def classified(body: str, field: str) -> Member:
	held = structs(body)
	name = field.split(".")[0]
	found = next(e.placement for e in held[name].entries
	             if e.placement.path == field)
	return classify(held[name], found, set(held))


def checked(body: str, field: str) -> Check:
	held = structs(body)
	name = field.split(".")[0]
	found = next(e.placement for e in held[name].entries
	             if e.placement.path == field)
	return classify_check(held[name], found, set(held))


def test_a_variable_member_is_decided_before_its_offset() -> None:
	"""The first of the two traps. A member sized by the data usually has a
	dynamic offset too, so a backend that asks about the offset first silently
	skips every variable member it has. Three backends shipped that."""
	body = "struct h { u8 v; u16 n; }\nstruct s { h hdr; u8 opts[hdr.n]; }"

	assert classified(body, "s.opts") is Member.VARIABLE


def test_an_array_of_structs_is_not_a_nested_struct() -> None:
	"""The second trap. It names a struct type, and treating it as one emits an
	accessor that takes no index -- and a validate call to a method that does
	not exist."""
	body = "struct r { u32 id; }\nstruct s { u8 n; r recs[4]; }"

	assert classified(body, "s.recs") is Member.ARRAY
	# And nothing to check: per-element validation is a cost the caller
	# chooses, so a struct array gets an accessor and no check.
	assert checked(body, "s.recs") is Check.NOTHING

	nested = "struct r { u32 id; }\nstruct s { u8 n; r one; }"
	assert classified(nested, "s.one") is Member.NESTED
	assert checked(nested, "s.one") is Check.NESTED


def test_a_scalar_at_a_dynamic_offset_is_still_a_scalar() -> None:
	"""The third way to get this wrong, found while factoring the first two:
	whether a dynamic offset can be *resolved* is the backend's business, and
	classifying it as unplaced drops every field after a variable member."""
	body = ("struct h { u8 v; u16 n; }"
	        "\nstruct s { h hdr; u8 opts[hdr.n]; u16 after; }")

	assert classified(body, "s.after") is Member.SCALAR
	assert checked(body, "s.after") is Check.CONSTRAINED


def test_a_reserved_field_is_neither_read_nor_ignored() -> None:
	body = "struct s { u4 a; reserved u4 [must_be_zero]; }"

	assert classified(body, "s.<reserved0>") is Member.RESERVED
	assert checked(body, "s.<reserved0>") is Check.RESERVED


def test_a_region_is_not_a_field() -> None:
	body = """struct h { u8 v; u16 length; }
	struct s {
		u8   hop;
		authenticated { h hdr; u8 nonce[12] [nonce]; }
		tag  u8[16];
	}
	"""
	assert classified(body, "s.authenticated") is Member.REGION


@pytest.mark.parametrize("target", ["c", "cpp", "python", "rust"])
def test_every_backend_uses_the_shared_order(target: str) -> None:
	"""The point of the module. A backend that grew its own dispatch would get
	the two traps above wrong in its own way, which is what happened three
	times before this existed.

	The C backend is the exception and is listed anyway: it predates the
	classifier and its dispatch is spread across `_field`, which is why it
	never had the bug -- it never had one place to get it wrong in.
	"""
	import importlib

	if target == "c":
		pytest.skip("the C backend dispatches per construct, not in one place")

	source = (ROOT / "situc" / "codegen" / target / "emit.py").read_text()
	assert "classify(struct, placement, self.structs)" in source
	assert "classify_check(struct, placement, self.structs)" in source


# -- obligations ------------------------------------------------------------
#
# Two backends numbered these themselves, from two different lists, and gave
# the same schema two answers. The bit a covered setter marks and the bit a
# recompute clears have to be one bit.


def schema_of(body: str) -> tuple[Schema, dict[str, ResolvedStruct]]:
	parsed = parse_text(PREAMBLE + body)
	return parsed, resolve(parsed, solve(parsed)).structs


BOTH = """struct s {
	u16 total;
	u8  a;
	authenticated inner { u8 b; }
	tag u8[16] covers(inner);
}
invariant s.total == size(s.a);
"""


def test_tags_and_invariants_share_one_numbering() -> None:
	parsed, found = schema_of(BOTH)

	held = obligations(parsed, found["s"])

	assert [one.bit for one in held] == [0, 1]
	assert [one.kind for one in held] == ["tag", "invariant"]


def test_tags_are_numbered_first() -> None:
	"""Not alphabetically, and not in declaration order across both kinds. Tags
	were numbered before invariants existed, and renumbering them would change
	what an already-generated header means for a caller who stored a bit."""
	parsed, found = schema_of(BOTH)

	first = obligations(parsed, found["s"])[0]

	assert first.kind == "tag"
	assert first.bit == 0


def test_an_obligations_label_is_not_its_identifier() -> None:
	"""`covered_by` holds a phrase, because "covered by invariant total" is what
	a diagnostic has to say. Pasting that where an identifier belongs produced
	`SITU_S_INVARIANT TOTAL_DIRTY`, a macro name with a space in it, in a header
	offered to a C compiler."""
	parsed, found = schema_of(BOTH)

	derived = next(one for one in obligations(parsed, found["s"])
	               if one.kind == "invariant")

	assert derived.label == "invariant total"
	assert derived.name  == "total"
	assert derived.name.isidentifier()


def test_every_label_a_placement_carries_resolves() -> None:
	"""The lookup is by label because that is what the layout solver recorded.
	A label that resolves to nothing gets silently mis-numbered rather than
	refused, so nothing may be left dangling."""
	parsed, found = schema_of(BOTH)
	held = found["s"]

	for placement in held.entries:
		for label in placement.placement.covered_by:
			assert obligation(parsed, held, label) is not None, label


def test_a_tag_is_dirty_and_a_derived_field_is_stale() -> None:
	"""Different words for one bit, because they are different sentences: a tag
	no longer matches the bytes, a field no longer equals its definition."""
	parsed, found = schema_of(BOTH)
	held = {one.kind: one.suffix for one in obligations(parsed, found["s"])}

	assert held == {"tag": "DIRTY", "invariant": "STALE"}


def test_an_invariant_belongs_only_to_its_own_struct() -> None:
	"""Both halves are evaluated against one view, so an invariant over `s`
	numbers nothing in `t` -- and a `t` that inherited the bit would collide
	with its own first tag."""
	parsed, found = schema_of(BOTH + "struct t { u8 x; }\n")

	assert obligations(parsed, found["t"]) == []
