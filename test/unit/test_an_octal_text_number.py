"""`octal` is a radix, beside `decimal` and `hex` (26.756).

cpio's portable `odc` header writes every number as fixed-width octal
text, and so does tar's ustar header; `radix` was `decimal | hex |
scaled`, so neither could be described past its magic. hull asked for it
(suggestion/hull.md section 19): it reads tar from registries and
`docker export` at hand-written offsets.

Every runtime parses digits against a base from 2 to 16 and refuses a
digit at or above it, so `octal` is the keyword. What this holds is that
all six readers agree, that 8 and 9 are refused, and that the value is the
octal one -- `000644` is 420, not 644.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import fourway
from test_walker_c import (COMPILER, c_verdict, packed_text, python_verdict,
                           shape_named_text)

SCHEMA = """target buffer;
endian big;
struct odc {
	u8         magic[6]  [must_eq = "070707"];
	octal u32  mode[6];
	octal u32  namesize[6];
	u8         name[namesize];
}
"""

#: The verdict, `mode`'s line, and the bytes.
CASES = {
	"a valid header":         ("0", "mode ok=1 value=420",
	                           b"070707000644000003ab\x00"),
	"an 8 is not octal":      ("2", "mode ok=0 value=0",
	                           b"070707000648000003ab\x00"),
	"the largest six digits": ("0", "mode ok=1 value=262143",
	                           b"070707777777000001\x00"),
}


def test_all_four_read_octal(tmp_path: Path) -> None:
	schema = tmp_path / "unit.situ"
	schema.write_text(SCHEMA, encoding="ascii")
	command = fourway.build(tmp_path, schema)
	for label, (verdict, line, packet) in CASES.items():
		given = {name: fourway.answers(argv, packet, tmp_path)
		         for name, argv in command.items()}
		first = next(iter(given.values()))
		assert all(answer == first for answer in given.values()), given
		lines = first.splitlines()
		assert line in lines, f"{label}: {lines!r}"
		assert f"validate {verdict}" in lines, f"{label}: {lines!r}"


@pytest.mark.skipif(COMPILER is None, reason="no C compiler")
def test_both_walkers_read_octal(tmp_path: Path) -> None:
	blob  = packed_text(SCHEMA)
	shape = shape_named_text(SCHEMA, "odc")
	for label, (verdict, _line, packet) in CASES.items():
		assert python_verdict(blob, packet, shape) == verdict, label
		assert c_verdict(tmp_path, blob, packet, shape) == verdict, label
