"""A `while` run whose element frames to nothing ends there (2026-11-24).

Framing a `while` run asks each element's `required` and steps by the
answer. An element that answered "whole, zero bytes" -- a peeked
discriminant behind leading whitespace, with nothing after it and
`default: error` -- never moved the walk, and C, C++, Rust and Python all
spun for ever on an empty buffer and on a whole message alike. Every other
walk already ends a run at a zero-extent element; framing now does too.

What the element itself answered for a buffer of nothing was whole, at
zero bytes, in all four: the discriminant sits behind a lead at a scanned
offset, framing guarded the variant at `have < pos`, and a byte that had
not arrived read as zero, which `default: error` matches with no arm. The
second test pins the fix (2026-11-26): every prefix without the
discriminant is short, by at least that byte.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import fourway
from situc.pack import pack
from test_four_backends_frame_alike import analysed
from walker.image import load
from walker.walk import acquire, frame_min

SCHEMA = """target buffer;
endian big;
whitespace ' ';
struct none_grant { u8 word[4] [must_eq = "none"]; }
struct user_grant { u8 scheme[5] [must_eq = "user:"]; u8 rights[2]; }
struct grant {
	peek u8 kind skip;
	variant body switch (kind) {
		case 'n': none_grant none;
		case 'u': user_grant user;
		default: error;
	}
}
struct permit { grant grants[] while (1) | end; }
"""


@pytest.mark.skipif(not fourway.COMPLETE, reason="needs all four toolchains")
def test_framing_a_run_of_empty_elements_ends(tmp_path: Path) -> None:
	source = tmp_path / "unit.situ"
	source.write_text(SCHEMA, encoding="ascii")
	command = fourway.build(tmp_path, source)
	for data in (b"", b" ", b"none", b"none user:rw", b"user:rw none"):
		said = {name: fourway.answers(argv, data, tmp_path)
		        for name, argv in command.items()}
		lines = {name: next(one for one in text.split("-- permit\n")[1]
		                    .splitlines() if one.startswith("required()"))
		         for name, text in said.items()}
		assert len(set(lines.values())) == 1, (data, lines)


#: `grant`'s `required()` line for each buffer: one answer per prefix
#: length, `*N` where the prefix is whole at N bytes and `N` where it needs
#: N. Worked by hand: the discriminant is the byte after the spaces, so a
#: prefix of k spaces needs k + 1, and `none` and `user:rw` are 4 and 7
#: after it.
PINNED = {
	b"":       "required() 1",
	b" ":      "required() 1 2",
	b"   ":    "required() 1 2 3 4",
	b" none":  "required() 1 2 5 5 5 *5",
	b" u":     "required() 1 2 8",
}


@pytest.mark.skipif(not fourway.COMPLETE, reason="needs all four toolchains")
def test_no_discriminant_is_never_a_whole_element(tmp_path: Path) -> None:
	source = tmp_path / "unit.situ"
	source.write_text(SCHEMA, encoding="ascii")
	command = fourway.build(tmp_path, source)
	for data, expected in PINNED.items():
		for name, argv in command.items():
			text = fourway.answers(argv, data, tmp_path)
			section = text.split("-- grant\n")[1].split("\n-- ")[0]
			line = next(one for one in section.splitlines()
			            if one.startswith("required()"))
			assert line == expected, (name, data, line)


def test_the_walker_frames_it_as_the_four_do(tmp_path: Path) -> None:
	"""`frame_min` is the backends' `required` as a walk, and had the same
	fault: zero for an empty buffer, one for a single space."""
	source = tmp_path / "unit.situ"
	source.write_text(SCHEMA, encoding="ascii")
	parsed, resolved = analysed(source)
	image = load(pack(parsed, resolved, metadata=True)[0])
	grant = next(index for index in range(len(image.structs))
	             if image.struct_name(index) == "grant")
	for data, expected in PINNED.items():
		answers = [int(word.lstrip("*"))
		           for word in expected.split()[1:]]
		walked = [frame_min(acquire(image, data[:length], grant))
		          for length in range(len(data) + 1)]
		assert walked == answers, (data, walked)
