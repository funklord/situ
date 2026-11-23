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


#: (cond, packet, valid), worked by hand. `| end` lets the frame end the
#: run; it does not let the run stop at an element it cannot read and
#: leave the bytes after it unread, which all four did (fuzznet).
ENDS = [
	("1", "01aa", True),
	("1", "01aa02bbcc", True),
	# A `while` run is never empty -- its first element is read before the
	# condition is asked -- so no frame of none is one of these.
	("1", "", False),
	# The second element cut short by the frame, and one past its `max`:
	# neither is an element, so neither ended the run.
	("1", "01aa05", False),
	("1", "01aa09bb", False),
	# Ended by its rule, the element with `n == 2`: what follows is not the
	# run's, and the struct need not fill its frame.
	("n != 2", "02bbccff", True),
	("n != 2", "01aa05", False),
]


@pytest.mark.skipif(not fourway.COMPLETE, reason="needs all four toolchains")
@pytest.mark.parametrize(("cond", "packet", "valid"), ENDS)
def test_a_run_ends_by_its_rule_or_at_the_frame_end(
		tmp_path: Path, cond: str, packet: str, valid: bool) -> None:
	said = _said(tmp_path, cond, bytes.fromhex(packet))
	assert len(set(said.values())) == 1, said
	assert ("validate 0" in said["c"]) == valid, said["c"]


@pytest.mark.skipif(not fourway.COMPLETE, reason="needs a C compiler")
@pytest.mark.parametrize(("cond", "packet", "valid"), ENDS)
def test_both_walkers_agree(tmp_path: Path, cond: str, packet: str,
                            valid: bool) -> None:
	from test_walker_c import (c_verdict, packed_text, python_verdict,
	                           shape_named_text)
	text  = RUN.replace("COND", cond)
	blob  = packed_text(text)
	shape = shape_named_text(text, "s")
	data  = bytes.fromhex(packet)
	said  = python_verdict(blob, data, shape)
	assert (said == "0") == valid, (packet, said)
	assert c_verdict(tmp_path, blob, data, shape) == said, packet
