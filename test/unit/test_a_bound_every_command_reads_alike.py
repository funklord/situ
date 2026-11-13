"""A bound is refused by every command or by none (26.983).

The four backends refused a bound they could not render from `_attr_checks`,
in the emitters, so `map`, `wire`, `pack` and the dissector published
artifacts for `[max = remaining]` where `build` exited 1 -- the class 26.233
closed for a string and `invariant.OPERATORS` for `%`, a shape at a time.
The solver refuses the whole set now, so `solve` alone is enough to ask.

And `[max = size(v)]`, the sibling named bare as a bound names one, was the
other way round: refused by the four and read by the walkers.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import fourway
from situc.diagnostics import SituError
from situc.layout import solve
from situc.parser import parse_text
from test_walker_c import (COMPILER, _inline_image, c_verdict, python_verdict,
                           shape_named_text)

def schema(bound: str) -> str:
	return ("target buffer;\nendian big;\nconst K = 4;\n"
	        "struct s {\n\tu8 n;\n\tu8 lens[2];\n\tu8 v[n];\n"
	        f"\tu8 z [max = {bound}];\n}}\n")


@pytest.mark.parametrize("bound", ["remaining", "popcount(n)", "lens[0]",
                                   "n == 1", "min(n, 3)"])
def test_the_solver_refuses_what_no_backend_renders(bound: str) -> None:
	with pytest.raises(SituError) as caught:
		solve(parse_text(schema(bound), path = "s.situ"))
	assert "which no reader compares against" in \
		caught.value.diagnostic.render()


@pytest.mark.parametrize("bound", ["n", "n + 1", "size(s.v)", "size(v)",
                                   "count(v)", "K * 2 + 1"])
def test_the_solver_takes_what_every_backend_renders(bound: str) -> None:
	solve(parse_text(schema(bound), path = "s.situ"))


#: `z` against the bare `size(v)`: two bytes of `v`, then 2, then 3.
MESSAGES = {"at": (bytes([2, 0, 0, 9, 9, 2]), "0"),
            "past": (bytes([2, 0, 0, 9, 9, 3]), "2")}


@pytest.mark.skipif(not fourway.COMPLETE, reason = "needs gcc, g++ and rustc")
def test_all_four_read_a_bare_size(tmp_path: Path) -> None:
	source = tmp_path / "unit.situ"
	source.write_text(schema("size(v)"), encoding = "ascii")
	command = fourway.build(tmp_path, source)
	assert len(command) == 4
	for label, (data, verdict) in MESSAGES.items():
		for backend, argv in command.items():
			answer  = fourway.answers(argv, data, tmp_path)
			section = answer[answer.index("-- s\n"):].split("\n-- ")[0]
			line = next(one for one in section.splitlines()
			            if one.startswith("validate "))
			assert line.split()[1] == verdict, (label, backend, answer)


@pytest.mark.skipif(COMPILER is None, reason = "needs a C compiler")
def test_both_walkers_read_a_bare_size(tmp_path: Path) -> None:
	text  = schema("size(v)")
	image = _inline_image(text)
	shape = shape_named_text(text, "s")
	for label, (data, verdict) in MESSAGES.items():
		assert python_verdict(image, data, shape) == verdict, label
		assert c_verdict(tmp_path, image, data, shape) == verdict, label


@pytest.mark.skipif(not fourway.COMPLETE, reason = "needs rustc")
@pytest.mark.parametrize("bound", ["size(v)", "count(v)", "size(s.v) + n"])
def test_rust_compares_a_layout_fact_without_a_warning(tmp_path: Path,
		bound: str) -> None:
	"""`size` answers `usize` and a bound compares `i64`: it did not compile."""
	from test_codegen_rust import build
	built = build(tmp_path, schema(bound), preamble = "")
	assert built.returncode == 0, built.stderr
