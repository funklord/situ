"""A pad at a known offset is checked over all its bytes (26.1139).

`u8 one; pad_to(4); u32 word;` pads three bytes, which 0043 makes
`must_be_zero`. All four backends checked the pad as its one-byte element:
the first of the three, so `00 00 01 00` validated clean -- and a pad of NO
bytes still read one, the next member's, so `u32 a; pad_to(4); u8 b;` with
`b = 5` was refused by all four. Both walkers were right; replaying the
fuzz corpora through the walker found the first, and asking what a pad of
none read found the second.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "test" / "unit"))

import pytest							# noqa: E402

import fourway							# noqa: E402
from test_walker_c import (COMPILER, c_verdict, image_for,	# noqa: E402
                           python_verdict, shape_named)

PADDED = ROOT / "test" / "schema" / "padded.situ"

#: aligned_header: each pad byte nonzero in turn, then a clean one.
THREE = {"0001000000000000": "2", "0000010000000000": "2",
         "0000000100000000": "2", "0000000000000005": "0"}

NONE_SCHEMA = "target buffer;\nendian big;\nstruct z { u32 a; pad_to(4); u8 b; }\n"
NONE_CASES = {"0000000105": "0", "0000000100": "0"}


def _validate(said: str, struct: str) -> str:
	lines = said.splitlines()
	at = lines.index(f"-- {struct}")
	return next(line for line in lines[at + 1:] if line.startswith("validate"))


def _cases(tmp_path: Path) -> list[tuple[Path, str, dict[str, str]]]:
	none = tmp_path / "zero_pad.situ"
	none.write_text(NONE_SCHEMA, encoding="utf-8")
	return [(PADDED, "aligned_header", THREE), (none, "z", NONE_CASES)]


@pytest.mark.skipif(not fourway.COMPLETE, reason="needs all four toolchains")
def test_four_backends_check_every_pad_byte(tmp_path: Path) -> None:
	for schema, struct, cases in _cases(tmp_path):
		built = tmp_path / schema.stem
		built.mkdir()
		command = fourway.build(built, schema)
		for message, verdict in cases.items():
			for name, argv in command.items():
				got = _validate(fourway.answers(argv, bytes.fromhex(message),
				                                built), struct)
				assert got == f"validate {verdict}", (name, schema.stem,
				                                      message)


@pytest.mark.skipif(COMPILER is None, reason="needs a C compiler")
def test_both_walkers_agree(tmp_path: Path) -> None:
	for schema, struct, cases in _cases(tmp_path):
		image = image_for(schema)
		shape = shape_named(schema, struct)
		for message, verdict in cases.items():
			data = bytes.fromhex(message)
			assert python_verdict(image, data, shape) == verdict, message
			assert c_verdict(tmp_path, image, data, shape) == verdict, message
