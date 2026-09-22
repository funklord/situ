"""The byte-level contract, and what a change to it costs (section 19.3).

Every case here is one `situc diff` gets wrong, because the two answer
different questions and only one of them was ever described as answering this
one. A cost ordering is not a compatibility ordering, and the clearest proof
is that moving a field out of an authenticated region ranks as an improvement
by cost and is a vulnerability by any other reading.
"""

from __future__ import annotations

import pytest

from situc import wire
from situc.layout import solve
from situc.parser import parse_text
from situc.resolve import resolve

PREAMBLE = "target buffer;\nendian big;\nbit_order msb_first;\n"


def signature(body: str, preamble: str = PREAMBLE) -> str:
	schema   = parse_text(preamble + body)
	resolved = resolve(schema, solve(schema))
	return wire.render(schema, resolved, "unit.situ")


def verdict(old: str, new: str, preamble: str = PREAMBLE,
		new_preamble: str | None = None) -> wire.Verdict:
	return wire.compare(signature(old, preamble),
	                    signature(new, new_preamble or preamble))


def kinds(found: wire.Verdict) -> set[str]:
	return {finding.kind for finding in found.findings}


def detail(found: wire.Verdict) -> str:
	return " ".join(finding.detail for finding in found.findings)


# -- what the signature records ---------------------------------------------


def test_it_records_position_width_and_byte_order() -> None:
	"""The three things that decide what a byte means."""
	text = signature("struct s { u8 a; u16 b; }")

	assert "endian big" in text
	assert "@0x0000   1" in text
	assert "@0x0001   2" in text
	assert "big" in text


def test_it_records_what_a_tag_authenticates() -> None:
	"""Not derivable from the layout, and the most expensive thing to get
	wrong: both sides read the same values and the tag simply fails."""
	text = signature("struct s { u8 hop; authenticated r { u16 q; } "
	                 "tag u8[16] covers(r); }")

	assert "tag covers: q r" in text


def test_it_records_which_enum_values_are_admitted() -> None:
	"""Under `default = error` a receiver refuses anything else, so the set is
	part of the contract rather than a detail of the type."""
	text = signature("enum k : u8 { a = 1, b = 2 }\nstruct s { k v; }")

	assert "enum k : u8 unknown=error" in text
	assert "a=1 b=2" in text


def test_it_records_no_capabilities() -> None:
	"""The map is the other file. Mixing them would mean a wire signature that
	churns when something gets faster, which teaches people to stop reading
	the diff."""
	text = signature("struct s { u8 n; u8 body[n]; }")

	for axis in ("Shifting", "Unstable", "Sequential", "AtomicWord"):
		assert axis not in text


# -- what a change to it costs ----------------------------------------------


def test_a_byte_order_flip_is_breaking() -> None:
	"""Every byte in every message, with the structure untouched. `situc diff`
	reports "No capability change" for this."""
	found = verdict("struct s { u16 a; }", "struct s { u16 a; }",
	                new_preamble="target buffer;\nendian little;\n")

	assert found.breaking
	assert "big -> little" in detail(found)


def test_swapping_two_members_of_one_width_is_breaking() -> None:
	"""The case that convinced me the existing tool was the wrong one: nothing
	about the capability vectors changes, so `diff` exits 0."""
	found = verdict("struct s { u16 alpha; u16 beta; }",
	                "struct s { u16 beta; u16 alpha; }")

	assert found.breaking


def test_a_field_leaving_authentication_is_reported_as_such() -> None:
	"""`situc diff` calls this an improvement, and by its own ordering it is
	one: a field with no tag over it is cheaper to write. It is also a
	security regression, which is why coverage is its own category here."""
	found = verdict(
		"struct s { u8 hop; authenticated r { u16 q; u16 amount; } "
		"tag u8[16] covers(r); }",
		"struct s { u8 hop; authenticated r { u16 q; } u16 amount; "
		"tag u8[16] covers(r); }")

	assert found.breaking
	assert "coverage" in kinds(found)
	assert "no longer authenticates amount" in detail(found)


def test_a_rename_is_an_api_change_and_a_wire_non_event() -> None:
	"""Position carries identity in situ (section 4), so a name is not on the
	wire at all. The signature records names only so a diff can say which
	field moved."""
	found = verdict("struct s { u16 alpha; }", "struct s { u16 renamed; }")

	assert not found.breaking
	assert kinds(found) == {"api"}


def test_tightening_a_constraint_is_backward_only() -> None:
	"""A new receiver reads everything an old sender sends; an old receiver may
	refuse what a new sender sends. Naming the direction is the whole point --
	"compatible" without one means nothing."""
	found = verdict("struct s { u16 n; }", "struct s { u16 n [max = 100]; }")

	assert not found.breaking
	assert kinds(found) == {"backward"}


def test_loosening_a_constraint_is_forward_only() -> None:
	found = verdict("struct s { u16 n [max = 100]; }", "struct s { u16 n; }")

	assert not found.breaking
	assert kinds(found) == {"forward"}


