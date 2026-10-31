"""`validate` validates every element of a run of structs (26.789).

    struct pt    { u8 x; u8 y [max = 9]; }
    struct piece { u8 len [max = 4]; u8 body[len]; }
    struct s_var { u8 n; piece r[n]; }

No backend validated a run's elements: C said "a loop on every parse, and a
caller who wants it should walk the elements", and the other five followed.
So a DNS label, an HTTP header field, a netlink attribute or an id3 frame
was checked as a member and never as an element of the run carrying it --
21 of the corpus's 26 runs of structs hold an element with checks of its
own. Set by the copyright holder: `validate` validates them.

Each run shape walks its elements its own way, and each element is what
the run's walk steps over, at that extent: a record the frame does not hold
ends the run, as it always did, and is not validated.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import fourway

from test_walker_c import (COMPILER, _inline_image, c_verdict,
                           python_verdict, shape_named_text)

SCHEMA = """target buffer;
endian big;
struct pt { u8 x; u8 y [max = 9]; }
struct piece { u8 len [max = 4]; u8 body[len]; }
struct s_fixed { u8 n; pt r[n]; }
struct s_lit { pt r[2]; }
struct s_var { u8 n; piece r[n]; }
struct s_wh { piece r[] while (len != 0) max 4; }
struct s_de { piece r[] until ";"; }
struct s_rest { u8 k; piece r[remaining]; }
"""

#: struct -> message -> what `validate` says: 0 whole, 1 short, 2 a
#: constraint broken.
CASES = {
	"s_fixed": {"0201020304": "0", "020102030a": "2",	# the last element
	            "02010a0304": "2", "00": "0"},		# ...and the first
	"s_lit":   {"01020304": "0", "010a0304": "2", "0102030a": "2"},
	"s_var":   {"0201aa00": "0", "0205aaaaaaaaaa00": "2",
	            "0201aa05": "1"},				# the frame first
	"s_wh":    {"01aa00": "0", "05aaaaaaaaaa00": "2",
	            "01aa0700": "0"},				# not held: not an element
	"s_de":    {"01aa3b": "0", "05aaaaaaaaaa3b": "2", "01aa073b": "0",
	            "3b": "0"},
	"s_rest":  {"0001aa00": "0", "0005aaaaaaaaaa": "2", "0001aa05": "1"},
}


def _verdict(answers: str, name: str) -> str:
	section = answers[answers.index(f"-- {name}\n"):].split("\n-- ")[0]
	return next(line.split()[1] for line in section.splitlines()
	            if line.startswith("validate"))


@pytest.mark.skipif(not fourway.COMPLETE, reason="needs gcc, g++ and rustc")
def test_all_four_validate_the_elements(tmp_path: Path) -> None:
	schema = tmp_path / "unit.situ"
	schema.write_text(SCHEMA, encoding="ascii")
	command = fourway.build(tmp_path, schema)
	for name, cases in CASES.items():
		for message, verdict in cases.items():
			given = {backend: _verdict(fourway.answers(
			                 argv, bytes.fromhex(message), tmp_path), name)
			         for backend, argv in command.items()}
			assert set(given.values()) == {verdict}, (name, message, given)


@pytest.mark.skipif(COMPILER is None, reason="no C compiler")
@pytest.mark.parametrize("name", CASES)
def test_both_walkers_validate_the_elements(name: str,
		tmp_path: Path) -> None:
	blob  = _inline_image(SCHEMA)
	shape = shape_named_text(SCHEMA, name)
	for message, verdict in CASES[name].items():
		raw = bytes.fromhex(message)
		in_python = python_verdict(blob, raw, shape)
		in_c      = c_verdict(tmp_path, blob, raw, shape)
		assert in_python == in_c == verdict, (message, in_python, in_c)
