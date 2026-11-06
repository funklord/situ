"""One field bounded by another, and a length over the two (26.902).

    u32 packet_length;
    u8  padding_length [max = packet_length - 1];
    u8  payload[packet_length - padding_length - 1];

An interval holds one field at a time, so that length read as negative, and
SSH's packet and HTTP/2's padded frame had no spelling. The bound was already
a check: what was missing was the solver reading it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import fourway
from test_walker_c import (COMPILER, _inline_image, c_verdict,
                           python_verdict, shape_named_text)
from situc.diagnostics import SituError
from situc.layout import solve
from situc.parser import parse_text

HEAD = "target buffer;\nendian big;\n"

SCHEMA = HEAD + """
struct ssh {
	u32 packet_length;
	u8  padding_length [max = packet_length - 1];
	u8  payload[packet_length - padding_length - 1];
	u8  padding[padding_length];
	u8  mac[remaining];
}

struct floors {
	u8 low;
	u8 high [min = low + 2];
	u8 gap[high - low - 2];
}

struct pinned {
	u8 whole;
	u8 part [must_eq = whole - 1];
	u8 one[whole - part];
}
"""

#: `packet_length` zero is the case a wrapped `a - 1` would let through.
CASES = {
	"ssh": {
		"00000001" "00":                "0",
		"00000003" "00" "aabb":         "0",
		"00000004" "01" "aabb" "cc":    "0",
		"00000004" "03" "aabbcc":       "0",
		"00000004" "01" "aabb" "cc" "dddd": "0",
		"00000000" "00":                "2",
		"00000000" "ff":                "2",
		"00000001" "01" "aa":           "2",
		"00000004" "04" "aabbccdd":     "2",
		"00000004" "ff":                "2",
		"ffffffff" "ff":                "1",
		"00000004" "01" "aa":           "1",
	},
	"floors": {
		"0002":                         "0",
		"0104" "aa":                    "0",
		"0001":                         "2",
		"fe00":                         "2",
		"0105":                         "1",
	},
	"pinned": {
		"0100" "aa":                    "0",
		"0504" "aa":                    "0",
		"0505" "aa":                    "2",
		"0007" "aa":                    "2",
	},
}


def _section(answers: str, struct: str) -> str:
	return answers[answers.index(f"-- {struct}\n"):].split("\n-- ")[0]


def _line(section: str, name: str) -> str:
	return next(line.split()[1] for line in section.splitlines()
	            if line.startswith(name + " "))


@pytest.mark.skipif(not fourway.COMPLETE, reason="needs gcc, g++ and rustc")
def test_all_four_hold_it_alike(tmp_path: Path) -> None:
	schema = tmp_path / "unit.situ"
	schema.write_text(SCHEMA, encoding="ascii")
	command = fourway.build(tmp_path, schema)
	for struct, cases in CASES.items():
		for message, verdict in cases.items():
			given = {backend: _section(fourway.answers(
			             argv, bytes.fromhex(message), tmp_path), struct)
			         for backend, argv in command.items()}
			assert len(set(given.values())) == 1, (message, given)
			assert _line(next(iter(given.values())), "validate") == verdict, \
				(message, given)


@pytest.mark.skipif(COMPILER is None, reason="needs a C compiler")
@pytest.mark.parametrize("struct", sorted(CASES))
def test_both_walkers_hold_it_alike(struct: str, tmp_path: Path) -> None:
	image = _inline_image(SCHEMA)
	shape = shape_named_text(SCHEMA, struct)
	for message, verdict in CASES[struct].items():
		data = bytes.fromhex(message)
		assert python_verdict(image, data, shape) == verdict, message
		assert c_verdict(tmp_path, image, data, shape) == verdict, message


REFUSED = {
	"no bound at all":
		"struct s { u8 a; u8 b; u8 d[a - b]; }",
	"a bound too loose by one":
		"struct s { u8 a; u8 b [max = a]; u8 d[a - b - 1]; }",
	"a bound in the wrong direction":
		"struct s { u8 a; u8 b [min = a]; u8 d[a - b]; }",
	"a bound on a field added, not subtracted":
		"struct s { u8 a [max = 9]; u8 b [max = a]; u8 d[b - a]; }",
	"a product of two fields":
		"struct s { u8 a; u8 b [max = a * a]; u8 d[a * a - b - 1]; }",
	"a bound naming a field declared later":
		"struct s { u8 a; u8 b [max = c]; u8 d[a - b]; u8 c; }",
}


@pytest.mark.parametrize("source", REFUSED.values(), ids=list(REFUSED))
def test_what_the_bound_does_not_prove_is_still_refused(source: str) -> None:
	with pytest.raises(SituError) as refused:
		solve(parse_text(HEAD + source))
	assert "negative" in str(refused.value)


def test_the_struct_minimum_counts_the_proved_floor() -> None:
	layout = solve(parse_text(HEAD + """
		struct s { u8 a; u8 b [max = a - 3]; u8 d[a - b]; }"""))
	assert layout.structs["s"].size_bits == (2 + 3) * 8
