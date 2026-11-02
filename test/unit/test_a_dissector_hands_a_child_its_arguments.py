"""The dissector hands a nested member its arguments (0068 step 6).

    struct entry { parameter u16 block [stream]; u8 tag; u8 data[block]; }
    struct vol   { u16 block; entry(block = block) ent; u8 after; }

A struct's reads of its parameters go through `<s>_arg`, which falls back
to its own preference at the top of a capture. A parent sets the value
there for the length of one call -- `Dissector:call` takes no arguments
-- and `handing` restores it after. So `ent` is sized by `vol.block` and
`after` is shown where the walker reads it. A run of argued elements is
still declined (26.803), and says so.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from situc.diagnostics import Source
from situc.dissector import generate
from situc.layout import solve
from situc.parser import parse
from situc.resolve import resolve
from test_dissector import LUA, compare

SCHEMA = """target buffer;
endian big;
struct entry { parameter u16 block [stream]; u8 tag; u8 data[block]; }
struct vol { u16 block [max = 6]; entry(block = block) ent;
             entry(block = 2) two; u8 after; }
struct arms { u8 block [max = 6]; u8 k;
              variant body switch (k) { case 1: entry(block = block) one;
                                        default: u8 raw; }
              u8 after; }
struct runs { u8 block; u8 n; entry(block = block) many[n]; u8 after; }
"""


def _lua() -> str:
	schema = parse(Source("unit.situ", SCHEMA))
	return generate(schema, resolve(schema, solve(schema)), "unit")


@pytest.mark.skipif(LUA is None, reason="no Lua interpreter")
def test_the_dissector_agrees_with_the_walker(tmp_path: Path) -> None:
	schema = tmp_path / "unit.situ"
	schema.write_text(SCHEMA, encoding="ascii")
	result = compare(schema)
	assert not result.differ, result.differ
	assert result.compared > 0


def test_the_parent_hands_the_value_and_a_run_is_declined() -> None:
	lua = _lua()
	assert "handing(entry_arg, {block = situ_uint(tvb, 0, 2, false)}" in lua
	assert "runs.many: a run its parent hands arguments" in lua
	# The child at the top of a capture still reads its preference.
	assert "__index = function(_, k) return entry.prefs[k] end" in lua
