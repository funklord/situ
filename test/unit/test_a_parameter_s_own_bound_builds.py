"""A bound on a `parameter` itself builds in all four (26.805).

    struct t { parameter u8 b [max = 10]; u8 x; }

26.545 keeps such a bound: the solver reads it, so the argument a caller
supplies is bounded where a size or another argument depends on it. No
backend checks it in `validate`, which reads the message and not the
argument. Python and Rust never emitted a check; C called `situ_t_b_get`
and C++ `this->b()`, neither of which exists, so neither header compiled.
They now emit none either, and the four agree, check ids included.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import fourway

SCHEMA = """target buffer;
endian big;
struct t { parameter u8 b [max = 10]; u8 x; }
struct u { parameter u16 b [stream, min = 1, max = 99]; u8 x[b]; }
struct plain { u8 y; }
"""


@pytest.mark.skipif(not fourway.COMPLETE, reason="needs gcc, g++ and rustc")
def test_all_four_build_it(tmp_path: Path) -> None:
	"""`plain` is there because the differential driver compares no struct
	that takes an argument, and building it is what compiles the others."""
	schema = tmp_path / "unit.situ"
	schema.write_text(SCHEMA, encoding="ascii")
	assert set(fourway.build(tmp_path, schema)) == {"c", "cpp", "rust",
	                                                "python"}
