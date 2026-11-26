"""A run whose element may run to the frame's end is declined, not broken.

ISO base media's box -- MP4, HEIF, AVIF -- is `u32 size; u8 kind[4];` and
then a body whose length `size` states, except that `size = 1` puts a
64-bit size after the kind and `size = 0` means the box runs to the end of
the file. A run of them, `box items[remaining]`, has elements one of which
may take the rest of the frame, so one box cannot be measured as an element
(`traverse.has_element_extent`).

C++ and Rust decline the run's count and accessor with a comment saying so.
Python emitted `items_count` returning `None` under `-> int`, and an
indexed accessor comparing an index with it -- a TypeError at the first
call (found 2026-11-27 trying ISO-BMFF as a protocol situ should read).
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from situc.codegen.python import generate	# noqa: E402
from situc.layout import solve			# noqa: E402
from situc.parser import parse_text		# noqa: E402
from situc.resolve import resolve		# noqa: E402

BOXES = """target buffer;
endian big;

struct large_body {
	u64 largesize;
	u8  data[max(largesize, 16) - 16];
}

struct box {
	u32 size;
	u8  kind[4];
	variant body switch (size) {
		case 0:  u8 to_end[remaining];
		case 1:  large_body large;
		default: u8 data[max(size, 8) - 8];
	}
}

struct boxes {
	box items[remaining];
}
"""


def test_python_declines_the_run_rather_than_answering_none() -> None:
	schema = parse_text(BOXES, "boxes.situ")
	module = generate(schema, resolve(schema, solve(schema)), "boxes").module
	assert "No items_count" in module
	assert "def items_count" not in module
	assert "def items(" not in module
