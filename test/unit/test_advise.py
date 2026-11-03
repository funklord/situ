"""The advisor: the suggestion catalog, its cost model, and the revision diff.

Section 18 calls this the differentiator, and the reason is the cost column.
"Move variable-length fields to the end" is folklore; "moving `opts` costs
nothing and returns two members to absolute addressing" is advice. So the tests
below check the numbers, not just that a suggestion fired.

The diff half is the same argument at a different timescale: a field that goes
from `InPlaceFixed` to `Shifting` is one line of schema and a fleet-wide
performance change, and the point of running this in review is that the second
fact arrives with the first.
"""

from __future__ import annotations

import pathlib
import re

import pytest

from situc import advise, revision
from situc.capability import Axis, Value
from situc.layout import solve
from situc.parser import parse_text
from situc.resolve import ResolvedSchema, resolve
from situc.traverse import own_entries

PREAMBLE = "endian big;\n"


def build(body: str) -> ResolvedSchema:
	schema = parse_text(PREAMBLE + body)
	return resolve(schema, solve(schema))


def suggestions(body: str) -> list[advise.Suggestion]:
	return advise.suggest(build(body))


def by_rule(body: str, rule: str) -> list[advise.Suggestion]:
	return [item for item in suggestions(body) if item.rule == rule]


def only(body: str, rule: str) -> advise.Suggestion:
	found = by_rule(body, rule)
	assert len(found) == 1, [item.rule for item in suggestions(body)]
	return found[0]


# -- the reordering suggestion, which section 26.9 names ---------------------


# Two arms of different sizes, and nothing after the variant: the padding buys
# a fixed size for `s` itself rather than an absolute offset for anything.
UNEQUAL_ARMS = """enum k : u8 { a = 1, b = 2, }
struct small { u16 x; }
struct large { u8 y[10]; }
struct s {
	k kind;
	variant body switch (kind) { case k.a: small p; case k.b: large q; }
}
"""


BADLY_ORDERED = """struct bad {
	u16 length [max = 1500];
	u8  opts[length];
	u32 seq;
	u16 kind;
	u8  tail;
}
"""


def test_a_badly_ordered_schema_gets_the_reordering_suggestion() -> None:
	found = only(BADLY_ORDERED, "move-dynamic-to-tail")

	assert found.subject == "bad.opts"
	assert "move this variable-length member after the fixed ones" in found.summary
	assert "3 members" in found.detail
	assert "`seq`, `kind`, `tail`" in found.detail


def test_the_reordering_suggestion_costs_nothing_and_says_so() -> None:
	"""Reordering moves no bytes. Printing that is the point, not omitting it.

	**The joining word is load-bearing and is asserted here on purpose.** The
	number counts bytes on the wire and nothing else, so a reordering is
	"nothing" however many peers it breaks -- and the basis is where that one
	invisible cost has to be named. It read "no bytes move, *and* every
	deployed peer reads the old order", which puts a caveat in the
	grammatical position of a second reason: after the word "nothing", a
	reader takes it as reassurance, which is the opposite of what it says.
	"But" is the whole fix, and a later edit that softens it back to "and"
	should fail here rather than pass quietly.
	"""
	found = only(BADLY_ORDERED, "move-dynamic-to-tail")

	assert found.cost.typical == 0
	assert found.cost.worst == 0
	assert found.cost.render() == ("cost: nothing (no bytes move, but a peer "
	                               "already speaking this format reads the "
	                               "old order)")
	assert ", but " in found.cost.basis, (
		f"the caveat has to read as one: {found.cost.basis!r}")
	assert "3 members return to AbsoluteStatic" in found.yields


def test_the_reordering_suggestion_ranks_first() -> None:
	"""18.2 calls it the highest-yield single change, so it heads the list."""
	found = suggestions(BADLY_ORDERED + """
	struct also_bad {
		u16 n [max = 4];
		u8  rest[n];
		u32 after;
	}
	""")
	assert found[0].rule == "move-dynamic-to-tail"
	# And within the rule, the instance that recovers more members comes first.
	assert found[0].subject == "bad.opts"


def test_a_well_ordered_schema_gets_no_reordering_suggestion() -> None:
	"""It asked the whole catalog until the catalog had something to say.

	The schema is well ordered -- its one variable-length member is already
	last -- and that is all this test was ever about, sitting in the
	reordering section. `pin-a-driven-length` arrived at 26.824 and has a
	second thing to say about it, correctly: pinning `n` makes `fine` a
	fixed-size struct, which it is not. So the assertion narrows to the rule
	the test is named for, and states what else now fires rather than
	dropping the whole-catalog claim.
	"""
	body = "struct fine { u32 seq; u16 n [max = 8]; u8 body[n]; }"

	assert by_rule(body, "move-dynamic-to-tail") == []
	assert [item.rule for item in suggestions(body)] == ["pin-a-driven-length"]


def test_a_tag_is_not_something_to_move_forward() -> None:
	"""It authenticates what precedes it, so advice to move it cannot be taken."""
	found = by_rule("""codec aead {
		length_preserving; seekable = linear; granularity = byte;
		authenticated; invertible; deterministic;
	}
	struct framed {
		u16 n [max = 32];
		sealed(aead) { u8 body[n]; }
		tag u8[16];
	}
	""", "move-dynamic-to-tail")
	assert found == []


# -- the rest of the catalog ------------------------------------------------


def test_a_varint_is_priced_against_a_fixed_width() -> None:
	"""18.2's worked case: a varint costs two bytes across most of its range."""
	found = only("""varint_type v { encoding = leb128; max_bits = 16; minimal; }
	struct s { v n; }
	""", "varint-to-fixed")

	assert "replace the varint with a fixed `u32`" in found.summary
	assert "spends 1 to 3 bytes" in found.detail
	# Typical at the widest encoding: three bytes against four, one byte.
	assert found.cost.typical == 1
	# Worst at the narrowest, which is the frame that pays most: one against
	# four. Both numbers were the typical one, so the largest cost in the
	# range was reported under the name of the smallest.
	assert found.cost.worst == 3


def test_an_unbounded_region_is_priced_as_unknown() -> None:
	"""Reporting zero would be a lie in the cheapest possible direction."""
	found = only("struct s { u8 rest[remaining]; }", "bound-unbounded")

	assert found.cost.unknown
	assert found.cost.render() == ("cost: depends on the bound chosen (the "
	                               "bound is the cost, and it is unchosen)")
	assert "statically allocatable" in found.yields
	assert "instead of `remaining`" in found.detail


def test_unequal_variant_arms_are_priced_as_padding() -> None:
	found = only(UNEQUAL_ARMS, "equalize-variant-arms")

	assert found.cost.worst == 8			# 10 - 2
	assert found.cost.typical == 4			# averaged over the arms
	assert "typical" in found.cost.render() and "worst case" in found.cost.render()


def test_every_suggestion_prints_the_basis_it_was_costed_on() -> None:
	"""`Cost.basis` says where the number came from. Eight rules supplied one
	and `render` dropped every one of them, so the advisor printed "cost: 31
	bytes typical" with no statement of what was being counted -- and a number
	whose derivation is not shown is a number a reader has to trust."""
	seen = [item for body in (BADLY_ORDERED, UNEQUAL_ARMS,
	                          "struct s { u8 rest[remaining]; }")
	        for item in suggestions(body)]

	assert seen
	for item in seen:
		assert item.cost.basis, f"{item.rule} costs without saying on what"
		assert f"({item.cost.basis})" in item.cost.render()


