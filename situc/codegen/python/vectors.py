"""Golden-vector tests over the Python accessors (sections 1 and 22).

26.829 found that section 1's fourth artifact reached one of its four
languages and built the Rust half. This is the Python half, and it is the
one that matters most of the four: **Python has no compiler.** A generated
C, C++ or Rust accessor that names a field wrongly, or builds an expression
that does not typecheck, fails at the build. The Python one fails at the
call -- so for a Python consumer a golden-vector suite is not a second
opinion, it is the first time the generated code runs at all.

`unittest` rather than pytest, because section 24 requires `situc` to run
from a bare interpreter with no third-party packages and a suite it hands a
user should ask no more than the compiler does:

    python3 -m unittest <name>_vectors

**One vector file, one parser, one validator**, for the reason
`rust/vectors.py` gives: `parse_vectors` and `_check` are the C backend's
and are imported, because the vector file is language-independent and two
answers to "is this vector well formed" is the shape this tree keeps paying
for. `py_name` is the Python backend's own, so the class this calls is the
class that backend emitted.

Per-field expectations are read and schema-checked and not yet compared,
exactly as in the Rust half, and the generated module says so with the
count. What each case asserts is that the bytes take a view and that
validation accepts them.
"""

from __future__ import annotations

import re

from situc import __version__, ast
from situc.codegen import refuse_parameters
from situc.codegen.c.vectors import Case, _byte_run, _check, _nested_of, _symbol
from situc.codegen.python.emit import py_name
from situc.resolve import ResolvedSchema

#: An expectation's value, where it is an integer Python can read.
#:
#: The trailing `[uUlL]*` is a C integer suffix and is stripped rather than
#: refused: `0xDEADBEEFu` appears in `test/schema/header.vectors` and is the
#: same number without it. Allowing it moved the comparable share of the
#: corpus from 140 expectations to 142, which is small and is also two
#: expectations that would otherwise have been silently uncompared.
INT_LITERAL = re.compile(r"(0[xX][0-9a-fA-F]+|\d+)[uUlL]*\Z")


def generate(schema: ast.Schema, resolved: ResolvedSchema, cases: list[Case],
		basename: str, prefix: str = "situ") -> str:
	"""A `unittest` module for `basename.py`, from `basename`'s vectors.

	`prefix` is accepted and unused, for the reason the Rust emitter gives:
	Python names accessors by class and attribute, so there is nothing for a
	prefix to disambiguate, and one signature across the backends is what
	lets `cmd_gen_tests` dispatch without a special case.
	"""
	refuse_parameters(schema)

	seen: dict[str, Case] = {}
	for case in cases:
		symbol = _symbol(case)
		if symbol in seen:
			raise ValueError(
				f"vector `{case.struct} {case.name}` is declared twice")
		seen[symbol] = case

	for case in cases:
		_check(resolved, case)

	compared, declined = _tally(resolved, cases)
	lines = _preamble(basename, compared, declined)

	for case in cases:
		lines.extend(_case_body(basename, resolved, case))

	lines.extend([
		"",
		'if __name__ == "__main__":',
		"\tunittest.main()",
	])
	return "\n".join(lines) + "\n"


def _comparable(resolved: ResolvedSchema, case: Case,
		path: str, value: str) -> str | None:
	"""The Python literal to compare this expectation against, or None.

	Measured over the corpus: 142 of 197 expectations are a plain scalar
	against an integer, 22 name a field inside a nested struct, 11 are a
	byte run, 4 are a text number whose getter is fallible, and 18 give a C
	enum constant -- `SITU_LEAP_INDICATOR_NONE` -- which is not the Python
	enum's spelling and cannot be spliced.

	The four it declines are declined for four different reasons and each
	is its own piece of work. What makes declining safe is that the module
	says how many and why: a suite that compares 72 per cent of its input
	and does not say so reads as coverage of all of it.
	"""
	if _nested_of(resolved, case.struct, path) is not None:
		return None
	if _byte_run(resolved, case.struct, path) is not None:
		return None

	found = resolved.find(f"{case.struct}.{path}")
	if found is not None and found.placement.radix is not None:
		return None

	match = INT_LITERAL.match(value)
	return match.group(1) if match else None


