"""`u8 payload[sum(lens)]`: lengths first, then what they size (26.896).

fuzznet's spool message is `leaf_len[count], payload`, and a length-prefixed
chunk array is a different wire layout. Without this the payload was an opaque
`[remaining]` run, so nothing tied it to the table and nothing after it could
be placed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import pytest

import fourway
from situc.diagnostics import SituError
from situc.layout import solve
from situc.parser import parse_text

HEAD = "target buffer;\nendian big;\n"
TABLE = HEAD + "struct s { u8 n; u16 lens[n]; u8 payload[sum(lens)]; u8 t; }\n"
LITTLE = TABLE.replace("endian big", "endian little")


def refusal(body: str) -> str:
	with pytest.raises(SituError) as caught:
		solve(parse_text(HEAD + body, path="s.situ"))
	return caught.value.diagnostic.render()


@pytest.mark.parametrize("body", [
	"struct s { u8 n; u8 x[sum(n)]; }",
	"struct s { i8 lens[2]; u8 x[sum(lens)]; }",
	"struct s { u8 x[sum(lens)]; u8 lens[2]; }",
	"struct s { u8 lens[remaining]; }\nstruct t { u8 x[sum(lens)]; }",
	'struct s { u8 lens[] until ","; u8 x[sum(lens)]; }',
])
def test_it_takes_a_counted_run_of_unsigned_integers(body: str) -> None:
	assert "takes a counted run of unsigned integers" in refusal(body + "\n")


@pytest.mark.skipif(not fourway.COMPLETE, reason="needs all four toolchains")
@pytest.mark.parametrize("text, order", [(TABLE, "big"), (LITTLE, "little")])
def test_the_four_place_what_follows(tmp_path: Path, text: str,
		order: Literal["big", "little"]) -> None:
	source = tmp_path / "unit.situ"
	source.write_text(text, encoding="ascii")
	command = fourway.build(tmp_path, source)
	assert len(command) == 4
	two, one = (2).to_bytes(2, order), (1).to_bytes(2, order)
	whole = bytes([2]) + two + one + b"\xaa\xbb\xcc\x07"
	lying = bytes([2]) + (9).to_bytes(2, order) * 2 + b"\x01\x02"
	huge  = bytes([1]) + b"\xff\xff" + b"\x01"
	for packet in (whole, lying, huge, whole[:3], b"\x00\x07"):
		given = {name: fourway.answers(argv, packet, tmp_path)
		         for name, argv in command.items()}
		assert len(set(given.values())) == 1, (packet.hex(), given)
	good = fourway.answers(command["c"], whole, tmp_path)
	assert "payload len=3 first=170" in good
	assert "t 7" in good and "validate 0" in good
	assert "validate 0" not in fourway.answers(command["c"], lying, tmp_path)