def test_adding_an_enum_member_breaks_a_receiver_that_rejects_unknowns() -> None:
	"""`default = error` is the default (8.7), so this is the common case and
	the surprising one: adding a value is free in most schema languages and is
	a break here, because the old receiver was written to refuse it."""
	found = verdict("enum k : u8 { a = 1 }\nstruct s { k v; }",
	                "enum k : u8 { a = 1, b = 2 }\nstruct s { k v; }")

	assert found.breaking
	assert "written to reject unknown values" in detail(found)


def test_adding_an_enum_member_is_free_when_unknowns_pass() -> None:
	"""The same edit, with one word different in the schema, and the opposite
	answer. The two look alike and could not differ more in a deployment."""
	found = verdict(
		"enum k : u8 { a = 1, default = pass }\nstruct s { k v; }",
		"enum k : u8 { a = 1, b = 2, default = pass }\nstruct s { k v; }")

	assert not found.breaking


def test_appending_a_member_is_not_assumed_safe() -> None:
	"""An old receiver sized its buffer from the old contract. Whether it
	ignores trailing bytes or rejects the message as overlong is a property of
	that receiver, which situ does not know -- so this is reported rather than
	waved through."""
	found = verdict("struct s { u16 a; }", "struct s { u16 a; u32 extra; }")

	assert not found.breaking
	assert "may reject the message as overlong" in detail(found)


def test_removing_a_member_is_breaking() -> None:
	found = verdict("struct s { u16 a; u32 b; }", "struct s { u16 a; }")

	assert found.breaking
	assert "an old sender still emits those bytes" in detail(found)


def test_an_unchanged_schema_says_so() -> None:
	found = verdict("struct s { u16 a; }", "struct s { u16 a; }")

	assert not found.findings
	assert wire.render_verdict(found) == "The wire contract is unchanged.\n"


# -- the classification that took two goes ----------------------------------


def test_a_changed_byte_order_is_not_a_relaxed_constraint() -> None:
	"""It first reported the per-field byte order as a constraint gained and
	one dropped, which put the same break twice in the reassuring half of the
	output. A fact about what the bytes *are* is not a fact about which of
	them are allowed."""
	found = verdict("struct s { u16 a; }", "struct s { u16 a [endian = little]; }")

	assert found.breaking
	assert "backward" not in kinds(found)
	assert "the same bytes now mean something else" in detail(found)


@pytest.mark.parametrize("edit", [
	'struct s { u8 a[] until "\\r\\n"; }',
	'struct s { u8 a[] until ","; }',
])
def test_a_changed_delimiter_is_breaking(edit: str) -> None:
	"""Where a member ends is the definition of what the next one is."""
	found = verdict('struct s { u8 a[] until ";"; }', edit)

	assert found.breaking


# -- one file, more than one version (section 19.4) -------------------------

V1 = "struct m [version = ver] { u8 ver; u16 length; }"
V2 = (
	"struct m [version = ver] { u8 ver; u16 length; "
	"u32 flags [since = 2]; }"
)


def test_appending_behind_a_since_is_provably_safe() -> None:
	"""The distinction the construct exists for. An old receiver reads the
	version field and knows the bytes are not its own -- which its own schema
	said before this edit existed."""
	found = verdict(V1, V2)

	assert not found.breaking
	assert "knows these bytes are not its own" in detail(found)


def test_appending_without_one_is_only_probably_safe() -> None:
	"""The same bytes in the same place, and situ cannot tell what an old
	receiver does with them."""
	found = verdict(V1, "struct m [version = ver] { u8 ver; u16 length; "
	                    "u32 flags; }")

	assert not found.breaking
	assert "may reject the message as overlong" in detail(found)


def test_the_signature_records_which_version_a_member_arrived_in() -> None:
	assert "since=2" in signature(V2)


# -- the constructs the signature was blind to ------------------------------
#
# Nine kinds of edit changed the generated parser and left this file
# byte-identical, so the committed-signature gate stayed green through all of
# them. Each test below is one of those edits, and the property asserted is
# the weakest one that would have caught it: the two schemas do not produce
# the same contract.

ARM_A = """
enum k : u8 { a = 1, b = 2 }
struct pa { u32 x; }
struct pb { u32 y; }
struct s {
	k v;
	variant body switch (v) {
		case k.a: pa first;
		case k.b: pb second;
		default: error;
	}
}
"""
ARM_B = ARM_A.replace("case k.a: pa first;", "case k.a: pb second;") \
             .replace("case k.b: pb second;\n\t\tdefault",
                      "case k.b: pa first;\n\t\tdefault")


def test_it_records_which_discriminant_value_selects_which_arm() -> None:
	"""`Placement.discriminant` and `arm_cases` are what every backend reads
	to emit the dispatch, and nothing here read either. The member line says
	where the variant starts and how wide it can be, which is the same
	sentence whichever arm each value picks."""
	text = signature(ARM_A)

	assert "body switch: v" in text
	assert "body case 1: first pa" in text
	assert "body case 2: second pb" in text
	assert "body case default: error" in text


def test_swapping_two_variant_arms_is_visible() -> None:
	"""Both arms are four bytes, so the member line is identical either way
	and the whole of the change is in the mapping. In `example/keystore` the
	same edit moved eighty-two lines of generated C and no byte of this
	file."""
	assert signature(ARM_A) != signature(ARM_B)

	found = verdict(ARM_A, ARM_B)
	assert found.breaking


