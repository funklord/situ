"""Parser tests for the phase 1 static subset (project.md section 26.1)."""

from __future__ import annotations

import pytest

from situc import ast
from situc.diagnostics import Source, SituError
from situc.parser import parse_decls, parse_text


def only_struct(source: str) -> ast.StructDecl:
	structs = parse_text(source).structs()
	assert len(structs) == 1
	return structs[0]


def first_field(source: str) -> ast.Field:
	member = only_struct(source).members[0]
	assert isinstance(member, ast.Field)
	return member


# -- directives -------------------------------------------------------------


@pytest.mark.parametrize(("source", "kind"), [
	("target buffer;", ast.TargetKind.BUFFER),
	("target mmio;",   ast.TargetKind.MMIO),
])
def test_target_directive(source: str, kind: ast.TargetKind) -> None:
	decl = parse_text(source).decls[0]
	assert isinstance(decl, ast.TargetDirective)
	assert decl.kind is kind


@pytest.mark.parametrize(("source", "endian"), [
	("endian big;",    ast.Endian.BIG),
	("endian little;", ast.Endian.LITTLE),
	("endian native;", ast.Endian.NATIVE),
])
def test_endian_directive(source: str, endian: ast.Endian) -> None:
	decl = parse_text(source).decls[0]
	assert isinstance(decl, ast.EndianDirective)
	assert decl.endian is endian


@pytest.mark.parametrize(("source", "order"), [
	("bit_order msb_first;", ast.BitOrder.MSB_FIRST),
	("bit_order lsb_first;", ast.BitOrder.LSB_FIRST),
])
def test_bit_order_directive(source: str, order: ast.BitOrder) -> None:
	decl = parse_text(source).decls[0]
	assert isinstance(decl, ast.BitOrderDirective)
	assert decl.bit_order is order


def test_import_directive() -> None:
	"""The directive parses. `parse_decls` rather than `parse_text`, because
	`parse` expands imports now (17.0a) and a schema parsed from a string has
	no directory to resolve one against -- which is a refusal of its own,
	tested in `test_wellformed`."""
	decl = parse_decls(Source("s.situ", 'import "std/codecs.situ";')).decls[0]
	assert isinstance(decl, ast.ImportDirective)
	assert decl.path == "std/codecs.situ"


def test_unknown_target_rejected() -> None:
	with pytest.raises(SituError, match="unknown target"):
		parse_text("target gpu;")


def test_unknown_endian_rejected() -> None:
	with pytest.raises(SituError, match="unknown endianness"):
		parse_text("endian middle;")


def test_unknown_bit_order_rejected() -> None:
	with pytest.raises(SituError, match="unknown bit order"):
		parse_text("bit_order sideways;")


def test_import_requires_a_string() -> None:
	with pytest.raises(SituError, match="expected a quoted path"):
		parse_text("import std;")


def test_directive_requires_semicolon() -> None:
	with pytest.raises(SituError, match=r"expected `;`"):
		parse_text("endian big")


# -- const ------------------------------------------------------------------


def test_const_decl() -> None:
	decl = parse_text("const MAX_PAYLOAD = 1500;").decls[0]
	assert isinstance(decl, ast.ConstDecl)
	assert decl.name == "MAX_PAYLOAD"
	assert isinstance(decl.value, ast.IntLiteral)
	assert decl.value.value == 1500


def test_const_expression() -> None:
	decl = parse_text("const N = 4 * 8 + 1;").decls[0]
	assert isinstance(decl, ast.ConstDecl)
	assert isinstance(decl.value, ast.Binary)
	assert decl.value.op == "+"


# -- enum -------------------------------------------------------------------


def test_enum_decl() -> None:
	decl = parse_text("enum MsgType : u8 { hello = 0x01, data = 0x02, }").enums()[0]
	assert decl.name == "MsgType"
	assert decl.backing.name == "u8"
	assert [member.name for member in decl.members] == ["hello", "data"]


def test_enum_without_trailing_comma() -> None:
	decl = parse_text("enum E : u8 { a = 1, b = 2 }").enums()[0]
	assert [member.name for member in decl.members] == ["a", "b"]


def test_enum_default_pass() -> None:
	decl = parse_text("enum E : u8 { a = 1, default = pass, }").enums()[0]
	assert decl.default is ast.EnumDefault.PASS
	assert decl.effective_default is ast.EnumDefault.PASS