def test_counts_in_a_suggestion_agree_with_the_verb_beside_them() -> None:
	"""The advisor is prose a person reads before editing a schema.

	`_count` spelled the noun and every verb beside it was written once, in
	the plural, so a single member read "1 member behind it are Dynamic" and
	"1 member return to AbsoluteStatic, and their accessors". One member
	behind a dynamic one is the common case, not the corner.

	The test that covered this asserted `yields.startswith("1 member
	return")`, which a corrected "returns" satisfies just as well: a prefix
	cannot see the end of the word it is checking, so it pinned the broken
	spelling and would have passed the fix without noticing either.
	"""
	one = only("struct s { u8 n; u8 body[n]; u16 tail; }", "move-dynamic-to-tail")
	assert "1 member behind it is Dynamic" in one.detail
	assert one.yields == ("1 member returns to AbsoluteStatic, and its "
	                      "accessor to base + K")

	two = only("struct s { u8 n; u8 body[n]; u16 tail; u16 more; }",
	           "move-dynamic-to-tail")
	assert "2 members behind it are Dynamic" in two.detail
	assert two.yields == ("2 members return to AbsoluteStatic, and their "
	                      "accessors to base + K")

	# The other three sentences that carry a count.
	varint = ("varint_type v { encoding = leb128; max_bits = 16; minimal; }\n"
	          "struct s { v n; u32 seq; }\n")
	assert "1 member after the varint keeps absolute offsets" in \
		only(varint, "varint-to-fixed").yields
	assert "1 covered field is writable in place" in \
		only("struct s { authenticated { u32 seq; } tag u8[16]; }",
		     "uncover-mutable-field").detail

	assert advise._bytes(1) == "1 byte"
	assert advise._bytes(-1) == "1 byte saved"
	assert advise._bytes(2) == "2 bytes"


def test_equalizing_is_ranked_by_what_it_buys_not_by_what_it_costs() -> None:
	"""`weight` orders the catalog, and this rule handed it the padding: the
	more absurd the equalization, the higher it sorted. `example/netlink`'s
	`default: opaque` arm prices at four gigabytes and outranked every useful
	suggestion in the file."""
	found = only(UNEQUAL_ARMS.rstrip()[:-1] + "\tu32 after;\n}\n",
	             "equalize-variant-arms")

	assert found.cost.worst == 8
	assert found.weight < found.cost.worst


def test_equalizing_a_trailing_variant_claims_only_what_it_delivers() -> None:
	"""Nothing follows the variant, so no member regains an absolute offset --
	but the struct holding it becomes a fixed size, which is the whole reason
	to pay the padding. Reporting "0 member(s) after the variant keep absolute
	offsets" states the change is worthless; it is not."""
	trailing = UNEQUAL_ARMS
	found    = only(trailing, "equalize-variant-arms")

	assert "`s` itself becomes a fixed size" in found.yields
	assert "member" not in found.yields

	# And taking it does that. The yield is measured, not asserted.
	assert not build(trailing).structs["s"].layout.is_fixed_size
	equalized = trailing.replace("switch (kind) {", "switch (kind) [equalize] {")
	assert build(equalized).structs["s"].layout.is_fixed_size


def test_an_alignment_hole_is_free_to_fill() -> None:
	found = by_rule("struct s { u8 a; reserved u8[3]; u8 b; u32 wide; }",
	                "fill-alignment-holes")

	assert found and found[0].cost.typical == 0
	assert "already spends 3 bytes on reserved padding" in found[0].detail


#: The same struct read two ways. A multi-byte scalar sitting off its
#: boundary is what the alignment rule fires on, and the byte order is what
#: decides whether reordering it buys anything.
MISALIGNED = """struct s {
	u8       flag;
	u32      counter;
	reserved u8[3];
	u16      seq;
}
"""


def test_the_alignment_trade_names_the_byte_order_that_undoes_it() -> None:
	"""On a little-endian host a big-endian scalar is read through a swap
	whatever the offset, so the value is never the memory and reordering it
	buys much less than the suggestion implies. `suggestion/fuzznet.md` made
	the point about `example/packet`, whose three reserved bytes are widely
	copied precisely because the example is good.

	Keyed on the declared byte order, and not on `repr`: an unaligned scalar
	is `ValueConverted` *because* it is unaligned, so keying on that attached
	the caveat to every finding this rule makes -- including the
	little-endian ones, where the reorder does buy the access. It was
	measured firing on both before it was keyed on the order instead.
	"""
	big = advise.suggest(build(MISALIGNED))
	swapped = [item for item in big if item.rule == "fill-alignment-holes"]
	assert swapped, [item.rule for item in big]
	assert "read through a swap" in swapped[0].yields

	little = advise.suggest(
		resolve(parse_text("endian little;\n" + MISALIGNED),
		        solve(parse_text("endian little;\n" + MISALIGNED))))
	native = [item for item in little if item.rule == "fill-alignment-holes"]
	assert native, [item.rule for item in little]
	assert "read through a swap" not in native[0].yields, (
		"a little-endian field is the memory, so the reorder does buy the "
		"aligned access and the caveat must not appear")


def test_a_tlv_region_is_offered_the_positional_trade() -> None:
	found = only("struct s { tlv opts (tag_type = u8); }", "tlv-to-positional")
	assert "O(1) access" in found.yields


def test_covered_mutable_fields_are_one_suggestion_with_a_price() -> None:
	"""One per tag, not one per field.

	18.2's trigger is *frequent* mutation and the schema does not say which
	fields those are. What the compiler can supply is the price of a write.
	"""
	found = only("""struct s {
		authenticated { u32 seq; u32 other; }
		tag u8[16];
	}
	""", "uncover-mutable-field")

	assert found.subject == "s.tag"
	assert "2 covered fields are writable" in found.detail
	assert "recomputation over 8 bytes" in found.detail


def test_the_recomputation_price_names_the_access_pattern_it_assumes() -> None:
	"""A cost per *write* assumes the frame is rewritten after it is built.

	A frame assembled once and sent writes every covered field before the
	tag exists and pays one recomputation between them, not one each -- so
	the suggestion is conditional, and `suggestion/fuzznet.md` read it as
	unconditional because nothing said otherwise. A ranked, costed
	suggestion whose cost model does not match the usage is the shape of a
	gate that cannot model what it checks: whoever knows enough ignores it,
	and whoever does not obeys it.

	The condition is in the text now, so it cannot be read as a flat price.
	"""
	found = only("""struct s {
		authenticated { u32 seq; u32 other; }
		tag u8[16];
	}
	""", "uncover-mutable-field")

	assert "rewritten after it is built" in found.detail
	assert "built once and sent" in found.detail


def test_a_sealed_field_is_not_offered_a_move_it_cannot_make() -> None:
	"""Moving it out of coverage means taking it out of the seal."""
	found = by_rule("""codec aead {
		length_preserving; seekable = linear; granularity = byte;
		authenticated; invertible; deterministic;
	}
	struct s {
		sealed(aead) { u32 inner; }
		tag u8[16];
	}
	""", "uncover-mutable-field")
	assert found == []


