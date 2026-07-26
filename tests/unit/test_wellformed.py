"""Whole-schema well-formedness checks.

Situ has no field numbers: position carries identity and names are the identity
(project.md section 4). Every duplicate here would be a silent correctness
problem rather than a style one.
"""

from __future__ import annotations

import pytest

from situc.diagnostics import SituError
from situc.parser import ATTRIBUTE_NAMES, parse_text


def rendered(source: str) -> str:
	with pytest.raises(SituError) as caught:
		parse_text(source, path="s.situ")
	return caught.value.diagnostic.render()


# -- duplicate declarations -------------------------------------------------


def test_duplicate_struct_name_rejected() -> None:
	assert "struct `S` is declared more than once" in rendered(
		"struct S { u8 a; } struct S { u8 b; }")


def test_duplicate_enum_name_rejected() -> None:
	assert "enum `E` is declared more than once" in rendered(
		"enum E : u8 { a = 1, } enum E : u8 { b = 2, }")


def test_duplicate_const_name_rejected() -> None:
	assert "const `N` is declared more than once" in rendered("const N = 1; const N = 2;")


def test_struct_and_const_share_one_namespace() -> None:
	"""`Foo x[Foo];` would be unreadable if they did not."""
	report = rendered("const X = 1; struct X { u8 a; }")
	assert "declared more than once" in report
	assert "types and constants share one namespace" in report


def test_struct_and_enum_collide() -> None:
	assert "declared more than once" in rendered(
		"struct X { u8 a; } enum X : u8 { a = 1, }")


def test_redeclaration_points_at_both_sites() -> None:
	report = rendered("struct S { u8 a; }\nstruct S { u8 b; }")
	assert "redeclared here" in report
	assert "first declared here" in report
	assert "s.situ:1:1" in report
	assert "s.situ:2:1" in report


# -- duplicate members ------------------------------------------------------


def test_duplicate_field_name_rejected() -> None:
	report = rendered("struct S { u8 a; u16 a; }")
	assert "field `a` is declared more than once" in report
	assert "the name is the identity" in report


def test_duplicate_field_across_a_positional_block_rejected() -> None:
	"""A positional block asserts staticness; it does not open a scope."""
	assert "field `a` is declared more than once" in rendered(
		"struct S { u8 a; positional { u8 a; } }")


def test_duplicate_field_inside_a_positional_block_rejected() -> None:
	assert "field `a` is declared more than once" in rendered(
		"struct S { positional { u8 a; u8 a; } }")


def test_same_field_name_in_different_structs_is_fine() -> None:
	schema = parse_text("struct A { u8 value; } struct B { u8 value; }")
	assert len(schema.structs()) == 2


def test_duplicate_enum_member_rejected() -> None:
	assert "enum member `a` is declared more than once" in rendered(
		"enum E : u8 { a = 1, a = 2, }")


def test_same_member_name_in_different_enums_is_fine() -> None:
	schema = parse_text("enum A : u8 { none = 0, } enum B : u8 { none = 0, }")
	assert len(schema.enums()) == 2


# -- duplicate attributes ---------------------------------------------------


def test_duplicate_attribute_rejected() -> None:
	assert "attribute `must_eq` is declared more than once" in rendered(
		"struct S { u8 a [must_eq = 1, must_eq = 2]; }")


def test_duplicate_struct_attribute_rejected() -> None:
	assert "attribute `allow_straddle` is declared more than once" in rendered(
		"struct S [allow_straddle, allow_straddle] { u8 a; }")


def test_duplicate_reserved_attribute_rejected() -> None:
	assert "attribute `preserve` is declared more than once" in rendered(
		"struct S { reserved u8 [preserve, preserve]; }")


def test_distinct_attributes_are_fine() -> None:
	schema = parse_text("struct S { u8 a [must_eq = 1, max = 4]; }")
	assert len(schema.structs()) == 1


# -- const shadowing an attribute name --------------------------------------


def test_const_named_after_an_attribute_rejected() -> None:
	"""Closes the hole left by decision 0006.

	`const max = 4;` would make `u8 buf[max];` parse as a flag rather than an
	array size, silently, with no way to spell the other reading.
	"""
	report = rendered("const max = 4;")
	assert "collides with an attribute name" in report
	assert "would read as an attribute, not as an array size" in report


@pytest.mark.parametrize("name", ["max", "min", "size", "secret", "preserve", "rw"])
def test_attribute_names_are_all_refused_as_constants(name: str) -> None:
	assert "collides with an attribute name" in rendered(f"const {name} = 1;")


def test_ordinary_const_names_are_fine() -> None:
	schema = parse_text("const MAX_PAYLOAD = 1500; struct S { u8 buf[MAX_PAYLOAD]; }")
	assert len(schema.consts()) == 1


def test_the_check_covers_the_whole_vocabulary() -> None:
	"""If a name is added to ATTRIBUTE_NAMES it must also become an illegal
	constant name, or decision 0006's ambiguity reopens for it."""
	for name in ATTRIBUTE_NAMES:
		assert "collides with an attribute name" in rendered(f"const {name} = 1;")


# -- type resolution --------------------------------------------------------


def test_unknown_type_rejected() -> None:
	report = rendered("struct S { Nope x; }")
	assert "unknown type `Nope`" in report
	assert "expected a scalar type or a struct or enum declared in this file" in report


def test_unknown_type_suggests_a_near_match() -> None:
	report = rendered("struct Header { u8 a; } struct S { Heade x; }")
	assert "a type named `Header` is declared; did you mean that?" in report


def test_unknown_type_suggests_a_case_difference() -> None:
	report = rendered("struct Header { u8 a; } struct S { header x; }")
	assert "did you mean that?" in report


def test_unknown_type_offers_no_wild_suggestion() -> None:
	"""A confident wrong suggestion is worse than none."""
	report = rendered("struct Header { u8 a; } struct S { Payload x; }")
	assert "did you mean" not in report


def test_unknown_type_in_a_positional_block_rejected() -> None:
	assert "unknown type `Nope`" in rendered("struct S { positional { Nope x; } }")


def test_forward_reference_to_a_later_struct_is_fine() -> None:
	"""Declaration order is not use order; the layout solver orders the work."""
	schema = parse_text("struct Outer { Inner x; } struct Inner { u8 a; }")
	assert len(schema.structs()) == 2


def test_enum_used_as_a_field_type_resolves() -> None:
	schema = parse_text("enum E : u8 { a = 1, } struct S { E kind; }")
	assert len(schema.structs()) == 1


def test_type_resolution_is_skipped_when_a_file_imports() -> None:
	"""The missing name may legitimately live in the imported file, and import
	resolution does not exist yet."""
	schema = parse_text('import "other.situ"; struct S { Elsewhere x; }')
	assert len(schema.structs()) == 1


# -- recursion --------------------------------------------------------------


def test_direct_recursion_rejected() -> None:
	assert "contains itself" in rendered("struct Node { u8 tag; Node next; }")


def test_mutual_recursion_rejected() -> None:
	assert "cycle: A -> B -> A" in rendered("struct A { B b; } struct B { A a; }")


def test_recursion_survives_the_other_checks() -> None:
	"""Recursion runs last, so a schema with both faults reports the other one
	first; this one has only the cycle."""
	report = rendered("struct A { B b; } struct B { C c; } struct C { A a; }")
	assert "recursive" in report
	assert "non-terminating" in report
