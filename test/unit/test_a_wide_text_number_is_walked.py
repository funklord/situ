"""Both walkers validate a text number whose ceiling is 2**64 - 1 (26.1083).

`decimal u64 value[remaining]` derives its ceiling from its type, and the
image's check row holds a signed `q`, which cannot: the packer marked every
such struct as one the image cannot validate, and both walkers answered
"cannot say" -- a registry's plainest count among them (0070). An unsigned
ceiling past `q` is held as its two's complement and read unsigned.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "test" / "unit"))

import pytest							# noqa: E402

from test_walker_c import (COMPILER, c_verdict, packed_text,	# noqa: E402
                           python_verdict, shape_named_text)


WIDE = """target buffer;
endian big;
struct wide { decimal u64 value[remaining]; }
"""


@pytest.mark.skipif(COMPILER is None, reason="needs a C compiler")
def test_both_walkers_validate_an_unbounded_u64_text_number(
		tmp_path: Path) -> None:
	blob  = packed_text(WIDE)
	shape = shape_named_text(WIDE, "wide")
	for raw, want in ((b"18446744073709551615", "0"),
	                  (b"18446744073709551616", "2"),
	                  (b"7", "0"), (b"x", "2")):
		assert python_verdict(blob, raw, shape) == want, raw
		assert c_verdict(tmp_path, blob, raw, shape) == want, raw
