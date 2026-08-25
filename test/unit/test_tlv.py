"""The item grammar of a `tlv` region (project.md section 9.5).

A tlv region says how to find its items: how a raw tag decodes into named
parts, and how those parts decide where each value ends. That is a grammar, and
for a long time the three arguments carrying it were kept as verbatim source
text "interpreted by the pass that needs them" -- a pass nobody wrote. The only
thing ever read out of a `value_size` dispatch was its `case` labels, scanned
for by a function that skipped the bodies.

So a `switch (wire)` naming no part of the tag parsed and described nothing,
and the walk that reads a protobuf message was hand-written in the C runtime
with `tag >> 3` and four wire types baked into it. These tests cover the first
half of closing that: the grammar is structure, and a region whose grammar no
walk could follow is refused with a blame chain.
"""

from __future__ import annotations

import pytest

from situc import ast
from situc.diagnostics import SituError
from situc.parser import parse_text

PREAMBLE = """target buffer;
endian little;
varint_type pb_varint { encoding = leb128; max_bits = 64; }
"""

GENERAL = PREAMBLE + """struct m {
	tlv fields (
		tag_type   = pb_varint,
		tag_decode = { field = tag >> 3, wire = tag & 0x7 },
		tag_identity = field,
		value_size = switch (wire) {
			case 0: self_delimiting,
			case 1: 8,
			case 2: prefixed(pb_varint),
			case 5: 4,
			default: error,
		},
		known = {
			1 : { name = user_id,  wire = 0, type = pb_varint },
			2 : { name = username, wire = 2, type = u8 },
		},
		unknown = preserve
	);
}
"""


def region(source: str) -> ast.Tlv:
	schema = parse_text(source, path="s.situ")
	member = schema.structs()[0].members[0]
	assert isinstance(member, ast.Tlv)
	return member


def rendered(source: str) -> str:
	with pytest.raises(SituError) as caught:
		parse_text(source, path="s.situ")
	return caught.value.diagnostic.render()


def one_region(body: str) -> str:
	return PREAMBLE + "struct m {\n\ttlv fields (\n" + body + "\n\t);\n}\n"


# -- the grammar is structure -----------------------------------------------


def test_the_tag_decodes_into_named_parts() -> None:
	parts = region(GENERAL).tag_decode

	assert [part.name for part in parts] == ["field", "wire"]
	assert all(isinstance(part.value, ast.Binary) for part in parts)


def test_the_dispatch_selects_on_a_decoded_part() -> None:
	sizes = region(GENERAL).value_size

	assert sizes is not None
	assert sizes.selector == "wire"


def test_each_arm_says_how_the_value_is_sized() -> None:
	sizes = region(GENERAL).value_size
	assert sizes is not None
	rules = {case.label: case.rule for case in sizes.cases}

	assert isinstance(rules[0], ast.SelfDelimiting)
	assert isinstance(rules[1], ast.FixedValue) and rules[1].size == 8
	assert isinstance(rules[2], ast.PrefixedValue)
	assert rules[2].length_type == "pb_varint"
	assert isinstance(rules[None], ast.RejectValue)


def test_the_wire_types_come_from_the_dispatch() -> None:
	"""They used to be scanned for separately, by a pass that skipped the
	bodies it was walking past. One derivation now, and `default` is not one of
	them -- it is what happens to a wire type the region does not accept."""
	assert region(GENERAL).wire_types == (0, 1, 2, 5)


def test_a_wire_type_resolves_through_the_default() -> None:
	fields = region(GENERAL)

	assert isinstance(fields.rule_for(2), ast.PrefixedValue)
	assert isinstance(fields.rule_for(3), ast.RejectValue)


def test_the_known_map_names_tags() -> None:
	known = region(GENERAL).known

	assert [(tag.tag, tag.name) for tag in known] == [(1, "user_id"), (2, "username")]
	assert known[0].wire == 0
	assert known[0].type_name == "pb_varint"


def test_the_simple_form_gives_a_name_and_nothing_else() -> None:
	"""Section 9.5's first example: `0x01 : Mtu`, sized by `length_type`."""
	known = region(one_region(
		"\t\ttag_type = u8, length_type = u8,\n"
		"\t\tknown = { 0x01 : Mtu, 0x02 : Window },\n"
		"\t\tunknown = error")).known

	assert [(tag.tag, tag.name) for tag in known] == [(1, "Mtu"), (2, "Window")]
	assert known[0].wire is None


def test_a_repeated_value_type_is_recorded() -> None:
	known = region(one_region(
		"\t\ttag_type = pb_varint,\n"
		"\t\ttag_decode = { wire = tag & 0x7 },\n"
		"\t\tvalue_size = switch (wire) { case 2: prefixed(pb_varint) },\n"
		"\t\tknown = { 2 : { name = names, wire = 2, type = u8[] } },\n"
		"\t\tunknown = error")).known

	assert known[0].type_name == "u8"
	assert known[0].repeated