TLV = """
varint_type pv { encoding = leb128; max_bits = 64; }
struct s {
	tlv fields (
		tag_type   = pv,
		tag_decode = { field = tag >> 3, wire = tag & 0x7 },
		tag_identity = field,
		value_size = switch (wire) {
			case 0: self_delimiting,
			case 1: 8,
			case 2: prefixed(pv),
			default: error,
		},
		duplicate_tags = allowed,
		known = { 1 : { name = who, wire = 0, type = pv } },
		unknown = preserve
	);
}
"""


def test_it_records_the_tlv_grammar() -> None:
	"""The whole contract of `example/protobuf` was one line reading
	`@0x0000 0.. tlv fields`: tag type, decode, identity, value sizes, the
	known tag map and both policies were recorded nowhere."""
	text = signature(TLV)

	assert "fields tlv tag: pv" in text
	assert "fields tlv decode field: tag >> 3" in text
	assert "fields tlv decode wire: tag & 0x7" in text
	assert "fields tlv identity: field" in text
	assert "fields tlv size 1: fixed 8" in text
	assert "fields tlv size 2: prefixed pv" in text
	assert "fields tlv size default: error" in text
	assert "fields tlv known 1: who wire=0 type=pv" in text
	assert "fields tlv duplicates: allowed" in text
	assert "fields tlv unknown: preserve" in text


@pytest.mark.parametrize("edit", [
	("known = { 1 :", "known = { 7 :"),		# the tag itself moves
	("tag >> 3", "tag >> 4"),			# every tag decodes differently
	("case 1: 8,", "case 1: 4,"),			# wire type 1 is half as wide
	("tag_identity = field", "tag_identity = wire"),	# `known` matches the other part
	("duplicate_tags = allowed", "duplicate_tags = error"),
	("unknown = preserve", "unknown = skip"),
])
def test_a_changed_tlv_grammar_is_visible(edit: tuple[str, str]) -> None:
	"""Each of these changes which bytes an item occupies, or which field an
	item is. None of them touched a member line."""
	assert signature(TLV) != signature(TLV.replace(*edit))


def test_it_records_where_a_located_member_sits() -> None:
	"""`_position` renders `~` here, and its comment says the member above is
	what fixes it. True of a member the data displaced and false of this one,
	which goes wherever `off` says however far that is from anything."""
	text = signature("struct s { u32 off; u32 other; u8 body[4] at off; }")

	assert "at=off" in text


def test_a_relocated_member_is_visible() -> None:
	"""`example/bmp`'s `pixels` is placed by `file.pixel_offset`. Pointing it
	at another field of the same width moves every byte of the image and left
	the signature alone."""
	found = verdict("struct s { u32 off; u32 other; u8 body[4] at off; }",
	                "struct s { u32 off; u32 other; u8 body[4] at other; }")

	assert found.breaking
	assert "the same bytes now mean something else" in detail(found)


WHILE = """
struct hdr { u8 kind; u8 rest; }
struct s { hdr chain[] while (kind == 0x11) max 4; u8 tail[remaining]; }
"""


def test_it_records_what_ends_a_run() -> None:
	"""`17` and not `0x11`, since 26.483: the signature records the value.

	This asserted the spelling until the day `sized-by=` was found publishing
	a const by name. What the test is for -- that a run's ending condition
	reaches the contract at all -- is unchanged, and the literal moved with
	the rule rather than the rule being bent to keep the literal.
	"""
	text = signature(WHILE)

	assert "while=kind==17" in text, "0x11 is 17; the contract records bytes"
	assert "while-max=4" in text


def test_a_changed_while_condition_is_visible() -> None:
	"""Nothing but the condition ends a run, so it is the boundary between
	this member and the next. `example/ipv6ext` changes eight lines of C for
	it and produced an identical signature."""
	edited = WHILE.replace("kind == 0x11", "kind == 0x22")

	assert signature(WHILE) != signature(edited)
	assert verdict(WHILE, edited).breaking


SELF_AS = ("struct s { authenticated r { u8 hop; "
           "checksum u8 sum[2] covers(r) [self_as = %s]; u16 q; } }")


def test_it_records_what_a_checksum_field_is_taken_as() -> None:
	"""`_coverage` records `tag_prefix` and says why: it is invisible in the
	structure. `self_as` is the same fact on the same construct."""
	assert "self_as=0" in signature(SELF_AS % "0")


def test_a_changed_self_as_is_visible() -> None:
	"""Two peers computing different sums over byte-identical messages, with
	every offset, width and name the same on both sides."""
	found = verdict(SELF_AS % "0", SELF_AS % "0xffff")

	assert found.breaking
	assert "the same bytes now mean something else" in detail(found)


VARINT = ("varint_type v { encoding = %s; max_bits = 64; }\n"
          "struct s { v n; u8 d[n]; }")


def test_it_records_a_varints_own_encoding() -> None:
	"""`varint=<name>` recorded the label on the change and not the change.
	`_directives` puts byte order first for this exact reason, and a varint's
	encoding is a byte order that the directive does not cover."""
	text = signature(VARINT % "be128")

	assert "varint v : be128 bits=64 bytes=10" in text


