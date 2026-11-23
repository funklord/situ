"""A `while` run: its condition, and where it may end (26.927).

Found under fuzznet's permit grammars, which read a list of words as
`grant grants[] while (1) | end`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import fourway

RUN = """target buffer;
endian big;
struct w { u8 n [max = 3]; u8 b[n]; }
struct s { w ws[] while (COND) | end; }
"""


def _said(tmp_path: Path, cond: str, packet: bytes) -> dict[str, str]:
	"""Struct `s`'s section of each backend's answer."""
	source = tmp_path / "unit.situ"
	source.write_text(RUN.replace("COND", cond), encoding="ascii")
	command = fourway.build(tmp_path, source)
	assert len(command) == 4
	return {name: fourway.answers(argv, packet, tmp_path).split("-- s\n")[1]
	        .split("\n-- ")[0] for name, argv in command.items()}


@pytest.mark.skipif(not fourway.COMPLETE, reason="needs all four toolchains")
@pytest.mark.parametrize("cond", ["1", "n & 1"])
def test_a_numeric_condition_is_true_where_not_zero(tmp_path: Path,
                                                    cond: str) -> None:
	"""`while (1)` emitted `if !(1)` in Rust, which takes only a bool, and
	the schema did not compile there; C, C++ and Python read a number as
	true where it is not zero, and Rust now says so."""
	said = _said(tmp_path, cond, bytes.fromhex("01aa03bbccdd"))
	assert len(set(said.values())) == 1, said
	assert "ws count=2" in said["c"], said["c"]
