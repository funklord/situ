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