def test_every_catalog_rule_is_exercised() -> None:
	"""A row added without a test shows up here rather than going unnoticed."""
	tested = {
		"move-dynamic-to-tail", "varint-to-fixed", "bound-unbounded",
		"equalize-variant-arms", "fill-alignment-holes", "tlv-to-positional",
		"uncover-mutable-field", "pin-a-driven-length", "count-a-while-run",
		"widen-bit-field",
		# Needs two covered regions with a member between them, which no test
		# schema above builds; covered by test_scattered_coverage below.
		"group-covered-regions",
	}
	assert {rule.name for rule in advise.CATALOG} == tested


def test_scattered_coverage_is_reported() -> None:
	found = only("""struct s {
		authenticated first { u32 a; }
		u32 between;
		authenticated second { u32 b; }
		tag u8[16];
	}
	""", "group-covered-regions")

	assert "2 regions with other members between them" in found.detail
	assert "one range to authenticate" in found.yields


def test_suggestions_carry_a_source_line() -> None:
	"""So an editor can put the advice where the construct is."""
	found = only(BADLY_ORDERED, "move-dynamic-to-tail")
	assert advise.to_dict(found)["line"] == 4



# -- the three rows added at 26.824 -----------------------------------------
#
# A length a field decides, a `while` run, and a byte of packed bit fields.
# All three were measured as candidates in 26.822 and none of their
# populations was what that record counted, which is why each test below
# pins the population as well as the wording.

BITS = "bit_order msb_first;\n"


def test_a_driven_length_is_offered_the_pin_its_driver_declares() -> None:
	"""`[max = N]` on the driver is a number somebody chose, so the pin names
	it and prices the padding exactly."""
	found = only("struct s { u16 n [max = 8]; u8 body[n]; u32 seq; }",
	             "pin-a-driven-length")

	assert found.summary == "pin `n` with `[must_eq = 8]`"
	# A frame already carrying eight bytes pays nothing; the empty one pays
	# all eight.
	assert (found.cost.typical, found.cost.worst) == (0, 8)


def test_a_driven_length_with_no_declared_bound_is_told_to_bound_it_first()\
		-> None:
	"""The branch that keeps the rule honest.

	Without a `[max]` on the driver the member's worst case is the driver's
	whole type range, so "pin at the maximum" would name a number the
	compiler accepts and nobody can use -- 26.163's defect, which is why the
	rule says bound it first and prices the pin as unchosen.
	"""
	found = only("struct s { u32 n; u8 body[n]; u32 seq; }",
	             "pin-a-driven-length")

	assert found.summary == "bound `n` with `[max = N]`, then pin it"
	assert found.cost.unknown
	assert ("4294967295 bytes -- the range of the length field's type"
	        in found.detail)


def test_the_unbounded_branch_says_nothing_false_about_a_small_width() -> None:
	"""The clause read "a number no caller can allocate", which is true of
	sqlite's 2^64 and ipv4's 2^32 and false of the 255 a `u8` length gives.

	It was written looking at the large cases and read as true because
	nothing smaller was in front of it. What holds at every width is where
	the number came from, and that is the argument anyway: the bound is the
	type's range rather than a decision. Found by reading the rendered
	output, like the straddle clause above.
	"""
	found = only("struct s { u8 n; u8 body[n]; u16 t; }",
	             "pin-a-driven-length")

	assert "255 bytes -- the range of the length field's type" in found.detail
	assert "no caller can allocate" not in found.detail


def test_a_parameter_is_not_a_length_anything_can_pin() -> None:
	"""`sized_by` also names a `[stream]` parameter, whose value is the
	caller's: `[must_eq]` cannot reach it, so advising it is advice nobody
	can take. Four of the corpus's driven members are this shape."""
	found = by_rule("""struct s {
		parameter u8 block [stream];
		u8 body[block];
		u32 seq;
	}
	""", "pin-a-driven-length")
	assert found == []


def test_a_while_run_over_a_fixed_element_is_offered_a_count() -> None:
	"""Every element the same width, so a count is all that is missing
	between walking the run and multiplying."""
	found = only("""struct e { u8 kind; u8 payload; }
struct s { e run[] while (kind == 0x33) max 6; u16 after; }
""", "count-a-while-run")

	assert found.summary.endswith("in a `u8`")
	assert (found.cost.typical, found.cost.worst) == (1, 1)
	assert "every element is 2 bytes" in found.detail


def test_a_while_run_over_a_variable_element_gets_no_count() -> None:
	"""The population correction, and the whole reason this rule has a guard.

	26.822 counted three instances off three `caused by:` clauses and treated
	all three as the population. Re-measured over the corpus there are eight
	`while` runs and two have a fixed-size element: for the other six a count
	says how many elements there are and still nothing about where any of
	them is, because each element's own length decides where the next begins.
	A suggestion promising `Random` access there would be promising what the
	rewrite cannot deliver, which is the one failure the taken-advice section
	below exists to catch.

	The guard is the element STRIDE, which the rule multiplies by in its own
	yield text. It was written as two conditions -- the stride and the
	element struct's `is_fixed_size` -- and removing the second left this
	test green, because `element_bits` is set for no variable-length element
	anywhere in the corpus. One predicate, so a sabotage of it is visible
	here.
	"""
	found = by_rule("""struct e { u8 kind; u8 len; u8 body[len]; }
struct s { e run[] while (kind == 0x33) max 6; u16 after; }
""", "count-a-while-run")
	assert found == []


def test_an_uncapped_while_run_cannot_price_its_count() -> None:
	"""No bound on the element count is no bound on the field that would
	carry it, which is `Cost.unknown`'s own case."""
	found = only("""struct e { u8 kind; u8 payload; }
struct s { e run[] while (kind == 0x33); u16 trail; u16 mark; }
""", "count-a-while-run")

	assert found.cost.unknown
	assert found.summary == "state the element count ahead of the run"


def test_packed_bit_fields_get_one_suggestion_for_the_run() -> None:
	"""One per run, not one per field, which is the judgement 18.2 already
	made for `uncover-mutable-field` and the same one: the fields share a
	byte, so their costs do not add, and the schema does not say which of
	them is written on a hot path.
	"""
	found = by_rule(BITS + """struct s {
	u1 urgent; u1 ack; u2 prio; reserved u4;
	u32 seq;
}
""", "widen-bit-field")

	assert len(found) == 1
	assert found[0].subject == "s.urgent"
	assert "`urgent`, `ack`, `prio`" in found[0].detail
	# One byte for the field lifted, and three for lifting all of them -- the
	# byte they share is why that is not four.
	assert (found[0].cost.typical, found[0].cost.worst) == (1, 1)
	assert "Lifting all 3 costs 3 bytes" in found[0].detail


def test_reserved_bits_are_not_a_candidate_for_a_byte() -> None:
	"""Nothing writes reserved bits, so an atomic write to them is not worth
	a byte -- and they are not the subject of advice about the fields beside
	them either, which is what naming the run's first member made them."""
	found = only(BITS + """struct s {
	reserved u3; u1 ack; u4 prio;
	u32 seq;
}
""", "widen-bit-field")

	assert found.subject == "s.ack"
	assert "2 fields share 1 byte" in found.detail
	assert "reserved" not in found.detail


def test_a_straddling_bit_field_is_named_as_paying_twice() -> None:
	"""A field crossing a byte boundary is a multi-byte read-modify-write, and
	the propagation table gives it a row of its own for that second reason.
	One rule here rather than two (26.822): it is the same remedy, and what
	changes is how much the field was paying before it is taken."""
	found = only(BITS + """struct s [allow_straddle] {
	u4 head; u12 middle; u4 tail; reserved u4;
	u32 seq;
}
""", "widen-bit-field")

	assert "`middle` crosses a byte boundary, so its write touches two" \
		in found.detail
	# And the wide field is priced at the bytes it needs rather than at one:
	# twelve bits lifted out of a three-byte run leave twelve behind, so the
	# run keeps two bytes and the field takes two.
	assert (found.cost.typical, found.cost.worst) == (1, 1)


