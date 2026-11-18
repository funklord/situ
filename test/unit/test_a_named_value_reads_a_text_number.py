"""A named value may read a text number, compare, and test a token (26.1079).

`let` read integer fields alone (26.1056), so a duration's seconds --
fuzznet bounds a duration in seconds, and writes it as digits and a unit --
had no spelling: a text number "is not one integer in the frame", and
neither a comparison nor a token member was arithmetic. 0070's third gap.

- A text number reads as its value, and as 0 wherever its checked getter
  refuses: digits that are not one, a value past the member's own domain.
  Both walkers read it as the backends' lax `_value` does, held to that
  domain now -- they took `4294967296` in a `decimal u32` as the number.
- A comparison is 1 or 0.
- A token member is compared with `==` or `!=` against one of its own arms,
  and nothing else: its reading is which arm it spells, and the order the
  arms were declared in is not a number a schema means.

Both walkers compute a token test through `spells` (26.1082), the arm's
bytes carried in the program.
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
from test_a_named_value_is_walked import LETS, python_lets	# noqa: E402
from test_walker_c import (_build_driver, _run_driver,		# noqa: E402
                           packed_text, shape_named_text)

SCHEMA = """target buffer;
endian big;

tokens time_unit {
	d = "d",
	h = "h",
	m = "m",
	s = "s",
	default = error,
}

struct twice {
	decimal u32  count[remaining]  [minimal];
	u8           unit;
	let u64 doubled = count * 2;
}

struct signed_twice {
	decimal i8   count until "d" max 5;
	let i64 doubled = count * 2;
}

struct duration {
	decimal u32  count[remaining]  [minimal];
	time_unit    unit[1];
	let u64 seconds = count * ((unit == time_unit.d) * 86400
	                         + (unit == time_unit.h) * 3600
	                         + (unit == time_unit.m) * 60
	                         + (unit == time_unit.s));
	let u8  daily = unit != time_unit.d;
}
"""

#: struct, frame, and every `let` line the four backends print.
BACKENDS = [
	("twice", b"14d", ["doubled 28"]),
	("twice", b"1xd", ["doubled 0"]),
	("twice", b"4294967296d", ["doubled 0"]),
	("twice", b"4294967295d", ["doubled 8589934590"]),
	("signed_twice", b"-128d", ["doubled -256"]),
	("signed_twice", b"-129d", ["doubled 0"]),
	("signed_twice", b"128d", ["doubled 0"]),
	("duration", b"14d", ["seconds 1209600", "daily 0"]),
	("duration", b"2h", ["seconds 7200", "daily 1"]),
	("duration", b"90m", ["seconds 5400", "daily 1"]),
	("duration", b"5s", ["seconds 5", "daily 1"]),
	("duration", b"14x", ["seconds 0", "daily 1"]),
	("duration", b"4294967296d", ["seconds 0", "daily 0"]),
]


def _lets(said: str, struct: str) -> list[str]:
	lines = said.splitlines()
	at = lines.index(f"-- {struct}")
	out = []
	for line in lines[at + 1:]:
		if line.startswith("-- "):
			break
		if line.split(" ", 1)[0] in ("doubled", "seconds", "daily"):
			out.append(line)
	return out


@pytest.mark.skipif(not fourway.COMPLETE, reason="needs all four toolchains")
def test_six_readers_read_a_text_number_in_a_named_value(
		tmp_path: Path) -> None:
	schema = tmp_path / "named.situ"
	schema.write_text(SCHEMA, encoding="ascii")
	command = fourway.build(tmp_path, schema)
	assert set(command) == {"c", "cpp", "rust", "python"}, command
	blob  = packed_text(SCHEMA)
	drive = _build_driver(tmp_path, blob, LETS)

	for struct, raw, want in BACKENDS:
		for name, argv in command.items():
			said = _lets(fourway.answers(argv, raw, tmp_path), struct)
			assert said == want, (name, struct, raw, said)
		shape  = shape_named_text(SCHEMA, struct)
		walked = python_lets(blob, raw, shape)
		assert _run_driver(drive, tmp_path, raw, shape, by_line=True) \
			== walked, (struct, raw)
		assert walked == [line.split()[1] for line in want], (raw, walked)


@pytest.mark.parametrize("let, why", [
	("let u64 v = unit + 1;", "uses `\\+` on a token"),
	("let u64 v = unit;", "a token member, outside a comparison"),
	("let u64 v = unit < time_unit.d;", "uses `<` on a token"),
	("let u64 v = count == time_unit.d;",
	 "other than one of its own arms"),
	("let u64 v = scaled_one * 2;", "a scaled number"),
])
def test_what_a_named_value_still_cannot_read(let: str, why: str) -> None:
	text = """target buffer;
endian big;
tokens time_unit { d = "d", default = error, }
struct s {
	scaled u32   scaled_one until " " max 8;
	decimal u32  count until ":" max 8;
	time_unit    unit[1];
	%s
}
""" % let
	with pytest.raises(Exception, match=why):
		parse(Source("lets.situ", text))
