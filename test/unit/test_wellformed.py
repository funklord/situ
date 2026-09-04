"""Whole-schema well-formedness checks.

Situ has no field numbers: position carries identity and names are the identity
(project.md section 4). Every duplicate here would be a silent correctness
problem rather than a style one.
"""

from __future__ import annotations

import pytest

from situc import wellformed
from situc.diagnostics import SituError
from situc.layout import solve
from situc.parser import ATTRIBUTE_NAMES, parse_text
from situc.types import NUMERIC_BOUNDS
from situc.resolve import resolve
from situc.unparse import unparse


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


def test_an_unknown_type_is_refused_even_where_a_file_imports() -> None:
	"""Type resolution used to step aside entirely when a schema imported,
	because the name might live in the imported file. Imports resolve now
	(17.0a), so every name a schema can see is present by the time this runs
	and an unknown one is a typo again."""
	assert "unknown type `Elsewhere`" in rendered(
		BUFFER + "struct S { Elsewhere x; }\n")


def test_an_unknown_type_without_an_import_does_not_blame_one() -> None:
	"""The note is conditional, and a schema with no import must not be told
	to look at one."""
	with pytest.raises(SituError) as caught:
		solve(parse_text('target buffer;\nendian big;\n'
		                 'struct s { elsewhere x; }'))

	assert "import resolution" not in caught.value.diagnostic.render()


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


def test_an_encoding_situ_cannot_check_is_refused() -> None:
	"""`ascii` and `utf8` are validated; anything else would be a claim the
	generated code never tests, which is worse than declaring nothing."""
	message = rendered("struct s { u8 name[8] [encoding = zzz]; }")

	assert "not an encoding situ validates" in message
	assert "`ascii` and `utf8`" in message


def test_bare_utf16_is_refused_and_names_the_two_orders() -> None:
	"""`utf16` alone does not say the byte order, and a default would be a
	guess a byte-swapped string passes silently (0044). The message points at
	the two named forms rather than merely refusing."""
	message = rendered("struct s { u16 name[8] [encoding = utf16]; }")

	assert "not an encoding situ validates" in message
	assert "utf16le" in message and "utf16be" in message


def test_utf16_on_an_eight_bit_element_is_refused() -> None:
	"""utf16 reads two-byte code units, so it validates a `u16` run and not a
	`u8` one; a check over the wrong element is worse than none (0044)."""
	message = rendered(BUFFER + "struct b { u8 s[8] [encoding = utf16le]; }\n")

	assert "16-bit encoding on a 8-bit element" in message
	assert "u16" in message


def test_utf8_on_a_sixteen_bit_element_is_refused() -> None:
	"""The mirror: ascii and utf8 read one byte at a time, so a `u16` run
	declaring one validates something other than the schema means."""
	message = rendered(BUFFER + "struct b { u16 s[8] [encoding = utf8]; }\n")

	assert "8-bit encoding on a 16-bit element" in message


def test_utf16le_on_a_sixteen_bit_run_is_accepted() -> None:
	parse_text(BUFFER + "struct b { u16 s[8] [encoding = utf16le]; }\n",
	           path="s.situ")


# -- what may seal (decision 0019) -------------------------------------------


AEAD = """codec aead {
	granularity = byte;
	length_preserving;
	seekable;
	authenticated;
	invertible;
	deterministic;
}
"""

PLAIN = """codec plain {
	granularity = byte;
	length_preserving;
	seekable;
	invertible;
	deterministic;
}
"""

SEALING = """struct h { u8 v; u16 length; }
struct s {
	u8 hop;
	authenticated { h hdr; u8 nonce[12]; }
	sealed(%s, nonce = nonce) { u16 inner; }
	tag u8[16];
}
"""


def test_a_codec_that_does_not_authenticate_cannot_seal() -> None:
	"""Section 14.3's gate hands out the interior once a tag has verified. A
	codec with no tag makes that a promise nothing keeps -- the ceremony
	without the substance."""
	message = rendered(PLAIN + 'impl plain extern "p";\n' + SEALING % "plain")

	assert "does not authenticate, so it cannot seal" in message
	assert "no tag to verify" in message
	assert "coded(plain)" in message


def test_a_derived_implementation_cannot_seal() -> None:
	"""Generated code is table driven, and a table indexed by secret data is a
	cache-timing channel -- which 14.6 forbids and this would reintroduce.

	No kernel derives `authenticated`, so the reachable case is a *pipeline*
	whose authentication comes from an extern stage and whose implementation is
	generated anyway. That is open question 12's own example: encrypt-then-code,
	where situ would emit a table-driven Reed-Solomon over sealed plaintext.
	"""
	body = (AEAD + 'impl aead extern "my_aead";\n'
	        "codec rs { kernel = polynomial(field = 256, n = 255, k = 223); }\n"
	        "impl rs derived;\n"
	        "codec protected = aead |> rs;\n"
	        "impl protected derived;\n" + SEALING % "protected")
	message = rendered(body)

	assert "has a derived implementation, so it cannot seal" in message
	assert "cache-timing channel" in message
	assert "extern" in message


def test_an_extern_authenticating_codec_may_seal() -> None:
	"""Which is the way out both diagnostics name, so it has to work."""
	parse_text(AEAD + 'impl aead extern "my_aead";\n' + SEALING % "aead")


def test_a_transform_that_does_not_authenticate_may_still_code() -> None:
	"""`coded` is for exactly this: a transform with no security claim."""
	parse_text(PLAIN + 'impl plain extern "p";\n'
	           "struct s { u8 hop; coded body(plain) { u16 inner; } }\n")


# -- invariants (open question 3) --------------------------------------------


def test_an_invariant_must_name_a_field_that_exists() -> None:
	message = rendered("struct s { u16 total; }\ninvariant s.nope == 1;")

	assert "has no field `nope`" in message
	assert "nothing would keep it true" in message


def test_an_invariant_must_name_a_struct_that_exists() -> None:
	message = rendered("struct s { u16 total; }\ninvariant other.total == 1;")

	assert "unknown struct `other`" in message


def test_an_invariant_may_not_reach_across_structs() -> None:
	"""It is evaluated against one view, and a field of another struct is not
	reachable from it."""
	message = rendered("struct a { u16 n; }\nstruct s { u16 total; }\n"
	                   "invariant s.total == size(a.n);")

	assert "not a field of `s`" in message
	assert "outside the struct this invariant maintains" in message


def test_an_invariant_may_not_derive_a_field_from_itself() -> None:
	"""Recomputing it would read the value it is about to write."""
	message = rendered("struct s { u16 total; u8 body[4]; }\n"
	                   "invariant s.total == s.total + 1;")

	assert "derives from itself" in message
	assert "circular" in message


def test_an_invariant_may_only_ask_layout_questions() -> None:
	"""`checksum(s.a)` used to reach the backends, which each declined it with
	"this build cannot evaluate it" -- true of a dynamic offset on a target
	that cannot resolve one, and misleading about a question that does not
	exist anywhere. A reader told the build was at fault goes looking for a
	better compiler."""
	message = rendered("struct s { u16 total; u8 a; }\n"
	                   "invariant s.total == checksum(s.a);")

	assert "`checksum` is not something an invariant can ask" in message
	assert "not a layout question" in message
	assert "`count`, `offset`, `size`" in message


def test_the_refusal_names_what_a_computed_value_actually_is() -> None:
	"""A blame chain has to end in a remedy (section 17). Here the remedy is
	not a different expression but a different construct: a value derived from
	the bytes is a tag or a codec, and those have their own dirty bits."""
	message = rendered("struct s { u16 total; u8 a; }\n"
	                   "invariant s.total == crc32(s.a);")

	assert "is a codec or a tag, not an invariant" in message


def test_a_nested_call_is_refused_too() -> None:
	"""The walk has to reach arguments, not just the outermost call."""
	message = rendered("struct s { u16 total; u8 a; }\n"
	                   "invariant s.total == size(s.a) + popcount(s.a);")

	assert "`popcount` is not something an invariant can ask" in message