def test_changing_a_varints_encoding_is_breaking() -> None:
	"""`example/sqlite` reads every varint in the file backwards under this
	edit, and nothing about the structure says so."""
	found = verdict(VARINT % "be128", VARINT % "leb128")

	assert found.breaking
	assert "be128" in detail(found) and "leb128" in detail(found)


def test_it_records_a_computed_array_length() -> None:
	"""`sized_by` holds a path and holds nothing for arithmetic over one, so
	the commonest shape there is -- a length split across two fields --
	recorded no length at all."""
	text = signature("struct s { u8 hi; u8 lo; u8 body[hi * 256 + lo]; }")

	assert "sized-by=hi*256+lo" in text


def test_swapping_the_halves_of_a_length_is_visible() -> None:
	"""Every payload boundary in every message moves, and before this the two
	signatures were byte-identical."""
	found = verdict("struct s { u8 hi; u8 lo; u8 body[hi * 256 + lo]; }",
	                "struct s { u8 hi; u8 lo; u8 body[lo * 256 + hi]; }")

	assert found.breaking
	assert "the same bytes now mean something else" in detail(found)


def test_it_records_which_member_carries_the_version() -> None:
	assert "version=ver" in signature(
		"struct m [version = ver] { u8 ver; u8 rev; u16 length; }")


def test_moving_the_version_field_is_visible() -> None:
	"""Which field a receiver reads the version out of decides whether a
	`since` member's bytes are there at all -- and this file leans on it to
	call an appended member provably safe."""
	found = verdict("struct m [version = ver] { u8 ver; u8 rev; u16 n; }",
	                "struct m [version = rev] { u8 ver; u8 rev; u16 n; }")

	assert found.breaking


INDEXED = """
struct cell { u16 tag; u8 body; }
struct wider { u32 tag; }
struct s {
	u8  page;
	u16 n;
	u16 m;
	indexed(offset_type = u16, count = n, base = page) {
		cell cells[];
	}
}
"""


def test_it_records_how_an_indexed_regions_table_is_read() -> None:
	"""The tenth edit of this kind, found while fixing the other nine. An
	`indexed` region's member line says where the table starts and that its
	length is decided by the data, which is true of every offset table there
	could be: `IndexTable` was read by every backend and by nothing here."""
	text = signature(INDEXED)

	assert "cells index entry: 2" in text
	assert "cells index base: member page" in text

	# The other two the table holds, on the member line where they already
	# were: `_index` states neither, and these are why.
	assert "cell       cells  sized-by=n" in text


@pytest.mark.parametrize("edit", [
	("base = page", "base = region"),	# the region's own first byte
	("base = page", "base = message"),	# anywhere in the frame
	("base = page", "base = m"),		# a different member's first byte
])
def test_moving_an_indexed_regions_offset_base_is_visible(
		edit: tuple[str, str]) -> None:
	"""Decision 0024, and the edit the previous pass named and left: every
	cell in every SQLite page moves and no offset, width, name or fact on any
	member line changes."""
	assert signature(INDEXED) != signature(INDEXED.replace(*edit))

	found = verdict(INDEXED, INDEXED.replace(*edit))
	assert found.breaking
	assert "every element it reaches is somewhere else" in detail(found)


def test_changing_an_index_entry_width_is_visible() -> None:
	"""Each offset is read out of twice as many bytes and the table is twice
	as long, so every element moves. The member line records the table's lower
	bound, which is `count * entry` and is zero for a count the data gives --
	so both widths render as `0..`."""
	wide = INDEXED.replace("offset_type = u16", "offset_type = u32")
	assert signature(INDEXED) != signature(wide)

	found = verdict(INDEXED, wide)
	assert found.breaking
	assert "`cells index entry` 2 -> 4" in detail(found)


def test_an_index_count_from_another_field_is_visible() -> None:
	"""Not on an `index` line: the count is where a length always is in this
	file, and stating it twice would report one change under two headings."""
	other = INDEXED.replace("count = n", "count = m")
	assert signature(INDEXED) != signature(other)
	assert verdict(INDEXED, other).findings


def test_an_index_count_given_as_a_literal_is_visible() -> None:
	"""Where no field gives it there is no `sized-by=` either, and the table's
	extent is the whole of the record: `count * entry`, which the width column
	carries because the entry width is now recorded beside it."""
	four = INDEXED.replace("count = n", "count = 4")
	five = INDEXED.replace("count = n", "count = 5")

	assert "8.." in signature(four)
	assert signature(four) != signature(five)
	assert verdict(four, five).findings


def test_an_indexed_regions_element_type_is_visible() -> None:
	"""Also not on an `index` line: an element type is a type, and the member
	line's type column is what `_compare_member` compares."""
	swapped = INDEXED.replace("cell cells[]", "wider cells[]")
	assert signature(INDEXED) != signature(swapped)

	found = verdict(INDEXED, swapped)
	assert found.breaking
	assert "cell -> wider" in detail(found)


# -- the comparison, where it said the opposite of what it meant ------------


def test_a_changed_type_is_not_a_rename() -> None:
	"""`_compare_member` compared `line.split()[:2]`, so the type column was
	never looked at and `u16` -> `i16` matched the rename arm: "api-only ...
	renamed a -> a", in the half of the output that says nothing moved. The
	same bytes are a different number."""
	found = verdict("struct s { u16 a; }", "struct s { i16 a; }")

	assert found.breaking
	assert "api" not in kinds(found)
	assert "u16 -> i16" in detail(found)