# -- a grammar no walk could follow is refused ------------------------------


def test_a_dispatch_on_an_undecoded_part_is_refused() -> None:
	report = rendered(one_region(
		"\t\ttag_type = pb_varint,\n"
		"\t\ttag_decode = { field = tag >> 3 },\n"
		"\t\tvalue_size = switch (wire) { case 0: self_delimiting },\n"
		"\t\tunknown = error"))

	assert "`wire` is not a part of the decoded tag" in report
	assert "`tag_decode` produces `field`" in report


def test_a_dispatch_with_no_tag_decode_at_all_says_so() -> None:
	report = rendered(one_region(
		"\t\ttag_type = pb_varint,\n"
		"\t\tvalue_size = switch (wire) { case 0: self_delimiting },\n"
		"\t\tunknown = error"))

	assert "this region declares no `tag_decode`" in report


def test_a_tag_part_reads_the_raw_tag_and_nothing_else() -> None:
	"""Nothing else has been read at that point. A part naming a field of the
	enclosing struct reads as though the tag could depend on it."""
	report = rendered(one_region(
		"\t\ttag_type = pb_varint,\n"
		"\t\ttag_decode = { wire = header & 0x7 },\n"
		"\t\tvalue_size = switch (wire) { case 0: self_delimiting },\n"
		"\t\tunknown = error"))

	assert "`header` is not in scope in a tag decode" in report


def test_a_known_tag_the_dispatch_rejects_is_refused() -> None:
	"""Naming a tag whose wire type has no size gives the schema an accessor
	that could never read anything."""
	report = rendered(one_region(
		"\t\ttag_type = pb_varint,\n"
		"\t\ttag_decode = { wire = tag & 0x7 },\n"
		"\t\tvalue_size = switch (wire) { case 0: self_delimiting, default: error },\n"
		"\t\tknown = { 1 : { name = a, wire = 2, type = u8 } },\n"
		"\t\tunknown = error"))

	assert "`a` declares a wire type the dispatch rejects" in report
	assert "`value_size` sizes wire types 0" in report
	assert "could be named and never read" in report


def test_two_arms_for_one_wire_type_are_refused() -> None:
	report = rendered(one_region(
		"\t\ttag_type = pb_varint,\n"
		"\t\ttag_decode = { wire = tag & 0x7 },\n"
		"\t\tvalue_size = switch (wire) { case 1: 4, case 1: 8 },\n"
		"\t\tunknown = error"))

	assert "wire type 1 is dispatched twice" in report
	assert "two answers for where the value ends" in report


def test_two_defaults_are_refused() -> None:
	report = rendered(one_region(
		"\t\ttag_type = pb_varint,\n"
		"\t\ttag_decode = { wire = tag & 0x7 },\n"
		"\t\tvalue_size = switch (wire) { default: error, default: error },\n"
		"\t\tunknown = error"))

	assert "at most one `default`" in report


def test_a_zero_length_value_is_refused() -> None:
	"""A walk over zero-extent items does not advance."""
	report = rendered(one_region(
		"\t\ttag_type = pb_varint,\n"
		"\t\ttag_decode = { wire = tag & 0x7 },\n"
		"\t\tvalue_size = switch (wire) { case 1: 0 },\n"
		"\t\tunknown = error"))

	assert "sizes its value at zero bytes" in report
	assert "does not advance" in report


def test_an_unknown_length_type_is_refused() -> None:
	report = rendered(one_region(
		"\t\ttag_type = pb_varint,\n"
		"\t\ttag_decode = { wire = tag & 0x7 },\n"
		"\t\tvalue_size = switch (wire) { case 2: prefixed(nonesuch) },\n"
		"\t\tunknown = error"))

	assert "unknown length type `nonesuch`" in report


def test_a_length_type_may_be_a_scalar() -> None:
	"""The simple form's `length_type = u8` in the general form's clothing."""
	sizes = region(one_region(
		"\t\ttag_type = u8,\n"
		"\t\ttag_decode = { wire = tag & 0x7 },\n"
		"\t\tvalue_size = switch (wire) { case 2: prefixed(u16) },\n"
		"\t\tunknown = error")).value_size

	assert sizes is not None
	rule = sizes.cases[0].rule
	assert isinstance(rule, ast.PrefixedValue) and rule.length_type == "u16"


def test_a_duplicate_tag_number_is_refused() -> None:
	report = rendered(one_region(
		"\t\ttag_type = u8, length_type = u8,\n"
		"\t\tknown = { 1 : a, 1 : b },\n"
		"\t\tunknown = error"))

	assert "tag `1` is declared more than once" in report


