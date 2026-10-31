"""`validate` holds the arm a variant selects to the frame (26.777).

    struct arm { u8 k; variant held switch (k) { case 1: u32 four;
                                                  case 2: u8 one; } }

A variant whose arms differ in size declares no length and can sit at a
constant offset, so `_fits_check` -- which asks about a member that
declares a length and about one at a dynamic offset -- asked nothing of
it, the shape 26.776 found in coded regions. `k = 1` in a two-byte
message validated clean in all four, while the arm's own accessor
refused it and `required` asked for five bytes.
"""

from __future__ import annotations

from pathlib import Path

import fourway

SCHEMA = """target buffer;
endian big;
struct arm {
	u8  k;
	variant held switch (k) {
		case 1:  u32  four;
		case 2:  u8   one;
		default: error;
	}
}
"""

#: message -> what `validate` says: 0 whole, 1 short, 3 an unknown arm.
CASES = {
	"0101":       "1",	# arm 1 is four bytes; one is here
	"0101020304": "0",
	"0207":       "0",
	"0907":       "3",	# no arm: the discriminant's refusal, unchanged
}


def test_all_four_refuse_an_arm_the_frame_cannot_hold(tmp_path: Path) -> None:
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
