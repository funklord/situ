"""A padded varint where `minimal` is declared reads as no value (26.1134).

Bitcoin's `compact_size` is `minimal`, so `fd f4 00` -- 244 in three bytes
where one would do -- is refused by every backend's getter: their `_len`
answers 3 and their `_get` refuses, and the differ prints `len=3 value=0`.
The walk's listing measured the length the same way and printed the
decoded 244, because its line read the transform alone and never asked
`varint_admissible`, the getter's refusal it keeps beside it. Random draws
almost never put `fd` before a small number, so the walker differential
had not met it; replaying the fuzz corpora through the walker did, 23
times in bitcoin's.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "test" / "unit"))

import pytest						# noqa: E402

import fourway						# noqa: E402
from every_schema import load_schema			# noqa: E402
from situc import pack as packer			# noqa: E402
from situc.layout import solve				# noqa: E402
from situc.resolve import resolve			# noqa: E402
from walker import report				# noqa: E402
from walker.image import load				# noqa: E402

SCHEMA = ROOT / "example" / "bitcoin" / "bitcoin.situ"

#: version, then an input count of 244 in three bytes, then an output
#: count of 220 in one, which `minimal` admits.
PADDED = bytes.fromhex("23f40000" "fdf400" "dcdcdc")


def _section(text: str, struct: str) -> list[str]:
	lines = text.splitlines()
	at = lines.index(f"-- {struct}")
	end = next((i for i in range(at + 1, len(lines))
	            if lines[i].startswith("-- ")), len(lines))
	return lines[at + 1:end]


def _walked() -> list[str]:
	parsed = load_schema(SCHEMA)
	resolved = resolve(parsed, solve(parsed))
	image = load(packer.pack(parsed, resolved, metadata=True)[0])
	return _section(report.listing(image, PADDED), "transaction")


def test_the_walk_gives_a_padded_count_no_value() -> None:
	walked = _walked()
	assert "input_count len=3 value=0" in walked, walked
	assert "output_count len=1 value=220" in walked, walked


@pytest.mark.skipif(not fourway.COMPLETE, reason="needs all four toolchains")
def test_the_walk_and_c_say_the_same_of_it(tmp_path: Path) -> None:
	command = fourway.build(tmp_path, SCHEMA)
	said = _section(fourway.answers(command["c"], PADDED, tmp_path),
	                "transaction")
	for line in _walked():
		if line.split(" ", 1)[0] in ("input_count", "output_count"):
			assert line in said, (line, said)
