"""A run ends by its own rule, or the frame stopped early (26.926, 26.927).

`T x[] until "D"` passed `validate` with no terminator anywhere, in all six
readers, wherever the run was the last member: the walk stopped at the frame
and nothing asked why. `required` has always called the same bytes incomplete.
The verdicts below are worked by hand, as agreement is what went wrong.

`T x[] while (c)` had the same hole, and `| end` is its answer as well: netlink's
attributes run to the end of their message, and an IPv6 chain may not.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import fourway
from test_an_integer_can_start_inside_a_byte import _all, _built
from test_nesting_costs_no_more_than_its_length import pack_text
from test_walker_c import (VERDICT, _build_driver, c_verdict, python_verdict,
                           shape_named_text)

SCHEMA = """target buffer;
endian big;
struct leaf  { u8 k; }
struct pair  { u8 a; u8 b; }
struct last  { leaf es[] until "\\xff"; }
struct mid   { leaf es[] until "\\xff"; u8 after; }
struct wide  { pair es[] until "\\xff"; }
struct loose { leaf es[] until "\\xff" | end; }
struct two   { leaf es[] until "\\r\\n"; }
struct held  { u8 tag; last inner; }
struct holder { u8 n; last all[n]; }
struct chosen {
	u8 kind;
	variant body switch (kind) {
		case 1:  last in_struct;
		default: error;
	}
}
struct direct {
	u8 kind;
	variant body switch (kind) {
		case 1:  leaf es[] until "\\xff";
		default: error;
	}
}
struct link   { u8 next; u8 v; }
struct chain  { link es[] while (next != 0); }
struct capped { link es[] while (next != 0) max 2; }
struct open   { link es[] while (next != 0) | end; }
struct after  { link es[] while (next != 0); u8 tail; }
struct in_arm {
	u8 kind;
	variant body switch (kind) {
		case 1:  link es[] while (next != 0);
		default: error;
	}
}
struct capped_arm {
	u8 kind;
	variant body switch (kind) {
		case 1:  link es[] while (next != 0) max 2;
		default: error;
	}
}
struct open_arm {
	u8 kind;
	variant body switch (kind) {
		case 1:  link es[] while (next != 0) | end;
		default: error;
	}
}
struct chained { u8 tag; chain inner; }
"""

WHOLE, CUT = "0", "2"

#: struct, message, verdict.
CASES = [
	("last", "ff", WHOLE),
	("last", "0102ff", WHOLE),
	("last", "0102", CUT),
	("mid", "01ff07", WHOLE),
	# The byte is the element's: 0xFF as `b`, where no element starts.
	("wide", "01ffff", WHOLE),
	("wide", "01ff", CUT),
	("wide", "0102" "ff", WHOLE),
	# One byte of a two-byte element, with no room for it or a terminator.
	("wide", "010203", CUT),
	("loose", "0102", WHOLE),
	("loose", "0102ff", WHOLE),
	("two", "0102" "0d0a", WHOLE),
	("two", "0102" "0d", CUT),
	("held", "0701ff", WHOLE),
	("held", "0701", CUT),
	("holder", "0201ff02ff", WHOLE),
	("holder", "0201ff02", CUT),
	("chosen", "0101ff", WHOLE),
	("chosen", "0101", CUT),
	("direct", "0101ff", WHOLE),
	("direct", "0101", CUT),
	# `next` says one more follows, and the frame stops.
	("chain", "0102", CUT),
	("chain", "0102" "0003", WHOLE),
	("chain", "0102" "0103", CUT),
	# Half an element is no element, and the last whole one said go on.
	("chain", "0102" "00", CUT),
	# The cap ends it as well as the condition.
	("capped", "0102" "0103", WHOLE),
	("capped", "0102", CUT),
	("capped", "0003", WHOLE),
	("open", "0102", WHOLE),
	("open", "0102" "0003", WHOLE),
	("after", "0102" "0003" "07", WHOLE),
	("in_arm", "01" "0102" "0003", WHOLE),
	("in_arm", "01" "0102", CUT),
	("capped_arm", "01" "0102" "0103", WHOLE),
	("capped_arm", "01" "0102", CUT),
	("open_arm", "01" "0102", WHOLE),
	("chained", "07" "0003", WHOLE),
	("chained", "07" "0102", CUT),
]


@pytest.mark.skipif(not fourway.COMPLETE, reason = "needs gcc, g++ and rustc")
def test_all_four_ask_the_walk(tmp_path: Path) -> None:
	command = _built(tmp_path, SCHEMA)
	for struct, sample, verdict in CASES:
		assert _all(command, bytes.fromhex(sample), struct, "validate",
		            tmp_path) == verdict, (struct, sample)


def test_both_walkers_ask_the_walk(tmp_path: Path) -> None:
	blob  = pack_text(SCHEMA)
	drive = _build_driver(tmp_path, blob, VERDICT)
	for struct, sample, verdict in CASES:
		shape = shape_named_text(SCHEMA, struct)
		data  = bytes.fromhex(sample)
		assert python_verdict(blob, data, shape) == verdict, (struct, sample)
		assert c_verdict(tmp_path, blob, data, shape, drive) == verdict, \
			(struct, sample)


def test_it_reads_back_as_it_was_written() -> None:
	from situc.parser import parse_text
	from situc.unparse import unparse
	said = unparse(parse_text(SCHEMA, path = "s.situ"))
	assert "while (next != 0) | end;" in said
	assert "while (next != 0) max 2;" in said