# -- versioned members (section 19.4) ----------------------------------------


def test_a_member_may_not_arrive_before_one_declared_earlier() -> None:
	"""The whole of `[since]`. Situ has no field numbers, so a member inserted
	before an existing one moves every byte after it -- and a schema claiming
	"this arrived in v2" is making a compatibility claim that has to be true."""
	message = rendered("struct s [version = v] { u8 v; u32 b [since = 2]; u16 a; }")

	assert "arrives in version 1, after a member that arrives in 2" in message
	assert "append-only" in message


def test_a_versioned_member_needs_a_version_field() -> None:
	"""A reader has to know which version a message is before it knows whether
	the bytes are there."""
	message = rendered("struct s { u32 b [since = 2]; }")

	assert "has a versioned member and no version field" in message


def test_the_version_field_may_not_be_versioned() -> None:
	message = rendered("struct s [version = v] { u8 v [since = 2]; u16 a [since = 2]; }")

	assert "cannot be one of them" in message
	assert "know the version to find the version" in message


def test_the_version_field_must_hold_a_number() -> None:
	message = rendered("struct s [version = v] { u8 v[4]; u16 a [since = 2]; }")

	assert "not a single scalar" in message


def test_a_version_field_with_nothing_versioned_yet_is_fine() -> None:
	"""The ordinary first revision of an extensible format. Refusing it would
	force the attribute into the same commit as the first new member, which is
	where its absence matters least."""
	parse_text("struct s [version = v] { u8 v; u16 a; }")


# -- runs ending on a condition (section 8.6.6) ------------------------------


def test_a_while_condition_may_only_read_the_element() -> None:
	"""The enclosing struct's later members are placed after the run, so
	asking about one would be circular -- and its earlier members are a
	temptation worth refusing, because a condition mixing both scopes reads as
	though it were evaluated once and it is evaluated per element."""
	message = rendered("struct e { u8 n; }\n"
	                   "struct s { u8 k; e x[] while (k == 1); }")

	assert "has no field `k`" in message
	assert "not against `s`" in message


def test_a_run_may_not_say_twice_where_it_ends() -> None:
	message = rendered('struct e { u8 n; }\n'
	                   'struct s { e x[] until "\\r\\n" while (n == 1); }')

	assert "says twice where its run ends" in message


def test_a_condition_needs_a_run_to_end() -> None:
	message = rendered("struct s { u8 x while (x == 1); }")

	assert "there is no run to end" in message


def test_a_condition_needs_an_element_with_fields() -> None:
	message = rendered("struct s { u8 x[] while (x == 1); }")

	assert "which is not a struct" in message


# -- an attribute has to sit where something reads it (14.5, 17.0) ----------
#
# Spelling was checked and place was not, so `[equalize]` on a plain field or
# `[rw]` outside a register was accepted, dropped, and produced output
# byte-identical to the schema without it. Each entry in the table was settled
# by reading the code that consumes the attribute, not inferred from its name
# -- an over-restrictive table refuses valid schemas, which is worse than the
# silence it replaces, so every rule here has a control below it.

BUFFER = "target buffer;\nendian big;\nbit_order msb_first;\n\n"


def test_an_access_mode_outside_a_register_is_refused() -> None:
	text = rendered(BUFFER + "struct b { u16 x [rw]; }\n")
	assert "`[rw]` means nothing here" in text
	assert "field of a `register` struct" in text


def test_an_access_mode_on_a_register_field_is_accepted() -> None:
	"""The control. `layout._access_mode` reads these only when the struct is
	a register and the member is a field, which is exactly what is allowed."""
	parse_text("target mmio;\nendian big;\nbit_order msb_first;\n\n"
	           "register r @ 0x00 {\n"
	           "\twidth = 32;\n"
	           "\taccess_width = 32;\n"
	           "\tbit armed [rs];\n"
	           "\tbit done [w1c];\n"
	           "\tu16 rest;\n"
	           "\tbit spare [ro];\n"
	           "}\n", path="s.situ")


def test_equalize_off_a_variant_is_refused() -> None:
	text = rendered(BUFFER + "struct b { u16 x [equalize]; }\n")
	assert "`[equalize]` means nothing here" in text
	assert "`variant`" in text


def test_a_struct_attribute_on_a_member_is_refused() -> None:
	assert "`[allow_straddle]` means nothing here" in rendered(
		BUFFER + "struct b { u16 x [allow_straddle]; }\n")


def test_allow_unverified_read_off_a_sealed_region_is_refused() -> None:
	text = rendered(BUFFER + "struct b { u16 x [allow_unverified_read]; }\n")
	assert "`sealed` region" in text


def test_minimal_on_a_binary_scalar_is_refused() -> None:
	"""`radix_minimal` is read only where `radix` is set, so this is inert on
	a scalar whose value is stored as bits rather than written as digits."""
	text = rendered(BUFFER + "struct b { u16 x [minimal]; }\n")
	assert "radix-encoded number" in text


def test_minimal_on_a_radix_field_is_accepted() -> None:
	"""The control, and the one that caught an over-restrictive first draft:
	`decimal` sets `radix` on the field rather than leaving an attribute, so a
	rule keyed on the wrong thing refused `example/http`."""
	parse_text(BUFFER + 'struct b { decimal u16 code until " " max 4 [minimal]; }\n',
	           path="s.situ")


def test_the_diagnostic_says_the_output_would_be_identical() -> None:
	"""Which is the argument for refusing rather than warning: there is no
	way for a reader to tell from the generated code that it did nothing."""
	text = rendered(BUFFER + "struct b { u16 x [equalize]; }\n")
	assert "byte-identical to the schema without it" in text


def test_a_delimited_radix_field_survives_a_reprint() -> None:
	"""Found by the attribute check rather than looked for: `unparse` emitted
	no `until` at all and dropped the radix keyword, so

	    decimal u16 code until " " max 4 [minimal]

	came back as `u16 code [minimal]` -- which parses, means a plain binary
	scalar, and says so nowhere. The attribute rule turned a silent change of
	meaning into a refusal, which is how it was noticed.
	"""
	source = BUFFER + 'struct b { decimal u16 code until " " max 4 [minimal]; }\n'
	once   = unparse(parse_text(source, path="s.situ"))
	assert "decimal u16 code" in once
	assert 'until " "' in once
	assert "max 4" in once
	# Idempotent, which is the property that says nothing is lost per pass
	# rather than merely surviving the first one.
	assert unparse(parse_text(once, path="s.situ")) == once


def test_an_unknown_attribute_is_refused() -> None:
	"""An attribute situ has never heard of was accepted and dropped.

	The placement table settles where a *known* attribute may sit and says
	nothing about one that does not exist: `_attribute_place` returns `None`
	for a name it has no row for, which is the same answer it gives for a name
	that is correctly placed. Measured before this check existed --
	`[wibble = 16, pad_to = 4, utterly_made_up]` on a plain field compiled, and
	the emitted C was byte-identical to the schema with all three deleted.

	The other half of the language already refuses the same mistake: an
	invented `require` predicate is rejected as "not a builtin" with the
	builtins listed.
	"""
	text = rendered(BUFFER + "struct b { u8 a [wibble = 16]; }\n")
	assert "unknown attribute `wibble`" in text
	assert "byte-identical" in text


def test_an_unknown_attribute_suggests_a_near_name() -> None:
	"""A typo is the likely case, so the diagnostic names the near miss --
	one edit only, since a wider search produces confident wrong suggestions."""
	assert "`[min]` exists; did you mean that?" in rendered(
		BUFFER + "struct b { u8 a [mi = 3]; }\n")


def test_an_unknown_attribute_on_a_struct_is_refused() -> None:
	"""Struct attributes are a separate list and were checked separately."""
	assert "unknown attribute `wibble`" in rendered(
		BUFFER + "struct b [wibble] { u8 a; }\n")


