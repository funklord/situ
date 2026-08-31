"""The Python version this repository claims, and what breaks it.

Two checks ask this question -- a static scan that always runs and a real
interpreter that runs where one is installed -- and a third asks it of
generated output. They share this module so the answer cannot differ between
them, which is the rule `every_schema.py` exists for one directory over.

WHICH MODULES ARE HELD TO THE FLOOR. `SHIPPED_MODULES`, and getting that list
wrong is a mistake this repository has already made three times in the
*other* gate. The Makefile records it: `mypy situc tools tests` read the
compiler and its suite, so "the module every generated module imports was the
one nothing checked"; `walker` was added "for the same reason"; and `editor`
was "named on arrival rather than after the same lesson a third time".

The floor check never got that widening. It read `bin/situc`, `situc/**` and
`tool/*` -- the same three trees, with the same three omissions -- so
`runtime/python`, `walker` and `editor` were shipped Python held to no
declared floor at all. Naming the list once is what stops a fourth.
"""

from __future__ import annotations

import ast
import io
import re
import tokenize
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def declared_floor() -> str:
	"""The oldest Python this compiler claims to run on, from the one place
	that states it as data: mypy's `python_version` in `pyproject.toml`."""
	text  = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
	found = re.search(r'^python_version\s*=\s*"(\d+\.\d+)"', text, re.M)
	assert found is not None, "pyproject.toml no longer declares python_version"
	return found.group(1)


def floor_version() -> tuple[int, ...]:
	return tuple(int(part) for part in declared_floor().split("."))


def shipped_modules() -> list[Path]:
	"""Every Python file this repository ships or runs, floor included.

	The three trees past `tools` are the ones the other gate had to learn
	one at a time; see the module docstring.
	"""
	return [ROOT / "bin" / "situc",
	        *sorted(ROOT.glob("situc/**/*.py")),
	        *sorted(ROOT.glob("tool/*.py")),
	        *sorted(ROOT.glob("runtime/python/**/*.py")),
	        *sorted(ROOT.glob("walker/**/*.py")),
	        *sorted(ROOT.glob("editor/**/*.py"))]


def floor_modules() -> list[Path]:
	"""Every Python the declared interpreter has to run, which is more than
	what ships.

	**CI installs the floor and runs `make check`**, so the suite executes at
	the declared version too -- and a construct the floor cannot tokenize
	fails there at *collection*, before one test runs, naming a file rather
	than a claim. Nothing held `test/` to the floor: `shipped_modules()`
	stops at `editor/`, and both gates read it.

	That is the fourth widening of a list of Python trees here and it has the
	cause the module docstring gives for the other three -- the list named
	what somebody was thinking about rather than what runs. It is the largest
	of the four: the suite is 83 modules against 102 shipped, so the gate that
	"never skips" was covering a little over half of what the floor has to
	parse, on a machine with no floor interpreter to catch the rest.

	Kept separate from `shipped_modules()` rather than folded into it,
	because the two answer different questions: what a user installs, and
	what the declared interpreter must be able to read.
	"""
	return [*shipped_modules(), *sorted(ROOT.glob("test/**/*.py"))]


#: The f-string token types 3.12 introduced, resolved by name because 3.11
#: does not have them -- and mypy runs at the declared floor, so naming them
#: directly is an error there. `None` means the interpreter running the tests
#: is 3.11 itself, where the tokenizer cannot produce a PEP 701 construct.
FSTRING_START  = getattr(tokenize, "FSTRING_START", None)
FSTRING_MIDDLE = getattr(tokenize, "FSTRING_MIDDLE", None)
FSTRING_END    = getattr(tokenize, "FSTRING_END", None)


def pep_701(source: str) -> list[tuple[int, str]]:
	"""Every f-string in `source` that only 3.12 and later can parse.

	`ast.parse(feature_version=...)` cannot answer this: PEP 701 is a
	tokenizer change and the flag does not reach the tokenizer -- confirmed
	rather than taken on trust, since it accepts both a same-quote nesting
	and a split expression without complaint.
	"""
	found: list[tuple[int, str]] = []
	stack: list[tuple[str, int, bool]] = []

	for token in tokenize.generate_tokens(io.StringIO(source).readline):
		if token.type == FSTRING_START:
			triple = token.string.endswith(("'''", '"""'))
			quote  = token.string[-3:] if triple else token.string[-1]
			stack.append((quote, token.start[0], triple))
			continue

		if token.type == FSTRING_END:
			quote, line, triple = stack.pop()
			# A triple-quoted f-string spans lines in 3.11 too, so only a
			# single-quoted one ending on another line says the *expression*
			# was broken across them.
			if not triple and token.end[0] != line:
				found.append((line, "an f-string expression split across lines"))
			continue

		if not stack:
			continue

		if token.type == tokenize.STRING and any(
				quote[0] in token.string for quote, _, _ in stack):
			found.append((token.start[0],
			              "a string reusing its f-string's quote character"))

		# The *literal* part of an f-string may carry a backslash in 3.11 and
		# the expression part may not. `FSTRING_MIDDLE` is the literal part,
		# so excluding it is what keeps a backslash escape in ordinary text
		# from being reported.
		if token.type != FSTRING_MIDDLE and "\\" in token.string:
			found.append((token.start[0],
			              "a backslash inside an f-string expression"))

	return found


def below_floor(source: str) -> list[tuple[int, str]]:
	"""Everything in `source` the declared floor cannot parse.

	Two instruments, because each is blind where the other sees. `ast.parse`
	with `feature_version` catches grammar added since the floor -- PEP 695
	type parameters, say -- and misses everything tokenizer-level; `pep_701`
	catches exactly the tokenizer-level f-string changes and nothing else.
	The C++ work made the same point one language over: a check that inspects
	one thing answers for that thing only.
	"""
	try:
		ast.parse(source, feature_version=floor_version()[:2])   # type: ignore[arg-type]
	except SyntaxError as caught:
		return [(caught.lineno or 0, caught.msg)]
	return [] if FSTRING_START is None else pep_701(source)
