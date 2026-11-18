"""A text number or a token set may run to the frame less a fixed tail (0070).

`[remaining]` left the fixed members after it their bytes only for a run of
plain `u8` (26.894, 26.905), so a duration written as fuzznet writes one --
digits, then a one-byte unit, `14d` -- had no spelling: `decimal u32
count[remaining]; time_unit unit[1];` was refused as "`[remaining]` must be
the last member of its frame". Digits and a token's spelling are bytes read
as something; the span is the same frame less the tail, and every reader
already sizes a `u8` run that way.

Held across six readers, the four backends' member listings as well as
their verdicts: a digit that is the unit, an empty count, a second spelling
under `[minimal]`, and a number past the member's domain.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "test" / "unit"))

import pytest							# noqa: E402

import fourway							# noqa: E402
from test_walker_c import (c_verdict, packed_text,		# noqa: E402
                           python_verdict, shape_named_text)

SCHEMA = """target buffer;
endian big;

tokens time_unit {
	d = "d",
	h = "h",
	m = "m",
	s = "s",
	default = error,
}

struct duration {
	decimal u32  count[remaining]  [minimal];
	time_unit    unit[1];
}
"""

#: Each frame: the count line every backend prints, then `validate`.
CASES = {
	b"14d":  ("count ok=1 value=14", "0"),
	b"14x":  ("count ok=1 value=14", "2"),
	b"d":    ("count ok=0 value=0", "2"),
	b"007s": ("count ok=1 value=7", "2"),
	b"1.5h": ("count ok=0 value=0", "2"),
	b"3":    ("count ok=0 value=0", "2"),
	b"4294967296m": ("count ok=0 value=0", "2"),
	b"4294967295m": ("count ok=1 value=4294967295", "0"),
}


@pytest.mark.skipif(not fourway.COMPLETE, reason="needs all four toolchains")
def test_six_readers_size_digits_less_their_unit(tmp_path: Path) -> None:
	schema = tmp_path / "duration.situ"
	schema.write_text(SCHEMA, encoding="ascii")
	command = fourway.build(tmp_path, schema)
	assert set(command) == {"c", "cpp", "rust", "python"}, command
	blob  = packed_text(SCHEMA)
	shape = shape_named_text(SCHEMA, "duration")

	for raw, (count, verdict) in CASES.items():
		for name, argv in command.items():
			said = fourway.answers(argv, raw, tmp_path).splitlines()
			assert [line for line in said
			        if line.startswith(("count ", "validate "))] \
				== [count, f"validate {verdict}"], (name, raw, said)
		assert python_verdict(blob, raw, shape) == verdict, raw
		assert c_verdict(tmp_path, blob, raw, shape) == verdict, raw


def test_a_run_of_records_still_has_to_be_last() -> None:
	"""Only bytes read as something: a run of records has no span to take
	the tail off, and that refusal stands."""
	from situc.diagnostics import Source
	from situc.layout import solve
	from situc.parser import parse

	text = """target buffer;
endian big;
struct item { u8 k; }
struct s { item items[remaining]; u8 tail; }
"""
	with pytest.raises(Exception, match="must be the last member"):
		solve(parse(Source("records.situ", text)))
