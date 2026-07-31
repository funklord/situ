"""An `indexed` region's offset table (project.md section 9.3).

The construct buys O(1) access to elements that need not be the same size, and
pays for it with a table of offsets. Where those offsets are measured from is
decision 0024's question, and it had no answer for a long time: section 9.3 has
written `base = table_start` since before there was a parser for it, and the
parser never read the argument. Nothing noticed, because no backend walked the
table -- so nothing ever needed to know where an offset pointed.

These tests cover the front end of closing that: the table's shape reaches the
solver, and a `base` no walk could resolve is refused where it is written.
"""

from __future__ import annotations

import pytest

from situc import ast
from situc.diagnostics import SituError
from situc.layout import IndexTable, solve
from situc.parser import parse_text

PREAMBLE = "target buffer;\nendian big;\nstruct R { u32 id; }\n"


def region(source: str) -> ast.Indexed:
	schema = parse_text(PREAMBLE + source, path="s.situ")
	held   = [member for struct in schema.structs() for member in struct.members
	          if isinstance(member, ast.Indexed)]
	assert held
	return held[0]


def table(source: str) -> IndexTable:
	schema = parse_text(PREAMBLE + source, path="s.situ")
	for struct in solve(schema).structs.values():
		for placement in struct.placements:
			if placement.kind == "indexed":
				assert placement.index_table is not None
				return placement.index_table
	raise AssertionError("no indexed placement")


def rendered(source: str) -> str:
	with pytest.raises(SituError) as caught:
		parse_text(PREAMBLE + source, path="s.situ")
	return caught.value.diagnostic.render()


SIMPLE = ("struct S { u16 n;"
	" indexed(offset_type = u16, count = n) { R entries[]; } }")


# -- the table's shape reaches the solver -----------------------------------


def test_the_entry_width_comes_from_the_offset_type() -> None:
	assert table(SIMPLE).entry_bits == 16


def test_a_count_from_a_field_is_recorded_as_a_path() -> None:
	held = table(SIMPLE)

	assert held.count_path == "n"
	assert held.count_fixed is None


def test_a_literal_count_is_resolved() -> None:
	held = table("struct S { u32 head;"
	             " indexed(offset_type = u32, count = 4) { R entries[]; } }")

	assert held.count_fixed == 4
	assert held.count_path is None


def test_the_element_type_is_recorded() -> None:
	assert table(SIMPLE).element == "R"


# -- what an offset is measured from (decision 0024) ------------------------


def test_the_region_is_the_default() -> None:
	"""Invariant 9's rule, not a preference: an offset measured from the region
	cannot name a byte outside it, so the region's extent bounds it."""
	assert region(SIMPLE).base is ast.IndexBase.REGION
	assert table(SIMPLE).base == "region"


def test_the_region_may_be_said_out_loud() -> None:
	held = region("struct S { u16 n; indexed(offset_type = u16, count = n,"
	              " base = region) { R entries[]; } }")

	assert held.base is ast.IndexBase.REGION
	assert held.base_member is None


def test_the_message_is_declared() -> None:
	"""A TIFF IFD and a ZIP central directory both measure from the start of
	the file, and an offset that can name anything in the message is the loud
	case."""
	held = region("struct S { u16 n; indexed(offset_type = u16, count = n,"
	              " base = message) { R entries[]; } }")

	assert held.base is ast.IndexBase.MESSAGE
	assert table("struct S { u16 n; indexed(offset_type = u16, count = n,"
	             " base = message) { R entries[]; } }").base == "message"


def test_a_member_may_be_the_base() -> None:
	"""TrueType's `loca` measures from the start of a different table, which is
	what section 9.3 meant by `base = table_start`."""
	held = region("struct S { u32 head; u16 n;"
	              " indexed(offset_type = u16, count = n, base = head)"
	              " { R entries[]; } }")

	assert held.base is ast.IndexBase.MEMBER
	assert held.base_member == "head"


def test_a_base_naming_nothing_is_refused() -> None:
	report = rendered("struct S { u16 n; indexed(offset_type = u16, count = n,"
	                  " base = nonesuch) { R entries[]; } }")

	assert "`base` names `nonesuch`, which is not a member of `S`" in report
	assert "declared before this region: `n`" in report
	assert "`region` measures from the table itself" in report


def test_a_base_declared_later_is_refused() -> None:
	"""The rule a size expression follows: the base has to be readable at the
	moment the table is walked."""
	report = rendered("struct S { u16 n; indexed(offset_type = u16, count = n,"
	                  " base = trailer) { R entries[]; } u32 trailer; }")

	assert "which is declared after this region" in report
	assert "readable at the moment the table is walked" in report


def test_a_base_that_is_not_a_name_is_refused() -> None:
	report = rendered("struct S { u16 n; indexed(offset_type = u16, count = n,"
	                  " base = 4) { R entries[]; } }")

	assert "`base` names what the offsets are measured from" in report
	assert "`region`, `message`, or a member declared before this one" in report


def test_the_base_shows_in_the_dump() -> None:
	from situc.dump import dump

	dumped = dump(parse_text(PREAMBLE + "struct S { u32 head; u16 n;"
	                         " indexed(offset_type = u16, count = n,"
	                         " base = head) { R entries[]; } }"))

	assert "indexed entries base=member(head)" in dumped
