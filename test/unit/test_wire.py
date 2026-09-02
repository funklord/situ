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
	text = signature(WHILE)

	assert "while=kind==0x11" in text
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


@pytest.mark.parametrize("body", [ARM_A, TLV, WHILE, SELF_AS % "0"])
def test_the_lines_beneath_the_members_are_not_members(body: str) -> None:
	"""The positional comparison walks the member lines by index, so a line
	that is not a member and is counted as one slides every member after it
	and reports the slide as a break. A schema compared with itself is the
	test that says the two kinds are told apart."""
	assert not wire.compare(signature(body), signature(body)).findings
