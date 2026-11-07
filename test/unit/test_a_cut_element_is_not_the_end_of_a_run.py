"""A varint the frame cuts short measures zero, and zero is not the run's end.

Each verdict here is by hand, because all six readers agreed on the wrong one:
`01 ff` under `r4` declares an element, cuts it, and has no `last`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import fourway
from test_nesting_costs_no_more_than_its_length import pack_text
from test_walker_c import (VERDICT, _build_driver, c_verdict, python_verdict,
                           shape_named_text)

SCHEMA = """target buffer;
endian big;
varint_type u { encoding = leb128; max_bits = 32; }
struct nothing { }
struct e { u v; }
struct w { peek u8 c; variant b switch (c) { case 1: e one; default: e other; } }
struct r1 { e es[remaining]; }
struct r3 { w es[remaining]; }
struct r4 { u8 n; e es[n]; u8 last; }
struct r5 { u8 n; nothing es[n]; u8 last; }
struct arm { u8 c; variant b switch (c) { case 1: r1 one; default: nothing other; } }
"""

#: struct, message, whether it is whole.
VERDICTS = [
	("r1", "ff", False), ("r1", "0180", False), ("r1", "0102", True),
	("r3", "ff", False), ("r3", "0180", False), ("r3", "0102", True),
	("r4", "01ff", False), ("r4", "0280", False), ("r4", "02010207", True),
	("r5", "0507", True),		# an element that is really no bytes
	("arm", "01ff", False), ("arm", "0101", True),
]


def _validate(command: list[str], message: bytes, struct: str,
		cwd: Path) -> str:
	said = fourway.answers(command, message, cwd)
	section = said[said.index(f"-- {struct}\n"):].split("\n-- ")[0]
	return next((line.split(" ", 1)[1] for line in section.splitlines()
	             if line.startswith("validate ")), "no-view")


@pytest.mark.skipif(not fourway.COMPLETE, reason = "needs gcc, g++ and rustc")
def test_all_four_refuse_a_cut_element(tmp_path: Path) -> None:
	schema = tmp_path / "unit.situ"
	schema.write_text(SCHEMA, encoding = "ascii")
	command = fourway.build(tmp_path, schema)
	for struct, message, whole in VERDICTS:
		for backend, argv in command.items():
			said = _validate(argv, bytes.fromhex(message), struct, tmp_path)
			assert (said == "0") == whole, (backend, struct, message, said)


def test_both_walkers_refuse_a_cut_element(tmp_path: Path) -> None:
	blob  = pack_text(SCHEMA)
	drive = _build_driver(tmp_path, blob, VERDICT)
	for struct, message, whole in VERDICTS:
		shape = shape_named_text(SCHEMA, struct)
		data  = bytes.fromhex(message)
		wanted = "0" if whole else "1"
		assert c_verdict(tmp_path, blob, data, shape, drive) == wanted, \
			(struct, message)
		assert python_verdict(blob, data, shape) == wanted, (struct, message)
