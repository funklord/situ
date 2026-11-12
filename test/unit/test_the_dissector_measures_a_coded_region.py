"""The dissector measures a coded region the data sizes (26.965).

    struct s { u8 n; coded body(c) { u16 x; u8 data[n]; } u8 k; }

`byte_span` answers a region's span with `size_bits`, which for one the
data sizes is the fixed part of its interior -- two bytes here -- so `k`
was shown two bytes in for any `n`, in every codec, the length-preserving
ones included. And a region behind a member the data sizes had no length
rule at all in the dissector. It measures a region by the backends' rule
now: the interior's extent through the codec's expansion. Found by
running the dissector against the walker over a sweep of coded regions
the four backends and both walkers already agreed on.

Offsets and values are worked out by hand.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from test_dissector import LUA, compare, read_back
from situc.cli import analyse
from situc.dissector import generate
from situc.parser import parse

CODEC = """codec c {{
	{expansion};
	granularity = byte;
	invertible;
	deterministic;
}}
impl c extern "my_c";
"""

SIZED = """target buffer;
endian big;
{codec}struct s {{
	u8 n;
	coded body(c) {{
		u16 x;
		u8  data[n];
	}}
	u8 k;
}}
"""

#: Expansion, and the region's bytes for an interior of `2 + n`.
EXPANSIONS: dict[str, Callable[[int], int]] = {"length_preserving":            lambda inner: inner,
              "expansion = +4":               lambda inner: inner + 4,
              "expansion = ratio_exact(2, 1)": lambda inner: inner * 2,
              "expansion = ratio_padded(4, 3)":
                  lambda inner: (inner + 2) // 3 * 4}


def _schema(tmp_path: Path, expansion: str) -> Path:
	schema = tmp_path / "s.situ"
	schema.write_text(SIZED.format(codec=CODEC.format(expansion=expansion)),
	                  encoding="ascii")
	return schema


@pytest.mark.skipif(LUA is None, reason="no Lua interpreter")
@pytest.mark.parametrize("expansion", sorted(EXPANSIONS))
def test_the_member_after_the_region_is_where_its_length_says(
		tmp_path: Path, expansion: str) -> None:
	schema = _schema(tmp_path, expansion)
	source, resolved, _ = analyse(schema)
	script = tmp_path / "s.lua"
	script.write_text(generate(parse(source), resolved, "s"),
	                  encoding="ascii")
	for n in (0, 3, 14):
		wire   = EXPANSIONS[expansion](2 + n)
		packet = bytes([n]) + bytes(range(1, wire + 1)) + b"\xEE"
		_, rows = read_back(script, "s", packet)
		shown = {name: (at, length, value)
		         for name, at, length, value in rows}
		assert shown["s.body"][:2] == (1, wire), (n, rows)
		assert shown["s.k"] == (1 + wire, 1, "238"), (n, rows)


@pytest.mark.skipif(LUA is None, reason="no Lua interpreter")
@pytest.mark.parametrize("expansion", sorted(EXPANSIONS))
def test_the_dissector_agrees_with_the_walker(
		tmp_path: Path, expansion: str) -> None:
	result = compare(_schema(tmp_path, expansion))
	assert result.compared > 0, "nothing compared"
	assert not result.differ, "\n".join(result.differ[:3])
