"""A `[remaining] max N` run of bytes is built as the plain span (26.1119).

fuzznet's local search ask ends in `u8 ask[remaining] max 489` and had no
builder in any backend -- "takes whatever is left, so its length is the
frame's" -- while the same run without `max` was the caller's bytes and a
length. The bound is the view's, and every builder validates what it
wrote against the view, so the cap is held there and not stated twice.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from every_schema import ROOT
from situc.codegen.c import build
from situc.codegen.c import generate as generate_c
from situc.layout import solve
from situc.parser import parse_text
from situc.resolve import resolve

SCHEMA = """target buffer;
endian big;
struct capped { u8 a; u8 rest[remaining] max 8; }
struct wide { u8 a; u16 rest[remaining] max 4; }
"""


def _refusals(source: str) -> dict[str, str]:
	schema = parse_text(source)
	resolved = resolve(schema, solve(schema))
	header = generate_c(schema, resolved, "x").files().get("x.h", "")
	shape = build.Shape(header, "situ", resolved.structs,
	                    {decl.name: decl for decl in schema.varints()})
	return dict(build.refusals(resolved, shape))


def test_a_capped_byte_run_has_a_builder_and_a_wider_one_has_none() -> None:
	refused = _refusals(SCHEMA)
	assert not any(name.startswith("capped") for name in refused), refused
	# Wider elements keep the refusal, which `test_a_refusal_says_what_the_
	# member_is` holds to the table of claims as a constructed shape.
	assert any(name.startswith("wide") and "takes whatever is left" in why
	           for name, why in refused.items()), refused


def test_the_python_builder_holds_the_cap(tmp_path: Path) -> None:
	(tmp_path / "s.situ").write_text(SCHEMA, encoding="ascii")
	done = subprocess.run(
		[sys.executable, str(ROOT / "bin" / "situc"), "build", "--target",
		 "python", "--layer", "edit", "--out", str(tmp_path),
		 str(tmp_path / "s.situ")], capture_output=True, text=True,
		timeout=300)
	assert done.returncode == 0, done.stderr
	sys.path[:0] = [str(tmp_path), str(ROOT / "runtime" / "python")]
	try:
		import s_build				# type: ignore[import-not-found]
		out = bytearray(32)
		assert s_build.build_capped(out, 1, bytes(8)) == 9
		assert bytes(out[:9]) == b"\x01" + bytes(8)
		with pytest.raises(Exception):
			s_build.build_capped(bytearray(32), 1, bytes(9))
	finally:
		del sys.path[:2]
		sys.modules.pop("s_build", None)
		sys.modules.pop("s", None)