def test_two_straddling_fields_are_both_named() -> None:
	"""The clause named `crossing[0]` and nothing else.

	No corpus schema has a run that straddles twice, so the message was true
	and incomplete and no gate could have said so -- it was found by reading
	the rendered output, which section 17 makes the product. `u4 a; u12 b;
	u12 c; u4 d` straddles at byte 1 and again at byte 3.

	The verb agreement goes with it, which is `_verb`'s whole reason for
	existing: "`b`, `c` crosses a byte boundary, so its write touches two" is
	the register that function was written to keep the advisor out of.
	"""
	found = only(BITS + """struct s [allow_straddle] {
	u4 a; u12 mid; u12 two; u4 d;
	u32 seq;
}
""", "widen-bit-field")

	assert ("`mid`, `two` cross a byte boundary, so their writes touch two"
	        in found.detail)


def test_every_rule_ranks_where_the_catalog_says_it_does() -> None:
	"""`rank` is written twice -- once in `CATALOG` and once in every
	`Suggestion` the rule builds -- and nothing but this compares them.

	Two places answering one question from different text is this tree's most
	productive lens, and the catalog grew by three rows and renumbered five
	the day it was written. A rule whose suggestions carry a stale rank sorts
	into the wrong class and no existing gate can see it: the order would
	still be *an* order, and `suggest`'s own sort assertion would still hold.
	"""
	ranks = {rule.name: rule.rank for rule in advise.CATALOG}
	body  = pathlib.Path(advise.__file__).read_text()

	written = {name: int(rank) for name, rank in
	           re.findall(r'rule    = "([a-z-]+)",.*?rank    = (\d+),',
	                      body, re.DOTALL)}

	assert written == ranks, "a rule's rank disagrees with its catalog row"


# -- the revision diff (18.3) -----------------------------------------------


def changes(old: str, new: str) -> list[revision.Change]:
	return revision.compare(build(old), build(new))


def test_diff_identifies_in_place_becoming_shifting() -> None:
	"""The regression section 26.9 names.

	A fixed array becoming a counted one is one word of schema, and it costs
	every writer the ability to update the field where it sits.
	"""
	found = changes("struct rec { u16 n [max = 8]; u8 body[8]; }",
	                "struct rec { u16 n [max = 8]; u8 body[n]; }")

	mutate = [change for change in found if change.axis is Axis.MUTATE]
	assert len(mutate) == 1
	assert mutate[0].path == "rec.body"
	assert mutate[0].kind == "weakened"
	assert mutate[0].before.render() == "InPlaceFixed"		# type: ignore[union-attr]
	assert mutate[0].after.render() == "Shifting"			# type: ignore[union-attr]
	assert mutate[0].is_regression


def test_diff_reports_nothing_for_an_unchanged_schema() -> None:
	same = "struct s { u32 a; u16 b; }"
	assert changes(same, same) == []
	assert revision.render([]) == "No capability change.\n"


def test_a_growing_bound_is_a_regression_even_at_the_same_axis_value() -> None:
	"""Bounded(0,100) to Bounded(0,400) costs every caller 300 bytes of buffer."""
	found = changes("struct s { u16 n [max = 100]; u8 body[n]; }",
	                "struct s { u16 n [max = 400]; u8 body[n]; }")

	size = [change for change in found
	        if change.axis is Axis.SIZE and change.path == "s.body"]
	assert size and size[0].kind == "weakened"


def test_a_shrinking_bound_is_an_improvement() -> None:
	found = changes("struct s { u16 n [max = 400]; u8 body[n]; }",
	                "struct s { u16 n [max = 100]; u8 body[n]; }")

	size = [change for change in found
	        if change.axis is Axis.SIZE and change.path == "s.body"]
	assert size and size[0].kind == "strengthened"
	assert not size[0].is_regression


def test_a_removed_field_is_a_regression() -> None:
	"""Harder than any axis moving: every caller stops compiling."""
	found = changes("struct s { u32 a; u32 b; }", "struct s { u32 a; }")

	assert [change.kind for change in found if change.path == "s.b"] == ["removed"]
	assert any(change.is_regression for change in found)


def test_a_renamed_field_is_a_removal_and_an_addition() -> None:
	"""Situ has no field numbers: the name is the identity (section 4)."""
	found = changes("struct s { u32 before; }", "struct s { u32 after; }")

	kinds = {change.path: change.kind for change in found}
	assert kinds == {"s.before": "removed", "s.after": "added"}


def test_an_added_field_alone_is_not_a_regression() -> None:
	found = changes("struct s { u32 a; }", "struct s { u32 a; u32 b; }")
	assert not any(change.is_regression for change in found)


def test_an_offset_that_moved_is_neither_direction() -> None:
	"""The layout changed; the capability did not."""
	found = changes("struct s { u16 a; u32 b; }", "struct s { u32 b; u16 a; }")

	offsets = [change for change in found if change.axis is Axis.OFFSET]
	assert offsets and all(change.kind == "moved" for change in offsets)
	assert not any(change.is_regression for change in offsets)


def test_the_report_reads_old_to_new_in_every_group() -> None:
	"""A direction marker that reversed the reading order would be unreadable."""
	rendered = revision.render(changes(
		"struct rec { u16 n [max = 8]; u8 body[8]; }",
		"struct rec { u16 n [max = 8]; u8 body[n]; }"))

	assert "! rec.body: mutate InPlaceFixed -> Shifting" in rendered
	assert "+ rec.body: atomic NonAtomic -> AtomicWord" in rendered
	assert "regression(s)" in rendered


def test_changes_serialise_for_ci() -> None:
	found = changes("struct rec { u16 n [max = 8]; u8 body[8]; }",
	                "struct rec { u16 n [max = 8]; u8 body[n]; }")
	payload = revision.to_dict(
		next(change for change in found if change.axis is Axis.MUTATE))

	assert payload["path"] == "rec.body"
	assert payload["before"] == "InPlaceFixed"
	assert payload["after"] == "Shifting"
	assert payload["regression"] is True


# -- versioned members (section 19.4) ---------------------------------------


def test_it_does_not_suggest_moving_a_member_past_a_versioned_one() -> None:
	"""`[since]` is append-only, so the compiler refuses the reordering this
	suggested -- and it was advertised as costing nothing. If it were legal it
	would move bytes for every deployed peer speaking the earlier version,
	which is the opposite of nothing."""
	found = by_rule("""struct s [version = v] {
		u8   v;
		u8   n;
		u8   body[n];
		u32  flags [since = 2];
	}
	""", "move-dynamic-to-tail")

	assert not found


def test_it_still_suggests_it_where_the_move_is_legal() -> None:
	"""The other half. Silencing a rule is only right if it stays useful for
	the case it was written for."""
	found = by_rule("struct s { u8 n; u8 body[n]; u32 flags; }",
	                "move-dynamic-to-tail")

	assert found


# -- the suggestion, taken ---------------------------------------------------
#
# Everything above asks what the advisor *says*. What follows takes it: the
# schema is rewritten the way the suggestion describes, re-solved, and the
# capability it promised is counted. That is the only check that can fail for
# the reason the advisor exists to avoid -- advice nobody can act on, or
# advice whose payoff is smaller than the number beside it (26.44).


