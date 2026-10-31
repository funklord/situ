"""`validate` holds a coded region's extent to the frame (26.776).

    struct codedw { u8 n; coded body(doubling) { u8 content[n]; } }

A coded or sealed region declares no length of its own: its extent is the
interior put through the codec's expansion. `_fits_check` asks about a
member that declares a length and about one at a dynamic offset, and the
region was neither, so nothing held it to the frame. `n = 2` promises a
four-byte body; a message carrying three validated clean in all four,
while a counted run `u8 d[n]` in the same place has always been refused.
"""

from __future__ import annotations

from pathlib import Path

import fourway

SCHEMA = """target buffer;
endian big;
codec doubling {
	expansion = ratio_exact(2, 1);
	granularity = byte;
	seekable = linear;
	invertible;
	deterministic;
}
impl doubling extern "my_doubling";
struct codedw {
	u8  n;
	coded body(doubling) {
		u8 content[n];
	}
}
struct codedv [version = ver] {
	u8  ver;
	u8  n;
	coded body(doubling) {
		u8   content[n];
		u16  x [since = 2];
	}
}
"""

#: (struct, message) -> what `validate` says: 0 whole, 1 short (BOUNDS).
CASES = {
	("codedw", "02aabbcc"):   "1",	# four promised, three here
	("codedw", "02aabbccdd"): "0",
	("codedw", "00"):         "0",
	("codedv", "0200aabb"):   "1",	# version 2 carries `x`: four bytes
	("codedv", "0101aa"):     "1",
	("codedv", "0101aabb"):   "0",
}


def _section(output: str, struct: str) -> list[str]:
	return output.split(f"-- {struct}\n", 1)[1].split("\n-- ", 1)[0] \
		.splitlines()


def test_all_four_refuse_a_region_the_frame_cannot_hold(tmp_path: Path) -> None:
	schema = tmp_path / "unit.situ"
	schema.write_text(SCHEMA, encoding="ascii")
	command = fourway.build(tmp_path, schema)
	for (struct, message), verdict in CASES.items():
		given = {name: next(line.split()[1] for line in
		                    _section(fourway.answers(argv,
		                                             bytes.fromhex(message),
		                                             tmp_path), struct)
		                    if line.startswith("validate"))
		         for name, argv in command.items()}
		assert set(given.values()) == {verdict}, (struct, message, given)
