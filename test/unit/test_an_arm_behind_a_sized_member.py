"""An arm member behind a member the data sizes, in the C walker (26.957).

An arm starts where its variant does and is no member of the struct, so
the C walker's offset sum, looking for it among the members, ran off the
end and answered "not a member". Where an arm's offset is written down
that never mattered; behind `u8 pre[m]` it is summed, and every arm read
refused. A `u16` arm validated anyway, having no check to read it for; a
text-number arm reported its digits broken on a message the four backends
and the Python walker accepted. Found by a sweep of variant shapes through
all six readers; `walk.py` has stopped at the variant since 26.909.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from test_walker_c import (COMPILER, c_arms, c_verdict, packed_text,
                           python_arms, python_verdict, shape_named_text)

SCHEMA = """target buffer;
endian big;
struct s {
	u8  m;
	u8  pre[m];
	u8  t;
	variant v switch (t) {
		case 1:  u16 x;
		case 2:  decimal u16 q[3];
		default: u8 z;
	}
	u8  k;
}
"""

#: `m` = 2, so the variant sits at a byte the data decides.
MESSAGES = {
	"a u16 arm":         bytes.fromhex("025f9d01383634"),
	"a text-number arm": bytes.fromhex("025f9d02383634ff"),
	"the default":       bytes.fromhex("025f9d0938ff"),
}


@pytest.mark.skipif(COMPILER is None, reason="no C compiler")
@pytest.mark.parametrize("label", sorted(MESSAGES))
def test_both_walkers_read_the_arm(tmp_path: Path, label: str) -> None:
	blob    = packed_text(SCHEMA)
	shape   = shape_named_text(SCHEMA, "s")
	message = MESSAGES[label]
	assert c_arms(tmp_path, blob, message, shape) \
		== python_arms(blob, message, shape), label
	assert c_verdict(tmp_path, blob, message, shape) \
		== python_verdict(blob, message, shape) == "0", label