def test_a_changed_constraint_value_is_one_finding() -> None:
	"""Decomposed into a gain and a loss it printed "0 breaking, 2
	compatible" under two headings that each asserted the direction the other
	denied -- and for `must_eq` both were wrong, there being no message the
	two builds both accept."""
	found = verdict("struct s { u16 n [must_eq = 1]; }",
	                "struct s { u16 n [must_eq = 2]; }")

	assert len(found.findings) == 1
	assert found.breaking
	assert "no message satisfies both" in detail(found)


def test_a_narrowed_bound_is_a_tightening_and_says_so() -> None:
	found = verdict("struct s { u16 n [max = 100]; }",
	                "struct s { u16 n [max = 50]; }")

	assert len(found.findings) == 1
	assert kinds(found) == {"backward"}
	assert "a tightening" in detail(found)


def test_a_widened_bound_is_a_loosening_and_says_so() -> None:
	found = verdict("struct s { u16 n [max = 100]; }",
	                "struct s { u16 n [max = 200]; }")

	assert len(found.findings) == 1
	assert kinds(found) == {"forward"}
	assert "a loosening" in detail(found)


def test_declaring_a_version_field_moves_no_bytes() -> None:
	"""Gaining or losing the declaration is not the break that moving it is:
	`[since]` with no version field is refused outright (19.4), so a struct
	that gained one had no optional bytes for it to gate. Reported as breaking
	it would have been the same break counted twice, once here and once per
	member that went."""
	found = verdict("struct m { u8 ver; u16 n; }",
	                "struct m [version = ver] { u8 ver; u16 n; }")

	assert not found.breaking
	assert kinds(found) == {"api"}


@pytest.mark.parametrize("body", [ARM_A, TLV, INDEXED, WHILE, SELF_AS % "0"])
def test_the_lines_beneath_the_members_are_not_members(body: str) -> None:
	"""The positional comparison walks the member lines by index, so a line
	that is not a member and is counted as one slides every member after it
	and reports the slide as a break. A schema compared with itself is the
	test that says the two kinds are told apart."""
	assert not wire.compare(signature(body), signature(body)).findings


def test_it_records_that_a_discriminant_is_peeked() -> None:
	"""`peek` is where the ARM begins, which is as sharp an interpretation
	fact as byte order.

	A peeked member is read and not spent (0057), so the arm the variant
	selects starts at the discriminant rather than after it. The signature
	named the discriminant and said nothing about this, so a peer reading it
	placed every arm one member late -- a wrong layout, not a narrower one.
	Found in `example/json`, whose seven arms all moved.
	"""
	body = ("struct a { u8 x; }\nstruct b { u8 x; u8 y; }\n"
	        "struct s {\n\tpeek u8  kind;\n"
	        "\tvariant held switch (kind) {\n"
	        "\t\tcase 1: a  as_a;\n\t\tdefault: b  as_b;\n\t}\n}\n")
	assert "kind  peek" in signature(body)
	assert "peek" not in signature(body.replace("peek u8", "u8"))


def test_dropping_peek_is_breaking() -> None:
	"""And the cost is stated, because the same bytes become a different
	message: every arm shifts by the discriminant's width.

	The DETAIL is asserted and not just the kind, because dropping `peek`
	also moves `held` from 0x00 to 0x01 and that alone makes the verdict
	breaking. Written the loose way this passed with the `peek` fact
	deleted from the signature -- a test carried by a second finding it was
	not about, which is the failure it was written to prevent one level up.
	"""
	arms = ("struct a { u8 x; }\nstruct b { u8 x; u8 y; }\n"
	        "struct s {\n\t%s u8  kind;\n"
	        "\tvariant held switch (kind) {\n"
	        "\t\tcase 1: a  as_a;\n\t\tdefault: b  as_b;\n\t}\n}\n")
	found = verdict(arms % "peek", arms % "")

	assert "breaking" in kinds(found)
	assert "kind: peek -> nothing" in detail(found)


def test_it_records_that_a_text_number_is_scaled() -> None:
	"""`scaled` and `decimal` are both base ten and do not read the same
	bytes: `12.5` is a number under one and malformed under the other, and
	what comes back is a pair rather than an integer (0056).

	`radix=10` alone said neither, so two members that parse differently had
	one signature -- and a peer implementing from it would refuse half the
	documents `example/json` accepts.
	"""
	scaled  = "struct s { scaled i64  v  until \",\"; }\n"
	decimal = "struct s { decimal i64  v  until \",\"; }\n"

	assert "radix=10 scaled" in signature(scaled)
	assert "scaled" not in signature(decimal)
	assert "breaking" in kinds(verdict(scaled, decimal))


# -- a scoped text encoding (0058) ------------------------------------------


SCOPED = ("tokens verb { helo = \"HELO\", ehlo = \"EHLO\" }\n"
          "struct s [encoding = utf8] {\n"
          "\tu8    name[16];\n"
          "\tu8    raw[4]  [encoding = ascii];\n"
          "\tverb  which   until \" \";\n"
          "\tu16   sequence;\n"
          "}\n")


