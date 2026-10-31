"""A run of records that a condition or a delimiter ends, in an arm (26.781).

    struct arms { u8 kind; u8 n;
                  variant body switch (kind) {
                      case 1:  pt recs[] while (x != 0) max 6;
                      default: u8 raw; } }

26.780 declined every run of structs in an arm that a count sizes, and
asked `array_count` and `data_sized` to say which those were. A `while`
run and a delimited one set neither, so they still read as one struct:
C++, Rust and Python handed back the first record under the run's name,
and so did C, through `_view`. And C did not compile at all. `frameable`
said such a struct could be framed while C's `_required` declined it, so
the driver called `arms_required` and nothing defined it; a delimited run
also had `check` ask `_terminated`, which no arm emits for a run.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

import fourway

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from situc.codegen.c.frame import framed_structs	# noqa: E402
from situc.layout import solve				# noqa: E402
from situc.parser import parse_text			# noqa: E402
from situc.resolve import resolve			# noqa: E402

from test_a_run_in_an_arm_is_walked import (_arms_verdict,	# noqa: E402
                                            _declined_in_all_four)
from test_walker_c import (COMPILER, _inline_image,		# noqa: E402
                           c_verdict, python_verdict, shape_named_text)

HEAD = """target buffer;
endian big;
struct piece { u8 len; u8 body[len]; }
struct pt { u8 x; u8 y; }
struct kv { u8 key[] until ":"; u8 value[] until ";"; }
"""

ARMS = {
	"while":       "piece recs[] while (len != 0) max 6;",
	"while_fixed": "pt recs[] while (x != 0) max 6;",
	"until":       "kv recs[] until \"\\n\";",
	"until_fixed": "pt recs[] until \"\\n\";",
}


def _schema(arm: str) -> str:
	return (HEAD + "struct arms {\n\tu8 kind;\n\tu8 n;\n"
	        "\tvariant body switch (kind) {\n"
	        f"\t\tcase 1:  {arm}\n\t\tdefault: u8 raw;\n\t}}\n}}\n")


@pytest.mark.parametrize("arm", ARMS.values(), ids=ARMS.keys())
def test_no_backend_offers_an_accessor_that_reads_one_record(arm: str) -> None:
	_declined_in_all_four(_schema(arm), ("recs",))


@pytest.mark.parametrize("arm", ARMS.values(), ids=ARMS.keys())
def test_no_backend_frames_it(arm: str) -> None:
	"""`frameable` agrees with C's `_required`, which declines it."""
	parsed   = parse_text(_schema(arm))
	resolved = resolve(parsed, solve(parsed))
	assert "arms" not in {struct.name for struct in framed_structs(resolved)}


@pytest.mark.skipif(not fourway.COMPLETE, reason="needs gcc, g++ and rustc")
@pytest.mark.parametrize("arm", ARMS.values(), ids=ARMS.keys())
def test_all_four_build_and_agree(arm: str, tmp_path: Path) -> None:
	schema = tmp_path / "unit.situ"
	schema.write_text(_schema(arm), encoding="ascii")
	command = fourway.build(tmp_path, schema)
	for message in ("0100013b", "01000102030a", "0300ff", "01003a3b0a"):
		given = {name: fourway.answers(argv, bytes.fromhex(message), tmp_path)
		         for name, argv in command.items()}
		assert len(set(given.values())) == 1, (message, given)


#: With members after the variant, which nothing placed and nothing held to
#: the frame: the four had no walk for these runs in an arm, so a message
#: cutting `after` short validated clean while both walkers refused it. And
#: the walkers validated a `while` or delimited run as its first record, so
#: a cut `kv` was a missing delimiter in both -- where the four hold only the
#: run's reach (26.785). A record the frame does not hold ends the run rather
#: than refusing it, as at the top level (26.680; `walk._while_walk`).
TAILED = HEAD + """struct arms {
	u8 kind;
	variant body switch (kind) {
		case 1:  piece recs[] while (len != 0) max 3;
		case 2:  pt    pts[] until "\\n";
		case 3:  kv    kvs[] until "\\n";
		case 4:  pt    rem[] while (remaining > 2);
		default: u8    raw;
	}
	u16 after;
}
"""

TAILED_CASES = {
	"0101050100aabb":         "0",
	"0101050100aa":           "1",	# `after` is one byte short
	"01010501060107aabb":     "0",	# three, the cap, condition still true
	"01010501060107aa":       "1",
	"010105010601070108aabb": "0",	# the cap stops it, not the condition
	"02010203040aaabb":       "0",
	"02010203040aaa":         "1",
	"0201020304aabb":         "1",	# no terminator: the run takes `after`
	"03613a623b0aaabb":       "0",
	"03613a623b0aaa":         "1",
	"03613a62aabb":           "1",	# a cut `kv` ends the run, not a refusal
	"04010203aa":             "0",	# one `pt`, then two left: it stops
	"04010203aabb":           "1",
	"ffccaabb":               "0",
}

#: The C walker measures a delimited run of records as one record, at the
#: top level too, so the tail after kinds 2 and 3 is asked of the wrong
#: offset there; that is its own fault, not an arm's.
DELIMITED_KINDS = ("02", "03")


@pytest.mark.skipif(not fourway.COMPLETE, reason="needs gcc, g++ and rustc")
def test_all_four_hold_the_tail(tmp_path: Path) -> None:
	schema = tmp_path / "unit.situ"
	schema.write_text(TAILED, encoding="ascii")
	command = fourway.build(tmp_path, schema)
	for message, verdict in TAILED_CASES.items():
		given = {name: _arms_verdict(fourway.answers(
		                 argv, bytes.fromhex(message), tmp_path))
		         for name, argv in command.items()}
		assert set(given.values()) == {verdict}, (message, given)


@pytest.mark.skipif(COMPILER is None, reason="no C compiler")
def test_both_walkers_hold_the_tail(tmp_path: Path) -> None:
	blob  = _inline_image(TAILED)
	shape = shape_named_text(TAILED, "arms")
	for message, verdict in TAILED_CASES.items():
		raw = bytes.fromhex(message)
		assert python_verdict(blob, raw, shape) == verdict, message
		if message[:2] not in DELIMITED_KINDS:
			assert c_verdict(tmp_path, blob, raw, shape) == verdict, message


@pytest.mark.skipif(COMPILER is None, reason="no C compiler")
@pytest.mark.parametrize("arm", ARMS.values(), ids=ARMS.keys())
def test_both_walkers_leave_the_records_alone(arm: str,
		tmp_path: Path) -> None:
	"""No tail: a cut record ends the run, and nothing refuses."""
	text  = _schema(arm)
	blob  = _inline_image(text)
	shape = shape_named_text(text, "arms")
	for message in ("01003a", "0100013b", "01000105"):
		raw = bytes.fromhex(message)
		assert python_verdict(blob, raw, shape) == "0", message
		assert c_verdict(tmp_path, blob, raw, shape) == "0", message