def moved_to_tail(body: str, member: str, before: str | None = None) -> str:
	"""The schema with `member` moved down, which is what the advice says."""
	lines  = body.splitlines(keepends=True)
	moving = next(line for line in lines if f" {member}[" in line
	              or line.strip().startswith(f"{member} ")
	              or f" {member};" in line)
	rest   = [line for line in lines if line is not moving]

	if before is None:
		closing = next(i for i, line in enumerate(rest) if line.strip() == "}")
	else:
		closing = next(i for i, line in enumerate(rest)
		               if f" {before}[" in line or f" {before};" in line)
	return "".join(rest[:closing] + [moving] + rest[closing:])


def offsets(body: str) -> dict[str, str]:
	resolved = build(body)
	return {entry.placement.path: entry.vector.get(Axis.OFFSET).base
	        for struct in resolved.structs.values()
	        for entry in struct.entries}


def test_the_reordering_suggestion_yields_what_it_says() -> None:
	"""The number in `yields` is a count of members, and it was the count of
	everything behind the mover rather than the count that gains.

	Whatever follows the *next* variable-length member is placed after a
	variable extent either way, so it does not gain and never could.
	"""
	body = """struct m {
	u16 length [max = 1500];
	u8  opts[length];
	u8  recs[length];
	u32 seq;
}
"""
	found  = next(item for item in by_rule(body, "move-dynamic-to-tail")
	              if item.subject == "m.opts")
	before = offsets(body)
	after  = offsets(moved_to_tail(body, "opts"))

	gained = [path for path, base in before.items()
	          if base != "AbsoluteStatic"
	          and after.get(path) == "AbsoluteStatic"]

	assert found.yields.startswith(f"{len(gained)} member"), \
		f"advisor promised `{found.yields}`, the rewrite delivered {gained}"


def test_the_reordering_suggestion_compiles_when_taken() -> None:
	"""A `[remaining]` member has to be last (8.5), so "move it to the end" is
	advice the compiler refuses -- which is 26.36's `[since]` defect in its
	other form, found by taking the advice rather than by reading it."""
	body = """struct m {
	u16 length [max = 1500];
	u8  opts[length];
	u32 seq;
	u8  tail[remaining];
}
"""
	found = only(body, "move-dynamic-to-tail")
	assert "before `tail`" in found.summary

	# And the rewrite it describes is one the solver accepts.
	build(moved_to_tail(body, "opts", before="tail"))


VARINT = "varint_type v { encoding = leb128; max_bits = 16; minimal; }\n"


def test_the_varint_yield_counts_only_the_members_that_gain() -> None:
	"""`move-dynamic-to-tail` learned that pinning an extent does not make
	everything behind it static -- only the members up to and including the
	next variable-length one -- and the lesson stopped at that rule.

	This one said "every member behind it keeps its static offset", which
	names no number and so could not be measured against one. Here three
	members follow the varint and two of them gain.
	"""
	body = VARINT + """struct s {
	v   n;
	u16 k [max = 8];
	u8  body[k];
	u32 seq;
}
"""
	found  = only(body, "varint-to-fixed")
	before = offsets(body)
	after  = offsets(body.replace("v   n;", "u32 n;"))

	gained = [path for path, base in before.items()
	          if base != "AbsoluteStatic"
	          and after.get(path) == "AbsoluteStatic"]

	assert len(gained) == 2, gained			# `seq` follows `body` either way
	assert found.yields == (f"a fixed extent, so {len(gained)} members after "
	                        "the varint keep absolute offsets"), \
		f"advisor promised `{found.yields}`, the rewrite delivered {gained}"


def test_a_trailing_varint_claims_only_what_it_delivers() -> None:
	"""Nothing follows it, so no member regains an offset -- but the struct
	holding it becomes a fixed size, which is the whole reason to spend the
	bytes. The old text promised offsets for members that do not exist and
	left the one real gain unsaid; `equalize-variant-arms` was given this
	same repair and the varint rule was not."""
	body  = VARINT + "struct s { v n; }\n"
	found = only(body, "varint-to-fixed")

	assert "`s` itself becomes a fixed size" in found.yields
	assert "member" not in found.yields

	# And taking it does that. The yield is measured, not asserted.
	assert not build(body).structs["s"].layout.is_fixed_size
	assert build(body.replace("v n;", "u32 n;")).structs["s"].layout.is_fixed_size


def test_equalizing_counts_only_the_members_that_gain() -> None:
	"""The same overclaim in the rule that already measures its trailing case.

	`test_equalizing_a_trailing_variant_claims_only_what_it_delivers` measures
	the count where it is zero, which is the one value that cannot be too
	large. With three members behind the variant it promised three.
	"""
	body = """enum k : u8 { a = 1, b = 2, }
struct small { u16 x; }
struct large { u8 y[10]; }
struct s {
	k kind;
	variant body switch (kind) { case k.a: small p; case k.b: large q; }
	u16 n [max = 8];
	u8  rest[n];
	u32 seq;
}
"""
	found  = only(body, "equalize-variant-arms")
	before = offsets(body)
	after  = offsets(body.replace("switch (kind) {", "switch (kind) [equalize] {"))

	gained = [path for path, base in before.items()
	          if base != "AbsoluteStatic"
	          and after.get(path) == "AbsoluteStatic"]

	assert len(gained) == 2, gained			# `seq` follows `rest` either way
	assert found.yields == ("a fixed extent, so 2 members after the variant "
	                        "keep absolute offsets")


def test_an_unbounded_struct_member_is_pointed_inside_its_type() -> None:
	"""Advice that cannot be taken is worse than none, and this branch gave
	some.

	"give this member a bound the compiler can read" leads an author to write
	`[max = N]` on a struct-typed member, which parsed, resolved, moved no
	axis and drew this same suggestion again on the next run -- the `[size =
	N]` defect the rule's own comment describes, arriving by another route.
	The compiler refuses that attribute now, so the advice names the thing
	that does bound it.
	"""
	body = """struct inner { u8 r[remaining]; }
struct s { u16 a; inner b; }
"""
	found = {item.subject: item for item in by_rule(body, "bound-unbounded")}

	assert set(found) == {"inner.r", "s.b"}
	assert "bound the region inside `inner`" in found["s.b"].detail
	assert "give this member a bound" not in found["s.b"].detail

	# And bounding the region inside clears both, which is what makes the
	# outer suggestion a pointer rather than a second thing to do.
	bounded = body.replace("u8 r[remaining];", "u16 n [max = 32]; u8 r[n];")
	assert not by_rule(bounded, "bound-unbounded")


def test_taking_the_alignment_advice_aligns_the_member() -> None:
	"""The rule's yield is prose -- "an aligned access" -- rather than a
	count, so what it promises is an axis rather than a number. Measured the
	same way: reorder so the reserved bytes precede the scalar, and the align
	axis it was weakened on is the one that moves."""
	before = "struct s { u8 flag; u32 counter; reserved u8[3]; u16 seq; }"
	after  = "struct s { u8 flag; reserved u8[3]; u32 counter; u16 seq; }"

	found = only(before, "fill-alignment-holes")
	assert found.subject == "s.counter"

	def align(body: str) -> str:
		struct = build(body).structs["s"]
		return next(str(entry.vector.get(Axis.ALIGN)) for entry in struct.entries
		            if entry.placement.path == "s.counter")

	assert align(before) == "Aligned(1)"
	assert align(after)  == "Aligned(4)"
	assert not by_rule(after, "fill-alignment-holes")


