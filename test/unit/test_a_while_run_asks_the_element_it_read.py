"""A `while` run asks its condition of the element it just read (26.1131).

Rust read an enum field of the element at a fixed offset from the RUN's
bytes, so in `example/ipv6ext` every header's `next` was the first
header's: a chain that began with `destination` never stopped at a header
saying `tcp`, ran to the frame's end, and Rust alone refused it. Found by
replaying the atheris corpus through the four-way differential; C, C++ and
Python were right.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "test" / "unit"))

import pytest							# noqa: E402

import fourway							# noqa: E402

#: A destination-options header (60), then one saying 169 -- not an
#: extension header, so the chain ends there, exactly at the frame's end.
#: Then the same with a payload byte after the chain.
CHAINS = [bytes.fromhex("3c00000000000000" "a900000000000032"),
          bytes.fromhex("3c00000000000000" "0600000000000000" "ff")]


def _section(said: str, struct: str) -> list[str]:
	lines = said.splitlines()
	at = lines.index(f"-- {struct}")
	end = next((i for i in range(at + 1, len(lines))
	            if lines[i].startswith("-- ")), len(lines))
	return lines[at + 1:end]


@pytest.mark.skipif(not fourway.COMPLETE, reason="needs all four toolchains")
def test_four_backends_end_the_chain_where_a_header_says_so(
		tmp_path: Path) -> None:
	command = fourway.build(tmp_path, ROOT / "example" / "ipv6ext"
	                        / "ipv6ext.situ")
	assert set(command) == {"c", "cpp", "rust", "python"}, command
	for raw in CHAINS:
		said = {name: _section(fourway.answers(argv, raw, tmp_path),
		                       "packet_tail")
		        for name, argv in command.items()}
		for name, lines in said.items():
			assert "chain count=2" in lines, (name, raw.hex(), lines)
			assert "validate 0" in lines, (name, raw.hex(), lines)
		assert len({tuple(lines) for lines in said.values()}) == 1, said
