"""A varint is read by its declared encoding wherever it stands (26.919).

A `tlv` tag and a `prefixed(T)` length were read as leb128 whatever `T` said, in
all four backends, and the walkers read them as declared. `minimal` was held to
leb128's length in Python and C++. Each is a message whose answer is worked out
here by hand from the encoding, since four readers agreeing proves nothing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import fourway

SCHEMA = """target buffer;
endian big;
varint_type big14 { encoding = be128; max_bits = 14; }
varint_type sq { encoding = be128; max_bits = 64; max_bytes = 9; minimal; }
struct framed {
	tlv items (
		tag_type     = big14,
		tag_decode   = { kind = tag },
		tag_identity = kind,
		value_size   = switch (kind) { default: prefixed(big14) },
	);
}
struct whole { sq v; u8 last; }
"""

#: message -> what `validate` says of `framed`. `81 00` is 128 in be128 and
#: would be 1 in leb128, so a length read the wrong way falls off the frame.
FRAMED = {
	"01" "03" "aabbcc":             "0",
	"01" "8100" + "cc" * 128:       "0",
	"8100" "01" "aa":               "0",
	"01" "8100" + "cc" * 127:       "1",
}

#: message -> `v`'s line. The ninth byte carries eight bits, so the top of the
#: range takes nine bytes where leb128's count says ten. `value=0` is the getter
#: refusing, as `minimal` is a getter's check and `validate` passes the bytes.
WHOLE = {
	"ffffffffffffffffff" "ee": "len=9 value=18446744073709551615",
	"808080808080808000" "ee": "len=9 value=0",
	"7f" "ee":                 "len=1 value=127",
	"817f" "ee":               "len=2 value=255",
	"807f" "ee":               "len=2 value=0",
}


def _section(answers: str, struct: str) -> str:
	return answers[answers.index(f"-- {struct}\n"):].split("\n-- ")[0]


def _field(section: str, name: str) -> str:
	return next(line.split(" ", 1)[1] for line in section.splitlines()
	            if line.startswith(name + " "))


@pytest.mark.skipif(not fourway.COMPLETE, reason = "needs gcc, g++ and rustc")
def test_all_four_read_be128_as_be128(tmp_path: Path) -> None:
	schema = tmp_path / "unit.situ"
	schema.write_text(SCHEMA, encoding = "ascii")
	command = fourway.build(tmp_path, schema)
	for message, verdict in FRAMED.items():
		given = {backend: _field(_section(fourway.answers(
		             argv, bytes.fromhex(message), tmp_path), "framed"),
		             "validate")
		         for backend, argv in command.items()}
		assert set(given.values()) == {verdict}, (message, given)
	for message, wanted in WHOLE.items():
		given = {backend: _field(_section(fourway.answers(
		             argv, bytes.fromhex(message), tmp_path), "whole"), "v")
		         for backend, argv in command.items()}
		assert set(given.values()) == {wanted}, (message, given)
