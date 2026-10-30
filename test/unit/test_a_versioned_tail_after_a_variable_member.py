"""A versioned tail behind a variable member is its version's length (26.768).

    struct varv [version = ver] { u8 ver; u8 n; u8 d[n]; u16 x [since = 2]; }

26.766 framed a versioned struct at its own version's length where every
member sits at a static offset. Behind `d[n]` none does, and the four
backends counted `x` whatever the version: a version-1 message was framed
two bytes long, swallowing the start of the next, and a member after a
nested one was read two bytes late. Both walkers already skipped the
absent tail.

`validate` refused the same whole version-1 message: the minimum it opens
with counted the tail, and three backends asked whether the frame held
`x` before asking whether the version carried it. C asked it inside the
version guard, as the others now do.

And the floor a counted run credits each unreached element with has to
leave the tail out too, or a run of these is told each needs two bytes
more than it reports and the total falls as they arrive -- 26.724's
monotone property. A fixed versioned struct's floor was already its
oldest version's (`fixed_run`).
"""

from __future__ import annotations

from pathlib import Path

import pytest

import fourway

from test_walker_c import (COMPILER, _inline_image, c_answers, python_answers,
                           shape_named_text)

SCHEMA = """target buffer;
endian big;
struct varv [version = ver] {
	u8   ver;
	u8   n;
	u8   d[n];
	u16  x [since = 2];
	u8   y [since = 3];
}
struct outer {
	varv  v;
	u8    after;
}
struct many {
	u8    count;
	varv  vs[count];
	u8    after;
}
struct fixed [version = ver] {
	u8   ver;
	u16  x [since = 2];
}
struct fixed_run {
	u8     count;
	fixed  fs[count];
}
"""

V1 = bytes([1, 2, 5, 6])
V2 = bytes([2, 2, 5, 6, 0, 0])
V3 = bytes([3, 0, 0, 0, 0])

#: (struct, message) -> the `required()` answer at the whole message, and
#: `after` where the struct has one.
CASES = {
	("varv", V1 + b"\xEE" * 3):          ("*4", None),	# a stream
	("varv", V2 + b"\xEE"):              ("*6", None),
	("varv", V3):                        ("*5", None),
	("outer", V1 + b"\x07"):             ("*5", "7"),
	("outer", V3 + b"\x07"):             ("*6", "7"),
	("many", b"\x03" + V1 + V2 + V3 + b"\x07"): ("*17", "7"),
	("fixed_run", b"\x02\x01\x01"):      ("*3", None),
}

#: A `varv` message -> what `validate` says: 0 whole, 1 short (BOUNDS).
VALIDATE = {
	V1:            "0",	# version 1 carries neither `x` nor `y`
	V2[:-1]:       "1",	# version 2 claims `x` and holds one byte of it
	V2:            "0",
	V3[:-1]:       "1",
	V3:            "0",
}


def _section(output: str, struct: str) -> list[str]:
	return output.split(f"-- {struct}\n", 1)[1].split("\n-- ", 1)[0] \
		.splitlines()


def _required(output: str, struct: str) -> list[str]:
	line = next(line for line in _section(output, struct)
	            if line.startswith("required()"))
	return line.split()[1:]


def test_all_four_frame_the_tail_by_the_version(tmp_path: Path) -> None:
	schema = tmp_path / "unit.situ"
	schema.write_text(SCHEMA, encoding="ascii")
	command = fourway.build(tmp_path, schema)
	for (struct, message), (framed, after) in CASES.items():
		given = {name: fourway.answers(argv, message, tmp_path)
		         for name, argv in command.items()}
		first = next(iter(given.values()))
		assert all(one == first for one in given.values()), given
		answers = _required(first, struct)
		assert answers[-1] == framed, (message.hex(), answers)
		if after is not None:
			assert f"after {after}" in _section(first, struct), \
				(message.hex(), _section(first, struct))
		# Never falls as bytes arrive (26.724), which an unreached
		# element credited at its newest version broke.
		needs = [int(one.lstrip("*")) for one in answers]
		assert needs == sorted(needs), (struct, message.hex(), answers)


def test_all_four_validate_a_message_by_its_version(tmp_path: Path) -> None:
	schema = tmp_path / "unit.situ"
	schema.write_text(SCHEMA, encoding="ascii")
	command = fourway.build(tmp_path, schema)
	for message, verdict in VALIDATE.items():
		given = {name: next(line.split()[1] for line in
		                    _section(fourway.answers(argv, message, tmp_path),
		                             "varv")
		                    if line.startswith("validate"))
		         for name, argv in command.items()}
		assert set(given.values()) == {verdict}, (message.hex(), given)


@pytest.mark.skipif(COMPILER is None, reason="no C compiler")
@pytest.mark.parametrize("message", [V1 + b"\x07", V2 + b"\x07",
                                     V3 + b"\x07"],
                         ids=["v1", "v2", "v3"])
def test_both_walkers_place_what_follows(message: bytes,
		tmp_path: Path) -> None:
	blob  = _inline_image(SCHEMA)
	shape = shape_named_text(SCHEMA, "outer")
	in_python = python_answers(blob, message, shape)[-1]
	in_c      = c_answers(tmp_path, blob, message, shape)[-1]
	assert in_python == in_c == "7", (in_python, in_c)
