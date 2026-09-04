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
	declared_value_bounds,
	has_computable_extent, is_own_member, local_name, obligation, obligations,
	own_members, span_bits,
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
		authenticated { h hdr; u8 nonce[12]; }
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
		authenticated { h hdr; u8 nonce[12]; }
		tag  u8[16];
	}
	"""
	assert classified(body, "s.authenticated") is Member.REGION


#: Every backend, read from the tree rather than typed here.
#:
#: The list was `["c", "cpp", "python", "rust"]`, which named all four and
#: was therefore complete -- until a fifth is added, which is the one moment
#: this test needs to fire. A backend that grows its own dispatch is exactly
#: what it exists to catch, and a new backend is exactly when that happens.
#: A hand-written population makes the name a claim about the day it was
#: written.
EMITTERS = sorted(path.parent.name
                  for path in (ROOT / "situc" / "codegen").glob("*/emit.py"))

# Deriving the population fixed one failure and opened another: pytest skips
# an empty parameter set rather than failing it, so a glob that stops matching
# takes this test with it and says nothing. That is the same hole
# `every_schema.py` carries a guard for, and this list was written an hour
# after that one without it.
assert EMITTERS, (
	f"no backend found under {ROOT / 'situc' / 'codegen'}; parametrizing over "
	f"an empty list would skip this test rather than fail it, which is how a "
	f"derived population goes quiet")

#: Backends that legitimately do not route through the shared classifier,
#: with the reason. Named rather than skipped by a string comparison inside
#: the test, so the exception is as visible as the rule.
OWN_DISPATCH = {
	"c": "predates the classifier; its dispatch is spread across `_field`, "
	     "which is why it never had the bug -- it never had one place to get "
	     "it wrong in",
}


@pytest.mark.parametrize("target", EMITTERS)
def test_every_backend_uses_the_shared_order(target: str) -> None:
	"""The point of the module. A backend that grew its own dispatch would get
	the two traps above wrong in its own way, which is what happened three
	times before this existed.
	"""
	if target in OWN_DISPATCH:
		pytest.skip(f"{target}: {OWN_DISPATCH[target]}")

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


def test_a_delimited_member_has_no_element_count() -> None:
	"""`x[] until "D"` is one run, not one element, and the solver used to
	record `array_count = 1` for it.

	Four consumers believed it and each needed its own code to disbelieve it:
	the classifier called it an ARRAY, `doc` labelled it `x[1]` and drew a
	one-byte box, the dissector read one byte and misaligned the rest of the
	packet, and `gen-checks` sized a synthesised instance from it. Removing it
	then exposed a fifth place that was only accidentally right -- the
	dissector reached its delimiter branch because the *nested struct* check
	above it failed, and it failed because of the lie.
	"""
	parsed, found = schema_of('struct s { u8 line[] until "\\r\\n"; u8 r[remaining]; }')
	line = next(p for p in own_members(found["s"]) if p.name == "line")

	assert line.array_count is None
	assert line.delimiter == b"\r\n"


def test_a_real_array_still_has_one() -> None:
	"""The other half. Removing a wrong value is only right if the right one
	survives."""
	_, found = schema_of("struct s { u16 xs[4]; }")
	xs = next(p for p in own_members(found["s"]) if p.name == "xs")

	assert xs.array_count == 4


def test_a_delimited_member_has_no_byte_span() -> None:
	"""`size_bits` for one is its *delimiter's* width -- an honest lower bound,
	and the one number that is not the answer to "which bytes does this
	touch".

	Returning it made a variable-length text field come out as a two-byte
	span, and the dissector declared `ProtoField.uint8` for an HTTP header
	name: Wireshark would have shown it as a single decimal number. That is
	the `array_count` lesson one level down, and harder to see -- the count
	was flatly false, and this is a true number answering a different
	question.
	"""
	_, found = schema_of('struct s { u8 line[] until "\\r\\n"; u8 r[remaining]; }')
	line = next(p for p in own_members(found["s"]) if p.name == "line")

	assert line.size_bits == 16		# the CRLF, and a true lower bound
	assert byte_span(line) is None
	assert container_bits(line, (8, 16, 32)) is None


def test_a_fixed_member_still_has_one() -> None:
	"""The half that keeps the other honest."""
	_, found = schema_of("struct s { u16 a; }")
	a = next(p for p in own_members(found["s"]) if p.name == "a")

	assert byte_span(a) == (0, 2)


# -- whether an instance can be measured from its own bytes -----------------


def measurable(body: str, struct: str) -> bool:
	found = structs(body)
	return has_computable_extent(found, found[struct])


def test_a_struct_of_fixed_members_can_be_measured() -> None:
	assert measurable("struct s { u8 a; u16 b; }", "s")


def test_so_can_one_sized_by_its_own_field() -> None:
	"""Variable, but every member's length is written down somewhere ahead of
	it, which is the whole of what this asks."""
	assert measurable("struct s { u8 n; u8 body[n]; }", "s")


def test_a_variant_can_be_when_every_arm_can() -> None:
	"""Its extent is whichever arm the discriminant selects, and the arms
	differ in length. That is an argument for a switch, not for giving up:
	this read `not measurable` first, and refusing variants refused every run
	over one -- which cost the DNS example the thing it exists to show."""
	assert measurable(
		"struct s { u8 k; variant v switch (k) { case 0: u8 a; "
		"case 1: u32 b; default: error; } }", "s")


def test_an_opaque_default_arm_cannot_be() -> None:
	"""It swallows whatever is left, which is `[remaining]` spelled
	differently and refused for the same reason."""
	assert not measurable(
		"struct s { u8 k; variant v switch (k) { case 0: u8 a; "
		"default: opaque; } }", "s")


def test_nor_can_an_arm_whose_own_member_cannot() -> None:
	"""Measurability is only as good as the worst arm."""
	assert not measurable(
		"struct s { u8 k; variant v switch (k) { case 0: u8 a; "
		"case 1: u8 rest[remaining]; default: error; } }", "s")


def test_a_remaining_member_cannot_be() -> None:
	"""Its length is the view's rather than the struct's, so an instance is
	exactly whatever view it was handed."""
	assert not measurable("struct s { u8 a; u8 rest[remaining]; }", "s")


def test_it_asks_the_same_of_a_nested_struct() -> None:
	"""The bug that produced this function. `name`'s extent is its labels'
	extent, so a parent holding one is only as measurable as it is."""
	body = ("struct inner { u8 k; variant v switch (k) { case 0: u8 a; "
	        "default: opaque; } }\n"
	        "struct outer { inner one; u16 after; }")

	assert not measurable(body, "outer")


def test_an_element_reached_only_through_a_run_is_still_asked() -> None:
	"""A run walks by adding its element's extent, so an element that cannot
	be measured makes the run unwalkable -- and the parent holding the run
	unmeasurable in turn."""
	body = ("struct e { u8 k; variant v switch (k) { case 0: u8 a; "
	        "default: opaque; } }\n"
	        "struct outer { e run[] while (k == 0) max 8; }")

	assert not measurable(body, "outer")


# -- what sizes a member, and who decides -----------------------------------

def kind_of(body: str, struct: str, member: str) -> Member:
	held = structs(body)[struct]
	one  = next(p for p in own_members(held) if p.name == member)
	return classify(held, one, set(structs(body)))


def test_a_constant_sized_array_is_an_array() -> None:
	"""`sized_by` is set for `u8 id[N]` where N is a constant, and the count
	is known all the same -- the frame was sized around it and the accessors
	are the fixed-array ones.

	Classified `VARIABLE` on `sized_by` alone, three backends looked for a
	field called `N`, found none, and dropped the member with a note saying
	they could not resolve it. C escaped by not using this function. The
	honest question is whether the *data* decides, and `array_count` answers
	it -- now that it is never a guess.
	"""
	body = "const N = 8;\nstruct s { u8 id[N]; u16 after; }"

	assert kind_of(body, "s", "id") is Member.ARRAY


def test_a_field_sized_array_is_variable() -> None:
	"""The other half, and the reason the two look alike: both set
	`sized_by`, and only one of them is a number the message chooses."""
	assert kind_of("struct s { u16 n; u8 body[n]; }", "s", "body") \
		is Member.VARIABLE


def test_arithmetic_over_a_field_is_variable_too() -> None:
	"""`size_expr` rather than `sized_by`, and still the data deciding."""
	assert kind_of("struct s { u8 len; u8 d[(len + 1) * 8 - 2]; }", "s", "d") \
		is Member.VARIABLE


# -- the value bounds a schema exports --------------------------------------


def bounds_of(body: str, struct: str, field: str) -> tuple[int | None, int | None]:
	schema   = parse_text(PREAMBLE + body)
	layout   = solve(schema)
	resolved = resolve(schema, layout)
	held     = resolved.structs[struct]
	for placement in own_members(held):
		if local_name(held, placement) == field:
			return declared_value_bounds(placement, layout.env)
	raise AssertionError(f"no field {field}")


def test_must_eq_exports_the_same_bounds_as_min_and_max() -> None:
	"""One constraint written two ways generates one surface.

	`[must_eq = 7]` is the point interval -- the solver reads it as
	`Interval.point` -- so it is a floor and a ceiling at once. It exported
	neither, while `[min = 7, max = 7]` exported both, so which attribute
	the author reached for changed what a caller could compile against.

	That matters for the reason the export exists at all: a caller
	validating the value it is about to write (a CLI flag, a config key)
	otherwise restates the number, and the restated one drifts.
	"""
	pair = "struct S { u32 a [min = 7, max = 7]; }"
	eq   = "struct S { u32 a [must_eq = 7]; }"
	assert bounds_of(eq, "S", "a") == (7, 7)
	assert bounds_of(eq, "S", "a") == bounds_of(pair, "S", "a")


def test_a_one_sided_bound_stays_one_sided() -> None:
	"""The control: folding `must_eq` into both must not fill in the other
	half of an ordinary `[max]`, which would export a floor nobody wrote."""
	assert bounds_of("struct S { u32 a [max = 9]; }", "S", "a") == (None, 9)
	assert bounds_of("struct S { u32 a [min = 3]; }", "S", "a") == (3, None)


def test_a_field_with_no_bound_exports_none() -> None:
	assert bounds_of("struct S { u32 a; }", "S", "a") == (None, None)


def test_pinned_runs_reads_a_byte_run_and_nothing_else() -> None:
	"""One rule for four backends and the packer (0052).

	A tuple rather than one run, because the same question is asked of a
	byte-run enum: what may this member hold. One alternative or six is the
	only difference between a pinned magic and an enum's membership.

	The narrowness is the decision rather than an implementation limit: a
	span of wider scalars has an endianness the literal does not, so `u16
	sig[2]` answers None even where the front end would have let it through.
	"""
	from situc.traverse import pinned_runs

	def run_of(body: str, field: str) -> tuple[bytes, ...] | None:
		schema   = parse_text(PREAMBLE + body)
		resolved = resolve(schema, solve(schema))
		held     = resolved.structs["S"]
		for placement in own_members(held):
			if local_name(held, placement) == field:
				return pinned_runs(placement)
		raise AssertionError(field)

	assert run_of('struct S { u8 sig[4] [must_eq = "WOZ2"]; }', "sig") \
		== (b"WOZ2",)
	assert run_of("struct S { u8 sig[4]; }", "sig") is None
	assert run_of("struct S { u32 a [must_eq = 7]; }", "a") is None