def test_enum_default_is_error_when_unspecified() -> None:
	"""Section 8.7: the safe option is the silent one."""
	decl = parse_text("enum E : u8 { a = 1, }").enums()[0]
	assert decl.default is None
	assert decl.effective_default is ast.EnumDefault.ERROR


def test_enum_backing_type_must_be_scalar() -> None:
	with pytest.raises(SituError, match="backing type must be a scalar"):
		parse_text("struct S { u8 a; } enum E : S { a = 1, }")


def test_enum_backing_type_is_mandatory() -> None:
	with pytest.raises(SituError, match=r"expected `:`"):
		parse_text("enum E { a = 1, }")


def test_unknown_enum_default_rejected() -> None:
	with pytest.raises(SituError, match="unknown enum default"):
		parse_text("enum E : u8 { a = 1, default = maybe, }")


# -- struct and fields ------------------------------------------------------


def test_struct_with_scalar_fields() -> None:
	decl = only_struct("struct Record { u32 id; u16 kind; u16 value; }")
	assert decl.name == "Record"
	assert [member.name for member in decl.members if isinstance(member, ast.Field)] == [
		"id", "kind", "value",
	]


def test_empty_struct() -> None:
	assert only_struct("struct Empty { }").members == ()


def test_named_type_field() -> None:
	field = first_field("enum MsgType : u8 { a = 1, } struct S { MsgType type; }")
	assert field.type_ref.name == "MsgType"
	assert not field.type_ref.is_scalar


def test_scalar_field_resolves_its_type() -> None:
	field = first_field("struct S { u32 seq; }")
	assert field.type_ref.is_scalar
	assert field.type_ref.scalar is not None
	assert field.type_ref.scalar.bits == 32


def test_bit_field() -> None:
	field = first_field("struct S { bit urgent; }")
	assert field.type_ref.scalar is not None
	assert field.type_ref.scalar.bits == 1


def test_fixed_array() -> None:
	field = first_field("struct S { u8 nonce[12]; }")
	assert field.array is not None
	assert isinstance(field.array.size, ast.IntLiteral)
	assert field.array.size.value == 12


def test_array_sized_by_constant() -> None:
	field = first_field("const N = 4; struct S { u8 buf[N]; }")
	assert field.array is not None
	assert isinstance(field.array.size, ast.NameRef)
	assert field.array.size.name == "N"


def test_offset_pin() -> None:
	field = first_field("struct S { u32 seq @ 0x06; }")
	assert isinstance(field.pin, ast.IntLiteral)
	assert field.pin.value == 6


def test_pin_after_array() -> None:
	field = first_field("struct S { u8 buf[4] @ 0x10; }")
	assert field.array is not None
	assert isinstance(field.pin, ast.IntLiteral)


def test_invalid_scalar_width_rejected() -> None:
	with pytest.raises(SituError, match="out of range"):
		parse_text("struct S { u65 wide; }")


def test_field_requires_semicolon() -> None:
	with pytest.raises(SituError, match=r"expected `;`"):
		parse_text("struct S { u8 a }")


def test_unclosed_struct_rejected() -> None:
	with pytest.raises(SituError, match="unexpected end of file"):
		parse_text("struct S { u8 a;")


# -- reserved ---------------------------------------------------------------


def test_reserved_declaration() -> None:
	member = only_struct("struct S { reserved u3 [must_be_zero]; }").members[0]
	assert isinstance(member, ast.Reserved)
	assert member.type_ref.name == "u3"
	assert [attr.name for attr in member.attrs] == ["must_be_zero"]


def test_reserved_defaults_to_no_attributes() -> None:
	member = only_struct("struct S { reserved u8; }").members[0]
	assert isinstance(member, ast.Reserved)
	assert member.attrs == ()


def test_reserved_needs_a_scalar_type() -> None:
	with pytest.raises(SituError, match="needs a scalar type"):
		parse_text("struct Inner { u8 a; } struct S { reserved Inner; }")


# -- attributes and the bracket ambiguity -----------------------------------


def test_attribute_with_value() -> None:
	field = first_field("struct S { u8 version [must_eq = 1]; }")
	assert field.array is None
	assert [attr.name for attr in field.attrs] == ["must_eq"]
	assert isinstance(field.attrs[0].value, ast.IntLiteral)


