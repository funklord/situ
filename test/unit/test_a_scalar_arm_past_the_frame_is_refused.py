"""A scalar arm behind a variable member said Ok for a byte it did not have.

26.325 gave an arm its frame question, and wrote it as a comparison with a
constant, so an arm at a dynamic offset got no question at all: Rust and Python
fell through to the lax read and answered zero with no error, where C and C++
answered Bounds. The caller holds a `Result`, so nothing says the value is not
there. `example/pes` found it, with four optional groups in a row (26.885).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import fourway						# noqa: E402

SCHEMA = """target buffer;
endian big;
enum colour : u8 { red = 1, blue = 2 }
struct s {
	u8 n;
	u8 lead[n];
	u8 kind;
	variant v switch (kind) {
		case 1: u8     byte_arm;
		case 2: u16    word_arm;
		case 3: colour enum_arm;
		default: opaque;
	}
}
"""

#: packet -> an arm line all four must print, each present and then one byte
#: short. `lead` moves the arm, so none of these offsets is a constant. The
#: probe does not ask an enum arm, which takes the same branch; `enum_arm` is
#: here because Rust compiles its two accessors under `-D warnings`.
ANSWERS = {
	bytes([2, 9, 9, 1, 0x41]):       "v_byte_arm ok=1 value=65",
	bytes([2, 9, 9, 1]):             "v_byte_arm ok=0 value=0",
	bytes([1, 9, 2, 0x12, 0x34]):    "v_word_arm ok=1 value=4660",
	bytes([1, 9, 2, 0x12]):          "v_word_arm ok=0 value=0",
	bytes([0, 2, 0x12, 0x34]):       "v_word_arm ok=1 value=4660",
	bytes([0, 2, 0x12]):             "v_word_arm ok=0 value=0",
}


@pytest.mark.skipif(not fourway.COMPLETE, reason="needs gcc, g++ and rustc")
def test_all_four_refuse_the_arm_the_frame_does_not_reach(
		tmp_path: Path) -> None:
	schema = tmp_path / "unit.situ"
	schema.write_text(SCHEMA, encoding="ascii")
	command = fourway.build(tmp_path, schema)
	for packet, expected in ANSWERS.items():
		given = {name: fourway.answers(argv, packet, tmp_path)
		         for name, argv in command.items()}
		first = next(iter(given.values()))
		assert all(answer == first for answer in given.values()), given
		assert expected in first.splitlines(), (packet.hex(), first)
