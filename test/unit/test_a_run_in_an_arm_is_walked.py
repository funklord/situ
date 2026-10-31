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

#: The other runs of structs an arm can hold: a literal count, and fixed
#: records counted and literal. The accessor family took each for its first
#: element too, and Python and both walkers validated that element alone --
#: so `[max]` on element 0 refused where C, C++ and Rust accepted, and on
#: element 1 nobody refused. A run's elements are not validated in any
#: backend; `check` holds the run to the frame (26.779).
RUNS = """target buffer;
endian big;
struct piece { u8 len; u8 body[len]; }
struct pt { u8 x; u8 y [max = 9]; }
struct arms {
	u8 kind;
	u8 n;
	variant body switch (kind) {
		case 1:  piece  two[2];
		case 2:  pt     pts[n];
		case 3:  pt     q[2];
		default: u8     raw;
	}
	u16 after;
}
"""

RUNS_CASES = {
	"0100010200aabb":     "0",
	"01000102":           "1",
	"02020102030ff0aabb": "0",
	"0202010203":         "1",
	"0202010a0304aabb":   "0",	# element 0 is over its `[max]`
	"030001020305":       "1",
	"030001ff0304aabb":   "0",	# and here
}

#: A `[remaining]` run, which walks to the frame's end rather than to a
#: count. The four declined it, so a record overrunning the frame validated
#: clean; the C walker took the byte count for a record count and reached
#: for a second record where one was the whole frame (26.783).
REMAINING = SCHEMA.replace("piece  recs[n]", "piece  recs[remaining]")

REMAINING_CASES = {
	"0100010501":         "1",	# the second record declares a byte not here
	"01000105":           "0",	# one record, and it is the whole frame
	"010001050102030405": "1",	# the third declares three and has two
	"01000201":           "1",
	"010000":             "0",
	"0100000000":         "0",
	"01000003":           "1",
}

SHAPES = [(SCHEMA, CASES), (TAILED, TAILED_CASES), (RUNS, RUNS_CASES),
          (REMAINING, REMAINING_CASES)]
IDS    = ["last", "tailed", "runs", "remaining"]


def _arms_verdict(answers: str) -> str:
	section = answers[answers.index("-- arms"):]
	return next(line.split()[1] for line in section.splitlines()
	            if line.startswith("validate"))


@pytest.mark.parametrize("text, cases", SHAPES, ids=IDS)
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
@pytest.mark.parametrize("text, cases", SHAPES, ids=IDS)
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
	for text, names in ((SCHEMA, ("recs",)), (RUNS, ("two", "pts", "q"))):
		_declined_in_all_four(text, names)


def _declined_in_all_four(text: str, names: tuple[str, ...]) -> None:
	parsed   = parse_text(text)
	resolved = resolve(parsed, solve(parsed))
	emitted  = {
		"c":      generate_c(parsed, resolved, "unit").header,
		"cpp":    generate_cpp(parsed, resolved, "unit").header,
		"rust":   generate_rs(parsed, resolved, "unit").module,
		"python": generate_py(parsed, resolved, "unit").module,
	}
	for name, emitted_text in emitted.items():
		for run in names:
			# Accessors by their definitions: `validate`'s own walk names
			# its locals after the member.
			for defined in (f"body_{run}(", f"body_{run}_extent",
			                f"body_{run}_view", f"body_{run}_get",
			                f"def body_{run}"):
				assert defined not in emitted_text, (name, defined)
			assert (f"`{run}` is not a shape this backend reaches into yet"
			        in emitted_text), (name, run)
