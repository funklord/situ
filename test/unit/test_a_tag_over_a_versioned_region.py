"""A tag over a region holding `[since]` members covers its version (26.771).

    struct sealeds [version = ver] {
        u8 ver;
        tag u8 mac[4] covers(part);
        authenticated part { u8 a; u16 b [since = 2]; }
    }

A region's placement is its newest version's size, and every backend's
covered range ended there: a whole six-byte version-1 message, which
holds one byte of `part`, was told its tag covers bytes 5..8 -- past the
frame -- so all four refused to say what it covers, and nothing could
compute or check the tag of an older message. Placed after `d[n]` the
four disagreed as well, and C's `validate` refused the whole version-1
message over the region's own frame check, taken at the newest size.
"""

from __future__ import annotations

from pathlib import Path

import fourway

SCHEMA = """target buffer;
endian big;
struct sealeds [version = ver] {
	u8   ver;
	tag  u8 mac[4] covers(part);
	authenticated part { u8 a; u16 b [since = 2]; }
}
struct sealedv [version = ver] {
	u8   ver;
	u8   n;
	u8   d[n];
	tag  u8 mac[4] covers(part);
	authenticated part { u8 a; u16 b [since = 2]; }
}
"""

#: (struct, message) -> (covered, validate)
CASES = {
	("sealeds", "0100000000aa"):        ("5+1", "0"),
	("sealeds", "0200000000aabbcc"):    ("5+3", "0"),
	("sealeds", "0200000000aabb"):      ("refused", "1"),	# claims `b`
	("sealedv", "0100aabbccdd11"):      ("6+1", "0"),
	("sealedv", "0201ee0102030411"):    ("refused", "1"),
	("sealedv", "0201ee01020304112233"): ("7+3", "0"),
}


def _section(output: str, struct: str) -> list[str]:
	return output.split(f"-- {struct}\n", 1)[1].split("\n-- ", 1)[0] \
		.splitlines()


def test_all_four_cover_the_region_at_its_version(tmp_path: Path) -> None:
	schema = tmp_path / "unit.situ"
	schema.write_text(SCHEMA, encoding="ascii")
	command = fourway.build(tmp_path, schema)
	for (struct, message), (covered, valid) in CASES.items():
		given = {name: fourway.answers(argv, bytes.fromhex(message), tmp_path)
		         for name, argv in command.items()}
		first = next(iter(given.values()))
		assert all(one == first for one in given.values()), given
		lines = _section(first, struct)
		assert f"mac.covered {covered}" in lines, (message, lines)
		assert f"validate {valid}" in lines, (message, lines)
