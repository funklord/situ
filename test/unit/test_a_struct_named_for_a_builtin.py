"""A struct may be called what a builtin is called, and the module still works.

`class bytes(View)` rebinds the name for the whole generated module, and the
module calls `bytes` for its own reasons. hull named a struct `bytes`, which is
what a canonical s-expression's atom is, and a reader raised a TypeError from
inside `situc verify` (26.879). C, C++ and Rust never met it.

Every name below is a builtin some generated module calls, measured over the
corpus rather than listed from memory, and each schema reaches `len`, `bytes`,
`int`, `min` and the rest by reading a real message.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from every_schema import ROOT
from situc.codegen.python import generate
from situc.layout import solve
from situc.parser import parse_text
from situc.resolve import resolve

#: What a generated module calls. `str`, `list` and `object` are corpus struct
#: names already, and stay here for that reason. `bool` is a scalar type in the
#: language, so no struct can take it.
CALLED = ("int", "len", "min", "max", "bytearray", "bytes", "range",
          "type", "any", "sum", "object", "list", "frozenset", "str")

SCHEMA = """target buffer;
endian big;

struct {name} {{
	u8 n;
	u8 payload[n];
}}

struct outer {{
	u8     count;
	{name} items[count];
	{name} ended[] until ")";
	u16    tail;
}}
"""

DRIVER = """
import sys
sys.path.insert(0, {here!r})
import unit
from situ_runtime import Message

data = bytearray([2, 1, 0x41, 2, 0x42, 0x43, 1, 0x44, 0x29, 0, 7])
view = unit.outer.at(Message(data), 0, len(data))
view.validate()
assert view.items_count == 2, view.items_count
assert bytes(view.items(1).payload) == b"BC", view.items(1).payload
assert view.ended_count == 1, view.ended_count
assert bytes(view.ended(0).payload) == b"D", view.ended(0).payload
assert view.tail == 7, view.tail
print("ok")
"""


def module(name: str) -> str:
	parsed = parse_text(SCHEMA.format(name=name), path="unit.situ")
	return generate(parsed, resolve(parsed, solve(parsed)), "unit").module


@pytest.mark.parametrize("name", CALLED)
def test_a_module_reads_a_message_through_a_struct_of_that_name(
		name: str, tmp_path: Path) -> None:
	(tmp_path / "unit.py").write_text(module(name), encoding="ascii")
	(tmp_path / "situ_runtime.py").write_text(
		(ROOT / "runtime" / "python" / "situ_runtime.py")
		.read_text(encoding="ascii"), encoding="ascii")
	(tmp_path / "driver.py").write_text(
		DRIVER.format(here=str(tmp_path)), encoding="ascii")
	ran = subprocess.run([sys.executable, str(tmp_path / "driver.py")],
	                     capture_output=True, text=True, cwd=tmp_path)
	assert ran.returncode == 0, ran.stderr
	assert ran.stdout.strip() == "ok"


def test_a_struct_is_still_constructed_as_the_struct() -> None:
	"""The rewrite must leave the view's own construction alone."""
	text = module("bytes")
	assert "_situ_bytes(self._msg.buffer" in text
	assert "= bytes(self._msg, " in text


def test_no_alias_where_no_struct_takes_a_builtin() -> None:
	"""Every other schema's module is byte-for-byte what it was."""
	assert "_situ_bytes" not in module("blob")