#: A register, because `[rw]` and `[wo]` are access modes and mean nothing
#: outside one (14.5). These two tests are about decision 0006's bracket
#: disambiguation rather than about registers, but the fixture still has to be
#: a schema somebody could write -- the attribute-placement rule refuses a
#: bare access mode on a buffer field, which is how these were found.
REGISTER = ("target mmio;\nendian big;\nbit_order msb_first;\n\n"
            "register S @ 0x00 {\n"
            "\twidth = 32;\n"
            "\taccess_width = 32;\n"
            "\t%s\n"
            "}\n")


def test_attribute_list() -> None:
	field = first_field(REGISTER % "bit start [wo, on_write = trigger];")
	assert field.array is None
	assert [attr.name for attr in field.attrs] == ["wo", "on_write"]


def test_bare_attribute_flag_is_not_an_array() -> None:
	"""Decision 0006: a lone known attribute name means attributes."""
	field = first_field(REGISTER % "bit enable [rw];")
	assert field.array is None
	assert [attr.name for attr in field.attrs] == ["rw"]


def test_bare_unknown_identifier_is_an_array_size() -> None:
	"""Decision 0006: anything not in the attribute vocabulary is a size."""
	field = first_field("struct S { u8 buf[MAX_PAYLOAD]; }")
	assert field.array is not None
	assert field.attrs == ()


def test_array_and_attributes_together() -> None:
	field = first_field("struct S { u8 buf[4] [must_eq = 0]; }")
	assert field.array is not None
	assert [attr.name for attr in field.attrs] == ["must_eq"]


def test_struct_level_attributes() -> None:
	decl = only_struct("struct S [allow_straddle] { u12 wide; }")
	assert [attr.name for attr in decl.attrs] == ["allow_straddle"]


def test_a_called_attribute_name_is_a_size_expression() -> None:
	"""Decision 0006, rule 3. `min` and `max` are attribute names *and*
	expression builtins, and in `x[min(a, b)]` the comma sits at bracket depth
	2 -- so the lone `min` matched the attribute rule and the parser reported
	"expected `]`" at the open parenthesis of a perfectly good size."""
	member = only_struct("struct S { u8 n; u8 buf[min(n, 4)]; }").members[1]
	assert isinstance(member, ast.Field)
	assert member.array is not None
	assert member.attrs == ()


def test_a_bare_attribute_name_is_still_an_attribute() -> None:
	field = first_field("struct S { u8 n [min = 4]; }")
	assert field.array is None
	assert [attr.name for attr in field.attrs] == ["min"]


def test_dotted_array_size_is_not_attributes() -> None:
	field = first_field("struct S { u8 opts[hdr.length]; }")
	assert field.array is not None
	assert isinstance(field.array.size, ast.Access)


# -- requirements -----------------------------------------------------------


def test_require_and_assert() -> None:
	schema = parse_text("require size(H) == 10; assert in_place(H.seq);")
	kinds  = [decl.kind for decl in schema.requirements()]
	assert kinds == [ast.RequirementKind.REQUIRE, ast.RequirementKind.ASSERT]


def test_requirement_predicate_is_a_call() -> None:
	requirement = parse_text("require absolute_static(Header);").requirements()[0]
	assert isinstance(requirement.expr, ast.Call)
	assert requirement.expr.name == "absolute_static"


def test_requirement_over_an_element_path() -> None:
	requirement = parse_text("require in_place(Message.recs[].value);").requirements()[0]
	assert isinstance(requirement.expr, ast.Call)
	arg = requirement.expr.args[0]
	assert isinstance(arg, ast.Access)
	assert arg.name == "value"
	assert isinstance(arg.base, ast.Index)
	assert arg.base.index is None


# -- expressions ------------------------------------------------------------


@pytest.mark.parametrize(("source", "op"), [
	("require a + b;",  "+"),
	("require a - b;",  "-"),
	("require a * b;",  "*"),
	("require a / b;",  "/"),
	("require a % b;",  "%"),
	("require a & b;",  "&"),
	("require a | b;",  "|"),
	("require a ^ b;",  "^"),
	("require a << b;", "<<"),
	("require a >> b;", ">>"),
	("require a == b;", "=="),
	("require a != b;", "!="),
	("require a <= b;", "<="),
	("require a >= b;", ">="),
])
def test_binary_operators(source: str, op: str) -> None:
	expr = parse_text(source).requirements()[0].expr
	assert isinstance(expr, ast.Binary)
	assert expr.op == op


