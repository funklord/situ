"""A wire bound written as an enum arm is compared by the arm's value
(26.1095).

26.1094 read a bare bound naming the member's own enum arm as that arm, so
the signature -- which records a bound's VALUE, 0041 -- turned fuzznet's
committed `must_eq=data` into `must_eq=4`. The two are one constraint, and
the comparison read the text: fuzznet's unchanged `spool/message` reported
four breaking findings. Each side's signature carries its enums' arms, so
the comparison resolves a name against them as it already reduces a hex
literal to its value.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from situc.wire import compare					# noqa: E402

BEFORE = """enum msg_type : u8 unknown=error
  have_query=1 have=2 want=3 data=4

struct msg_data size=2
  @0x0000   1         u8         version  must_eq=1
  @0x0001   1         msg_type type  must_eq=data
"""


def test_an_arm_and_its_value_are_one_constraint() -> None:
	verdict = compare(BEFORE, BEFORE.replace("must_eq=data", "must_eq=4"))
	assert not [one for one in verdict.findings
	            if one.kind == "breaking"], verdict.findings


def test_a_different_arm_is_still_a_break() -> None:
	verdict = compare(BEFORE, BEFORE.replace("must_eq=data", "must_eq=3"))
	assert [one for one in verdict.findings if one.kind == "breaking"], \
		verdict.findings
