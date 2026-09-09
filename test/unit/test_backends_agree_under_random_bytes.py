"""What four backends answer for bytes nobody meant to send.

The suite has compared backends on *well-formed* buffers since the C++ one
landed: one program, one buffer, every field read through both headers. That
check is what makes "a schema means one thing" more than a slogan, and it has
one blind spot -- a message somebody chose to be hostile.

That blind spot cost something twice. A member placed after a variable-length
region has an offset the message decides, and for a length the frame cannot
hold the four did four different things: C read out of bounds, C++ handed out a
span past the buffer, Rust panicked, Python clamped in silence. And a frame
shorter than a struct's minimum was a view in two backends and an error in the
other two, which is the check section 20.2 says every constant-offset access
below it depends on (26.27).

So this asks the other question, over every schema in the repository. The
drivers are generated (`situc/codegen/differ.py`) from the same layout the
accessors come from, so what is asked of one backend is asked of all four:
pseudo-random buffers in, one canonical listing out, diffed. The seed is fixed
so a disagreement reproduces.

*Which* pseudo-random buffers turned out to be most of the question. They were
uniform bytes of uniform length, which is one distribution and the least
searching one: a text protocol never parsed under it, and a frame small enough
for a declared length to overrun it was rare. Drawing from four alphabets and
mostly short lengths -- the same number of buffers, differently spread -- found
four disagreements the first time it ran, one of them a generated accessor
handing a caller fifty-five bytes out of a five-byte frame (26.35).

What is *not* asked is written down in that module: a subset of member kinds,
because a probe that is spelled wrong in one language reports a disagreement
that is not there. The subset is the thing to grow.

It read and never wrote, for its whole life. Every backend emits setters, and a
schema means one thing in four languages only if it also means one thing when
written -- a byte order reversed in a setter, a bit field written with a
read-modify-write that clobbers its neighbour, a member writable in three
languages and not the fourth. There is a write pass now: every writable scalar
takes a pattern, each backend prints what it reads back, and the whole buffer is
printed once at the end. The buffer is the assertion (26.35).

The machinery -- compile four, run four, diff -- moved to `fourway.py` when
`test_composed_schemas` began asking the same question of schemas nobody
wrote. What stays here is the corpus: every schema this repository builds.
"""

from __future__ import annotations

import random
from pathlib import Path

import pytest

from every_schema import SCHEMAS, ids
from fourway import COMPLETE, answers, build, draw
from situc.layout import solve
from situc.parser import parse_text
from situc.resolve import resolve

#: Buffers per schema. Enough to reach past the acquiring bounds check on the
#: bigger frames, few enough that four processes per buffer stay quick.
COUNT = 48
SEED  = 20260801


@pytest.mark.skipif(not COMPLETE, reason="needs all four toolchains")
@pytest.mark.parametrize("schema", SCHEMAS, ids=ids(SCHEMAS))
def test_the_four_agree_about_bytes_nobody_meant_to_send(
		schema: Path, tmp_path: Path) -> None:
	command = build(tmp_path, schema)
	if not command:
		pytest.skip("no struct a driver can acquire")

	rng     = random.Random(SEED)
	reached = 0

	for _ in range(COUNT):
		packet = draw(rng)
		given  = {name: answers(argv, packet, tmp_path)
		          for name, argv in command.items()}

		if "no-view" not in given["c"]:
			reached += 1

		assert len(set(given.values())) == 1, (
			f"{schema.name}: the four disagree about a "
			f"{len(packet)}-byte buffer:\n  {packet.hex()}\n"
			+ "\n".join(f"-- {name}\n{text}" for name, text in given.items()))

	# A run where every buffer was refused at acquisition would pass while
	# testing nothing, which is the failure mode of a random-input test.
	assert reached >= 1, f"{schema.name}: no buffer reached an accessor"

WAIVED = """target buffer;
endian big;

codec sealing_aead {
	length_preserving;
	seekable;
	granularity = byte;
	authenticated;
	invertible;
	deterministic;
}

impl sealing_aead extern "my_sealing_aead";

struct waived {
	u8  hop;
	sealed body(sealing_aead) [allow_unverified_read] {
		u16  seq;
	}
	tag u8  mac[16] covers(body);
}
"""


def test_a_waived_interior_is_compared() -> None:
	"""`[allow_unverified_read]` had the one interior nobody compared.

	Two skips in a row, each right on its own. The gate probe returns early
	on a waived region -- there is no gate type and no `_open` to name -- and
	the test below it skips anything with a `sealed_by`, on the stated
	grounds that "the interior is asked about there". For a gate that is
	true. For a waiver the branch above asked nothing, so the reason was
	false in exactly the case it was being applied to, and the interior fell
	out of the comparison entirely.

	Which matters more here than for an ordinary member: this is the one
	construct in the language whose purpose is to give up a guarantee, so its
	interior is read on a plain view by four separately-written backends and
	was checked against nothing.
	"""
	from situc.codegen import differ

	schema   = parse_text(WAIVED)
	resolved = resolve(schema, solve(schema))
	asked    = differ.asks(
		resolved.structs["waived"],
		{struct.name for struct in differ.structs_of(resolved)})

	locals_ = [ask.local for ask in asked]
	assert "body_seq" in locals_, \
		f"the waived interior is not compared; asked about {locals_}"

	# And on the plain view, not through a gate that does not exist.
	seq = next(ask for ask in asked if ask.local == "body_seq")
	assert seq.probe is differ.Probe.SCALAR
	assert not any(ask.probe is differ.Probe.SEALED for ask in asked), \
		"a waived region has no gate to open"