def test_every_known_attribute_is_spelled_acceptably() -> None:
	"""The control: the check must not refuse a name the parser accepts.

	A vacuous version of the test above would pass just as loudly, so this
	asserts the complement -- every name in `ATTRIBUTE_NAMES` survives the
	spelling check, whatever the placement table then says about where it sits.
	"""
	for name in sorted(ATTRIBUTE_NAMES):
		source = BUFFER + "struct b { u8 a [%s]; }\n" % name
		try:
			parse_text(source, path="s.situ")
		except SituError as caught:
			# Refused for its place, its value or being unimplemented is
			# fine and expected -- refused for its *spelling* is not.
			assert "unknown attribute" not in caught.diagnostic.render(), name


def test_every_attribute_is_accounted_for() -> None:
	"""A new attribute has to have its place decided, or say it has not.

	The standing form of this section's rule. Without it the table is a
	snapshot that decays: an attribute added later would be neither placed nor
	listed as unplaced, and the silence it was added under is exactly what was
	just removed. Failing here is not a bug -- it is the question "where is
	this read?" arriving at the moment somebody can still answer it.
	"""
	placed = set(wellformed.PLACED_ATTRS)
	# Placed by rules that predate the table, in their own checks. The last
	# three were in `UNPLACED_ATTRS` and did not belong there: each already
	# refuses a wrong position with a better diagnostic than this table could
	# give, because each has the resolved layout to hand and this runs on the
	# AST.
	#
	#   bits             `Solver._narrow_bcd` -- "`[bits]` is for a
	#                    packed-decimal field, and `u8` is not one"
	#   since            `check_versions` -- a versioned member with no
	#                    version field on the struct
	#   require_aligned  `resolve._check_alignment` -- refuses a bit-packed
	#                    field, one with no static offset, and one that lands
	#                    short of its natural boundary
	#
	# `require_aligned` is the one that would have been got wrong by measuring
	# rather than reading: on `u8 a` at offset zero the generated C is
	# byte-identical with and without it, which looks exactly like an
	# unread attribute and is a check passing.
	elsewhere = {"quoted", "escape", "timeout_ms", "retries",
	             "bits", "since", "require_aligned"}

	known = (placed | wellformed.UNPLACED_ATTRS | elsewhere
	         | set(wellformed.UNIMPLEMENTED_ATTRS))

	assert set(ATTRIBUTE_NAMES) - known == set(), (
		"attribute with no place decided and not listed as unplaced")
	assert known - set(ATTRIBUTE_NAMES) == set(), (
		"a name in the tables that the parser does not accept")
	# The four groups do not overlap: an attribute is placed, or unplaced, or
	# unimplemented, and two of those at once is a table disagreeing with
	# itself rather than a fact about the language.
	assert not placed & wellformed.UNPLACED_ATTRS
	assert not placed & set(wellformed.UNIMPLEMENTED_ATTRS)


# The second batch. Each was settled by comparing the generated C *and* the
# capability map with and against the attribute -- an attribute can be read
# into one artifact and not the other, and a tool that inspected only the code
# called `[non_canonical]` inert on a plain field when it sets the canonical
# axis and a blame reason. `[trim]`, `[case_insensitive]` and
# `[nul_terminated]` were spared the same way and stay unplaced.


def test_a_reserved_policy_on_an_ordinary_field_is_refused() -> None:
	"""`_reserved_policy` reads these from a `reserved` member."""
	for name in ("preserve", "unknown", "must_be_one"):
		text = rendered(BUFFER + "struct b { u16 x [%s]; }\n" % name)
		assert "`reserved` member" in text


def test_a_reserved_policy_on_a_reserved_member_is_accepted() -> None:
	for name in ("preserve", "unknown", "must_be_one", "must_be_zero"):
		parse_text(BUFFER + "struct b { u8 a; reserved u8 [%s]; }\n" % name,
		           path="s.situ")


# The third batch. `[covers]` joins `[nonce]` and `[trusted]` as a spelling of
# something that is really a clause, and `[endian]`, `[min]`, `[max]` and
# `[must_eq]` get places. Each was measured in five positions -- generated C
# and capability map both -- before a rule was written for it.


def test_no_rmw_as_a_member_attribute_is_refused_as_misplaced() -> None:
	"""And not as unimplemented, which is what it used to say.

	`no_rmw` is a register *setting* -- `no_rmw;` in the body, like
	`volatile` -- and it is honoured: with it, `ctrl.enable` is
	`mutate=RewriteRequired` and has no single-bit setter; without it neither
	holds, which `test_a_partial_field_with_unsafe_reads_loses_its_setter`
	asserts and four tests in `test_registers` fail without.

	The old message was "read-modify-write suppression is not honoured by this
	build", so the compiler shipped a test proving the feature works beside a
	diagnostic telling authors it does not -- and sent them away from a safety
	property whose purpose is making an unsafe read-modify-write a compile
	error. Misplaced is not unimplemented.
	"""
	text = rendered(
		"target mmio;\nendian little;\nbit_order lsb_first;\n\n"
		"register r @ 0x00 {\n\twidth = 32;\n\taccess_width = 32;\n"
		"\tbit enable [rw, no_rmw];\n\treserved u31 [preserve];\n}\n")
	assert "`[no_rmw]` means nothing here" in text
	assert "a `register` body" in text
	assert "not implemented" not in text


# -- a pinned footprint, decision 0039 -------------------------------------


def test_a_pin_needs_an_array_sized_by_an_expression() -> None:
	"""Every refusal is a member that already answers "how many bytes is
	this", and two things saying one thing is 17.0's ambiguity rather than a
	redundancy to tolerate."""
	CASES = [
		("u8 a [size = 4];",                  "an array member"),
		("u8 a[8] [size = 8];",               "a literal length"),
		("u8 n; u8 a[remaining] [size = 8];", "`[remaining]` runs to"),
		('u8 a[] until ":" [size = 8]; u8 b;', "already say where"),
	]
	for body, expected in CASES:
		assert expected in rendered(BUFFER + "struct b { %s }\n" % body), body


def test_a_pin_on_an_expression_sized_array_is_accepted() -> None:
	"""The control. The pin is for exactly the member that has a length the
	message chooses and a footprint the schema wants fixed."""
	parse_text(BUFFER + "struct b { u8 n; u8 a[n] [size = 8]; }\n", path="s.situ")


# -- codec sizes, decision 0038 --------------------------------------------

SIZED_AEAD = (BUFFER + "codec ae {\n\tauthenticated;\n\tlength_preserving;\n"
        "\tinvertible;\n\ttag_bytes = 16;\n\tnonce_bytes = 12;\n}\n\n"
        'impl ae extern "x";\n\n')


def sealed(tag: str, nonce: str = "u8 nonce[12];") -> str:
	return (SIZED_AEAD + "struct s {\n\t" + nonce +
	        "\n\tsealed(ae, nonce = nonce) {\n\t\tu8 body[4];\n\t}\n\t"
	        + tag + "\n}\n")


def test_a_tag_matching_its_codec_is_accepted() -> None:
	"""The control, and the case every committed schema is in."""
	parse_text(sealed("tag u8[16];"), path="s.situ")


def test_a_tag_narrower_than_its_codec_is_refused() -> None:
	"""`tag u8[1]` used to compile beside a 16-byte AEAD, so a deliberate
	truncation and a typo were the same text."""
	text = rendered(sealed("tag u8[8];"))
	assert "is 8 bytes and `ae` produces 16" in text
	assert "`[truncated]`" in text


def test_a_truncated_tag_is_accepted_when_it_says_so() -> None:
	"""OSCORE uses eight bytes on constrained links, so truncation is a design
	choice rather than an error -- which is why it is made sayable rather than
	banned. The attribute is the author stating the loss is intended."""
	parse_text(sealed("tag u8[8] [truncated];"), path="s.situ")


