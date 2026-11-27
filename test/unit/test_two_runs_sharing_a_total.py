"""Two runs that share a fixed total have that total as their worst case.

`u8 field[length]; u8 payload[183 - length];` is 183 bytes whatever
`length` is. Summed member by member the worst case was 366 -- each run at
its own maximum, which no message reaches -- so MPEG-TS's adaptation field
was reported at twice its size and `situc advise` told the transport stream
to pad every arm to it (26.1188). Only the upper bound is corrected; the
lower bound and the offsets are the walk's.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from situc.diagnostics import Source
from situc.layout import solve
from situc.parser import parse

ROOT = Path(__file__).resolve().parents[2]
HEAD = "target buffer;\nendian big;\n"


def _max_bytes(body: str) -> int | None:
	schema = parse(Source("unit.situ", HEAD + f"struct s {{ {body} }}\n"))
	held = solve(schema).structs["s"].size_max_bits
	return None if held is None else held // 8


def test_the_complement_is_the_total() -> None:
	assert _max_bytes("u8 n [max = 182]; u8 a[n]; u8 b[183 - n];") == 184


def test_anything_else_still_sums() -> None:
	# Another field, and a different element width.
	assert _max_bytes("u8 n [max = 182]; u8 m [max = 182]; u8 a[n];"
	                  " u8 b[183 - m];") == 2 + 182 + 183
	assert _max_bytes("u8 n [max = 90]; u8 a[n]; u16 b[183 - n];") == 1 + 90 + 366


def test_advise_no_longer_pads_mpegts_arms() -> None:
	said = subprocess.run([sys.executable, "-m", "situc", "advise",
	                       str(ROOT / "example" / "mpegts" / "mpegts.situ")],
	                      cwd=ROOT, capture_output=True, text=True, timeout=120)
	assert said.returncode == 0, said.stderr
	assert "pad every arm" not in said.stdout