def test_a_struct_states_the_encoding_its_text_members_hold() -> None:
	"""`endian` and `bit_order` have been "a file-level directive,
	overridable per struct, overridable per field" since section 8.3 was
	written. Encoding had a file directive meaning a different thing and a
	per-field attribute, with nothing between them (0058).

	Three behaviours in one signature, because they are one rule: the scope
	reaches a member that states nothing, a member that states its own wins,
	and a member that could not carry one by hand does not get one.
	"""
	found = signature(SCOPED)

	assert "name  big encoding=utf8" in found
	assert "raw  big encoding=ascii" in found		# its own wins
	assert "sequence  big\n" in found			# a scalar takes none

	# And "its own wins" by NOT BEING GIVEN a second one, which the rendered
	# line cannot show: every consumer takes the first `encoding` attribute,
	# so a member carrying both still renders its own and the signature
	# looks right. Two encodings on one placement is a thing no schema can
	# write, so the scope must not produce it.
	schema   = parse_text(PREAMBLE + SCOPED, path="s.situ")
	resolved = resolve(schema, solve(schema))
	for struct in resolved.structs.values():
		for entry in struct.entries:
			stated = [one for one in entry.placement.attrs
			          if one.name == "encoding"]
			assert len(stated) <= 1, (
				f"{entry.placement.path} carries {len(stated)} encodings")


def test_a_scoped_encoding_reaches_a_token_set_member() -> None:
	"""A token set's member has no brackets and is still a delimited run of
	bytes -- "its type carries the run-ness that `u8 x[] until \\" \\"` writes
	in the brackets" -- so `wellformed` lets one carry an encoding by hand
	and the scope has to reach it for the same reason."""
	assert "verb       which  encoding=utf8" in signature(SCOPED)


def test_a_scope_only_produces_attributes_a_schema_could_write() -> None:
	"""The invariant that makes the scope safe, and it is not obvious.

	`wellformed` runs BEFORE the layout, and the scope resolves onto the
	member during it -- so nothing re-checks what the scope produced. A
	predicate wider than `wellformed`'s would put a member in the image that
	a hand-written schema is refused for.

	Measured: the first version gave `u16 sequence` `encoding=utf8`, which
	writing by hand is refused for -- "a byte array or a delimited run -- a
	single scalar has no encoding to state". So this asserts the two agree,
	by writing out what the scope produced and checking `wellformed` accepts
	it.
	"""
	from situc import ast, wellformed
	from situc.layout import _takes_an_encoding
	from situc.parser import parse_text as parse

	schema = parse(PREAMBLE + SCOPED, path="s.situ")
	tokens = {decl.name: decl for decl in schema.token_sets()}

	scoped:  list[ast.Member] = []
	refused: list[ast.Member] = []
	for struct in schema.structs():
		for member in struct.members:
			if not hasattr(member, "attrs"):
				continue
			(scoped if _takes_an_encoding(member, tokens)
			 else refused).append(member)

	assert scoped and refused, "both halves need a case"

	# Every member the scope would reach must survive `wellformed` carrying
	# an encoding it did not write -- which is what the scope gives it.
	for member in scoped:
		one = PREAMBLE + (
			"tokens verb { helo = \"HELO\", ehlo = \"EHLO\" }\n"
			"struct one {\n\t"
			+ _redeclare(member) + "\n}\n")
		wellformed.check(parse(one, path="one.situ"))


def _redeclare(member: object) -> str:
	"""The member as a schema line, with an explicit `[encoding = utf8]`.

	Rendered rather than unparsed because what is under test is whether
	`wellformed` accepts the ATTRIBUTE on that shape of member, and the
	shapes are three.
	"""
	name = getattr(member, "name", "x")
	if getattr(member, "until", None) is not None:
		return f"verb  {name}  until \" \" [encoding = utf8];"
	array = getattr(member, "array", None)
	size  = getattr(getattr(array, "size", None), "value", 4)
	return f"u8  {name}[{size}]  [encoding = utf8];"


# ---------------------------------------------------------------------------
# An argument is part of the contract (0050)
# ---------------------------------------------------------------------------

PARAMETERISED = """target buffer;
endian big;
bit_order msb_first;

struct frame {
	parameter u8 n [stream];
	u8        body[n];
	u8        tail;
}
"""


def test_the_signature_names_a_parameter_rather_than_placing_it() -> None:
	"""0048's reason, applied to 0050's construct.

	Two peers that disagree about an argument disagree about the bytes, so
	a contract that does not mention it cannot be checked. And it is NAMED
	rather than placed: the signature said `@0x0000 1 u8 n` for a member
	occupying nothing -- the same offset as the member after it -- which is
	the one description that must not make a false claim about where the
	bytes are making one.
	"""
	rendered = signature(PARAMETERISED, preamble="")

	row = next(one for one in rendered.splitlines()
	           if one.strip().startswith("parameter"))
	assert row.split() == ["parameter", "-", "u8", "n", "[stream]"], row

	# The member it sizes still carries its own position, so the branch has
	# not swallowed the ordinary case.
	body = next(one for one in rendered.splitlines()
	            if one.strip().startswith("@") and "body" in one)
	assert "sized-by=n" in body


