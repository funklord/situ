"""`[encoding = from(f)]` reads this struct's `f`, not a nested one's
(26.762).

All four backends found the source by matching the last path component
over every entry, and a nested struct's field of the same name comes
first in layout order. Here `h.says` is an `other_charset`, which has no
arm spelled `us-ascii`, so the check found nothing to apply and every
backend accepted a body that is not ASCII under a declared `us-ascii`.
The packer looks the source up by its path and was right, so the walkers
were too; renaming the field made the four refuse it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import fourway

SCHEMA = """target buffer;
endian big;
tokens document_charset {
	utf8   = "utf-8",
	ascii  = "us-ascii",
}
tokens other_charset {
	ascii  = "x",
	utf8   = "y",
}
struct inner {
	other_charset  says  until ";";
}
struct s {
	inner             h;
	document_charset  says  until ";";
	u8                body[]  until "\\0"  [encoding = from(says)];
}
"""

#: packet -> `validate` as all four answer it: 0 is SITU_OK, 2 a constraint.
CASES = {
	b"x;us-ascii;\xc3\xa9\x00": "2",
	b"y;utf-8;\xc3\xa9\x00":    "0",
	b"x;us-ascii;ab\x00":       "0",
}


@pytest.mark.parametrize("packet", sorted(CASES), ids=repr)
def test_the_struct_s_own_source_decides(packet: bytes, tmp_path: Path) -> None:
	schema = tmp_path / "unit.situ"
	schema.write_text(SCHEMA, encoding="ascii")
	command = fourway.build(tmp_path, schema)
	given = {}
	for name, argv in command.items():
		section = fourway.answers(argv, packet, tmp_path) \
			.split("-- s\n", 1)[1].split("\n-- ", 1)[0]
		given[name] = next(line for line in section.splitlines()
		                   if line.startswith("validate"))
	assert set(given.values()) == {f"validate {CASES[packet]}"}, given
