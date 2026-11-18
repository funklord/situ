"""A token set may take its extent from a length rather than a delimiter (0070).

A token set decided no width and took its end from `until`, so a value that
is the whole of its frame -- a settings cell's `on`, a keyword that ends a
datagram -- had no spelling: `flag_word word[remaining]` was refused as
"gives a width to a token set". The width was never the set's; a length the
message gives is as good an end as a delimiter, and the span is compared
against the arms the same way.

Held across six readers, since the four backends share the decision layer
and only the walkers read the packed image independently: the arms admit,
anything else is a constraint, and a span sized by a length field is
compared whole -- `o` is a prefix of `on` and of `off`, and no spelling.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "test" / "unit"))

import pytest							# noqa: E402

import fourway							# noqa: E402
from situc.diagnostics import Source				# noqa: E402
from situc.parser import parse					# noqa: E402
from situc.wellformed import check as check_wellformed	# noqa: E402
from test_walker_c import (c_verdict, packed_text,		# noqa: E402
                           python_verdict, shape_named_text)

SCHEMA = """target buffer;
endian big;

tokens flag_word {
	on = "on",
	off = "off",
	one = "1",
	zero = "0",
	default = error,
}

struct flag {
	flag_word  word[remaining];
}

struct counted {
	u8         n;
	flag_word  word[n];
	u8         rest[remaining];
}

struct pair {
	flag_word  word[2];
	u8         rest[remaining];
}
"""

#: Each frame, and what `validate` answers for `flag`, `counted` and
#: `pair` -- the last a FIXED width, which only `on` fills.
CASES = {
	b"on":   ("0", "1", "0"),	# `counted` reads n = 'o': past the frame
	b"off":  ("0", "1", "2"),	# `pair` holds `of`
	b"1":    ("0", "1", "1"),
	b"yes":  ("2", "1", "2"),
	b"o":    ("2", "1", "1"),
	b"onn":  ("2", "1", "0"),
	b"\x02on": ("2", "0", "2"),
	b"\x03off!": ("2", "0", "2"),
	b"\x01o": ("2", "2", "2"),	# a prefix of two arms, and neither
	b"\x02no": ("2", "2", "2"),
	b"\x00": ("2", "2", "1"),	# the empty span is no spelling
}


def _validate(said: str, struct: str) -> str:
	"""The struct's `validate`, or `1` where the frame is below its minimum
	and the driver could not acquire a view -- the walkers' answer for the
	same refusal, made earlier."""
	lines = said.splitlines()
	at = lines.index(f"-- {struct}")
	for line in lines[at + 1:]:
		if line == "no-view":
			return "1"
		if line.startswith("validate "):
			return line.split()[1]
	raise AssertionError(f"no verdict for {struct}")


@pytest.mark.skipif(not fourway.COMPLETE, reason="needs all four toolchains")
def test_six_readers_compare_a_sized_token_span(tmp_path: Path) -> None:
	schema = tmp_path / "cell.situ"
	schema.write_text(SCHEMA, encoding="ascii")
	command = fourway.build(tmp_path, schema)
	assert set(command) == {"c", "cpp", "rust", "python"}, command
	blob = packed_text(SCHEMA)

	for raw, want in CASES.items():
		for struct, expected in zip(("flag", "counted", "pair"), want):
			shape = shape_named_text(SCHEMA, struct)
			row = {name: _validate(fourway.answers(argv, raw, tmp_path),
			                       struct)
			       for name, argv in command.items()}
			row["pywalk"] = python_verdict(blob, raw, shape)
			row["cwalk"]  = c_verdict(tmp_path, blob, raw, shape)
			assert set(row.values()) == {expected}, (struct, raw, row)


def test_a_width_no_spelling_has_is_refused() -> None:
	text = SCHEMA.replace("flag_word  word[remaining];",
	                      "flag_word  word[5];")
	with pytest.raises(Exception, match="no spelling of `flag_word` is"):
		check_wellformed(parse(Source("cell.situ", text)))


def test_a_sized_token_in_an_arm_is_refused_until_it_is_read() -> None:
	text = """target buffer;
endian big;
tokens t { a = "a", default = error, }
struct s {
	u8 k;
	variant body switch (k) {
		case 1:  t   word[remaining];
		default: u8  rest[remaining];
	}
}
"""
	with pytest.raises(Exception, match="inside a variant arm"):
		check_wellformed(parse(Source("arm.situ", text)))
