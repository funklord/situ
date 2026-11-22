"""Two reads of what a member declares, bounded as the backends bound them
(26.1142).

Found by fuzzing the Python walker coverage-guided against the generated
Python reader's differ -- the corpora replayed before were guided by the
generated code, not by the walk.

- `u8 n; decimal u32 count[n]` over `30 30` declares 48 digits with one
  in the frame. `digits_of` sliced the buffer, which is quietly shorter
  past its end, read the one digit and answered `count ok=1 value=0`;
  the four backends and the C walker refuse the read.
- wasm's `uleb count; uleb type_index[count]` with a five-byte count past
  32 bits: the backends hold every leaf of a size expression to
  2147483647 (14.2b), and so does this walk for a run of records -- but
  a run of varints reported the raw 34359738367.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "test" / "unit"))

from every_schema import load_schema				# noqa: E402
from situc import pack as packer				# noqa: E402
from situc.layout import solve					# noqa: E402
from situc.resolve import resolve				# noqa: E402
from walker import report					# noqa: E402
from walker.image import load					# noqa: E402


def _section(schema: Path, data: bytes, struct: str) -> list[str]:
	parsed = load_schema(schema)
	image = load(packer.pack(parsed, resolve(parsed, solve(parsed)),
	                         metadata=True)[0])
	lines = report.listing(image, data).splitlines()
	at = lines.index(f"-- {struct}")
	end = next((i for i in range(at + 1, len(lines))
	            if lines[i].startswith("-- ")), len(lines))
	return lines[at + 1:end]


def test_digits_the_frame_does_not_hold_are_no_number() -> None:
	said = _section(ROOT / "test" / "schema" / "edges.situ",
	                bytes.fromhex("3030"), "counted_digits")
	assert "count ok=0 value=0" in said, said


def test_a_varint_run_count_is_bounded_at_the_leaf() -> None:
	said = _section(ROOT / "example" / "wasm" / "wasm.situ",
	                bytes.fromhex("ffffffff7f" "68"), "function_section")
	assert "type_index count=2147483647 span=1 [0]=104" in said, said
