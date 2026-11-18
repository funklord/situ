"""A scaled text number in an unsigned member refuses a minus sign (26.1072).

`situ_parse_scaled` built the magnitude's ceiling from the member's minimum,
and from a minimum of zero that came out zero; `ceiling - digit` then
wrapped, so every digit passed and `-75` in a `scaled u16` read as 65461 in
C, C++ (which calls C's) and Rust (whose debug build would have panicked on
the same subtraction). Python and both walkers refused it. The walker's
sweep found it, its draws differing from the four-way's, once it listed text
numbers at all.

`-0` stays a number, zero, in all four: it is the one spelling a minus sign
leaves room for in an unsigned member, and Python and the walkers already
read it so.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import fourway

SCHEMA = """target buffer;
endian big;

struct reading {
	scaled u16  value  until " "  max 12;
	u8          rest[remaining];
}
"""

#: The member's digits, then what every backend's getter answers.
CASES = {
	"-75 ": "value ok=0 value=0 exp=0",
	"-0 ":  "value ok=1 value=0 exp=0",
	"7 ":   "value ok=1 value=7 exp=0",
	"75.5 ": "value ok=1 value=755 exp=-1",
	"65536 ": "value ok=0 value=0 exp=0",
}


@pytest.mark.skipif(not fourway.COMPLETE, reason="needs all four toolchains")
def test_all_four_refuse_a_negative_unsigned_scaled_number(
		tmp_path: Path) -> None:
	schema = tmp_path / "unit.situ"
	schema.write_text(SCHEMA, encoding="ascii")
	command = fourway.build(tmp_path, schema)
	assert set(command) == {"c", "cpp", "rust", "python"}, command

	for digits, want in CASES.items():
		raw = digits.encode("ascii")
		for name, argv in command.items():
			said = [line for line in
			        fourway.answers(argv, raw, tmp_path).splitlines()
			        if line.startswith("value ok=")]
			assert said == [want], (name, digits, said)


NARROW = """target buffer;
endian big;
bit_order msb_first;

struct narrow {
	decimal u3  d[1];
	hex     u2  h[1];
	u8          rest[remaining];
}
"""


@pytest.mark.skipif(not fourway.COMPLETE, reason="needs all four toolchains")
def test_all_four_refuse_a_digit_wider_than_its_member(tmp_path: Path) -> None:
	"""`situ_parse_uint` checked `value > (max - digit) / radix`, and below
	sixteen `max - digit` wrapped: `9` read into a `decimal u3` and `F` into
	a `hex u2` in C, C++ and Rust, `validate` passing the message, where
	Python refused both (26.1072). No corpus schema has a text number this
	narrow, so no sweep had asked."""
	schema = tmp_path / "narrow.situ"
	schema.write_text(NARROW, encoding="ascii")
	command = fourway.build(tmp_path, schema)
	for raw, want in ((b"9F", ["d ok=0 value=0", "h ok=0 value=0"]),
	                  (b"73", ["d ok=1 value=7", "h ok=1 value=3"])):
		for name, argv in command.items():
			said = [line for line in
			        fourway.answers(argv, raw, tmp_path).splitlines()
			        if line.startswith(("d ok=", "h ok="))]
			assert said == want, (name, raw, said)
