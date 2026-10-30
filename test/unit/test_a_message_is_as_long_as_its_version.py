"""A message is as long as its own version says (26.766).

Section 19.4: "the struct's extent is a range -- v1 is three bytes, v3 is
nine". Every reader answered nine whatever the message's version said:
the four backends' `required` and extent, and both walkers' extent. So a
stream of version-1 messages was framed nine bytes at a time, each one
swallowing the start of the next, and a member after a nested version-1
struct was read from where a version-3 one would end.

Now the members every version carries, then each `[since]` step the
message's version reaches, in all six -- where the versioned members are a
tail, which is the shape 19.4 describes. All six agree on every answer
below, and the answers are the ones 19.4 gives.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import fourway

from test_walker_c import (COMPILER, _inline_image, c_answers, python_answers,
                           shape_named_text)

SCHEMA = """target buffer;
endian big;
struct versioned [version = ver] {
	u8   ver;
	u16  length;
	u32  flags [since = 2];
	u16  extra [since = 3];
}
struct outer {
	versioned  v;
	u8         after;
}
struct many {
	u8         n;
	versioned  vs[n];
	u8         after;
}
"""

V1 = bytes([1, 0, 0])
V2 = bytes([2, 0, 0, 0, 0, 0, 0])
V3 = bytes([3]) + bytes(8)

#: (struct, message) -> the `required()` answer at the whole message, and
#: `after` where the struct has one.
CASES = {
	("versioned", V1 + b"\xEE" * 6): ("*3", None),	# a stream: more follows
	("versioned", V2 + b"\xEE" * 2): ("*7", None),
	("versioned", V3):               ("*9", None),
	("outer", V1 + b"\x07"):         ("*4", "7"),
	("outer", V3 + b"\x07"):         ("*10", "7"),
	("many", b"\x02" + V1 + V3 + b"\x07"): ("*14", "7"),
}


def _section(output: str, struct: str) -> list[str]:
	return output.split(f"-- {struct}\n", 1)[1].split("\n-- ", 1)[0] \
		.splitlines()


def test_all_four_frame_a_message_by_its_version(tmp_path: Path) -> None:
	schema = tmp_path / "unit.situ"
	schema.write_text(SCHEMA, encoding="ascii")
	command = fourway.build(tmp_path, schema)
	for (struct, message), (framed, after) in CASES.items():
		given = {name: fourway.answers(argv, message, tmp_path)
		         for name, argv in command.items()}
		first = next(iter(given.values()))
		assert all(one == first for one in given.values()), given
		lines = _section(first, struct)
		required = next(line for line in lines
		                if line.startswith("required()"))
		assert required.split()[-1] == framed, (message.hex(), required)
		if after is not None:
			assert f"after {after}" in lines, (message.hex(), lines)


@pytest.mark.skipif(COMPILER is None, reason="no C compiler")
@pytest.mark.parametrize("message", [V1 + b"\x07", V2 + b"\x07",
                                     V3 + b"\x07"],
                         ids=["v1", "v2", "v3"])
def test_both_walkers_place_what_follows_a_nested_version(message: bytes,
		tmp_path: Path) -> None:
	blob  = _inline_image(SCHEMA)
	shape = shape_named_text(SCHEMA, "outer")
	# `after` alone: what the two walkers make of the nested struct's own
	# "value" is 26.721's open question and not this one.
	in_python = python_answers(blob, message, shape)[-1]
	in_c      = c_answers(tmp_path, blob, message, shape)[-1]
	assert in_python == in_c == "7", (in_python, in_c)
