"""The dissector assembles a bit field by its bit order, not by `endian`
(26.960).

Every backend loads a bit-packed scalar by its bit order -- `endian` orders
whole-byte scalars -- and so do both walkers. The dissector chose `add_le`
for any field in an `endian little` schema unless the field was LSB-first,
so an MSB-first bit field straddling a byte was assembled little-endian:
every straddling field of five layouts in a little-endian, MSB-first
schema showed a number no other reader gave. Found by running the
dissector against the walker over a sweep of straddling layouts that the
four backends and both walkers already agreed on.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from test_dissector import LUA, compare

SCHEMA = """target buffer;
endian little;
bit_order msb_first;
struct s [allow_straddle] {
	u3   f0;
	u13  f1;
	i7   f2;
	u9   f3;
	u8   k;
}
"""


@pytest.mark.skipif(LUA is None, reason="no Lua interpreter")
def test_a_straddling_field_reads_as_the_walker_reads_it(tmp_path: Path) -> None:
	schema = tmp_path / "bits.situ"
	schema.write_text(SCHEMA, encoding="ascii")
	result = compare(schema)
	assert result.compared > 0, "nothing compared"
	assert not result.differ, "\n".join(result.differ[:3])
