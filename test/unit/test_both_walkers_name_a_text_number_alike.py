"""Both walkers name the same check refusing a text number (26.1078).

Two ways the C walk named a different check from the Python walk, the
backends and 0051's order, neither reachable from the corpus until a
variable-width `[minimal]` text number was in it:

- it parsed the VALUE before asking the SPELLING, so `a5` -- written
  minimally and not a number -- was `digits_minimal` there and `digits`
  everywhere else;
- a text number the message counts, `count[n]` with `n` past the frame,
  had no fit check: the packer writes no `fits_frame` row for one and the
  Python walk asks it from the placement, so the C walk read the digits and
  named the spelling or the read.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "test" / "unit"))

import pytest							# noqa: E402

from test_walker_c import (COMPILER, WHY, _build_driver, c_why,	# noqa: E402
                           packed_text, shape_named_text)
from walker import report					# noqa: E402
from walker.image import load					# noqa: E402
from walker.walk import acquire					# noqa: E402

pytestmark = pytest.mark.skipif(COMPILER is None, reason="needs a C compiler")

WHOLE = """target buffer;
endian big;
struct x { decimal u32 count[remaining] [minimal]; }
"""

COUNTED = """target buffer;
endian big;
struct x { u8 n; decimal u32 count[n] [minimal]; u8 rest[remaining]; }
"""

CASES = {
	WHOLE: {b"a5": "digits", b"05": "digits_minimal", b"-5": "digits",
	        b"5": None},
	COUNTED: {b"\x09a5": "fits_frame", b"\x02a5": "digits",
	          b"\x0205x": "digits_minimal", b"\x0215x": None},
}


@pytest.mark.parametrize("text", list(CASES), ids=["whole", "counted"])
def test_both_walkers_name_the_check(text: str, tmp_path: Path) -> None:
	blob  = packed_text(text)
	image = load(blob)
	shape = shape_named_text(text, "x")
	drive = _build_driver(tmp_path, blob, WHY)
	for raw, check in CASES[text].items():
		python = report.failed_check(image, acquire(image, raw, shape), shape)
		want   = report.CLEAN if check is None else ("placement[1]"
		         if text is COUNTED else "placement[0]", check)
		assert python == want, (raw, python)
		assert c_why(tmp_path, blob, raw, shape, drive) == python, raw
