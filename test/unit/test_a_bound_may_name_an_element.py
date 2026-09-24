"""A bound may read one element of a run, and all four backends agree.

`[max = n[0].n]` bounds a member by a field of the run's first element.
Section 19.3's signature has published such a bound as committed contract for
as long as the parser has accepted one, while `situc build` refused the
schema outright -- two commands disagreeing about one file, which is the
shape 26.497 recorded and left open.

The resolution is shared: `invariant.element_target` answers which run, which
index and which member, out of the struct's own entry table, so what compiles
does not depend on the target. Each backend spells the read, and every one of
them reads at a computed offset rather than walking -- the resolver refuses a
variable stride, a non-literal index and a bit-packed member, so element `k`
is at `run + k * stride + member` and no walk is needed.

**An empty run makes the message malformed.** Element zero of a run of no
elements does not exist, and the bytes at that offset belong to whatever
follows; comparing against them would be comparing against a number nobody
wrote. Settled by the copyright holder 2026-09-24. Each backend emits the
refusal beside the comparison rather than inside it, because the two say
different things: one is "this run is too short to describe", the other
"this value is out of range".

**Four rather than six, and the other two ABSTAIN rather than disagree.**
`pack` folds a bound to a constant and cannot fold one that names a field,
so it sets `whole = False` and the struct is marked not validatable -- the
walkers then decline to give a verdict at all rather than guessing one.
That is `pack.py`'s stated intent and situ's own cannot-say rule, not a
gap in this feature: a plain `[max = count]` has behaved that way for as
long as it has existed. Measured with a control, the same schema with
`[max = 3]` is validatable and answers `validate 0` and `validate 2`
correctly. Recorded in 26.500; making the image carry a bound as bytecode
is a format change and two walkers, and is not this test's to assert.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import fourway

#: `inner` exists to give the run an element with a field in it. The bound
#: names element zero's `n`, and `body` exists so that `len` has something to
#: size -- a bound on a member nothing reads would be checked and pointless.
SCHEMA = """target buffer;
endian big;

struct inner {
	u8  n;
}

struct outer {
	u8     count;
	inner  n[count];
	u16    len  [max = n[0].n];
	u8     body[len];
}
"""

#: count, then that many elements, then `len` big-endian, then `len` bytes.
#:
#: **The empty-run packet is chosen so that only the guard can refuse it**,
#: which took a sabotage to get right. With `count = 0` the read for element
#: zero lands on `len`'s own high byte, so the obvious packet -- `len = 5`,
#: high byte 5 -- is refused by the COMPARISON as readily as by the guard,
#: and a backend emitting no guard passes the test. `len = 0` makes the
#: misread value zero and `0 <= 0` satisfies the bound, so the guard is the
#: only thing left that can speak. Measured both ways: with Rust's guard
#: removed it answers `validate 0` where the other three answer `validate 2`.
CASES = {
	"an empty run has no element zero":   bytes([0, 0, 0]),
	"within the element's bound":         bytes([1, 10, 0, 5]) + bytes(5),
	"past the element's bound":           bytes([1, 3, 0, 5]) + bytes(5),
}


def test_the_four_agree_about_a_bound_naming_an_element(tmp_path: Path) -> None:
	"""Four descriptions of one schema, asked the same question of one buffer.

	Agreement is the property worth holding: a bound one backend enforces and
	another ignores means the schema says two different things about which
	messages are valid, which is what this compiler exists to prevent.
	"""
	schema = tmp_path / "unit.situ"
	schema.write_text(SCHEMA, encoding="ascii")

	try:
		command = fourway.build(tmp_path, schema)
	except fourway.BuildFailed as failed:
		pytest.skip(f"a backend could not be built here: {failed}")

	for label, packet in CASES.items():
		given = {name: fourway.answers(argv, packet, tmp_path)
		         for name, argv in command.items()}
		first = next(iter(given.values()))
		assert all(answer == first for answer in given.values()), (
			f"{label}: the four backends disagree\n"
			+ "\n".join(f"  {name}: {answer!r}"
			            for name, answer in given.items()))
