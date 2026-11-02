"""A constraint's bound may name a `parameter` (26.798).

    struct note { parameter u8 mode; u8 v [max = mode]; }

0050 lets a parameter decide meaning freely, and a bound is meaning. Every
backend rendered the bound through its sibling-value hook, which reads a
member's getter, so C called `situ_note_mode_get` -- an accessor nobody
emits -- and did not compile, while C++, Rust and Python read
`mode`, which a view does not have. The bound now reads the argument.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

import fourway
from situc.cli import analyse
from situc.codegen.python import generate as generate_py
from situc.parser import parse

SCHEMA = """target buffer;
endian big;
struct note { parameter u8 mode; u8 v [max = mode]; }
struct plain { u8 x; }
"""


@pytest.mark.skipif(not fourway.COMPLETE, reason="needs gcc, g++ and rustc")
def test_all_four_build_it(tmp_path: Path) -> None:
	"""`plain` is there because the differential driver compares no struct
	that takes an argument, and building it is what compiles the other."""
	schema = tmp_path / "unit.situ"
	schema.write_text(SCHEMA, encoding="ascii")
	assert set(fourway.build(tmp_path, schema)) == {"c", "cpp", "rust",
	                                                "python"}


def test_the_bound_is_the_argument(tmp_path: Path) -> None:
	schema = tmp_path / "unit.situ"
	schema.write_text(SCHEMA, encoding="ascii")
	source, resolved, _ = analyse(schema)
	(tmp_path / "unit.py").write_text(
		generate_py(parse(source), resolved, "unit").module, encoding="ascii")
	(tmp_path / "situ_runtime.py").write_text(
		(fourway.RUNTIME / "python" / "situ_runtime.py")
		.read_text(encoding="ascii"), encoding="ascii")
	sys.path.insert(0, str(tmp_path))
	try:
		spec = importlib.util.spec_from_file_location(
			"unit", tmp_path / "unit.py")
		assert spec is not None and spec.loader is not None
		unit = importlib.util.module_from_spec(spec)
		spec.loader.exec_module(unit)
		runtime = sys.modules["situ_runtime"]
	finally:
		sys.path.remove(str(tmp_path))

	def validate(value: int, mode: int) -> None:
		message = runtime.Message(bytearray([value]))
		unit.note.at(message, 0, arg_mode=mode).validate()

	validate(3, 3)
	with pytest.raises(runtime.ConstraintError):
		validate(4, 3)
