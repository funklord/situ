"""A run whose element only sometimes runs to the frame's end is a run.

ISO base media's box -- MP4, HEIF, AVIF -- is `u32 size; u8 kind[4];` and
then a body whose length `size` states, except that `size = 1` puts a
64-bit size after the kind and `size = 0` means the box runs to the end of
the file. In a run of them, `box items[remaining]`, a size-0 box takes the
rest of the frame and so is the last, and the run ends with it as any run
ends at its frame.

`traverse.has_element_extent` asked whether ANY arm ran to the end, so the
box could not be an element: C walked the run, and C++, Rust and Python
declined its count -- Python first by answering `None` under `-> int`, a
TypeError at the first call. Asked now whether EVERY instance does
(26.1180), which is the shape that makes a run of one; that one is still
declined.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "runtime" / "python"))

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

#: An element that always takes the rest: a run of these is one element.
TAILS = """target buffer;
endian big;

struct tail {
	u8 kind;
	u8 rest[remaining];
}

struct tails {
	tail items[remaining];
}
"""


def _module(text: str, name: str) -> tuple[str, types.ModuleType]:
	schema = parse_text(text, f"{name}.situ")
	source = generate(schema, resolve(schema, solve(schema)), name).module
	module = types.ModuleType(name)
	exec(compile(source, f"{name}.py", "exec"), module.__dict__)
	return source, module


def _count(module: types.ModuleType, data: bytes) -> int:
	from situ_runtime import Message  # type: ignore[import-not-found]
	whole = module.boxes.at(Message(bytearray(data)), 0, len(data))
	return int(whole.items_count)


def test_python_walks_a_run_whose_last_box_takes_the_rest() -> None:
	source, module = _module(BOXES, "boxes")
	assert "def items_count" in source
	sized = (12).to_bytes(4, "big") + b"free" + b"abcd"
	open_end = (0).to_bytes(4, "big") + b"mdat" + b"payload"
	assert _count(module, sized + open_end) == 2
	assert _count(module, sized + sized) == 2
	# Shorter than a box's header: no box, where Python alone once held one.
	assert _count(module, bytes(4)) == 0


def test_an_element_that_always_takes_the_rest_is_still_declined() -> None:
	source, _ = _module(TAILS, "tails")
	assert "def items_count" not in source
	assert "def items(" not in source
