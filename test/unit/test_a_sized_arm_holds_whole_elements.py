"""A `[remaining]` run of fixed elements in a sized arm is whole elements
(26.1164).

As a struct's own member such a run refuses bytes no element accounts for
(26.736). In an arm of a variant that states its size -- the arm's frame
is that size -- all four backends and both walkers accepted `body[3]` over
one two-byte pair and a stray byte (openmlx4, inbox 20261122T1445Z).
"""
from __future__ import annotations

from pathlib import Path

import pytest

import fourway
from test_walker_c import COMPILER, c_verdict, image_for, python_verdict

SCHEMA = """target buffer;
endian big;

struct pair { u8 a; u8 b; }

struct fixed_arm {
	u8 size;
	u8 kind;
	variant body[size] switch (kind) {
		case 0:  pair items[remaining];
		case 1:  u16  words[remaining];
		default: u8   raw[remaining];
	}
}
"""

#: (bytes, verdict): a stray byte after whole pairs or words is BOUNDS;
#: whole ones and a byte arm, which divides anything, are clean.
CASES = [("0300010203", "1"), ("040001020304", "0"), ("0200aabb", "0"),
         ("0301aabbcc", "1"), ("0401aabbccdd", "0"), ("0307aabbcc", "0")]


@pytest.mark.skipif(not fourway.COMPLETE, reason="needs all four toolchains")
def test_four_backends_hold_the_arm_to_whole_elements(tmp_path: Path) -> None:
	schema = tmp_path / "unit.situ"
	schema.write_text(SCHEMA, encoding="ascii")
	command = fourway.build(tmp_path, schema)
	for message, want in CASES:
		for backend, argv in command.items():
			said = fourway.answers(argv, bytes.fromhex(message), tmp_path)
			section = said[said.index("-- fixed_arm\n"):].split("\n-- ")[0]
			verdict = next(line.split()[1] for line in section.splitlines()
			               if line.startswith("validate "))
			assert verdict == want, (backend, message)


@pytest.mark.skipif(COMPILER is None, reason="no C compiler")
def test_both_walkers_hold_the_arm_to_whole_elements(tmp_path: Path) -> None:
	schema = tmp_path / "unit.situ"
	schema.write_text(SCHEMA, encoding="ascii")
	blob = image_for(schema)
	for message, want in CASES:
		data = bytes.fromhex(message)
		assert python_verdict(blob, data, 1) == want, message
		assert c_verdict(tmp_path, blob, data, 1) == want, message
