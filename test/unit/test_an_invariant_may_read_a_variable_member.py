"""`invariant s.len == size(s.body)` over a member the data sizes.

The corpus's one invariant sums scalars, so `size` only ever folded to a number
and no backend had been asked for a run-time extent there. Rust named
`body_extent` on its `Mut`, which has none, and did not compile (26.897).
"""

from __future__ import annotations

from pathlib import Path

import pytest

import fourway

SCHEMA = """target buffer;
endian big;
struct rec { u8 k; u8 v[k]; }
struct s { u16 len; rec body; u8 z; }
invariant s.len == size(s.body);
"""


@pytest.mark.skipif(not fourway.COMPLETE, reason="needs all four toolchains")
def test_the_four_build_and_recompute_alike(tmp_path: Path) -> None:
	source = tmp_path / "unit.situ"
	source.write_text(SCHEMA, encoding="ascii")
	command = fourway.build(tmp_path, source)
	assert len(command) == 4
	packet = bytes([0, 3, 2, 9, 9, 7])
	given = {name: fourway.answers(argv, packet, tmp_path)
	         for name, argv in command.items()}
	assert len(set(given.values())) == 1, given