def test_multiplication_binds_tighter_than_addition() -> None:
	expr = parse_text("require a + b * c;").requirements()[0].expr
	assert isinstance(expr, ast.Binary)
	assert expr.op == "+"
	assert isinstance(expr.right, ast.Binary)
	assert expr.right.op == "*"


def test_parentheses_override_precedence() -> None:
	expr = parse_text("require (a + b) * c;").requirements()[0].expr
	assert isinstance(expr, ast.Binary)
	assert expr.op == "*"
	assert isinstance(expr.left, ast.Binary)
	assert expr.left.op == "+"


def test_comparison_binds_looser_than_arithmetic() -> None:
	expr = parse_text("require size(H) + 2 == 10;").requirements()[0].expr
	assert isinstance(expr, ast.Binary)
	assert expr.op == "=="


def test_subtraction_is_left_associative() -> None:
	expr = parse_text("require a - b - c;").requirements()[0].expr
	assert isinstance(expr, ast.Binary)
	assert isinstance(expr.left, ast.Binary)
	assert expr.left.op == "-"


@pytest.mark.parametrize("op", ["-", "~", "!"])
def test_unary_operators(op: str) -> None:
	expr = parse_text(f"require {op}a;").requirements()[0].expr
	assert isinstance(expr, ast.Unary)
	assert expr.op == op


def test_call_with_multiple_arguments() -> None:
	expr = parse_text("require aligned(H.seq, 4);").requirements()[0].expr
	assert isinstance(expr, ast.Call)
	assert len(expr.args) == 2


def test_nested_call() -> None:
	expr = parse_text("require align_up(size(H), 4) == 12;").requirements()[0].expr
	assert isinstance(expr, ast.Binary)
	assert isinstance(expr.left, ast.Call)
	assert isinstance(expr.left.args[0], ast.Call)


def test_missing_expression_rejected() -> None:
	with pytest.raises(SituError, match="expected an expression"):
		parse_text("const N = ;")


def test_unclosed_parenthesis_rejected() -> None:
	with pytest.raises(SituError, match=r"expected `\)`"):
		parse_text("const N = (1 + 2;")


# -- constructs belonging to later phases -----------------------------------


def test_remaining_array_size_parses() -> None:
	field = first_field("struct S { u8 trailer[remaining]; }")
	assert field.array is not None
	assert isinstance(field.array.size, ast.Remaining)


def test_positional_block_is_accepted() -> None:
	member = only_struct("struct S { positional { u16 a; u16 b; } }").members[0]
	assert isinstance(member, ast.PositionalBlock)
	assert len(member.members) == 2


def test_unknown_declaration_rejected() -> None:
	with pytest.raises(SituError, match="unknown declaration"):
		parse_text("gadget Foo { }")


def test_declaration_must_start_with_a_keyword() -> None:
	with pytest.raises(SituError, match="expected a declaration"):
		parse_text("; struct S { }")


# -- recursive types --------------------------------------------------------


def test_direct_recursion_rejected() -> None:
	with pytest.raises(SituError, match="contains itself") as caught:
		parse_text("struct Node { u8 tag; Node next; }")
	assert "non-terminating" in caught.value.diagnostic.render()


def test_mutual_recursion_rejected() -> None:
	with pytest.raises(SituError, match="recursive") as caught:
		parse_text("struct A { B b; } struct B { A a; }")
	assert "cycle:" in caught.value.diagnostic.render()


def test_three_way_recursion_rejected() -> None:
	with pytest.raises(SituError, match="recursive"):
		parse_text("struct A { B b; } struct B { C c; } struct C { A a; }")


def test_recursion_through_a_positional_block_rejected() -> None:
	with pytest.raises(SituError, match="contains itself"):
		parse_text("struct Node { positional { Node next; } }")


def test_recursion_through_an_array_rejected() -> None:
	with pytest.raises(SituError, match="contains itself"):
		parse_text("struct Node { Node children[4]; }")


def test_repeated_use_of_a_type_is_not_recursion() -> None:
	schema = parse_text("struct Leaf { u8 a; } struct Tree { Leaf x; Leaf y; }")
	assert len(schema.structs()) == 2


