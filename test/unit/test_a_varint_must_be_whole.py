"""`validate` refuses a varint cut short by the frame (26.778).

    struct vint { u8 k; lv v; }      lv: leb128, at most five bytes

A varint's length is in its own bytes, so it declares none `_fits_check`
can hold against the frame, and at a constant offset it was not asked
about at all: `01 80` -- a continuation byte, then the end -- validated
clean in all four while `required` asked for a third byte. The shape of
26.776 and 26.777, a member between both branches of the check.
"""

from __future__ import annotations

from pathlib import Path

import fourway

SCHEMA = """target buffer;
endian big;
varint_type lv {
	encoding  = leb128;
	max_bits  = 32;
	max_bytes = 5;
}
struct vint {
	u8  k;
	lv  v;
}
"""

#: message -> what `validate` says: 0 whole, 1 short (BOUNDS).
CASES = {
	"0180":         "1",	# continues, and the frame ends
	"0101":         "0",
	"01ff7f":       "0",
	"01ffffffffff": "1",	# past its five bytes and still continuing
}


def test_all_four_refuse_a_varint_cut_short(tmp_path: Path) -> None:
	schema = tmp_path / "unit.situ"
	schema.write_text(SCHEMA, encoding="ascii")
	command = fourway.build(tmp_path, schema)
	for message, verdict in CASES.items():
		given = {name: next(line.split()[1] for line in
		                    fourway.answers(argv, bytes.fromhex(message),
		                                    tmp_path).splitlines()
		                    if line.startswith("validate"))
		         for name, argv in command.items()}
		assert set(given.values()) == {verdict}, (message, given)