def test_a_tag_wider_than_its_codec_is_refused_even_when_marked() -> None:
	"""`[truncated]` excuses a *narrower* tag and nothing else. No spelling
	gets more authentication out of a primitive than it produces, so there is
	nothing an author could mean by the wider case."""
	for tag in ("tag u8[32];", "tag u8[32] [truncated];"):
		text = rendered(sealed(tag))
		assert "is 32 bytes and `ae` produces 16" in text
		assert "authenticate nothing" in text


def test_a_nonce_of_the_wrong_width_is_refused() -> None:
	"""No exemption either way: a nonce is an input rather than a result, so a
	narrower one is a different nonce rather than a truncation of one."""
	text = rendered(sealed("tag u8[16];", "u8 nonce[8];"))
	assert "takes a 12-byte nonce" in text


def test_a_codec_that_states_no_size_checks_nothing() -> None:
	"""Silence claims nothing, which is already the rule for a declaration the
	compiler cannot verify. An extern codec's implementation belongs to
	somebody else, and an author who does not know its tag width must still be
	able to declare the codec."""
	quiet = (BUFFER + "codec ae {\n\tauthenticated;\n\tlength_preserving;\n"
	         "\tinvertible;\n}\n\n" + 'impl ae extern "x";\n\n'
	         "struct s {\n\tu8 nonce[12];\n"
	         "\tsealed(ae, nonce = nonce) {\n\t\tu8 body[4];\n\t}\n"
	         "\ttag u8[3];\n}\n")
	parse_text(quiet, path="s.situ")


def test_truncated_outside_a_tag_is_refused() -> None:
	"""Its place, so it cannot sit where `check_codec_sizes` never reads it."""
	assert "a `tag` or `checksum`" in rendered(
		BUFFER + "struct b { u8 a [truncated]; }\n")


def test_a_codec_size_must_be_a_literal() -> None:
	"""A size the compiler cannot see is one it cannot check a field against,
	and guessing would be worse than the silence 0038 permits."""
	text = rendered(BUFFER + "codec ae {\n\tauthenticated;\n"
	                "\ttag_bytes = 0;\n}\n")
	assert "needs a literal byte count" in text


def test_the_covers_attribute_is_refused() -> None:
	"""`covers(a, b)` is a clause on a `coded` region (14.1a). The attribute
	spelling is in `ATTRIBUTE_NAMES` for bracket disambiguation only, and is
	read by nothing -- the third of its kind after `nonce` and `trusted`."""
	assert "`covers` is not implemented" in rendered(
		BUFFER + "struct b { u8 a [covers = b]; u8 b; }\n")


def test_endian_on_a_single_byte_is_refused() -> None:
	"""A byte has no byte order, so nothing narrows and nothing is emitted."""
	text = rendered(BUFFER + "struct b { u8 a [endian = little]; }\n")
	assert "more than one byte" in text


def test_endian_on_a_struct_member_is_refused() -> None:
	"""The case worth having a diagnostic for: it looks like it should reach
	the members inside and does not, because the inner struct's scope was
	narrowed from its own declaration. 8.3 scopes `endian` per struct and per
	field, and a struct-typed member is neither."""
	assert "does not pass one inward" in rendered(
		BUFFER + "struct i { u16 x; }\nstruct b { i a [endian = little]; }\n")


def test_endian_on_a_wider_scalar_is_accepted() -> None:
	"""The control. A table that refuses a valid schema is worse than the
	silence it replaces -- including on an array, whose elements each have a
	byte order."""
	for decl in ("u16 a", "u16 a[4]", "u32 a"):
		parse_text(BUFFER + "struct b { %s [endian = little]; }\n" % decl,
		           path="s.situ")


def test_a_bound_on_an_array_is_refused() -> None:
	"""An array has no single value for `validate` to compare."""
	for name in ("min = 1", "max = 4", "must_eq = 2"):
		text = rendered(BUFFER + "struct b { u16 a[4] [%s]; }\n" % name)
		assert "no single value to bound" in text


def test_a_bound_on_a_struct_typed_member_is_refused() -> None:
	"""It has no value either, and this one was costing something.

	`bound-unbounded` tells an author to put an upper bound on an unbounded
	member. Written on a member whose type is a struct, `[max = N]` parsed,
	resolved, moved no axis, and drew the same suggestion again on the next
	run -- the `[size = N]` defect this rule already refuses, arriving under
	a different name. The advisor now points inside the type instead.
	"""
	for name in ("min = 1", "max = 32", "must_eq = 2"):
		text = rendered(BUFFER + "struct i { u8 r[remaining]; }\n"
		                "struct b { i a [%s]; }\n" % name)
		assert "no single value to bound" in text, name
		assert "a bound inside the struct it names" in text, name


def test_a_bound_on_an_enum_typed_member_is_accepted() -> None:
	"""The control, and the reason the rule cannot key on "the type name is
	not a width". An enum-typed member has exactly one value and `validate`
	compares it; only a struct-typed one has none."""
	parse_text(BUFFER + "enum k : u8 { a = 1, b = 2, }\n"
	           "struct b { k a [min = 1, max = 2]; }\n", path="s.situ")


def test_a_bound_on_a_text_number_is_accepted() -> None:
	"""The control that matters, and the one the first version of the rule got
	wrong. `decimal u32 magic[6]` is a six-character number with one value, so
	the brackets are a width rather than a repeat -- cpio constrains its magic
	exactly that way (26.113) and was refused until this exemption existed."""
	parse_text(BUFFER + "struct b { decimal u32 a[6] [min = 70701, max = 70702]; }\n",
	           path="s.situ")
	parse_text(BUFFER + "struct b { u8 a [min = 1, max = 4]; }\n", path="s.situ")


def test_must_be_zero_is_placed_on_reserved_members() -> None:
	"""26.60 kept this out of the table -- inert-by-default is not the same
	as unread -- and 0041 placed it after all, because the distinction had a
	second half nobody measured: on an *ordinary* field it is read by
	nothing anywhere, while the wire signature still carried the claim.
	`tcp_pseudo_header.zero [must_be_zero]` promised every peer a check the
	generated `validate` did not make. On a reserved member it still says
	the default out loud, which stays legal; on a field the enforced
	spelling is `[must_eq = 0]`, which is what tcp says now."""
	assert "must_be_zero" in wellformed.PLACED_ATTRS
	text = rendered(BUFFER + "struct b { u8 zero [must_be_zero]; }\n")
	assert "`[must_eq = 0]`" in text
	parse_text(BUFFER + "struct b { u8 a; reserved u8 [must_be_zero]; }\n",
	           path="s.situ")


def test_a_value_bound_on_a_reserved_member_is_refused() -> None:
	"""The other direction of the test above, and it was a silent lie.

	`must_be_zero` on an ordinary field promised a check nothing made, and
	0041 placed it for that. The mirror was worse and nothing caught it:
	`reserved u16 [must_eq = 0x4D42]` was accepted, and `_reserved_policy`
	reads only `must_be_zero`, `must_be_one`, `preserve` and `unknown` --
	defaulting to the first. So the generated C compared the bytes against
	**zero**, under a comment reading `[must_be_zero]`. Not the constraint
	ignored but its opposite enforced, and unreadable from the emitted
	source too (26.233).

	A reserved member states its content as a policy; `[must_eq]` is the
	spelling for an ordinary field. Both halves are asserted so the pair
	cannot drift into agreeing.
	"""
	for attr in ("must_eq = 0x4D42", "min = 1", "max = 9"):
		text = rendered(BUFFER + f"struct b {{ reserved u16 [{attr}]; }}\n")
		assert "means nothing here" in text, attr
		assert "states its content as a policy" in text, attr

	# The policies themselves stay legal, and so does a bound on a field.
	parse_text(BUFFER + "struct b { reserved u16 [must_be_zero]; u8 a; }\n",
	           path="s.situ")
	parse_text(BUFFER + "struct b { u16 x [must_eq = 5]; u8 a; }\n",
	           path="s.situ")