def test_diamond_reference_is_not_recursion() -> None:
	schema = parse_text(
		"struct Leaf { u8 a; }"
		"struct Mid1 { Leaf l; }"
		"struct Mid2 { Leaf l; }"
		"struct Top { Mid1 a; Mid2 b; }"
	)
	assert len(schema.structs()) == 4


# -- endian markers (section 8.3) -------------------------------------------


def test_endian_marker_declaration() -> None:
	decl = parse_text(
		"endian_marker bo : u16 { little = 0x4949, big = 0x4D4D, }").markers()[0]
	assert decl.name == "bo"
	assert decl.backing.name == "u16"
	assert isinstance(decl.little, ast.IntLiteral)
	assert decl.little.value == 0x4949


def test_marker_needs_both_orders() -> None:
	with pytest.raises(SituError, match="does not declare big"):
		parse_text("endian_marker bo : u16 { little = 0x4949, }")


def test_marker_rejects_an_unknown_member() -> None:
	with pytest.raises(SituError, match="unknown marker member `middle`"):
		parse_text("endian_marker bo : u16 { middle = 1, }")


def test_marker_rejects_a_duplicate_order() -> None:
	with pytest.raises(SituError, match="`little` is declared twice"):
		parse_text("endian_marker bo : u16 { little = 1, little = 2, }")


def test_marker_backing_must_be_byte_sized() -> None:
	"""The marker is read before its own byte order is known, so it has to be a
	plain byte sequence."""
	with pytest.raises(SituError, match="not a whole number of bytes"):
		parse_text("endian_marker bo : u12 { little = 1, big = 2, }")


def test_marker_field_in_a_struct() -> None:
	schema = parse_text(
		"endian_marker bo : u16 { little = 0x4949, big = 0x4D4D, }"
		"struct S [endian = from(bo)] { endian_marker bo; u16 a; }")
	member = schema.structs()[0].members[0]
	assert isinstance(member, ast.MarkerField)
	assert member.name == "bo"


def test_endian_from_attribute_parses_as_a_call() -> None:
	decl = parse_text(
		"endian_marker bo : u16 { little = 1, big = 2, }"
		"struct S [endian = from(bo)] { endian_marker bo; }").structs()[0]
	assert decl.attrs[0].name == "endian"
	assert isinstance(decl.attrs[0].value, ast.Call)


# -- varint types (section 8.1.1) -------------------------------------------


def test_varint_declaration() -> None:
	decl = parse_text(
		"varint_type leb128 { encoding = leb128; max_bits = 64; minimal; }").varints()[0]
	assert decl.name == "leb128"
	assert decl.encoding is ast.VarintEncoding.LEB128
	assert decl.max_bits == 64
	assert decl.minimal
	assert decl.transform is None


def test_varint_worst_case_length() -> None:
	"""Seven payload bits per byte, so 64 bits needs ten."""
	decl = parse_text(
		"varint_type v { encoding = leb128; max_bits = 64; }").varints()[0]
	assert decl.max_bytes == 10

	decl = parse_text(
		"varint_type v { encoding = leb128; max_bits = 32; }").varints()[0]
	assert decl.max_bytes == 5


def test_varint_zigzag_transform() -> None:
	decl = parse_text("varint_type zz { encoding = leb128; transform = zigzag; "
	                  "max_bits = 64; minimal; }").varints()[0]
	assert decl.transform is ast.VarintTransform.ZIGZAG


def test_varint_minimal_is_never_defaulted() -> None:
	"""Section 17.0 lists non-minimal acceptance as an ambiguity to resolve
	explicitly: it decides whether the format can be canonical."""
	decl = parse_text("varint_type v { encoding = leb128; max_bits = 64; }").varints()[0]
	assert not decl.minimal


def test_varint_needs_an_encoding() -> None:
	with pytest.raises(SituError, match="does not declare an encoding"):
		parse_text("varint_type v { max_bits = 64; }")


def test_varint_needs_max_bits() -> None:
	with pytest.raises(SituError, match="does not declare `max_bits`"):
		parse_text("varint_type v { encoding = leb128; }")


def test_varint_max_bits_is_bounded() -> None:
	with pytest.raises(SituError, match="must be a literal from 1 to 64"):
		parse_text("varint_type v { encoding = leb128; max_bits = 65; }")


