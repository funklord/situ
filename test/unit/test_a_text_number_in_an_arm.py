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