def test_two_tags_may_not_share_a_name() -> None:
	report = rendered(one_region(
		"\t\ttag_type = u8, length_type = u8,\n"
		"\t\tknown = { 1 : a, 2 : a },\n"
		"\t\tunknown = error"))

	assert "known tag `a` is declared more than once" in report
	assert "what the generated accessor is called" in report


def test_a_duplicate_tag_part_is_refused() -> None:
	report = rendered(one_region(
		"\t\ttag_type = pb_varint,\n"
		"\t\ttag_decode = { wire = tag & 0x7, wire = tag >> 3 },\n"
		"\t\tvalue_size = switch (wire) { case 0: self_delimiting },\n"
		"\t\tunknown = error"))

	assert "tag part `wire` is declared more than once" in report


def test_a_known_tag_needs_a_name() -> None:
	report = rendered(one_region(
		"\t\ttag_type = u8, length_type = u8,\n"
		"\t\tknown = { 1 : { wire = 0, type = u8 } },\n"
		"\t\tunknown = error"))

	assert "a known tag needs a `name`" in report


def test_an_unknown_known_tag_attribute_is_refused() -> None:
	report = rendered(one_region(
		"\t\ttag_type = u8, length_type = u8,\n"
		"\t\tknown = { 1 : { name = a, packed = 1 } },\n"
		"\t\tunknown = error"))

	assert "unknown attribute `packed` on a known tag" in report


# -- which part names an item (decision 0023) -------------------------------


def test_the_identity_part_is_declared() -> None:
	assert region(GENERAL).identity_part() == "field"


def test_one_part_needs_no_declaration() -> None:
	"""Nothing to be ambiguous about, so nothing to say."""
	fields = region(one_region(
		"\t\ttag_type = pb_varint,\n"
		"\t\ttag_decode = { field = tag >> 3 },\n"
		"\t\tvalue_size = switch (field) { case 0: self_delimiting },\n"
		"\t\tknown = { 1 : { name = a, wire = 0 } },\n"
		"\t\tunknown = error"))

	assert fields.identity is None
	assert fields.identity_part() == "field"


def test_the_simple_form_keys_on_the_raw_tag() -> None:
	fields = region(one_region(
		"\t\ttag_type = u8, length_type = u8,\n"
		"\t\tknown = { 1 : Mtu },\n"
		"\t\tunknown = error"))

	assert fields.identity_part() is None


def test_two_parts_and_a_known_map_must_say_which() -> None:
	"""The wrong choice finds an item and not the one asked for, which is why
	this is an error rather than a default."""
	report = rendered(one_region(
		"\t\ttag_type = pb_varint,\n"
		"\t\ttag_decode = { field = tag >> 3, wire = tag & 0x7 },\n"
		"\t\tvalue_size = switch (wire) { case 0: self_delimiting },\n"
		"\t\tknown = { 1 : { name = a, wire = 0 } },\n"
		"\t\tunknown = error"))

	assert "does not say which part of the tag names an item" in report
	assert "`field`, `wire`" in report
	assert "add `tag_identity = <part>`" in report
	assert "still finds an item" in report


def test_two_parts_without_a_known_map_need_not_say() -> None:
	"""Nothing is keyed by identity, so requiring an answer would be asking for
	one nothing uses."""
	fields = region(one_region(
		"\t\ttag_type = pb_varint,\n"
		"\t\ttag_decode = { field = tag >> 3, wire = tag & 0x7 },\n"
		"\t\tvalue_size = switch (wire) { case 0: self_delimiting },\n"
		"\t\tunknown = error"))

	assert fields.identity_part() is None


def test_an_identity_naming_no_part_is_refused() -> None:
	report = rendered(one_region(
		"\t\ttag_type = pb_varint,\n"
		"\t\ttag_decode = { field = tag >> 3, wire = tag & 0x7 },\n"
		"\t\ttag_identity = nonesuch,\n"
		"\t\tvalue_size = switch (wire) { case 0: self_delimiting },\n"
		"\t\tunknown = error"))

	assert "`tag_identity` names `nonesuch`, which the tag does not decode" in report
	assert "`field`, `wire`" in report


def test_a_value_rule_that_is_not_one_is_refused() -> None:
	report = rendered(one_region(
		"\t\ttag_type = pb_varint,\n"
		"\t\ttag_decode = { wire = tag & 0x7 },\n"
		"\t\tvalue_size = switch (wire) { case 0: whatever },\n"
		"\t\tunknown = error"))

	assert "expected a value size" in report
	assert "`prefixed(<length type>)`" in report
