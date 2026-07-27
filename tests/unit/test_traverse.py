"""The struct walk every backend and artifact shares.

This existed as five copies before it existed as one function, and the copies
were not identical -- which is the argument for the module. With backends
planned for C++, Rust and Python (section 20.1), the rules below are the ones
each of them would otherwise get slightly wrong in its own way.
"""

from __future__ import annotations

import pytest

from situc.layout import solve
from situc.parser import parse_text
from situc.resolve import ResolvedStruct, resolve
from situc.traverse import (
	byte_span, container_bits, is_own_member, local_name, own_members, span_bits,
)

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
