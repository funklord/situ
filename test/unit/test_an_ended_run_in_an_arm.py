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

from test_a_run_in_an_arm_is_walked import _declined_in_all_four  # noqa: E402

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