def test_the_four_held_attributes_are_placed() -> None:
	"""0041's other three, each refused where every backend ignores it and
	accepted where its consumer reads it. The unplaced set closes at the two
	attributes genuinely read in any position."""
	for body, expected in [
			("u8 a [trim];",             "a delimited member"),
			("u8 a[4] [case_insensitive];", "a delimited member"),
			("u8 a [nul_terminated];",   "a counted byte array")]:
		assert expected in rendered(BUFFER + "struct b { %s }\n" % body), body

	parse_text(BUFFER + 'struct b { u8 a[] until ":" [trim, case_insensitive];'
	           " u8 n; u8 c[n] [nul_terminated]; }\n", path="s.situ")
	assert wellformed.UNPLACED_ATTRS == frozenset({"secret", "non_canonical"})


def test_encoding_on_a_lone_scalar_is_refused() -> None:
	text = rendered(BUFFER + "struct b { u8 x [encoding = ascii]; }\n")
	assert "byte array or a delimited run" in text


def test_encoding_on_an_array_is_accepted() -> None:
	parse_text(BUFFER + "struct b { u8 s[8] [encoding = ascii]; }\n",
	           path="s.situ")


def test_self_as_off_a_tag_is_refused() -> None:
	text = rendered(BUFFER + "struct b { u16 x [self_as = 0]; }\n")
	assert "`tag` or `checksum`" in text


def test_volatile_on_a_member_is_refused() -> None:
	"""A register *setting*, parsed from the register body. It is in the
	attribute vocabulary for bracket disambiguation, which is not the same as
	a member ever carrying one."""
	text = rendered(BUFFER + "struct b { u16 x [volatile]; }\n")
	assert "`register` body" in text


# -- imports resolve (17.0a) -----------------------------------------------


def test_an_unknown_codec_says_nothing_about_imports() -> None:
	"""The note that named the import gap is gone with the gap: a codec that
	is not declared is not declared, and an imported file's codecs are here
	by the time this check runs."""
	text = rendered(BUFFER + "struct b {\n\tcoded body(aes_gcm_128)"
	                " { u8 x; }\n}\n")
	assert "import resolution" not in text
	assert "declare it with" in text


# The third batch, and the last that had rules to find. Sweeping all twenty
# remaining names through the two-artifact test settled most of them without a
# rule: nine are read on a plain scalar, `bits` and `since` refuse for
# themselves, `covers` is a clause rather than an attribute, and
# `must_be_zero` and `require_aligned` are satisfied-by-default rather than
# unread -- a check that passes looks exactly like one nothing runs.


def test_bit_order_on_a_whole_byte_scalar_is_refused() -> None:
	"""It decides how a *packed* field's bits sit in its byte. A whole-byte
	scalar has `endian` for the question it does have."""
	text = rendered(BUFFER + "struct b { u16 x [bit_order = lsb_first]; }\n")
	assert "bit-packed field" in text
	assert "`endian`" in text


def test_bit_order_on_a_packed_field_is_accepted() -> None:
	parse_text(BUFFER + "struct b { bit a [bit_order = lsb_first]; bit c;"
	           " reserved u6; }\n", path="s.situ")


def test_a_side_effect_outside_a_register_is_refused() -> None:
	"""`on_read` and `on_write` are SystemRDL side effects, and a bus is what
	makes a read an event at all."""
	for name, value in (("on_read", "clear"), ("on_write", "trigger")):
		text = rendered(BUFFER + "struct b { u16 x [%s = %s]; }\n"
		                % (name, value))
		assert "`register` struct" in text


def test_a_side_effect_on_a_register_field_is_accepted() -> None:
	parse_text("target mmio;\nendian big;\nbit_order msb_first;\n\n"
	           "register r @ 0x00 {\n\twidth = 32;\n\taccess_width = 32;\n"
	           "\tu8 f [ro, on_read = clear];\n\tu8 g;\n\tu16 h;\n}\n",
	           path="s.situ")


def test_version_on_a_member_is_refused() -> None:
	"""`version` names the field a struct's `[since]` members are counted
	against, and it is written on the struct: `struct s [version = v]`."""
	text = rendered(BUFFER + "struct b { u16 x [version]; }\n")
	assert "a struct, naming the field" in text


def test_a_nonce_attribute_is_refused() -> None:
	"""The only nonce anything reads is a sealed region's `nonce = ref`
	argument. `[nonce]` beside it said the same thing to nobody, and 14.1's
	table listed it as though a mark were being made."""
	text = rendered(BUFFER + "struct b { u8 n[12] [nonce]; }\n")
	assert "`nonce` is not implemented" in text
	assert "sealed(codec, nonce = field)" in text


def test_a_trusted_attribute_is_refused() -> None:
	"""A codec's trust is derived from whether it has an `impl`; the only
	`trusted` in the compiler is a status string `capmap` prints."""
	text = rendered(BUFFER + "struct b { u16 x [trusted]; }\n")
	assert "`trusted` is not implemented" in text
	assert "derived from its `impl`" in text


def test_the_nonce_argument_still_works() -> None:
	"""The control, and the point: refusing the attribute must not touch the
	thing that actually names a nonce."""
	parse_text(BUFFER + "codec aead {\n\tgranularity = byte;\n"
	           "\tlength_preserving;\n\tseekable;\n\tauthenticated;\n"
	           "\tinvertible;\n\tdeterministic;\n}\n"
	           "impl aead extern \"x\";\n"
	           "struct b {\n\tauthenticated a { u8 n[12]; }\n"
	           "\tsealed s(aead, nonce = n) { u16 v; }\n"
	           "\ttag u8[16] covers(a, s);\n}\n", path="s.situ")


# -- the argv exercise's two defects, 26.124 --------------------------------


def test_a_string_in_a_run_condition_is_refused() -> None:
	"""The front end accepted `while (text != "--")` and the C backend
	emitted a comparison against the literal's address, calling a getter no
	delimited member has -- generated code that does not compile. Found by
	writing an argv schema whose run ends at `--`."""
	text = rendered(
		BUFFER + 'struct arg { u8 text[] until "\\0"; }\n'
		'struct line { arg a[] while (text != "--") max 4; u8 z; }\n')
	assert "a string is not one" in text
	assert "_eq" in text


def test_a_byte_run_in_a_run_condition_is_refused() -> None:
	"""26.113's rule -- a byte run has no value -- met in the condition
	language: `_find_member` found the field and nothing asked whether it
	carries a value."""
	text = rendered(
		BUFFER + 'struct arg { u8 n; u8 text[] until "\\0"; }\n'
		"struct line { arg a[] while (text != 0) max 4; u8 z; }\n")
	assert "no value a condition can compare" in text


def test_an_integer_run_condition_is_accepted() -> None:
	"""The control: dnsname's shape, fields against numbers."""
	parse_text(BUFFER + "struct el { u2 form; u6 rest; }\n"
	           "struct run { el e[] while (form == 0 && rest != 0) max 8; }\n",
	           path="s.situ")


def test_located_on_a_variant_arm_member_is_refused() -> None:
	"""Every backend places an arm member after the discriminant and none
	consults `at` -- the C emitter hard-coded the offset with the expression
	nowhere in it. Accepted and ignored is the silently-nothing shape, so it
	is refused until arm re-addressing is a decision rather than a patch."""
	text = rendered(
		BUFFER + "struct e { u8 first;\n"
		"\tvariant body switch (first) {\n"
		"\t\tcase 0x2D: u8 option[remaining];\n"
		"\t\tdefault:   u8 positional[remaining] at 0;\n\t}\n}\n")
	assert "`at` on a variant arm member is not implemented" in text


def test_located_outside_an_arm_is_untouched() -> None:
	"""The control: a top-level located member is a working construct
	(sqlite and bmp carry one) and the refusal must not reach it."""
	parse_text(BUFFER + "struct s { u32 offset; u8 data[4] at offset; }\n",
	           path="s.situ")


# -- nonce reuse, the check 14.8 claimed and never had (26.127) -------------


SEALING_AEAD = (BUFFER + "codec ae {\n\tauthenticated;\n\tlength_preserving;\n"
                "\tinvertible;\n}\n\n" + 'impl ae extern "x";\n\n')


