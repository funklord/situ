"""A fixed-width text number as a variant arm: can the four be asked at all?

    struct arms {
    	u8 kind;
    	variant body switch (kind) {
    		case 1:  decimal u32 code[3] [min = 200, max = 599];
    		default: u8  other;
    	}
    	u8 tail;
    }

**No, until this.** All four backends decline the member in words --
"`code` is not a shape this backend reaches into yet" -- emitting only the
infallible `_value` helper that the offset arithmetic AFTER the arm needs.
`situc diff`'s driver generator did not match that decline: the construct
names no count, so it fell past every clause in `_arms` to the scalar
probe and the driver asked for `situ_arms_body_code_get`, which nothing
defines. `cc` refused it and offered `situ_arms_body_other_get` instead.

Not compiling is the good outcome. What it MEANT is the finding: the
construct could not be compared four ways at all, so no differential
could say anything about it -- which is how 26.850's gap, a declared
`[min]`, `[max]` and `decimal` that none of the six readers enforces, got
to stay invisible under a harness built to find exactly that.

The delimited form of the same construct -- `decimal u32 code[] until " "`
-- IS asked and always was, because every backend gives that one span
accessors. The corpus carries one text-number arm,
`edges.wide_delim_arm.held.code`, and it is the delimited one, so no
corpus driver changes: 172 of them, 43 schemas in four languages,
byte-identical with the skip and without.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import fourway
from test_walker_c import (COMPILER, _inline_image, c_verdict,
                           python_verdict, shape_named_text)

SCHEMA = """target buffer;
endian big;

struct arms {
	u8 kind;
	variant body switch (kind) {
		case 1:  decimal u32 code[3] [min = 200, max = 599];
		default: u8  other;
	}
	u8 tail;
}
"""

#: In range, and the three the schema forbids. The verdicts are not asserted
#: here -- 26.850 records that all six accept every one of them -- and what
#: is asserted is that the question can be PUT, which it could not be.
MESSAGES = ("01323030 00", "01313939 00", "01363030 00", "01414243 00")


@pytest.mark.skipif(not fourway.COMPLETE, reason="needs all four toolchains")
def test_the_four_drivers_build_for_a_text_number_arm(tmp_path: Path) -> None:
	"""The driver compiles in all four languages, and answers.

	`fourway.build` raises `BuildFailed` where a driver names a function no
	backend defines, so this failing is what the defect looked like: a
	`BuildFailed("c", ...)` naming `situ_arms_body_code_get`.

	The answers are read but not asserted against each other, because the
	six readers AGREE here and agree on the wrong thing (26.850). A gate
	asserting today's verdicts would have to be rewritten by the fix; this
	one will not.
	"""
	schema = tmp_path / "unit.situ"
	schema.write_text(SCHEMA, encoding="ascii")
	command = fourway.build(tmp_path, schema)
	assert set(command) == {"c", "cpp", "rust", "python"}, command

	for hexed in MESSAGES:
		raw = bytes.fromhex(hexed.replace(" ", ""))
		for name, argv in command.items():
			answers = fourway.answers(argv, raw, tmp_path)
			# The arm's own line is absent by design -- no backend reaches
			# into the member -- so what proves the driver ran is the
			# struct's section and the member after the variant.
			assert "-- arms" in answers, (name, hexed, answers)
			assert "tail " in answers, (name, hexed, answers)


#: message -> `validate`'s verdict, `0` being OK and `2` Constraint. The
#: three the schema forbids are forbidden three different ways: below the
#: declared minimum, above the declared maximum, and not a number at all.
#: The last is the one the backends' own docstring promises.
CASES = {
	"01323030 00": "0",	# 200, in range
	"01313939 00": "2",	# 199, below `[min = 200]`
	"01363030 00": "2",	# 600, above `[max = 599]`
	"01414243 00": "2",	# "ABC", not digits in base 10
	"00414243 00": "0",	# the default arm: the same bytes are `other`
}


#: The DELIMITED form, which is the same construct with a scan instead of a
#: width -- `edges.wide_delim_arm.held.code` is the corpus's own, and this
#: mirrors it. 26.423 declined it in all four together, deliberately, and
#: named the obstacle: the member path reads a parsed property, and an
#: arm's property of that name answers its BYTES. The fixed-width clause
#: above answers it by parsing the span, which is what this needs too
#: (26.852).
DELIMITED = """target buffer;
endian big;

