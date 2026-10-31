"""`validate` walks a counted run of records in a variant arm (26.689).

    struct arms { u8 kind; u8 n;
                  variant body switch (kind) { case 1: piece recs[n];
                                               default: u8 raw; } }

No backend walks a run in an arm for an accessor, so the variant has no
length any of them can compute and `_variant_length` declined it whole --
and `check` then said nothing about any arm. A second record declaring a
byte the message does not hold validated clean in C, C++ and Rust. Python
and both walkers took `piece recs[n]` for one `piece`, so they checked the
first record and no other, and agreed with the three -- and C++, Rust and
Python each emitted an accessor reading that one record.

Now each arm is asked on its own: the run is walked as a top-level counted
run's `_fits` walks it, and an arm with a length is held to the frame
under its own discriminant value.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

import fourway

from test_walker_c import (COMPILER, _inline_image, c_verdict,
                           python_verdict, shape_named_text)

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from situc.codegen.c import generate as generate_c	# noqa: E402
from situc.codegen.cpp import generate as generate_cpp	# noqa: E402
from situc.codegen.python import generate as generate_py	# noqa: E402
from situc.codegen.rust import generate as generate_rs	# noqa: E402
from situc.layout import solve				# noqa: E402
from situc.parser import parse_text			# noqa: E402
from situc.resolve import resolve			# noqa: E402

SCHEMA = """target buffer;
endian big;
struct piece { u8 len; u8 body[len]; }
struct arms {
	u8 kind;
	u8 n;
	variant body switch (kind) {
		case 1:  piece  recs[n];
		default: u8     raw;
	}
}
"""

#: message -> what `validate` says: 0 whole, 1 short.
CASES = {
	"0102010501":     "1",	# the second record declares a byte not here
	"0102010501ff":   "0",
	"01020105010203": "0",	# trailing bytes are the frame's, not the run's
	"0102000100":     "0",
	"0101":           "1",	# the one record is not here at all
	"0100":           "0",
	"03020105":       "0",	# the default arm
	"0300":           "1",	# ...which is one byte, and none is here
}

#: And with a member after the variant, which no backend can place and so
#: nothing else holds to the frame: each arm asks for its own length and
#: the fixed tail behind it (26.779). `edges.situ`'s `unwalkable_arm`.
TAILED = SCHEMA.replace("\t}\n}\n", "\t}\n\tu16 after;\n}\n")

TAILED_CASES = {
	"0102010501ffaabb": "0",
	"0102010501ffaa":   "1",	# `after` is one byte short
	"0102010501aabb":   "1",	# the second record takes `after`'s bytes
	"01000000":         "0",
	"3e4f757b":         "1",	# the default arm, then one byte of `after`
	"3e4f757b00":       "0",
}

SHAPES = [(SCHEMA, CASES), (TAILED, TAILED_CASES)]


def _arms_verdict(answers: str) -> str:
	section = answers[answers.index("-- arms"):]
	return next(line.split()[1] for line in section.splitlines()
	            if line.startswith("validate"))


@pytest.mark.parametrize("text, cases", SHAPES, ids=["last", "tailed"])
def test_all_four_walk_the_run(text: str, cases: dict[str, str],
		tmp_path: Path) -> None:
	schema = tmp_path / "unit.situ"
	schema.write_text(text, encoding="ascii")
	command = fourway.build(tmp_path, schema)
	for message, verdict in cases.items():
		given = {name: _arms_verdict(fourway.answers(
		                 argv, bytes.fromhex(message), tmp_path))
		         for name, argv in command.items()}
		assert set(given.values()) == {verdict}, (message, given)


@pytest.mark.skipif(COMPILER is None, reason="no C compiler")
@pytest.mark.parametrize("text, cases", SHAPES, ids=["last", "tailed"])
def test_both_walkers_walk_the_run(text: str, cases: dict[str, str],
		tmp_path: Path) -> None:
	blob  = _inline_image(text)
	shape = shape_named_text(text, "arms")
	for message, verdict in cases.items():
		raw = bytes.fromhex(message)
		in_python = python_verdict(blob, raw, shape)
		in_c      = c_verdict(tmp_path, blob, raw, shape)
		assert in_python == in_c == verdict, (message, in_python, in_c)


def test_no_backend_offers_an_accessor_that_reads_one_record() -> None:
	"""The run is declined in all four, as C declined it.

	C++, Rust and Python took `piece recs[n]` for one `piece`: `body_recs`
	handed back the first record under the run's name, and
	`body_recs_extent` measured that one.
	"""
	parsed   = parse_text(SCHEMA)
	resolved = resolve(parsed, solve(parsed))
	emitted  = {
		"c":      generate_c(parsed, resolved, "unit").header,
		"cpp":    generate_cpp(parsed, resolved, "unit").header,
		"rust":   generate_rs(parsed, resolved, "unit").module,
		"python": generate_py(parsed, resolved, "unit").module,
	}
	for name, text in emitted.items():
		# Accessors by their definitions: `validate`'s own walk names its
		# locals after the member.
		for defined in ("body_recs(", "body_recs_extent", "body_recs_get",
		                "def body_recs"):
			assert defined not in text, (name, defined)
		assert "`recs` is not a shape this backend reaches into yet" in text, name
