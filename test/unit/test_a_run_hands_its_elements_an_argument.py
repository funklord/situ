"""A run hands every element the same arguments (0068 step 3).

    struct entry { parameter u16 block [stream]; u8 tag [max = 5];
                   u8 data[block]; }
    struct vol   { u16 block; u8 n; entry(block = block) counted[n];
                   entry(block = 1) lit[2]; u8 after; }

Every run shape -- counted, literal, `[remaining]`, `while` and delimited
-- measures each element with the argument, so the four backends and both
walkers agree about where the run ends, how many elements it holds, and
whether an element breaks its own rule.
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
struct vol { u16 block; u8 n; entry(block = block) counted[n];
             entry(block = 1) lit[2]; u8 after; }
struct rest { u16 block; entry(block = block) all[remaining]; }
struct wh { u16 block; entry(block = block) w[] while (tag != 0) max 4;
            u8 after; }
struct de { u16 block; entry(block = block) d[] until ";"; u8 after; }
"""

#: struct -> message -> what `validate` says: 0 whole, 1 short, 2 a rule
#: broken inside an element.
CASES = {
	"vol":  {"00010201aa02bb03cc04ddee": "0",
	         "00010201aa07bb03cc04ddee": "2",	# a counted element
	         "00010201aa02bb03cc09ddee": "2",	# a literal one
	         "00010201aa02bb03cc04dd":   "1"},	# `after` cut off
	"rest": {"000101aa02bb": "0", "000101aa07bb": "2"},
	"wh":   {"000101aa02bb00ccee": "0", "000107aa00ccee": "2"},
	"de":   {"000101aa02bb3bee": "0", "000101aa07bb3bee": "2"},
}


def _validate(answers: str, name: str) -> str:
	section = answers[answers.index(f"-- {name}\n"):].split("\n-- ")[0]
	return next(line.split()[1] for line in section.splitlines()
	            if line.startswith("validate"))


@pytest.mark.skipif(not fourway.COMPLETE or COMPILER is None,
                    reason="needs gcc, g++ and rustc")
def test_six_readers_agree_on_every_run_shape(tmp_path: Path) -> None:
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