struct arms {
	u8 pick;
	variant held switch (pick) {
		case 1:  decimal u32 code[] until " " max 4 [min = 200, max = 599];
		case 2:  u8  fixed[3];
		default: error;
	}
	u8 after;
}
"""

#: The digits, then the delimiter, then `after`. The second arm is three
#: bytes and no number, which is the pair: the same bytes are constrained
#: under one discriminant and not under the other.
DELIMITED_CASES = {
	"01323030205A": "0",	# "200 ", in range
	"01313939205A": "2",	# "199 ", below the min
	"01363030205A": "2",	# "600 ", above the max
	"01414243205A": "2",	# "ABC ", not digits
	"024142435A":   "0",	# the other arm
}

#: A NARROWER type than the digits can express, which is what makes the
#: declared domain falsifiable at all. `decimal u32 code[3]` has a ceiling
#: of 999 derived from its own three digits, so no three-digit spelling can
#: exceed it -- the row is a tautology and a check resting on it cannot
#: fail. `decimal u8 code[3]` has a ceiling of 255, which three digits CAN
#: exceed, so "999" is the input with an edge.
NARROW = """target buffer;
endian big;

struct arms {
	u8 kind;
	variant body switch (kind) {
		case 1:  decimal u8 code[3] [min = 2];
		default: u8  other;
	}
	u8 tail;
}
"""

NARROW_CASES = {
	"01323535 00": "0",	# "255", exactly the ceiling
	"01393939 00": "2",	# "999", which three digits can spell and `u8`
				# cannot hold -- the declared domain, not the width
	"01303032 00": "0",	# "002", at `[min = 2]`
	"01303031 00": "2",	# "001", below it
	"00393939 00": "0",	# the default arm
}


def _verdict(answers: str) -> str:
	section = answers[answers.index("-- arms"):]
	return next(line.split()[1] for line in section.splitlines()
	            if line.startswith("validate"))


@pytest.mark.skipif(not fourway.COMPLETE, reason="needs all four toolchains")
@pytest.mark.parametrize("text, cases",
                         [(SCHEMA, CASES), (NARROW, NARROW_CASES),
                          (DELIMITED, DELIMITED_CASES)],
                         ids=["wide", "narrow", "delimited"])
def test_all_four_enforce_a_text_number_arm(
		text: str, cases: dict[str, str], tmp_path: Path) -> None:
	"""The declared digits and the declared range, behind the discriminant.

	Asked as a pair with the default arm, which is the whole of the fix:
	the same four bytes are a number the schema constrains under `kind = 1`
	and an ordinary member under anything else, so a check asked
	unconditionally refuses every message the other arm describes
	(26.414).
	"""
	schema = tmp_path / "unit.situ"
	schema.write_text(text, encoding="ascii")
	command = fourway.build(tmp_path, schema)

	for hexed, verdict in cases.items():
		raw = bytes.fromhex(hexed.replace(" ", ""))
		given = {name: _verdict(fourway.answers(argv, raw, tmp_path))
		         for name, argv in command.items()}
		assert set(given.values()) == {verdict}, (hexed, given)


@pytest.mark.skipif(COMPILER is None, reason="no C compiler")
@pytest.mark.parametrize("text, cases",
                         [(SCHEMA, CASES), (NARROW, NARROW_CASES),
                          (DELIMITED, DELIMITED_CASES)],
                         ids=["wide", "narrow", "delimited"])
def test_both_walkers_enforce_a_text_number_arm(
		text: str, cases: dict[str, str], tmp_path: Path) -> None:
	"""The other two readers, which share only the image.

	The packer's arm pass ends by dropping everything that is not
	`Check.CONSTRAINED`, which a text number is not, so neither walker had
	a row to read. Unlike the reserved kind (26.848), the value kinds alone
	are not enough here: `DIGITS_VALID` and `DIGITS_MINIMAL` are member-path
	kinds in both walkers and neither arm reader rendered them.
	"""
	blob  = _inline_image(text)
	shape = shape_named_text(text, "arms")
	for hexed, verdict in cases.items():
		raw = bytes.fromhex(hexed.replace(" ", ""))
		in_python = python_verdict(blob, raw, shape)
		in_c      = c_verdict(tmp_path, blob, raw, shape)
		assert str(in_python) == str(in_c) == verdict, \
			(hexed, in_python, in_c)