def test_taking_the_uncover_advice_uncovers_the_field() -> None:
	"""The yield is prose again -- "writes that invalidate no tag" -- and the
	axis it means is `auth`. Move one covered field out of the region and it
	is the only one that changes."""
	before = """struct s {
	authenticated { u32 seq; u32 other; }
	tag u8[16];
}
"""
	after = """struct s {
	u32 seq;
	authenticated { u32 other; }
	tag u8[16];
}
"""
	def auth(body: str) -> dict[str, str]:
		return {entry.placement.path: str(entry.vector.get(Axis.AUTH))
		        for struct in build(body).structs.values()
		        for entry in struct.entries}

	assert "2 covered fields are writable" in only(before, "uncover-mutable-field").detail
	assert auth(before)["s.seq"] == "Covered(tag)"
	assert auth(after)["s.seq"] == "Uncovered"
	assert auth(after)["s.other"] == "Covered(tag)"

	# The tag still covers something, so the rule still fires -- over one
	# field rather than two, which is what the advice was for.
	assert "1 covered field is writable" in only(after, "uncover-mutable-field").detail


def test_taking_the_grouping_advice_leaves_one_range() -> None:
	"""`group-covered-regions` promises "one range to authenticate instead of
	several". Move the member that splits them out from between."""
	before = """struct s {
	authenticated first { u32 a; }
	u32 between;
	authenticated second { u32 b; }
	tag u8[16];
}
"""
	after = """struct s {
	u32 between;
	authenticated first { u32 a; u32 b; }
	tag u8[16];
}
"""
	assert "2 regions" in only(before, "group-covered-regions").detail
	assert not by_rule(after, "group-covered-regions")


def test_the_tlv_trade_is_partial_and_says_so() -> None:
	"""The one yield in the catalogue that is not meant to clear its own
	suggestion: lifting the tags you always send leaves the region for the
	rest, and the text says "the rest can stay in the region". A test that
	asserted the rule stops firing would be asserting the opposite of what
	the advice offers."""
	before = "struct s { u16 hdr; tlv opts (tag_type = u8); }"
	after  = "struct s { u16 hdr; u32 ttl; tlv opts (tag_type = u8); }"
	found  = only(before, "tlv-to-positional")

	assert "the rest can stay in the region" in found.yields

	# The lifted field gets the address the region could not give it...
	assert offsets(after)["s.ttl"] == "AbsoluteStatic"
	# ...and the region is still worth reporting, because it is still there.
	assert only(after, "tlv-to-positional").subject == "s.opts"


def test_every_catalog_rule_has_its_yield_measured() -> None:
	"""`yields` is the one claim a suggestion makes that no assertion about
	its message can check: it is a prediction about a schema that does not
	exist yet, so the only way to be wrong about it and stay green is to
	never build that schema.

	Two rules were, and both were wrong. `equalize-variant-arms` and
	`varint-to-fixed` promised offsets to members that do not gain, and
	`bound-unbounded` named an attribute the compiler accepted and ignored
	(26.163, 26.164). Every one was found by taking the advice.

	So a rule added to the catalog needs a test below the marker, not only
	one that watches it fire. This reads the marker rather than a list,
	because a list of rules with measured yields is a second place to forget.
	"""
	source = pathlib.Path(__file__).read_text()
	# `maxsplit=1`, because the marker occurs TWICE in this file -- once as
	# the section heading and once as the literal on this line -- so the
	# default split put everything between them in [1] and everything below
	# this gate in [2]. A taken-advice test written after this function was
	# therefore invisible to it, and the gate reported the one thing it
	# exists to deny: that the advice had not been taken. Found adding the
	# three rows at 26.824, whose tests landed below here.
	taken  = source.split("# -- the suggestion, taken", 1)[1]

	missing = sorted(rule.name for rule in advise.CATALOG
	                 if f'"{rule.name}"' not in taken)
	assert not missing, (
		f"{missing} fire in a test but nothing takes the advice and measures "
		"what it bought")


def test_every_priced_rule_costs_what_it_says_on_the_wire() -> None:
	"""`Cost` counts bytes on the wire and `worst` is the most a frame pays.

	That is measurable without knowing anything about a value distribution:
	where the rewritten schema is fixed, the frame that was smallest before is
	the one that pays most, so the worst cost is the difference between the
	two minima. `typical` is not measurable the same way -- each rule averages
	over what it knows, arms for one and encoding widths for another -- so
	this pins the number that has one meaning.

	`varint-to-fixed` reported both numbers at the varint's *widest*
	encoding, making `worst` the extra paid by a frame already paying the
	most: the smallest cost in the range under the name of the largest. A
	`u16` against a one-to-two byte varint priced at "nothing", which is what
	`Cost` refuses to do for an unbounded region on the ground that zero is
	"a lie in the cheapest possible direction".
	"""
	varint = "varint_type v { encoding = leb128; max_bits = 16; minimal; }\n"
	arms   = """enum k : u8 { a = 1, b = 2, }
struct small { u16 x; }
struct large { u8 y[10]; }
"""
	cases = [
		("move-dynamic-to-tail",
		 "struct s { u16 n [max = 1500]; u8 opts[n]; u32 seq; }",
		 "struct s { u16 n [max = 1500]; u32 seq; u8 opts[n]; }"),
		("fill-alignment-holes",
		 "struct s { u8 flag; u32 counter; reserved u8[3]; u16 seq; }",
		 "struct s { u8 flag; reserved u8[3]; u32 counter; u16 seq; }"),
		("varint-to-fixed",
		 varint + "struct s { v n; u32 seq; }\n",
		 varint + "struct s { u32 n; u32 seq; }\n"),
		("equalize-variant-arms",
		 arms + "struct s { k kind; variant b switch (kind)"
		        " { case k.a: small p; case k.b: large q; } u32 after; }\n",
		 arms + "struct s { k kind; variant b switch (kind) [equalize]"
		        " { case k.a: small p; case k.b: large q; } u32 after; }\n"),
		# The two rows added at 26.824 whose rewrite is fixed, which is this
		# method's precondition. `count-a-while-run` is not one of them: a
		# counted run is still variable, and its smallest frame is a byte
		# SMALLER than the `while` run's, because a `while` is a
		# post-condition and carries at least one element where a count may
		# say zero. Its byte is checked against the count field itself, in
		# the taken-advice section.
		("pin-a-driven-length",
		 "struct s { u16 n [max = 8]; u8 body[n]; u32 seq; }",
		 "struct s { u16 n [max = 8, must_eq = 8]; u8 body[n]; u32 seq; }"),
		("widen-bit-field",
		 BITS + "struct s { u1 urgent; u1 ack; u2 prio; reserved u4;"
		        " u32 seq; }\n",
		 BITS + "struct s { u8 urgent; u1 ack; u2 prio; reserved u5;"
		        " u32 seq; }\n"),
	]

	def smallest_frame(body: str) -> int:
		return build(body).structs["s"].layout.size_bits // 8

	for rule, before, after in cases:
		found = only(before, rule)
		paid  = smallest_frame(after) - smallest_frame(before)
		assert found.cost.worst == paid, (
			f"{rule} prices its worst case at {found.cost.worst} byte(s); the "
			f"smallest frame pays {paid}")


