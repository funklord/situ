"""`default: opaque` takes any discriminant no `case` names (26.1100).

Section 9.6 says an opaque default arm "swallows whatever is left", and
`traverse.unmatched_values_pass` said so in its docstring -- and tested
only for a default arm selecting a member. A bare `default: opaque;`
selects none, so it read as `default: error`: all four backends and both
walkers answered VERSION for the value the arm exists to take, and agreed
with each other the whole time, which is why no differential saw it.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "test" / "unit"))

import pytest							# noqa: E402

import fourway							# noqa: E402

SCHEMA = """target buffer;
endian big;
struct section {
	u8 kind;
	u8 len;
	variant payload[len] switch (kind) {
		case 0: u8 raw[remaining];
		default: opaque;
	}
}
"""

#: 0 the case, 1 a length past the frame, and an unnamed kind is taken.
CASES = [(bytes.fromhex("00024548"), "0"),
         (bytes.fromhex("ff024548"), "0"),
         (bytes.fromhex("ff00"), "0"),
         (bytes.fromhex("ffa94548"), "1"),
         (bytes.fromhex("00a94548"), "1")]


def _validate(said: str) -> str:
	lines = said.splitlines()
	at = lines.index("-- section")
	return next(line.split()[1] for line in lines[at + 1:]
	            if line.startswith("validate "))


@pytest.mark.skipif(not fourway.COMPLETE, reason="needs all four toolchains")
def test_four_backends_take_an_unnamed_kind(tmp_path: Path) -> None:
	schema = tmp_path / "opaque.situ"
	schema.write_text(SCHEMA, encoding="ascii")
	command = fourway.build(tmp_path, schema)
	for raw, want in CASES:
		for name, argv in command.items():
			got = _validate(fourway.answers(argv, raw, tmp_path))
			assert got == want, (name, raw.hex(), got)


def test_both_walkers_take_an_unnamed_kind(tmp_path: Path) -> None:
	from test_walker_c import (COMPILER, VERDICT, _build_driver, c_verdict,
	                           packed_text, python_verdict, shape_named_text)
	blob  = packed_text(SCHEMA)
	shape = shape_named_text(SCHEMA, "section")
	drive = (_build_driver(tmp_path, blob, VERDICT)
	         if COMPILER is not None else None)
	for raw, want in CASES:
		assert python_verdict(blob, raw, shape) == want, ("python", raw.hex())
		if drive is not None:
			assert c_verdict(tmp_path, blob, raw, shape, drive) == want, \
				("c walker", raw.hex())
