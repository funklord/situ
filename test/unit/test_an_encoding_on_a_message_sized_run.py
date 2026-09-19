"""`[encoding = ascii]` is a claim about the bytes, however they are sized.

Three of the four backends did not enforce it on a run whose length the
message declares, each in its own way (26.447): C++ passed a count of
zero so the check ran over no bytes and accepted anything, and Rust and
Python returned before reaching the attribute at all. C was correct
throughout and is the reference.

The literal-length sibling is the instrument. It carries the same
attribute over the same bytes and was answered correctly in all four the
whole time, so a disagreement between the two spellings is the finding
and neither value alone is. `c/emit.py` states the stake: an encoding
nobody checks is worse than none.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fourway import COMPLETE, answers, build

#: Same attribute twice: once over a run the message sizes, once over one
#: the schema does. Sharing a file so a single build answers for both.
SCHEMA = """target buffer;
endian big;

struct declared { u8 n; u8 text[n] [encoding = ascii]; }
struct literal  { u8 text[3] [encoding = ascii]; }
"""

#: 0 clean, 2 refused by a constraint.
OK, CONSTRAINT = "0", "2"


def _verdict(text: str, struct: str) -> str:
	seen = False
	for line in text.splitlines():
		if line.startswith("-- "):
			seen = line == f"-- {struct}"
		elif seen and line.startswith("validate"):
			return line.split()[-1]
		elif seen and line in ("no-view", "needs-arguments"):
			return line
	return "absent"


@pytest.mark.skipif(not COMPLETE, reason="needs all four toolchains")
@pytest.mark.parametrize("struct,message,want", [
	("declared", bytes([2, 0x41, 0xFF]), CONSTRAINT),
	("declared", bytes([2, 0x41, 0x42]), OK),
	("literal",  bytes([0x41, 0xFF, 0x43]), CONSTRAINT),
	("literal",  bytes([0x41, 0x42, 0x43]), OK),
], ids=["declared-rejects-high-byte", "declared-accepts-ascii",
        "literal-rejects-high-byte", "literal-accepts-ascii"])
def test_the_four_backends_enforce_an_encoding_on_either_spelling(
		tmp_path: Path, struct: str, message: bytes, want: str) -> None:
	"""One cell of the pair, in all four backends.

	The two `literal` cases are the control and they are not decoration:
	they passed before the fix and after it, which is what says the check
	itself works and only the message-sized path was wrong. A test of the
	`declared` cases alone could be satisfied by a backend that had
	learned to refuse everything, which is precisely what C++ did to
	`nul_terminated` on the same path.
	"""
	schema = tmp_path / "s.situ"
	schema.write_text(SCHEMA, encoding="ascii")
	command = build(tmp_path, schema)
	assert command, "no backend built the schema"

	said = {name: _verdict(answers(cmd, message, tmp_path), struct)
	        for name, cmd in command.items()}
	assert set(said.values()) == {want}, (struct, message.hex(), said)


@pytest.mark.skipif(not COMPLETE, reason="needs all four toolchains")
def test_a_real_cpio_entry_is_not_refused(tmp_path: Path) -> None:
	"""The corpus payoff, and the reason this was worth chasing.

	`cpio_entry.name` is `u8 name[header.namesize] [nul_terminated]`, and
	the same zero count made generated C++ emit
	`situ_nul_terminated(name().data(), 0)` -- which is false for every
	input, so that backend refused EVERY cpio entry, this project's own
	golden vector included.

	Nothing caught it because the vectors are checked by `situc verify`,
	which runs the accessors in memory through Python -- and Python was
	one of the backends that emitted no check at all on this path. The
	golden vectors were being validated by the one reader that ignored the
	attribute.

	**What this does NOT catch, measured rather than assumed.** Two
	separate changes each cure it on their own: giving the check the run's
	real length, and skipping the check on a message-sized run to match
	the other three backends. Reverting either alone leaves this green,
	and only reverting BOTH -- which is the state it was found in -- turns
	it red. So it guards the behaviour and pins neither mechanism, and a
	future change that drops one of the two will not be caught here. The
	mechanism is guarded by the parametrised cases above, which do fail to
	a single sabotage.
	"""
	cpio = Path(__file__).resolve().parents[2] / "example" / "cpio"
	greeting = bytes.fromhex("".join("""
		30 37 30 37 30 31 30 30 31 42 31 31 32 39 30 30 30 30 38 31
		42 34 30 30 30 30 30 33 45 38 30 30 30 30 30 33 45 38 30 30
		30 30 30 30 30 31 36 41 37 30 42 41 42 42 30 30 30 30 30 30
		30 36 30 30 30 30 30 30 30 30 30 30 30 30 30 30 32 42 30 30
		30 30 30 30 30 30 30 30 30 30 30 30 30 30 30 30 30 30 30 30
		30 44 30 30 30 30 30 30 30 30 67 72 65 65 74 69 6E 67 2E 74
		78 74 00 00 68 65 6C 6C 6F 0A 00 00""".split()))
	assert len(greeting) == 132

	command = build(tmp_path, cpio / "cpio.situ")
	said = {name: _verdict(answers(cmd, greeting, tmp_path), "cpio_entry")
	        for name, cmd in command.items()}
	assert set(said.values()) == {OK}, said