def test_one_nonce_feeding_two_sealed_regions_is_refused() -> None:
	"""14.8 has claimed this refusal since the survey was written -- "one
	nonce field feeding two sealed regions is refused, which is real
	nonce-reuse reasoning" -- and it was never implemented: the schema below
	built in every backend. Under one key a repeated nonce is the worst
	failure an AEAD has; GCM yields the authentication key."""
	text = rendered(SEALING_AEAD +
	                "struct s {\n\tu8 n[12];\n"
	                "\tsealed one(ae, nonce = n) { u8 a[4]; }\n"
	                "\tsealed two(ae, nonce = n) { u8 b[4]; }\n"
	                "\ttag u8[16];\n}\n")
	assert "`n` seeds two sealed regions" in text
	assert "authentication key" in text


def test_distinct_nonces_for_distinct_regions_are_accepted() -> None:
	"""The control, and the remedy the diagnostic names."""
	parse_text(SEALING_AEAD +
	           "struct s {\n\tu8 n[12];\n\tu8 m[12];\n"
	           "\tsealed one(ae, nonce = n) { u8 a[4]; }\n"
	           "\tsealed two(ae, nonce = m) { u8 b[4]; }\n"
	           "\ttag u8[16];\n}\n", path="s.situ")


def test_the_same_nonce_name_in_two_structs_is_fine() -> None:
	"""Two structs are two messages; their fields are different fields."""
	one = ("struct %s {\n\tu8 n[12];\n"
	       "\tsealed one(ae, nonce = n) { u8 a[4]; }\n\ttag u8[16];\n}\n")
	parse_text(SEALING_AEAD + one % "s" + one % "t", path="s.situ")


# -- key selection, decision 0040 -------------------------------------------


def test_a_key_selector_is_accepted_with_and_without_a_nonce() -> None:
	"""`key = field` names the field whose value selects the region's key --
	DTLS's epoch, QUIC's key-phase bit, WireGuard's receiver index."""
	parse_text(SEALING_AEAD +
	           "struct s {\n\tu16 epoch;\n\tu8 n[12];\n"
	           "\tsealed one(ae, nonce = n, key = epoch) { u8 a[4]; }\n"
	           "\ttag u8[16];\n}\n", path="s.situ")
	parse_text(SEALING_AEAD +
	           "struct s {\n\tu16 epoch;\n"
	           "\tsealed one(ae, key = epoch) { u8 a[4]; }\n"
	           "\ttag u8[16];\n}\n", path="s.situ")


def test_a_key_selector_gets_the_nonce_ordering_rule() -> None:
	"""The key is picked before anything it decrypts is decoded, so the
	selector has to be parsed strictly earlier -- the nonce's rule, for the
	nonce's reason."""
	text = rendered(SEALING_AEAD +
	                "struct s {\n\tu8 n[12];\n"
	                "\tsealed one(ae, nonce = n, key = late) { u8 a[4]; }\n"
	                "\tu16 late;\n\ttag u8[16];\n}\n")
	assert "unknown key selector field `late`" in text


def test_a_key_selector_must_carry_a_value() -> None:
	"""26.113's rule, met here as it was in run conditions: a byte run has
	no value to select by. QUIC's key-phase *bit* is the case that must
	pass, and does -- `bit` is integer-domain."""
	text = rendered(SEALING_AEAD +
	                'struct s {\n\tu8 r[] until "\\0";\n\tu8 n[12];\n'
	                "\tsealed one(ae, nonce = n, key = r) { u8 a[4]; }\n"
	                "\ttag u8[16];\n}\n")
	assert "no value to select by" in text


def test_a_derived_shift_register_must_say_what_only_the_emitter_reads() -> None:
	"""A signature and a generated implementation need different things.

	`shift_register` derives every property it reports from one word,
	`feedback`, so the derivation never looks at taps, width or seed. The
	emitter reads all three and defaulted two of them:

	    no seed   generated 0xFFFF, a different keystream
	    no width  generated a 16-bit register, where `taps = 0x1` is NRZI
	              at width 1 and something else entirely at 16
	    no taps   no implementation, and nothing naming the missing argument

	No committed schema omits any of them, so the defaults could only ever
	be wrong. This is the omission half of the same shape 26.156 closed for
	misspelling: an argument nothing reads, and an argument nothing wrote.
	"""
	whole = ("taps = 0xB400, width = 16, seed = 0xACE1, feedback = input")
	for missing, kernel in [
			("seed",  "taps = 0xB400, width = 16, feedback = input"),
			("width", "taps = 0xB400, seed = 0xACE1, feedback = input"),
			("taps",  "width = 16, seed = 0xACE1, feedback = input")]:
		text = rendered(f"endian big;\ncodec s {{ kernel = "
		                f"shift_register({kernel}); }}\n"
		                f"impl s derived;\nstruct t {{ u8 a; }}\n")
		assert f"does not say `{missing}`" in text, text
		assert "generates a different code in silence" in text, text

	# Stated in full it parses, which is the half a refusal test cannot show.
	parse_text(f"endian big;\ncodec s {{ kernel = "
	           f"shift_register({whole}); }}\n"
	           f"impl s derived;\nstruct t {{ u8 a; }}\n")


def test_a_signature_without_an_implementation_needs_none_of_that() -> None:
	"""13.1's normal case for a protocol under design, and the reason this
	check hangs off the `impl` rather than off the derivation.

	A codec that declares a kernel and binds nothing generates nothing, so
	the arguments only the emitter reads are not missing -- they are not
	wanted. Putting the requirement in `kernels.derive` would refuse the
	derivation tests that state nothing but `feedback`, which is exactly what
	those tests are for.

	**An `extern` binding is the case the guard is actually for**, and the
	first version of this test did not reach it: a codec with no `impl` never
	enters the loop the check hangs off, so removing the `ImplKind.DERIVED`
	guard left it passing. The implementation is the caller's there, situ
	emits none, and the emitter's arguments are as unwanted as they are for a
	bare signature.
	"""
	from situc.parser import parse_text

	# No binding: never reaches the check.
	parse_text("endian big;\n"
	           "codec s { kernel = shift_register(taps = 5, "
	           "feedback = input); }\n")

	# Bound to somebody else's code: reaches it, and must pass.
	parse_text("endian big;\n"
	           "codec s { kernel = shift_register(taps = 5, "
	           "feedback = input); }\n"
	           "impl s extern \"my_scrambler\";\n")


def test_every_region_kind_refuses_an_argument_it_does_not_read() -> None:
	"""`sealed` refused one and its three neighbours did not, in this file.

	The rule and its reason were written for `sealed` -- "anything else would
	state what the generated code does not do" -- and `coded`, `indexed` and
	`tlv` went on accepting anything. What that cost is sharpest on
	`indexed`: `_index_base` scans the list for `base` and falls back to
	`IndexBase.REGION` when it does not find one, so `basse = page_type`
	measured every offset in the table from the region rather than from the
	named member, silently. Three readers stepped past it, and the attribute
	checker could not see it at all -- it walks `member.attrs`, and a region
	keeps its arguments in `.args`.

	Each is asked for the near miss as well as the refusal, because a message
	listing nine valid names without pointing at the one you meant makes the
	reader do the diff.
	"""
	cases = [
		("an indexed", "base",
		 "endian big;\nstruct s { u8 hdr[4]; "
		 "indexed (basse = hdr) { u8 a; } }\n", "basse"),
		("a tlv", "duplicate_tags",
		 "endian big;\nstruct s { "
		 "tlv t (tag_type = u8, duplicat_tags = allowed); }\n",
		 "duplicat_tags"),
		("a coded", "nonce",
		 "endian big;\ncodec c { length_preserving; }\n"
		 "struct s { coded b(c, noncee = 1) { u8 x[4]; } }\n", "noncee"),
	]

	for article, meant, source, written in cases:
		text = rendered(source)
		assert f"`{written}` is not an argument {article} region takes" in text, text
		assert f"did you mean `{meant}`?" in text, text
		assert "state what the generated code does not do" in text, text


