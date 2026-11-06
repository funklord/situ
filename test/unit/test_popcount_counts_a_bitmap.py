"""`popcount` counts what a presence bitmap says follows it (26.899).

    u32 present;
    u32 values[popcount(present)];
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

SCHEMA = """target buffer;
endian big;
struct s { u32 present; u8 v[popcount(present)]; u8 after [max = 9]; }
struct wide { u64 present; u8 v[popcount(present)]; u8 after; }
"""

#: Bit 31 and bit 63 are the reason a read is not held to `LEAF_MAX` here.
CASES = {
	"s": {
		"00000000" "07":                  "0",
		"00000003" "aabb" "07":           "0",
		"80000001" "aabb" "07":           "0",
		"ffffffff" + "aa" * 32 + "07":    "0",
		"80000000" "aa" "63":             "2",
		"00000003" "aa":                  "1",
	},
	"wide": {
		"8000000000000000" "aa" "07":     "0",
		"ff000000ffffffff" + "aa" * 40 + "07": "0",
	},
}


def _section(answers: str, struct: str) -> str:
	return answers[answers.index(f"-- {struct}\n"):].split("\n-- ")[0]


def _line(section: str, name: str) -> str:
	return next(line.split()[1] for line in section.splitlines()
	            if line.startswith(name + " "))


@pytest.mark.skipif(not fourway.COMPLETE, reason="needs gcc, g++ and rustc")
def test_all_four_count_alike(tmp_path: Path) -> None:
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
def test_both_walkers_count_alike(struct: str, tmp_path: Path) -> None:
	image = _inline_image(SCHEMA)
	shape = shape_named_text(SCHEMA, struct)
	for message, verdict in CASES[struct].items():
		data = bytes.fromhex(message)
		assert python_verdict(image, data, shape) == verdict, message
		assert c_verdict(tmp_path, image, data, shape) == verdict, message


def test_a_signed_value_has_no_bits_to_count() -> None:
	with pytest.raises(SituError) as refused:
		solve(parse_text("target buffer;\nendian big;\n"
		                 "struct s { i16 m; u8 v[popcount(m)]; }\n"))
	assert "unsigned" in str(refused.value)


def test_the_solver_bounds_the_count_by_the_width() -> None:
	layout = solve(parse_text("target buffer;\nendian big;\n"
	                          "struct s { u8 m; u8 v[popcount(m)]; }\n"))
	assert layout.structs["s"].size_max_bits == (1 + 8) * 8
