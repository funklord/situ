"""A `while` run whose element frames to nothing ends there (2026-11-24).

Framing a `while` run asks each element's `required` and steps by the
answer. An element that answered "whole, zero bytes" -- a peeked
discriminant behind leading whitespace, with nothing after it and
`default: error` -- never moved the walk, and C, C++, Rust and Python all
spun for ever on an empty buffer and on a whole message alike. Every other
walk already ends a run at a zero-extent element; framing now does too.

What the element itself answers for a buffer of nothing is a separate
question, recorded open in `project.md`; this pins only that the four
answer at all, and answer alike.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import fourway

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
