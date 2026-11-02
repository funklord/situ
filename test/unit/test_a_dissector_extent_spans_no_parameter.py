"""A dissector's extent counts a `parameter` as nothing (26.808).

    struct entry { parameter u16 block [stream]; u8 tag; u8 data[block]; }
    struct vol   { u8 block [max = 6]; entry(block = block) ent; u8 after; }

The Lua extent function added every fixed member's `size_bits`, so
`entry_extent` was `3 + block` where `entry` is `1 + block` bytes: the
parameter's two bytes of width are not in the message. Every other
function that adds members up asks `traverse.fixed_span_bits`, which
says so. Nothing measured an argued struct from outside until 0068 let
one be nested, and then `vol.after` was shown two bytes late.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from test_dissector import LUA, compare

SCHEMA = """target buffer;
endian big;
struct entry { parameter u16 block [stream]; u8 tag; u8 data[block]; }
struct vol { u8 block [max = 6]; entry(block = block) ent; u8 after; }
"""


@pytest.mark.skipif(LUA is None, reason="no Lua interpreter")
def test_the_dissector_agrees_with_the_walker(tmp_path: Path) -> None:
	schema = tmp_path / "unit.situ"
	schema.write_text(SCHEMA, encoding="ascii")
	result = compare(schema)
	assert not result.differ, result.differ
	assert result.compared > 0
