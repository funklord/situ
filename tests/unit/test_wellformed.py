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


def test_type_resolution_is_skipped_when_a_file_imports() -> None:
	"""The missing name may legitimately live in the imported file, and import
	resolution does not exist yet."""
	schema = parse_text('import "other.situ"; struct S { Elsewhere x; }')
	assert len(schema.structs()) == 1


def test_a_type_from_an_import_says_why_it_cannot_be_found() -> None:
	"""Stepping aside above is only half an answer.

	The solver has to have a layout, so it raises `unknown type` a moment
	later -- and told the author their type does not exist, which is not what
	went wrong: the type may be perfectly good and the resolution that would
	find it is not built. The note is the difference between a diagnostic
	that sends someone looking for a typo and one that names the gap.
	"""
	with pytest.raises(SituError) as caught:
		solve(parse_text('target buffer;\nendian big;\n'
		                 'import "other.situ"; struct s { elsewhere x; }'))

	rendered = caught.value.diagnostic.render()
	assert "unknown type `elsewhere`" in rendered
	assert "import resolution is not implemented" in rendered


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
	message = rendered("struct s { u8 name[8] [encoding = utf16]; }")

	assert "not an encoding situ validates" in message
	assert "`ascii` and `utf8`" in message


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
	authenticated { h hdr; u8 nonce[12] [nonce]; }
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
	rule keyed on the wrong thing refused `examples/http`."""
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


def test_every_attribute_is_accounted_for() -> None:
	"""A new attribute has to have its place decided, or say it has not.

	The standing form of this section's rule. Without it the table is a
	snapshot that decays: an attribute added later would be neither placed nor
	listed as unplaced, and the silence it was added under is exactly what was
	just removed. Failing here is not a bug -- it is the question "where is
	this read?" arriving at the moment somebody can still answer it.
	"""
	placed = set(wellformed.PLACED_ATTRS)
	# Placed by rules that predate the table, in their own checks.
	elsewhere = {"quoted", "escape", "timeout_ms", "retries"}

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


def test_must_be_zero_is_left_alone() -> None:
	"""It is `_reserved_policy`'s default, so writing it changes no byte --
	and is still not meaningless, because it says out loud what the silence
	already meant. Inert-by-default is not the same as unread, which is the
	distinction that keeps it out of the table."""
	assert "must_be_zero" not in wellformed.PLACED_ATTRS


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


# -- a codec that should have come from an import ---------------------------
#
# `check_types_resolve` steps aside when a schema imports, and the solver
# names the gap when it cannot lay the type out. A codec had neither half.

CODEC_USE = (BUFFER + "struct b {\n\tcoded body(aes_gcm_128) { u8 x; }\n}\n")


def test_an_unknown_codec_names_the_import_gap() -> None:
	"""Before this, the author was told the codec was undeclared and advised
	to write it out by hand: the import doing nothing, reported as their
	mistake."""
	text = rendered('import "std/codecs.situ";\n' + CODEC_USE)
	assert "import resolution is not implemented" in text
	# The gap first, the workaround second -- a reader has to be able to tell
	# "not built yet" from "you wrote it wrong".
	assert text.index("import resolution") < text.index("declare it with")


def test_an_unknown_codec_without_an_import_says_nothing_about_imports() -> None:
	"""The control. A schema with no import has an ordinary typo, and a note
	about unimplemented resolution would send the reader somewhere useless."""
	text = rendered(CODEC_USE)
	assert "import resolution" not in text
	assert "declare it with" in text


def test_an_impl_naming_an_imported_codec_says_the_same() -> None:
	text = rendered('import "std/codecs.situ";\n' + BUFFER
	                + 'impl aes_gcm_128 extern "x";\nstruct b { u16 v; }\n')
	assert "import resolution is not implemented" in text
