"""`validate` refuses a message whose version claims a field it cuts short.

`[since = N]` says a member is carried by every message of version N or
later, and 19.4 says `validate` reads the version first and skips a member
the message does not carry. A message that DOES carry it, by its own
version, and whose frame stops short of it is malformed -- and every
backend's accessor said so, answering BOUNDS where VERSION means "older
than the field". `check` asked only for the struct's minimum, so all four
validated it clean, and both walkers agreed (26.746).

Found by `test_required_against_the_walker.py`'s instrument: `required` and
the walker both measured `edges.versioned` at nine bytes on an eight-byte
buffer that validated.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import fourway
from test_walker_c import (COMPILER, c_verdict, packed_text, python_verdict,
                           shape_named_text)

SCHEMA = """target buffer;
endian little;
struct versioned [version = ver] {
	u8   ver;
	u16  length;
	u32  flags [since = 2];
	u16  extra [since = 3];
}
"""

#: The verdict each reader must give, and the bytes.
CASES = {
	"version 1, carrying neither":          ("0", bytes.fromhex("010100")),
	"version 2, short of `flags`":          ("1", bytes.fromhex("02010002")),
	"version 2, whole":                     ("0", bytes.fromhex("02010002000000")),
	"version 3, one byte short of `extra`": ("1", bytes.fromhex("03010002000000aa")),
	"version 3, whole":                     ("0", bytes.fromhex("03010002000000aabb")),
}


def test_all_four_refuse_a_versioned_field_cut_short(tmp_path: Path) -> None:
	schema = tmp_path / "unit.situ"
	schema.write_text(SCHEMA, encoding="ascii")
	try:
		command = fourway.build(tmp_path, schema)
	except fourway.BuildFailed as failed:
		pytest.skip(f"a backend could not be built here: {failed}")

	for label, (verdict, packet) in CASES.items():
		given = {name: fourway.answers(argv, packet, tmp_path)
		         for name, argv in command.items()}
		for name, answer in given.items():
			assert f"validate {verdict}" in answer.splitlines(), (
				f"{label}: {name} answered {answer!r}")


@pytest.mark.skipif(COMPILER is None, reason="no C compiler")
def test_both_walkers_refuse_a_versioned_field_cut_short(
		tmp_path: Path) -> None:
	blob  = packed_text(SCHEMA)
	shape = shape_named_text(SCHEMA, "versioned")
	for label, (verdict, packet) in CASES.items():
		assert python_verdict(blob, packet, shape) == verdict, label
		assert c_verdict(tmp_path, blob, packet, shape) == verdict, label
