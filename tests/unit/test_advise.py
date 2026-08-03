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

import pytest

from situc import advise, revision
from situc.capability import Axis
from situc.layout import solve
from situc.parser import parse_text
from situc.resolve import ResolvedSchema, resolve

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
	assert "3 member(s)" in found.detail
	assert "`seq`, `kind`, `tail`" in found.detail


def test_the_reordering_suggestion_costs_nothing_and_says_so() -> None:
	"""Reordering moves no bytes. Printing that is the point, not omitting it."""
	found = only(BADLY_ORDERED, "move-dynamic-to-tail")

	assert found.cost.typical == 0
	assert found.cost.worst == 0
	assert found.cost.render() == "cost: nothing"
	assert "3 member(s) return to AbsoluteStatic" in found.yields


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


def test_a_well_ordered_schema_gets_nothing() -> None:
	assert suggestions("struct fine { u32 seq; u16 n [max = 8]; u8 body[n]; }") == []


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
	# Three bytes worst case against four fixed: one byte, not free.
	assert found.cost.worst == 1


def test_an_unbounded_region_is_priced_as_unknown() -> None:
	"""Reporting zero would be a lie in the cheapest possible direction."""
	found = only("struct s { u8 rest[remaining]; }", "bound-unbounded")

	assert found.cost.unknown
	assert found.cost.render() == "cost: depends on the bound chosen"
	assert "statically allocatable" in found.yields
	assert "instead of `remaining`" in found.detail


def test_unequal_variant_arms_are_priced_as_padding() -> None:
	found = only("""enum k : u8 { a = 1, b = 2, }
	struct small { u16 x; }
	struct large { u8 y[10]; }
	struct s {
		k kind;
		variant body switch (kind) { case k.a: small p; case k.b: large q; }
	}
	""", "equalize-variant-arms")

	assert found.cost.worst == 8			# 10 - 2
	assert found.cost.typical == 4			# averaged over the arms
	assert "typical" in found.cost.render() and "worst case" in found.cost.render()


def test_an_alignment_hole_is_free_to_fill() -> None:
	found = by_rule("struct s { u8 a; reserved u8[3]; u8 b; u32 wide; }",
	                "fill-alignment-holes")

	assert found and found[0].cost.typical == 0
	assert "already spends 3 bytes on reserved padding" in found[0].detail


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
	assert "2 covered field(s)" in found.detail
	assert "recomputation over 8 bytes" in found.detail


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
		"uncover-mutable-field",
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

	assert found.yields.startswith(f"{len(gained)} member(s)"), \
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