def test_a_region_argument_that_is_read_still_parses() -> None:
	"""The other half, and the one that catches a vocabulary built too narrow.

	Closing the kernel argument vocabulary refused `symbol` -- a documented,
	implemented form that no schema and no test exercised (26.159) -- so each
	name in `REGION_ARGUMENTS` needs something that writes it. `coded` taking
	a nonce is the one that would have been lost here: the wellformed check
	only ever mentioned `sealed`, and it is `layout._region_argument`, shared
	between the two, that reads it.
	"""
	from situc.parser import parse_text

	parse_text("endian big;\nstruct s { u8 hdr[4]; "
	           "indexed (base = hdr) { u8 a; } }\n")
	parse_text("endian big;\n"
	           "codec c { length_preserving; seekable = linear; "
	           "granularity = byte; }\n"
	           "struct s { u8 iv[8]; coded b(c, nonce = iv) { u8 x[4]; } }\n")


def test_an_unknown_region_argument_is_refused() -> None:
	"""`sealed(ae, wibble = x)` was accepted with the argument read by
	nothing -- the same silence 26.117 closed for attributes, one construct
	over. The vocabulary is `nonce` and `key`, stated in the diagnostic."""
	text = rendered(SEALING_AEAD +
	                "struct s {\n\tu8 n[12];\n"
	                "\tsealed one(ae, wibble = n) { u8 a[4]; }\n"
	                "\ttag u8[16];\n}\n")
	assert "`wibble` is not an argument a sealed region takes" in text
	assert "`key = field`" in text


# -- the file target (0047) -------------------------------------------------


def test_an_unbounded_message_needs_append_on_a_file() -> None:
	"""A message with no end, in a medium that has one.

	The first struct is what a reader acquires, so its extent is the file's.
	Unbounded is coherent about a stream and incoherent about a file nobody
	is appending to: nothing would decide where it stops. `example/sqlite` is
	the one of the seven file formats measured for 0047 with an unbounded
	top-level extent, and it is exactly the format that grows.
	"""
	def built(source: str) -> None:
		"""The extent is known to `solve`, so the check is in `resolve` beside
		the other layout-dependent ones rather than in `wellformed`, which
		reads the AST."""
		schema = parse_text(source, path="s.situ")
		resolve(schema, solve(schema))

	with pytest.raises(SituError) as caught:
		built("target file;\nendian big;\n"
		      "struct s { u8 rest[remaining]; }\n")
	text = caught.value.diagnostic.render()
	assert "has no upper bound under `target file`" in text
	assert "`target file append`" in text

	# Both ways out, and the control: a buffer's extent is the caller's.
	for source in ("target file append;\nendian big;\n"
	               "struct s { u8 rest[remaining]; }\n",
	               "target file;\nendian big;\n"
	               "struct s { u16 n [max = 8]; u8 body[n]; }\n",
	               "target buffer;\nendian big;\n"
	               "struct s { u8 rest[remaining]; }\n"):
		built(source)


def test_a_register_still_needs_mmio_under_a_file() -> None:
	"""The refusal that already existed covers the new target, because it is
	keyed on `is not MMIO` rather than on `is BUFFER`."""
	text = rendered("target file;\nendian little;\nbit_order lsb_first;\n"
	                "register ctrl @ 0x00 {\n"
	                "\twidth        = 32;\n"
	                "\taccess_width = 32;\n"
	                "\tbit enable [rw];\n"
	                "\treserved u31;\n"
	                "}\n")
	assert "needs `target mmio`" in text


# -- an attribute that reads as nothing -------------------------------------


@pytest.mark.parametrize("name", sorted(wellformed.RELATION_ONLY_ATTRS))
def test_a_policy_attribute_on_a_member_is_refused(name: str) -> None:
	"""`[timeout_ms]` and `[retries]` are read from a `relation`'s attrs and
	from nowhere else, so on a field they are accepted and skipped.

	Worse than an ordinary misplacement, which is why it is a test rather
	than a note. A relation stating `retries` without `timeout_ms` is
	refused for stating half a retransmission policy -- situ will not invent
	an interval. On a member neither half means anything and nothing said
	so, so the author who wrote it got no policy and no complaint.
	"""
	assert "means nothing here" in rendered(
		"struct S { u32 a [%s = 3]; }" % name)


def test_a_policy_attribute_on_a_relation_is_still_read() -> None:
	"""The control for the refusal above: it must not reach the place these
	attributes belong, which is the one place a wrong `_attribute_place`
	entry would break."""
	schema = parse_text("""
		struct H { u16 id; }
		relation r(q: H, p: H) [timeout_ms = 5000, retries = 2] {
			must p.id == q.id;
		}
	""", path="s.situ")
	names = {attr.name for relation in schema.relations()
	         for attr in relation.attrs}
	assert {"timeout_ms", "retries"} <= names


@pytest.mark.parametrize("name", sorted(NUMERIC_BOUNDS))
def test_a_bound_with_no_value_is_refused(name: str) -> None:
	"""`[max]` parses, because the bracket syntax admits a bare name -- that
	is how `[secret]` is written -- and `Solver.constrain` skips any
	attribute whose value is `None`. So the field kept its full range while
	the schema said otherwise.

	Found by generating C with the attribute and without it and comparing:
	byte-identical output is the definition the refusal's own message uses.
	"""
	assert f"`[{name}]` needs a value" in rendered(
		"struct S { u32 a [%s]; }" % name)


@pytest.mark.parametrize("name", sorted(NUMERIC_BOUNDS))
def test_a_bound_with_a_value_is_still_accepted(name: str) -> None:
	"""The control: the refusal must be about the missing value and not
	about the attribute."""
	parse_text("struct S { u32 a [%s = 7]; }" % name, path="s.situ")


def test_a_flag_attribute_still_takes_no_value() -> None:
	"""`[secret]` is bare by design, so the refusal above must not be a rule
	against bare attributes generally."""
	parse_text("struct S { u32 a [secret]; }", path="s.situ")


def test_the_bound_check_reaches_inside_a_block() -> None:
	"""`_walk_members` descends, and a check that only saw a struct's top
	level would pass a schema whose blocks hid the fault."""
	assert "`[max]` needs a value" in rendered("""
		struct S {
			authenticated b { u32 a [max]; }
			checksum u8 c[2] covers(b);
		}
	""")


# -- a byte run pinned to a literal (0052) ----------------------------------


def test_a_byte_run_may_be_pinned_to_a_literal() -> None:
	"""The construct three schemas needed and none could write.

	Before this, `[must_eq]` on an array was refused outright, so a magic was
	one field per byte -- six loads, six branches, six invented member names,
	and comments rendering the bytes in decimal.
	"""
	parse_text('struct S { u8 sig[4] [must_eq = "WOZ2"]; }', path="s.situ")


def test_a_pinned_run_must_match_its_declared_length() -> None:
	"""The one mistake this construct invites and the per-byte spelling could
	not make: writing four fields, you count them."""
	assert "pins 5 byte(s) of a 4-byte run" in rendered(
		'struct S { u8 sig[4] [must_eq = "WOZ22"]; }')
	assert "pins 3 byte(s) of a 4-byte run" in rendered(
		'struct S { u8 sig[4] [must_eq = "WOZ"]; }')


def test_only_a_byte_element_may_be_pinned() -> None:
	"""A span of wider scalars has an endianness the literal does not, which
	is the confusion 0024 is about -- so `u16 sig[2] [must_eq = "BM"]` is
	refused rather than silently picking a byte order."""
	assert "means nothing here" in rendered(
		'struct S { u16 sig[2] [must_eq = "BM"]; }')


def test_a_numeric_bound_on_a_run_is_still_refused() -> None:
	"""The exception is `must_eq` against a literal, not arrays generally.
	An ordering on a span is not a thing this language defines."""
	assert "means nothing here" in rendered(
		"struct S { u8 sig[4] [must_eq = 7]; }")
	assert "means nothing here" in rendered(
		'struct S { u8 sig[4] [max = 7]; }')


