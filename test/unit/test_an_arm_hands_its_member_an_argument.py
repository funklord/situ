"""A variant arm hands its member the arguments (0068 step 4).

    struct entry { parameter u16 block [stream]; u8 tag [max = 5];
                   u8 data[block]; }
    struct arms  { u16 block; u8 kind; u8 n;
                   variant body switch (kind) {
                       case 1: entry(block = block) one;
                       case 2: entry(block = block) many[n];
                       case 3: entry(block = 1) wh[] while (tag != 0) max 4;
                       case 4: entry(block = block) rest[remaining];
                       default: u8 raw; } }

A single member and each run shape, as an arm, and an arm with members
after the variant. The argument is the parent's, evaluated where the arm
is chosen, and the four backends and both walkers agree.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import fourway
from test_walker_c import (COMPILER, _inline_image, c_verdict,
                           python_verdict, shape_named_text)

SCHEMA = """target buffer;
endian big;
struct entry { parameter u16 block [stream]; u8 tag [max = 5]; u8 data[block]; }
struct arms {
	u16 block;
	u8 kind;
	u8 n;
	variant body switch (kind) {
		case 1: entry(block = block) one;
		case 2: entry(block = block) many[n];
		case 3: entry(block = 1) wh[] while (tag != 0) max 4;
		case 4: entry(block = block) rest[remaining];
		default: u8 raw;
	}
}
struct armed_after {
	u16 block;
	u8 kind;
	variant body switch (kind) {
		case 1: entry(block = block) one;
		case 2: entry(block = block) de[] until ";";
		default: u8 raw;
	}
	u8 after;
}
"""

CASES = {
	"arms": {"0001010002aa": "0", "0001010007aa": "2",
	         "0001020201aa02bb": "0", "0001020201aa07bb": "2",
	         "0001030001aa00bb": "0", "0001030007aa00bb": "2",
	         "0001040001aa02bb": "0", "0001040001aa07bb": "2"},
	"armed_after": {"00010102aaee": "0", "00010107aaee": "2",
	                "00010201aa02bb3bee": "0", "00010201aa07bb3bee": "2"},
}


def _validate(answers: str, name: str) -> str:
	section = answers[answers.index(f"-- {name}\n"):].split("\n-- ")[0]
	return next(line.split()[1] for line in section.splitlines()
	            if line.startswith("validate"))


@pytest.mark.skipif(not fourway.COMPLETE or COMPILER is None,
                    reason="needs gcc, g++ and rustc")
def test_six_readers_agree_on_every_arm(tmp_path: Path) -> None:
	schema = tmp_path / "unit.situ"
	schema.write_text(SCHEMA, encoding="ascii")
	command = fourway.build(tmp_path, schema)
	blob = _inline_image(SCHEMA)
	for name, cases in CASES.items():
		shape = shape_named_text(SCHEMA, name)
		for message, verdict in cases.items():
			packet = bytes.fromhex(message)
			answers = [fourway.answers(argv, packet, tmp_path)
			           for argv in command.values()]
			assert len({answer[answer.index(f"-- {name}\n"):]
			            .split("\n-- ")[0] for answer in answers}) == 1, \
				(name, message)
			assert _validate(answers[0], name) == verdict, (name, message)
			assert python_verdict(blob, packet, shape) == verdict, \
				(name, message)
			assert c_verdict(tmp_path, blob, packet, shape) == verdict, \
				(name, message)