def test_the_heading_names_the_class_because_the_order_is_not_by_yield() -> None:
	"""`rank` is a fixed position in the catalog, so every instance of a class
	outranks every instance of the one below it however little it recovers.
	The heading said "highest yield first", which is a claim about the whole
	list that the sort does not make.

	Both readings of "highest yield" fail on the catalog's own order, and this
	pins the one that is measurable: a suggestion further down the list
	recovering strictly more than one above it.
	"""
	body = """varint_type v { encoding = leb128; max_bits = 16; minimal; }
enum k : u8 { a = 1, b = 2, }
struct small { u16 x; }
struct large { u8 y[10]; }
struct tiny { v n; }
struct big {
	k kind;
	variant body switch (kind) { case k.a: small p; case k.b: large q; }
	u32 one; u32 two; u32 three; u32 four; u32 five;
}
"""
	found = suggestions(body)
	rules = [item.rule for item in found]

	assert "highest-yield class first" in advise.render(found)

	varint    = found[rules.index("varint-to-fixed")]
	equalized = found[rules.index("equalize-variant-arms")]

	# The varint sorts above the equalization and recovers strictly less:
	# the equalization delivers everything it does and five members besides.
	assert rules.index("varint-to-fixed") < rules.index("equalize-variant-arms")
	assert "member" not in varint.yields
	assert "5 members after the variant keep absolute offsets" in equalized.yields
	assert "`big` itself becomes a fixed size" in equalized.yields

	# And the ordering is exactly what `suggest` documents: catalog order,
	# then the instance that recovers most within a class.
	assert [(item.rank, -item.weight, item.subject) for item in found] == \
		sorted((item.rank, -item.weight, item.subject) for item in found)



# -- the three rows added at 26.824, taken ----------------------------------


def axes(body: str, axis: Axis) -> dict[str, str]:
	"""Every member's value on one axis, which is what a rewrite moves."""
	resolved = build(body)
	return {entry.placement.path: entry.vector.get(axis).base
	        for struct in resolved.structs.values()
	        for entry in struct.entries}


def test_pinning_a_driven_length_yields_what_it_says() -> None:
	"""`[must_eq = N]` on the driver, and the three things it returns.

	The rule hands `_pinned_yield` the same two measurements the varint and
	variant rules hand it, so this checks the rewrite delivers both: the
	members behind return to absolute offsets, and the struct becomes a
	fixed size. The member's own `mutate` goes with them, which the summary
	does not promise and the propagation row does.
	"""
	before = "struct s { u16 n [max = 8]; u8 body[n]; u32 seq; }"
	after  = "struct s { u16 n [max = 8, must_eq = 8]; u8 body[n]; u32 seq; }"
	found  = only(before, "pin-a-driven-length")

	gained = [path for path, base in axes(before, Axis.OFFSET).items()
	          if base != "AbsoluteStatic"
	          and axes(after, Axis.OFFSET).get(path) == "AbsoluteStatic"]
	assert gained == ["s.seq"]
	assert found.yields.startswith("a fixed extent, so 1 member after `body`")
	assert "`s` itself becomes a fixed size" in found.yields

	assert not build(before).structs["s"].layout.is_fixed_size
	assert build(after).structs["s"].layout.is_fixed_size

	assert axes(before, Axis.MUTATE)["s.body"] == "Shifting"
	assert axes(after, Axis.MUTATE)["s.body"] == "InPlaceFixed"


def test_bounding_a_driven_length_first_is_advice_the_compiler_takes() -> None:
	"""The other branch, which names two edits rather than one.

	`[max = N]` on the driver has to come before the pin, and the thing worth
	checking is that the compiler accepts the pair and that the worst case
	the member reports stops being the driver's whole type range -- 2^32-1
	bytes, which is the number the detail prints and nobody can allocate.
	"""
	loose = "struct s { u32 n; u8 body[n]; u32 seq; }"
	found = only(loose, "pin-a-driven-length")
	assert found.summary == "bound `n` with `[max = N]`, then pin it"

	def worst(body: str) -> int:
		placement = next(entry.placement
		                 for entry in own_entries(build(body).structs["s"])
		                 if entry.placement.name == "body")
		assert placement.size_max_bits is not None
		return placement.size_max_bits // 8

	assert worst(loose) == 0xFFFFFFFF
	bounded = "struct s { u32 n [max = 1500]; u8 body[n]; u32 seq; }"
	assert worst(bounded) == 1500

	# And the pin the bound makes sayable is one the solver accepts.
	pinned = "struct s { u32 n [max = 1500, must_eq = 1500]; u8 body[n]; u32 seq; }"
	assert build(pinned).structs["s"].layout.is_fixed_size


def test_counting_a_while_run_yields_the_random_access_it_promises() -> None:
	"""The rewrite the summary describes, and the axis it claims.

	Not in `test_every_priced_rule_costs_what_it_says_on_the_wire`, and the
	reason is that gate's own precondition: it measures a cost as the
	difference between two smallest frames, which holds "where the rewritten
	schema is fixed". A counted run is not -- and its smallest frame is a
	byte SMALLER than the `while` run's, because a `while` is a
	post-condition and so carries at least one element where a count may say
	zero. The count field still costs one byte in every frame carrying the
	same elements, which is what the rule prices.
	"""
	element = "struct e { u8 kind; u8 payload; }\n"
	before  = element + "struct s { e run[] while (kind == 0x33) max 6; u16 after; }\n"
	after   = element + "struct s { u8 count [max = 6]; e run[count]; u16 after; }\n"
	found   = only(before, "count-a-while-run")

	assert axes(before, Axis.ACCESS)["s.run"] == "Sequential"
	assert axes(after, Axis.ACCESS)["s.run"] == "Random"
	assert found.yields.startswith("`Random` access")

	# The byte the rule charges for is the count field and nothing else.
	counted = next(entry.placement
	               for entry in own_entries(build(after).structs["s"])
	               if entry.placement.name == "count")
	assert counted.size_bits // 8 == found.cost.worst == 1


def test_widening_a_bit_field_yields_the_three_axes_it_promises() -> None:
	"""One field out of the packed run, and `atomic`, `align` and `repr` with
	it -- which is three axes for one byte and the reason this row is worth
	having at all. The propagation row names only the atomic one as its
	remedy's object, so the other two are the rewrite paying more than the
	table claims rather than less.
	"""
	before = BITS + "struct s { u1 urgent; u1 ack; u2 prio; reserved u4; u32 seq; }\n"
	after  = BITS + "struct s { u8 urgent; u1 ack; u2 prio; reserved u5; u32 seq; }\n"
	found  = only(before, "widen-bit-field")

	for axis, weak, strong in ((Axis.ATOMIC, "NonAtomic", "AtomicWord"),
	                           (Axis.ALIGN,  "Unaligned", "Aligned"),
	                           (Axis.REPR,   "ValueConverted", "MemoryIdentical")):
		assert axes(before, axis)["s.urgent"] == weak, axis
		assert axes(after,  axis)["s.urgent"] == strong, axis

	assert found.yields.startswith("an atomic write, a byte address")
	# And the byte it costs is a byte the rewrite really spends.
	paid = (build(after).structs["s"].layout.size_bits
	        - build(before).structs["s"].layout.size_bits) // 8
	assert paid == found.cost.worst == 1

# -- what the diff was calling neither direction (26.187) -------------------


