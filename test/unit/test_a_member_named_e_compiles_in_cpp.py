"""A member named `e` compiles in C++ (26.806).

    struct pt { u8 x [max = 3]; }
    struct s  { u8 a; pt e; }

`validate` checks a nested member through
`if (const err e = e(_held); ...)`, and the local `e` is in scope from its
own initialiser, so the member's accessor was the local called as a
function: "'e' cannot be used as a function". A text number's check had
the same shape. Both now call through `this->`.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import fourway
from situc.codegen.cpp import generate as generate_cpp
from situc.diagnostics import Source
from situc.layout import solve
from situc.parser import parse
from situc.resolve import resolve

SCHEMA = """target buffer;
endian big;
struct pt { u8 x [max = 3]; }
struct s { u8 a; pt e; }
struct t { decimal u16 e until ":" [max = 9]; }
"""


@pytest.mark.skipif(fourway.HOST_CXX is None, reason="no C++ compiler")
def test_it_compiles(tmp_path: Path) -> None:
	schema = parse(Source("unit.situ", SCHEMA))
	header = tmp_path / "unit.hpp"
	header.write_text(generate_cpp(schema, resolve(schema, solve(schema)),
	                               "unit").header, encoding="ascii")
	assert fourway.HOST_CXX is not None
	built = subprocess.run(
		[fourway.HOST_CXX, "-std=c++17", "-fsyntax-only", "-Wall", "-Wextra",
		 "-Werror", f"-I{fourway.RUNTIME / 'c'}",
		 f"-I{fourway.RUNTIME / 'cpp'}", str(header)],
		capture_output=True, text=True)
	assert built.returncode == 0, built.stderr