def test_varint_rejects_an_unknown_property() -> None:
	with pytest.raises(SituError, match="unknown varint property `wibble`"):
		parse_text("varint_type v { encoding = leb128; max_bits = 8; wibble; }")


def test_varint_rejects_a_repeated_property() -> None:
	with pytest.raises(SituError, match="`minimal` is given twice"):
		parse_text("varint_type v { encoding = leb128; max_bits = 8; minimal; minimal; }")


def test_varint_rejects_an_unknown_encoding() -> None:
	with pytest.raises(SituError, match="unknown encoding `sqlite`"):
		parse_text("varint_type v { encoding = sqlite; max_bits = 8; }")


def test_varint_name_collides_like_any_other_type() -> None:
	with pytest.raises(SituError, match="declared more than once"):
		parse_text("struct v { u8 a; } varint_type v { encoding = leb128; max_bits = 8; }")


# -- variants (section 9.6) -------------------------------------------------


ENUMS = "enum K : u8 { hello = 1, data = 2, close = 3, }\nstruct A { u16 x; }\n"


def test_variant_parses() -> None:
	schema = parse_text(ENUMS + "struct S { K k; variant b switch (k) { "
	                    "case K.hello: A a; default: error; } }")
	variant = schema.structs()[-1].members[1]
	assert isinstance(variant, ast.Variant)
	assert variant.name == "b"
	assert len(variant.arms) == 2
	assert variant.default_arm is not None


def test_variant_default_error_arm_carries_no_member() -> None:
	schema  = parse_text(ENUMS + "struct S { K k; variant b switch (k) { "
	                     "case K.hello: A a; default: error; } }")
	variant = schema.structs()[-1].members[1]
	assert isinstance(variant, ast.Variant)
	default = variant.default_arm
	assert default is not None and default.is_error and default.member is None


def test_variant_needs_at_least_one_arm() -> None:
	with pytest.raises(SituError, match="has no arms"):
		parse_text(ENUMS + "struct S { K k; variant b switch (k) { } }")


def test_variant_takes_at_most_one_default() -> None:
	with pytest.raises(SituError, match="at most one `default` arm"):
		parse_text(ENUMS + "struct S { K k; variant b switch (k) { "
		           "default: error; default: error; } }")


def test_variant_must_cover_its_enum() -> None:
	"""Section 9.6: a missing case without a default arm is an error."""
	with pytest.raises(SituError, match="does not cover every value of `K`") as caught:
		parse_text(ENUMS + "struct S { K k; variant b switch (k) { case K.hello: A a; } }")

	report = caught.value.diagnostic.render()
	assert "`data`" in report and "`close`" in report
	assert "`default: error;` rejects an unknown discriminant" in report


def test_a_default_arm_makes_a_partial_variant_legal() -> None:
	schema = parse_text(ENUMS + "struct S { K k; variant b switch (k) { "
	                    "case K.hello: A a; default: error; } }")
	assert len(schema.structs()) == 2


def test_a_non_enum_discriminant_needs_a_default() -> None:
	"""Nothing tells the compiler which values are covered, so the only way to
	be total is to say what happens to the rest."""
	with pytest.raises(SituError, match="has no `default` arm"):
		parse_text(ENUMS + "struct S { u8 k; variant b switch (k) { case 1: A a; } }")


# -- opaque and indexed (sections 9.3, 9.4) ---------------------------------


def test_opaque_parses() -> None:
	member = only_struct("struct S { u16 n; opaque payload [n]; }").members[1]
	assert isinstance(member, ast.Opaque)
	assert member.name == "payload"


def test_indexed_parses() -> None:
	schema = parse_text("struct R { u32 id; }"
	                    "struct S { u16 n; indexed(offset_type = u16, count = n) "
	                    "{ R entries[]; } }")
	member = schema.structs()[-1].members[1]
	assert isinstance(member, ast.Indexed)
	assert member.name == "entries"
	assert isinstance(member.argument("offset_type"), ast.NameRef)


def test_indexed_holds_exactly_one_element_declaration() -> None:
	with pytest.raises(SituError, match="exactly one element declaration"):
		parse_text("struct R { u32 id; } struct S { u16 n; "
		           "indexed(offset_type = u16, count = n) { R a[]; R b[]; } }")