def test_stream_is_part_of_the_contract() -> None:
	"""It says the argument may decide POSITION rather than only meaning, so
	a peer reading the signature learns whether the layout below moves with
	the argument. A `[stream]` dropped here would leave two peers agreeing
	about a name and disagreeing about what it can do."""
	plain = signature(PARAMETERISED.replace(
		"parameter u8 n [stream];\n\tu8        body[n];",
		"parameter u8 n;\n\tu8        body[4];"), preamble="")

	row = next(one for one in plain.splitlines()
	           if one.strip().startswith("parameter"))
	assert "[stream]" not in row, row


# -- the value, not the author's spelling of it (26.474) --------------------


def test_a_constant_is_recorded_as_its_value() -> None:
	"""A receiver that is already deployed cannot resolve `TAG`.

	The signature recorded `must_eq=TAG` while the generated C enforced
	`!= 4`, so the file was recording something other than what is
	enforced -- which 0041 calls the defect rather than a lesser form of
	the truth. The consequence was measurable: changing the constant
	moved the byte every peer checks and `situc wire --check` reported
	"is current", where the identical change written as a literal
	reported a breaking one.
	"""
	shown = signature("const TAG = 4;\n\nstruct s { u8 tag [must_eq = TAG]; u8 r; }",
	                  preamble="endian big;\n\n")
	assert "must_eq=4" in shown
	assert "must_eq=TAG" not in shown, (
		"the signature published a name that exists only in this schema")


def test_a_field_reference_keeps_its_name() -> None:
	"""The control, and the reason this is not a blanket flattening.

	A field is a pointer INTO the byte stream: a peer locates `n` and
	reads the length from it, so the name is the enforceable fact and a
	number would destroy it -- there is no one number, the value differs
	per message. `evaluate` raises for a field reference, which is what
	makes it the discriminator rather than a second rule to keep in step.
	"""
	shown = signature(
		"struct s { u8 n; u8 body[n]; u8 limit; u8 value [max = limit]; }",
		preamble="endian big;\n\n")
	assert "sized-by=n" in shown, "a field-sized run lost the field's name"
	assert "max=limit" in shown, "a field reference was flattened to a number"


def test_an_encoding_keeps_its_spelling() -> None:
	"""The second control. `[encoding = utf8]` is checked against
	`TEXT_ENCODINGS` by NAME and is not a number, so a schema declaring
	`const ascii = 7;` would otherwise publish `encoding=7` -- a value no
	peer can act on, naming a different encoding from the one enforced.
	That is why the resolution is restricted to `VALUE_ATTRS` rather than
	applied to every wire attribute."""
	shown = signature(
		"const ascii = 7;\n\nstruct s { u8 n; u8 body[n] [encoding = ascii]; }",
		preamble="endian big;\n\n")
	assert "encoding=ascii" in shown
	assert "encoding=7" not in shown


def test_a_remaining_run_is_not_confused_with_a_constant() -> None:
	"""`const remaining = 4;` is legal and coexists with the `[remaining]`
	keyword, which wins the layout -- the member is genuinely unbounded
	while `env.consts` holds a 4. Resolving the name would publish a
	length the format does not have, so the keyword is guarded before the
	lookup."""
	shown = signature("const remaining = 4;\n\nstruct s { u8 a; u8 body[remaining]; }",
	                  preamble="endian big;\n\n")
	assert "sized-by=remaining" in shown
	assert "sized-by=4" not in shown, (
		"the `[remaining]` keyword was resolved as though it were the const")


def test_two_spellings_of_one_value_compare_equal() -> None:
	"""The mirror defect, and the one that cried wolf. `must_eq = 0x0800`
	and `must_eq = 2048` are the same byte, and the signature reported a
	BREAKING change between them because it compared spellings. Canonical
	form removes it."""
	assert signature("struct s { u16 t [must_eq = 0x0800]; u8 r; }",
	                 preamble="endian big;\n\n") == \
	       signature("struct s { u16 t [must_eq = 2048]; u8 r; }",
	                 preamble="endian big;\n\n")


# -- the signature records values, not spellings (26.483) --------------------

#: cpio's shape, reduced. The pad run is `align_up(PAD + n, 4) - (PAD + n)`,
#: which is 0..3 bytes wide for EVERY value of `PAD` -- so the width column
#: cannot see the constant move and only `sized-by=` can. A fixture whose
#: width changed would pass against the unfixed compiler for the wrong
#: reason, which is what the first draft of this test did.
PADDED = ("const PAD = %d;\n"
	"struct s { u16 n; u8 body[n];\n"
	"  reserved u8 [align_up(PAD + n, 4) - (PAD + n)];\n"
	"  u8 tail; }\n")

#: A `while` predicate is the other expression the signature publishes.
BEATS = ("struct beat { u8 kind; u8 payload; }\n"
	"struct beats { beat pulse[] while (kind == %s) max 6; }\n")