# -- preamble: fixed bytes nobody may read (0052) ---------------------------


def test_a_preamble_pins_bytes_and_has_no_name() -> None:
	"""Anonymous is what makes it inaccessible: there is no name for an
	accessor to be called."""
	schema = parse_text('struct S { preamble u8[4] = "WOZ2"; u32 n; }',
	                    path="s.situ")
	member = list(schema.structs())[0].members[0]
	assert member.pinned == b"WOZ2"


def test_a_preamble_may_omit_its_length() -> None:
	"""The literal already says how many bytes there are, so requiring the
	author to repeat the count is a second place to be wrong."""
	schema = parse_text('struct S { preamble u8 = "BM"; u32 n; }', path="s.situ")
	assert list(schema.structs())[0].members[0].pinned == b"BM"


def test_a_preamble_length_must_agree_with_its_literal() -> None:
	assert "pins 3 byte(s) of a 4-byte run" in rendered(
		'struct S { preamble u8[4] = "WOZ"; }')


def test_a_preamble_is_a_run_of_bytes() -> None:
	"""A literal has no byte order to give a wider element (0024)."""
	assert "`preamble` is a run of bytes, found `u16`" in rendered(
		'struct S { preamble u16[2] = "BM"; }')


def test_a_preamble_needs_a_literal() -> None:
	assert "`preamble` is pinned to a literal" in rendered(
		"struct S { preamble u8[4] = 7; }")


def test_a_reserved_run_is_still_a_policy_not_a_value() -> None:
	"""The two vocabularies must not merge, which is 26.233. `preamble` is
	the spelling for stated content; `reserved` keeps its policies."""
	assert "a `reserved` member states its content as a policy" in rendered(
		"struct S { reserved u8[4] [must_eq = 3]; }")


# -- a byte-run enum (0052) -------------------------------------------------


def test_a_byte_run_enum_names_spans() -> None:
	"""`enum format : u8[2] { bmp = "BM" }` -- the construct the copyright
	holder asked for by name.

	Not sugar for a `u16`: `"BM"` as one is 0x424D or 0x4D42 depending on
	endianness, and an author writing a signature is not thinking about byte
	order at all. A span has none, so the arm means the same under both --
	which is why every format reference writes signatures as text.
	"""
	schema = parse_text('enum m : u8[2] { bmp = "BM", pe = "MZ" }\n'
	                    "struct S { m t; }", path="s.situ")
	enum = next(iter(schema.enums()))
	assert enum.width == 2


def test_a_byte_run_enum_field_is_the_run_it_denotes() -> None:
	"""The field declares no array and is two bytes, because the enum says
	how wide one of its values is."""
	schema = parse_text('enum m : u8[2] { bmp = "BM" }\nstruct S { m t; }',
	                    path="s.situ")
	field = list(schema.structs())[0].members[0]
	assert field.array is not None
	assert field.array.size.value == 2


def test_every_arm_is_as_wide_as_the_enum() -> None:
	"""An enum whose arms differ in length is a grammar, not a value."""
	assert "is 3 byte(s) of a 2-byte enum" in rendered(
		'enum m : u8[2] { bmp = "BMP" }\nstruct S { m t; }')


def test_a_byte_run_enum_is_backed_by_u8() -> None:
	assert "a byte-run enum is backed by `u8`" in rendered(
		'enum m : u16[2] { bmp = "BM" }\nstruct S { m t; }')


def test_a_byte_run_enum_arm_is_a_literal() -> None:
	assert "is not a byte string" in rendered(
		'enum m : u8[2] { bmp = 7 }\nstruct S { m t; }')


def test_an_ordinary_enum_still_refuses_a_string_arm() -> None:
	"""The brackets are what makes the arms bytes, so without them a string
	is still not an integer.

	Refused when the values are folded rather than in the front end, which
	is where it has always been -- the byte-run checks are earlier because
	they are about a construct's own vocabulary, and this is about an
	expression that does not evaluate.
	"""
	schema = parse_text('enum m : u16 { bmp = "BM" }\nstruct S { m t; }',
	                    path="s.situ")
	with pytest.raises(SituError) as caught:
		solve(schema)
	assert "a string is not an integer expression" in str(caught.value)


# -- a checksum that computes itself (0053) ---------------------------------


CODEC = """
codec crc32 {
	kernel = polynomial(width = 32, poly = 0x04C11DB7, init = 0xFFFFFFFF,
	                    xorout = 0xFFFFFFFF, reflect);
}
"""


def checksum_schema(impl: str = "impl crc32 derived;",
		clause: str = "is crc32", keyword: str = "checksum") -> str:
	return (f"target file append;\nendian little;\n{CODEC}{impl}\n"
	        f"struct S {{\n"
	        f"\t{keyword} u8 crc[4] covers(body) {clause};\n"
	        f"\tauthenticated body {{ u8 rest[remaining]; }}\n}}\n")


def test_a_checksum_may_name_a_derived_codec() -> None:
	"""respec's finding: `covers(R)` declares coverage and staleness and
	never the arithmetic, so a truncated image read as fine."""
	parse_text(checksum_schema(), path="s.situ")


def test_a_tag_may_not_name_a_codec() -> None:
	"""14.1 stands for tags: situ does not implement AEAD, and constant-time
	behaviour and key handling are not a layout compiler's to own."""
	assert "a `tag` does not name the codec that computes it" in rendered(
		checksum_schema(keyword = "tag"))


def test_the_codec_must_be_derived() -> None:
	"""`derived` is the tier situ already generates, which is what makes
	this a wiring change rather than a reversal of 14.1. An `extern` codec
	is the caller's function."""
	assert "is not a derived codec" in rendered(
		checksum_schema(impl = 'impl crc32 extern "my_crc";'))


def test_the_codec_must_have_an_implementation() -> None:
	assert "has no implementation" in rendered(checksum_schema(impl = ""))


def test_the_codec_must_exist() -> None:
	assert "no codec named `crc99`" in rendered(
		checksum_schema(clause = "is crc99"))


def test_a_checksum_without_a_codec_is_unchanged() -> None:
	"""The construct is opt-in: every checksum written before 0053 still
	means what it meant, with the arithmetic in the caller."""
	parse_text(checksum_schema(clause = ""), path="s.situ")


# -- a literal is bytes, not text (0052) ------------------------------------


@pytest.mark.parametrize("body,where", [
	('struct S { u8 sig[4] [must_eq = "\\x89PNG"]; }', "must_eq"),
	('struct S { preamble u8[4] = "\\x89PNG"; }',      "preamble"),
	('enum m : u8[4] { png = "\\x89PNG" }\nstruct S { m t; }', "enum"),
])
def test_a_high_byte_in_a_literal_is_one_byte(body: str, where: str) -> None:
	"""`\\xNN` is one byte, which is what the lexer's own docstring says.

	All three constructs encoded the literal as UTF-8, so `\\x89` became two
	bytes and a four-byte run was refused as five. It failed for exactly the
	bytes a magic is most likely to contain -- PNG's signature could not be
	written at all, and WOZ2's `\\x8d` counted double -- while every ASCII
	test passed.

	The tree already had the answer twice: `until` and the delimiter
	attribute have always used latin-1. Three new copies of a decision made
	twice is what `literal_bytes` now prevents.
	"""
	parse_text(body, path="s.situ")


def test_a_literal_that_is_not_bytes_is_refused() -> None:
	"""A code point above 255 is text, and text that needs an encoding is a
	different member with an `[encoding]` on it.

	Latin-1 is the boundary rather than ASCII: `\xe9` is one byte and fits,
	which is why the refusal has to be tested with something that does not.
	"""
	assert "bytes" in rendered('struct S { u8 s[1] [must_eq = "\u4e2d"]; }')
	assert "bytes" in rendered('struct S { preamble u8[1] = "\u4e2d"; }')