def test_an_alignment_that_weakened_is_a_regression() -> None:
	"""`align` carries strength in its parameter and the diff ignored it.

	`capability.py` puts `Axis.ALIGN` in `NUMERIC_STRENGTH` and says why --
	"`Aligned(8)` is stronger than `Aligned(2)`" -- but `_parameter_change`
	special-cased `SIZE` and fell through to "moved" for everything else. So
	a field that lost its alignment was reported as a layout change and the
	run exited 0, while `situc build` **refuses the same edit** when a
	`require aligned(...)` names it. The tool that exists to catch a
	regression in review called it neither direction.
	"""
	assert revision._parameter_change(
		Axis.ALIGN, Value("Aligned", ("8",)), Value("Aligned", ("4",))) \
		== "weakened"
	assert revision._parameter_change(
		Axis.ALIGN, Value("Aligned", ("4",)), Value("Aligned", ("8",))) \
		== "strengthened"

	# End to end: moving a `u32` behind four bytes weakens its alignment.
	changes = revision.compare(
		build("struct s { u32 v; u8 a; u8 b; u8 c; u8 d; }"),
		build("struct s { u8 a; u8 b; u8 c; u8 d; u32 v; }"))
	weakened = [c for c in changes
	            if c.axis is Axis.ALIGN and c.kind == "weakened"]
	assert weakened, [(c.path, c.axis, c.kind) for c in changes]


def test_a_sub_byte_size_that_grew_is_a_regression() -> None:
	"""The verdict used to turn on whether the size divided by eight.

	`render_size` writes a whole number of bytes as `2` and anything else as
	`12bit`, and `_worst` was `int(tail) if tail.isdigit() else 0`. Both
	sides of a sub-byte change collapsed to 0, `0 > 0` was False, and the
	`else` arm returned "strengthened" -- so a field that grew from four bits
	to six was printed under **Improvements**, while the same edit one byte
	wide was a regression.
	"""
	assert revision._parameter_change(
		Axis.SIZE, Value("Fixed", ("4bit",)), Value("Fixed", ("6bit",))) \
		== "weakened"
	assert revision._parameter_change(
		Axis.SIZE, Value("Fixed", ("6bit",)), Value("Fixed", ("4bit",))) \
		== "strengthened"

	# The byte-sized control, which was already right and must stay so.
	assert revision._parameter_change(
		Axis.SIZE, Value("Fixed", ("1",)), Value("Fixed", ("2",))) == "weakened"

	# And the two are on one scale now, so a byte is worse than seven bits.
	assert revision._parameter_change(
		Axis.SIZE, Value("Fixed", ("7bit",)), Value("Fixed", ("1",))) \
		== "weakened"


def test_a_worst_case_that_did_not_move_is_neither_direction() -> None:
	"""Not measurably worse is not better.

	`_parameter_change` had two outcomes for `SIZE`, so anything that was not
	a regression was reported as an improvement. `Bounded(2, 10)` becoming
	`Bounded(4, 10)` doubles the minimum extent and leaves the maximum where
	it was: neither value is stronger -- `is_at_least` answers True in both
	directions -- and "moved" is what the offset case already says.
	"""
	assert revision._parameter_change(
		Axis.SIZE, Value("Bounded", ("2", "10")), Value("Bounded", ("4", "10"))) \
		== "moved"

	# The documented case still reads as the regression it is.
	assert revision._parameter_change(
		Axis.SIZE, Value("Bounded", ("0", "1500")),
		Value("Bounded", ("0", "4096"))) == "weakened"


# -- a tag's extent is its own region's, not every same-named region's -------


TWIN_COVERAGE = """
codec summing { kernel = ones_complement(width = 16); }
impl summing derived;

struct leaf {
	authenticated body { u16 y; }
	checksum u8 lsig[2] covers(body) is summing;
}
struct outer {
	authenticated body { u16 own_field; }
	leaf first;
	leaf second;
	checksum u8 osig[2] covers(body) is summing;
}
"""


def _own_tag(resolved: ResolvedSchema, struct: str, tag: str) -> int:
	"""One tag's extent, reached the way the advisor reaches it.

	Through `own_entries`, because that is what `_members` is and what
	every rule loops over. Calling `_covered_bytes` with a tag the
	advisor never passes it tests the function on an input it does not
	receive -- which is what the first version of this fixture did, and
	the shape `evidence.md` calls a test that names the hazard and
	covers the safe path.
	"""
	held = resolved.structs[struct]
	for entry in own_entries(held):
		if entry.placement.path == tag:
			return advise._covered_bytes(held, entry.placement)
	raise AssertionError(f"{tag} is not an own member of {struct}")


def test_a_tags_extent_counts_only_the_region_beside_it() -> None:
	"""`_covered_bytes` summed every placement in the struct whose bare
	NAME was in `tag_covers`. A struct that declares its own
	`authenticated body` while also holding two members whose type
	declares one has three placements called `body`, and the tag was
	credited with all three.

	`outer.body` is the `u16` this tag covers, so the answer is 2 and
	the answer before the fix was 6 -- it grows with the number of
	members whose type happens to reuse the name.

	The extent is the suggestion's whole claim, section 18 calling the
	cost column the differentiator, and it also sets `weight`, so an
	inflated one outranks suggestions that really do cost more.

	What makes this the interesting one is where it sat. The candidate
	filter ten lines above asks `covered_by`, which is qualified because
	26.519 and 26.522 qualified it. The extent, in the same function,
	still asked by bare name.
	"""
	resolved = build(TWIN_COVERAGE)

	assert _own_tag(resolved, "leaf", "leaf.lsig") == 2, \
		"the un-nested case was always right"
	assert _own_tag(resolved, "outer", "outer.osig") == 2, \
		"the tag was credited with the nested structs' regions as well"


SCATTERED_TWINS = """
codec summing { kernel = ones_complement(width = 16); }
impl summing derived;

struct leaf {
	authenticated a { u16 la; }
	authenticated b { u16 lb; }
	checksum u8 lsig[2] covers(a, b) is summing;
}
struct outer {
	authenticated a { u16 oa; }
	authenticated b { u16 ob; }
	leaf first;
	leaf second;
	checksum u8 osig[2] covers(a, b) is summing;
}
"""


def test_scattered_coverage_does_not_see_a_nested_structs_regions() -> None:
	"""The same bare name, one function over, and a worse failure.

	`_find_scattered_coverage` gathered the covered regions the same
	way, so `outer.osig` picked up six placements across three structs.
	Six regions in three structs are never contiguous, so the rule fired
	and told the reader to group regions that are already adjacent --
	advice that would have them restructure a correct schema.

	`outer.a` and `outer.b` are declared back to back with nothing
	between them, which is what makes this a false positive rather than
	a wrong number. The message gave it away and nobody read it: `detail`
	and `weight` count `tag_covers`, so it said "2 regions" about a
	decision taken over six.
	"""
	assert by_rule(SCATTERED_TWINS, "group-covered-regions") == []


def test_scattered_coverage_still_fires_when_it_should() -> None:
	"""The control for the test above, which would otherwise pass just as
	well against a rule that had stopped working."""
	split = """
codec summing { kernel = ones_complement(width = 16); }
impl summing derived;

struct split {
	authenticated first  { u16 a; }
	u16           between;
	authenticated second { u16 b; }
	checksum u8   sum[2] covers(first, second) is summing;
}
"""
	assert len(by_rule(split, "group-covered-regions")) == 1