def test_redefining_a_const_a_size_expression_uses_is_breaking() -> None:
	"""Silent on a real change, which is the direction that costs.

	`sized-by=` published the author's spelling, so the constant reached the
	contract by NAME and redefining it moved the padding run in every cpio
	entry while `wire --check` reported the signature current. 26.474 fixed
	this for a BARE const in `_sized_by`; that path takes a plain string and
	this one an expression, and only the string had been taught to resolve.
	"""
	found = verdict(PADDED % 110, PADDED % 200)

	assert found.breaking
	assert "110" in detail(found) and "200" in detail(found), (
		"the finding has to name both values, not the const")


def test_respelling_a_literal_is_not_a_wire_change() -> None:
	"""And loud on no change, which is the same defect pointed the other way.

	`expr_to_source` prints `IntLiteral.text`, so `0x33` and `51` rendered
	differently and `wire --check` called identical bytes "not backward
	compatible". A contract nobody can trust to be quiet is one people stop
	reading.
	"""
	assert not verdict(BEATS % "0x33", BEATS % "51").findings


def test_a_character_literal_is_recorded_as_the_byte_it_is() -> None:
	"""`','` and `44` are one fact, and the attributes already agreed.

	`arp.situ` writes `[must_eq = 0x0800]` and its committed signature
	records `must_eq=2048`, because 26.474 made the attribute path evaluate.
	A `while` predicate comparing against `','` had stayed a spelling, so the
	two halves of one artifact disagreed about what a literal is.
	"""
	assert not verdict(BEATS % "','", BEATS % "44").findings


#: Four spellings of sixteen the lexer already normalises -- it strips `_`
#: and reads the radix -- and which the signature then un-normalised. Nobody
#: would go looking for these; they were found by asking what else can be
#: respelled, rather than by reading the two instances already known.
SIXTEEN = ("0x10", "0b10000", "1_6", "016")


@pytest.mark.parametrize("spelling", SIXTEEN)
def test_a_size_expression_records_the_number_not_its_radix(
		spelling: str) -> None:
	"""The case a user actually meets, since hex lives in size expressions.

	`while=` had the instance that was found, and `sized-by=` had the same
	defect and no reproduction. Testing only the found one is how a class
	gets half-fixed -- which is what 26.474 did to this very file.
	"""
	assert not verdict(f"struct s {{ u16 n; u8 a[{spelling} + n]; }}",
	                   "struct s { u16 n; u8 a[16 + n]; }").findings


def test_an_enum_member_resolves_inside_an_expression_too() -> None:
	"""`_sized_by` resolved a dotted arm and `_valued` did not, so one
	artifact gave two answers: `u8 a[kv.alpha]` published `17` through the
	string path while `u8 a[kv.alpha + n]` published the name. Same defect
	one code path along as the entry this test belongs to, found inside the
	fix for it.
	"""
	held = ("enum kv : u8 { alpha = 17, beta = 18, }\n"
		"struct s { u8 n; u8 a[%s]; u8 t; }\n")

	assert not verdict(held % "kv.alpha + n", held % "17 + n").findings


def test_the_case_of_a_hex_digit_is_not_a_wire_change() -> None:
	"""`0xFF` and `0xff`, which `0X10` is not -- the lexer refuses a capital
	radix marker and accepts either case in the digits, so this is the case
	variation the language actually has. Measured rather than assumed: the
	first draft of the row above tried `0X10` and was refused.
	"""
	assert not verdict("struct s { u16 n; u8 a[0xFF + n]; }",
	                   "struct s { u16 n; u8 a[0xff + n]; }").findings


def test_a_const_sharing_a_field_name_does_not_eat_the_field() -> None:
	"""The substitution is scope-aware, and the first version was not.

	A bare name in a `while` predicate is a field of the element struct --
	`check_repeats` refuses anything else -- so a const that happens to share
	the name is not what the predicate means. Substituting on `name in
	env.consts` turned `while (kind == 0x33)` into `while=153==51`: two
	constants compared to each other, published as the contract, with the
	field reference gone.

	Legal to write, and zero schemas in the corpus do it, which is exactly
	why nothing caught it. A size expression is the opposite case and the
	compiler settles it rather than this file: `const n = 99` beside a field
	`n` emits `SITU_S_A_COUNT 100u`, so there the const wins.
	"""
	collides = ("const kind = 0x99;\n"
		"struct beat { u8 kind; u8 payload; }\n"
		"struct beats { beat pulse[] while (kind == %s) max 6; }\n")

	text = signature(collides % "0x33")

	assert "while=kind==51" in text, "the field reference has to survive"
	assert "153" not in text, "the const is not what the predicate names"
	assert not verdict(collides % "0x33", collides % "51").findings


def test_where_a_member_is_read_from_records_values_too() -> None:
	"""`at=` is the third leak and the one a `_shown` grep cannot find.

	The spelling arrives on `placement.located` under an ordinary name, so
	it was invisible to the search that found the other two. A const in it
	moves WHERE the member is read from -- a stronger claim than how wide it
	is -- and it reached the contract by name. It was also the one fact
	rendered unsquashed, putting the author's spaces inside a line whose
	facts are space-delimited.
	"""
	held = ("const AT_BIAS = %d;\n"
		"struct s { u32 spot; u8 payload[2] at spot + AT_BIAS; }\n")

	text = signature(held % 3)

	assert "at=spot+3" in text, "resolved, and squashed"
	assert "AT_BIAS" not in text
	assert verdict(held % 3, held % 5).breaking
