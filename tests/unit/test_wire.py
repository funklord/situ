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
