"""A reserved field that a variant arm selects, and who enforces it (26.848).

    struct arms { u8 kind;
                  variant body switch (kind) {
                      case 1:  reserved u8 [must_be_zero];
                      default: u8 other; } }

**Nobody did.** Section 8.8 calls reserved bits malleability control -- a
receiver that ignores them lets a sender vary bytes the format calls
fixed, which is a bypass primitive in anything authenticated -- and these
bytes were free in all four languages situ generates. Two routes could
have reached the member and neither asked: the validate loop iterates the
struct's own entries and an arm's member has a dotted path, and
`_arm_member_checks`, which is reached through the variant and exists for
exactly this case, asked the enum and the attribute bounds only.

A reserved field inside a STRUCT arm was always enforced, through that
struct's own `validate`, which is why nine of the corpus's ten reserved
arm members were covered and the tenth -- the only FIELD arm, added for
this -- was not.

**Asked as a pair, which is the whole of the fix.** The byte that is
reserved under `kind = 1` is an ordinary member under any other
discriminant, so a check asked unconditionally refuses every message the
other arm describes. That is not hypothetical: `example/packet` emitted
the `connect` arm's magic inline and refused every packet that was not a
CONNECT (26.414), and the answer to a check asked unconditionally is to
ask it conditionally rather than to delete it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import fourway
from test_walker_c import (COMPILER, _inline_image, c_verdict,
                           python_verdict, shape_named_text)

SCHEMA = """target buffer;

struct arms {
	u8 kind;
	variant body switch (kind) {
		case 1:  reserved u8 [must_be_zero];
		default: u8  other;
	}
}
"""

#: message -> `validate`'s verdict. The six readers share one vocabulary
#: here: `0` is OK and `2` is Constraint, which is the code the refusal has
#: to be rather than merely nonzero -- a bounds error (`1`) over these bytes
#: would mean the arm was read outside the frame, which is a different
#: answer that happens to be red.
CASES = {
	"0100": "0",	# the arm is selected and the reserved byte is zero
	"01ff": "2",	# ...and broken
	"0101": "2",	# ...by one bit, which is the malleability 8.8 names
	"00ff": "0",	# the other arm: the same byte is `other`, and free
	"02ff": "0",
}

#: `must_be_one` beside `must_be_zero`, AT THE SAME OFFSET, which is the
#: strongest form the construct can take: one byte, two policies, and which
#: one applies is the discriminant's answer. The value compared against is
#: read from the policy and the field's width rather than assumed to be
#: zero -- `must_be_one` means every bit, so a reserved `u8` is compared
#: against 0xFF, and four backends plus the packer each derive that width.
#:
#: A narrower field would say it better and an arm cannot be one: `u4` is
#: refused with "arm `arm` must be a whole number of bytes", so the nibble
#: case cannot be posed here and the width derivation is exercised at eight
#: bits.
ONES = """target buffer;

struct arms {
	u8 kind;
	variant body switch (kind) {
		case 1:  reserved u8 [must_be_one];
		case 2:  reserved u8;
		default: u8  other;
	}
}
"""

#: The same byte under three arms: all bits for `case 1`, none for `case 2`,
#: and anything at all for the member the default arm names.
ONES_CASES = {
	"01ff": "0",
	"01fe": "2",	# one bit short of every bit
	"0100": "2",
	"0200": "0",	# `case 2` states no policy, so 8.8's default applies
	"02ff": "2",
	"00ff": "0",	# the default arm: `other` is an ordinary member
}


#: A PAD inside an arm, which is where this fix had to stop rather than
#: widen. `pad_to(4)` is legal in an arm, its policy is always
#: `must_be_zero` -- the compiler refuses `[must_be_one]` on a pad, saying
#: the attribute "belongs on a `reserved` member, whose policy it sets" --
#: and NONE of the six readers checks it. That is asserted here as
#: agreement rather than as enforcement, which is the honest shape for a
#: named decline: the day somebody teaches four backends to check a pad arm
#: without the packer, this goes red.
#:
#: Why it is declined rather than checked: the pad spans three bytes and
#: `body_pad` reads ONE of them -- `self._read(1, 1, ...)` while bounds-
#: checking three -- so a comparison through the accessor catches a nonzero
#: first byte and misses the other two. Measured on `01000001` with the
#: scalar comparison in place: all four answered OK. The member path
#: answers with a run scan instead (the packer's kind 13), and porting that
#: to an arm is its own piece of work (26.848).
PAD = """target buffer;

struct arms {
	u8 kind;
	variant body switch (kind) {
		case 1:  pad_to(4);
		default: u8  other;
	}
}
"""

#: Four bytes, the pad being bytes 1..3, and every one of these accepted by
#: all six. The two that SHOULD be refused are marked, so the entry says
#: what the gap is rather than merely pinning today's answer.
PAD_CASES = {
	"01000000": "0",
	"01000001": "0",	# the gap: the last byte of the span is unread
	"01010000": "0",	# the gap: the first byte is read by nobody either
	"00ffffff": "0",	# correct: the default arm, one byte of `other`
}


def _verdict(answers: str) -> str:
	section = answers[answers.index("-- arms"):]
	return next(line.split()[1] for line in section.splitlines()
	            if line.startswith("validate"))


@pytest.mark.skipif(not fourway.COMPLETE, reason="needs all four toolchains")
@pytest.mark.parametrize("text, cases",
                         [(SCHEMA, CASES), (ONES, ONES_CASES),
                          (PAD, PAD_CASES)],
                         ids=["must_be_zero", "must_be_one", "pad"])
def test_all_four_enforce_a_reserved_arm(text: str, cases: dict[str, str],
		tmp_path: Path) -> None:
	schema = tmp_path / "unit.situ"
	schema.write_text(text, encoding="ascii")
	command = fourway.build(tmp_path, schema)
	assert command, "the fragment built no drivers"

	for message, verdict in cases.items():
		given = {name: _verdict(fourway.answers(
		                argv, bytes.fromhex(message), tmp_path))
		         for name, argv in command.items()}
		assert set(given.values()) == {verdict}, (message, given)


@pytest.mark.skipif(COMPILER is None, reason="no C compiler")
@pytest.mark.parametrize("text, cases",
                         [(SCHEMA, CASES), (ONES, ONES_CASES),
                          (PAD, PAD_CASES)],
                         ids=["must_be_zero", "must_be_one", "pad"])
def test_both_walkers_enforce_a_reserved_arm(text: str, cases: dict[str, str],
		tmp_path: Path) -> None:
	"""The other two readers, which are the independent ones.

	The four backends share an emitter tree; the walkers share only the
	image, so they are what can disagree with all four at once -- and they
	did, by the same gap one layer down. The image is written in two
	passes, and the arm pass (26.418) writes "the two families the four
	backends check on an arm and nothing else": a pinned run, a delimiter,
	a run's span, a scalar's bounds, an enum's membership. A reserved field
	was in neither pass, so both walkers read `01ff` as clean while
	refusing the same byte for the same field outside an arm.

	The same verdict words as the four, deliberately compared without a
	mapping: the walkers answer `2` for Constraint and so do the drivers,
	and a translation table here would be a second place to be wrong about
	what the two readers are being asked.
	"""
	blob  = _inline_image(text)
	shape = shape_named_text(text, "arms")
	for message, verdict in cases.items():
		raw       = bytes.fromhex(message)
		expected  = verdict
		in_python = python_verdict(blob, raw, shape)
		in_c      = c_verdict(tmp_path, blob, raw, shape)
		assert str(in_python) == str(in_c) == expected, \
			(message, in_python, in_c)