def _reason(resolved: ResolvedSchema, case: Case,
		path: str, value: str) -> str:
	"""Why one expectation is not compared, in the words a reader needs."""
	if _nested_of(resolved, case.struct, path) is not None:
		return "a field inside a nested struct, which reads through its own view"
	if _byte_run(resolved, case.struct, path) is not None:
		return "a byte run, which compares as bytes rather than as a number"

	found = resolved.find(f"{case.struct}.{path}")
	if found is not None and found.placement.radix is not None:
		return "a text number, whose getter is fallible"
	return "a value that is not an integer literal, such as a C enum constant"


def _tally(resolved: ResolvedSchema,
		cases: list[Case]) -> tuple[int, dict[str, int]]:
	compared = 0
	declined: dict[str, int] = {}
	for case in cases:
		for path, value in case.expectations:
			if _comparable(resolved, case, path, value) is not None:
				compared += 1
			else:
				why = _reason(resolved, case, path, value)
				declined[why] = declined.get(why, 0) + 1
	return compared, declined


def _preamble(basename: str, compared: int,
		declined: dict[str, int]) -> list[str]:
	lines = [
		f'"""Generated by situc {__version__} from {basename}.situ and its'
		f' vectors -- do not edit.',
		"",
		"Golden-vector tests over the Python accessors, read from the same",
		"vector file every other `--target` reads. One file, so the backends",
		"cannot be asked different questions about one schema.",
		"",
		"Run it with the generated module and the runtime importable:",
		"",
		f"    python3 -m unittest {basename}_vectors",
		"",
		"Each case asserts that the bytes take a view and that validation",
		"accepts them. In Python that is more than it sounds: there is no",
		"compiler here, so this is the first time the generated accessors",
		"run at all.",
	]

	if compared:
		lines.extend([
			"",
			f"{compared} field expectation(s) are compared: a plain scalar",
			"against an integer, read through the generated property.",
		])

	if declined:
		lines.extend([
			"",
			f"{sum(declined.values())} are NOT compared, and",
			"`situc gen-tests --target c` compares them:",
			"",
		])
		for why, count in sorted(declined.items()):
			lines.append(f"  {count} -- {why}")
		lines.extend([
			"",
			"Every one of them is still read and checked against the schema,",
			"so a vectors file naming a field this schema does not have is",
			"refused here too.",
		])

	lines.extend([
		'"""',
		"",
		"from __future__ import annotations",
		"",
		"import unittest",
		"",
		"import situ_runtime",
		"",
		f"import {basename}",
		"",
		"",
		"class Vectors(unittest.TestCase):",
	])
	return lines


def _case_body(basename: str, resolved: ResolvedSchema,
		case: Case) -> list[str]:
	name = _symbol(case)

	# A variable struct's `at` takes the message length and a fixed one's
	# does not, which is the Python backend's own branch on
	# `layout.is_fixed_size` and not a second reading of it. Getting this
	# wrong is 26.36 in another language: the first version called
	# `at(msg, 0)` for every struct and 13 of the corpus's 27 vector files
	# raised `TypeError: at() missing 1 required positional argument`,
	# which is what a suite for a language with no compiler is for.
	struct = resolved.structs[case.struct]
	extent = "" if struct.layout.is_fixed_size else ", len(raw)"

	return [
		f"\tdef test_{name}(self) -> None:",
		f"\t\traw = bytearray(bytes.fromhex(\"{case.data.hex()}\"))",
		"\t\tmsg = situ_runtime.Message(raw)",
		"",
		# `SituError` is the runtime's own base, so one except clause covers
		# bounds, constraint, version and checksum alike. Named rather than
		# bare, because a bare `except` would swallow the `self.fail` an
		# inner clause raises and report the wrong thing.
		"\t\ttry:",
		f"\t\t\tview = {basename}.{py_name(case.struct)}.at(msg, 0{extent})",
		"\t\texcept situ_runtime.SituError as exc:",
		f"\t\t\tself.fail(f\"`{case.name}` must take a view: {{exc}}\")",
		"",
		"\t\ttry:",
		"\t\t\tview.validate()",
		"\t\texcept situ_runtime.SituError as exc:",
		f"\t\t\tself.fail(f\"`{case.name}` must validate: {{exc}}\")",
		*_expectations(resolved, case),
		"",
	]


def _expectations(resolved: ResolvedSchema, case: Case) -> list[str]:
	"""What the vector says about each field, where this target can say it."""
	lines: list[str] = []
	for path, value in case.expectations:
		literal = _comparable(resolved, case, path, value)
		if literal is None:
			continue
		if not lines:
			lines.append("")
		lines.append(
			f"\t\tself.assertEqual(view.{py_name(path)}, {literal},"
			f" \"{path}\")")
	return lines
