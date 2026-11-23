"""Which spellings of a text number a reader accepts is the schema's choice.

The holder, asked through fuzznet (inbox 20261123T1050Z) whether leading
zeros are allowed: "Shouldn't this be programmable in situ?" It is:
without `[minimal]` every form accepts `007` and the map calls the field
NonCanonical; with it, every form refuses a leading zero, and a
hexadecimal one an upper-case digit as well. These are the verdicts,
worked by hand and held in all four backends, one form at a time:
delimited (`until`), to the end of the frame (`[remaining]`, fuzznet's
settings value), a fixed width, hexadecimal and signed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import fourway

FORMS = """target buffer;
endian big;
struct s {
	decimal u32 a until "," MIN;
	hex u32     b until "," MIN;
	decimal i32 c until "," MIN;
	decimal u16 d[3] MIN;
	decimal u64 e[remaining] MIN;
}
"""

#: One field spelled otherwise at a time; the rest are each value's one
#: spelling, so a refusal names the field under test.
PLAIN = {"a": b"1", "b": b"1", "c": b"1", "d": b"100", "e": b"1"}


def _packet(field: str, spelling: bytes) -> bytes:
	parts = dict(PLAIN, **{field: spelling})
	return (parts["a"] + b"," + parts["b"] + b"," + parts["c"] + b","
	        + parts["d"] + parts["e"])


#: (field, spelling, accepted without [minimal], accepted with it)
CASES = [
	("a", b"007", True, False),
	("a", b"0", True, True),
	("b", b"0ff", True, False),
	("b", b"FF", True, False),
	("b", b"ff", True, True),
	("c", b"-007", True, False),
	("c", b"-7", True, True),
	# A sign is one optional `-`: `+7` and `-0` are second spellings of
	# values that have one, refused whatever the schema says (8.6.2).
	("c", b"+7", False, False),
	("c", b"-0", False, False),
	("d", b"007", True, False),
	("e", b"007", True, False),
	("e", b"0", True, True),
]


@pytest.mark.skipif(not fourway.COMPLETE, reason="needs all four toolchains")
@pytest.mark.parametrize("minimal", [False, True], ids=["loose", "minimal"])
def test_every_form_takes_the_schemas_word(tmp_path: Path,
                                           minimal: bool) -> None:
	source = tmp_path / "unit.situ"
	source.write_text(FORMS.replace("MIN", "[minimal]" if minimal else ""),
	                  encoding="ascii")
	command = fourway.build(tmp_path, source)
	assert len(command) == 4
	assert "validate 0" in fourway.answers(
		command["c"], _packet("a", PLAIN["a"]), tmp_path)
	for field, spelling, loose, strict in CASES:
		packet = _packet(field, spelling)
		given  = {name: fourway.answers(argv, packet, tmp_path)
		          for name, argv in command.items()}
		assert len(set(given.values())) == 1, (field, spelling, given)
		accepted = "validate 0" in given["c"]
		assert accepted == (strict if minimal else loose), (field, spelling)
