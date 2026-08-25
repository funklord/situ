"""`situc build --target python --single-file` (26.70).

The mode exists for programs whose shipped artifact is one file importing
nothing outside the standard library -- recovery tools, installers,
initramfs helpers, embedded agents. They are the programs least able to
hand-write a correct binary parser and, until this, the ones situ excluded
by packaging rather than by fit.

Three properties, and the first is the one the adopting project's CI
actually runs.
"""

from __future__ import annotations

import ast as python_ast
import importlib.util
import sys
from pathlib import Path

import pytest

from every_schema import ROOT, SCHEMAS, ids
from situc.codegen.c.vectors import parse_vectors
from situc.codegen.python import generate as generate_py
from situc.codegen.python import single
from situc.diagnostics import Source
from situc.layout import solve
from situc.parser import parse
from situc.resolve import resolve


def built(path: Path) -> tuple[str, str]:
	"""The ordinary module and the single-file one, for one schema."""
	source   = Source(str(path), path.read_text(encoding="ascii"))
	schema   = parse(source)
	resolved = resolve(schema, solve(schema))

	plain = generate_py(schema, resolved, path.stem).module
	return plain, single.inline(plain, path.stem)


def load(name: str, text: str, tmp: Path) -> object:
	"""Execute a module from text, in a directory of its own.

	Its own directory and its own name, for the reason invariant 101 gives:
	two versions of one module in one process is exactly what this test is,
	and sharing either lets the second import come back from the first.
	"""
	where = tmp / name
	where.mkdir()
	path = where / f"{name}.py"
	path.write_text(text, encoding="ascii")

	spec = importlib.util.spec_from_file_location(name, path)
	assert spec is not None and spec.loader is not None
	module = importlib.util.module_from_spec(spec)
	sys.modules[name] = module
	try:
		spec.loader.exec_module(module)
	finally:
		sys.modules.pop(name, None)
	return module


@pytest.mark.parametrize("path", SCHEMAS, ids=ids(SCHEMAS))
def test_it_imports_nothing_outside_the_standard_library(path: Path) -> None:
	"""The gate the adopting project runs, run here.

	Their CI walks the AST for imports and fails on anything outside
	`sys.stdlib_module_names`, so this is that check rather than an
	approximation of it. A single file that still needs a package installed
	has not solved the problem it was written for.
	"""
	_, one = built(path)

	found: set[str] = set()
	for node in python_ast.walk(python_ast.parse(one)):
		if isinstance(node, python_ast.Import):
			found |= {alias.name.split(".")[0] for alias in node.names}
		elif isinstance(node, python_ast.ImportFrom) and node.level == 0 \
				and node.module:
			found.add(node.module.split(".")[0])

	found.discard("__future__")
	outside = sorted(name for name in found
	                 if name not in sys.stdlib_module_names)

	assert not outside, f"{path.stem}: imports outside the stdlib: {outside}"
	assert "situ_runtime" not in found, (
		f"{path.stem}: still imports the runtime it was meant to inline")


@pytest.mark.parametrize("path", SCHEMAS, ids=ids(SCHEMAS))
def test_it_runs_and_answers_what_the_two_file_module_answers(
		path: Path, tmp_path: Path) -> None:
	"""The property that makes the trim safe.

	A dependency closure that dropped one helper too many produces a module
	that imports cleanly and raises `NameError` the first time a caller
	reaches the missing path -- which a test that only executes the module
	would not notice. So both modules read the same bytes and must give the
	same answers, including the same refusals.
	"""
	plain, one = built(path)

	# The two-file module needs its runtime importable; the single-file one
	# is the whole point and must not.
	beside = tmp_path / "beside"
	beside.mkdir()
	(beside / "situ_runtime.py").write_text(
		(ROOT / "runtime" / "python" / "situ_runtime.py").read_text(
			encoding="ascii"), encoding="ascii")

	sys.path.insert(0, str(beside))
	try:
		two = load("two", plain, tmp_path)
	finally:
		sys.path.remove(str(beside))

	alone = load("alone", one, tmp_path)

	# Everything the two-file module offers must be there, which is what
	# catches a closure that dropped a class rather than a helper. Not the
	# reverse: the single-file module also carries the runtime internals the
	# kept definitions reach -- `StaleViewError`, `EnumT` -- which the
	# two-file one never imported because it never named them.
	missing = _surface(two) - _surface(alone)
	assert not missing, (
		f"{path.stem}: the single-file module is missing {sorted(missing)}")

	corpus = path.with_suffix(".vectors")
	if not corpus.exists():
		return

	seen = 0
	for case in parse_vectors(Source(str(corpus),
	                                 corpus.read_text(encoding="ascii"))):
		if not hasattr(alone, case.struct):
			continue
		assert _answer(two, case.struct, case.data) \
			== _answer(alone, case.struct, case.data), (
			f"{path.stem}: `{case.name}` reads differently in the "
			f"single-file module")
		seen += 1

	assert seen, f"{path.stem}: no vector named a struct either module has"


def _surface(module: object) -> set[str]:
	return {name for name in dir(module) if not name.startswith("_")}


def _answer(module: object, struct: str, data: bytes) -> str:
	"""What one module makes of one vector: a value or a refusal."""
	held    = getattr(module, struct)
	message = getattr(module, "Message")

	try:
		fixed = getattr(held, "SIZE_BYTES", 0)
		view  = (held.at(message(bytearray(data)), 0) if fixed
		         else held.at(message(bytearray(data)), 0, len(data)))
		view.validate()
	except Exception as refused:                       # noqa: BLE001
		return f"{type(refused).__name__}"
	return "ok"


def test_the_trim_actually_trims() -> None:
	"""Inlining all 608 lines into a module that reaches forty of them is a
	different way of being unusable, so the closure has to drop something."""
	whole = len(single.runtime_source().split("\n"))

	path       = ROOT / "example" / "arp" / "arp.situ"
	plain, one = built(path)

	added = len(one.split("\n")) - len(plain.split("\n"))
	assert added < whole * 0.8, (
		f"the closure kept {added} of {whole} runtime lines for a schema "
		f"that reads six fields; it is not trimming")


def test_a_name_the_runtime_also_defines_is_refused(tmp_path: Path) -> None:
	"""`struct View` is a legal schema and an illegal thing to inline.

	One definition would shadow the other in a single file, and which one
	wins depends on the order they are emitted in -- so it is refused rather
	than resolved.
	"""
	schema = tmp_path / "clash.situ"
	schema.write_text("target buffer;\nendian big;\n\n"
	                  "struct View {\n\tu8  a;\n}\n", encoding="ascii")

	source   = Source(str(schema), schema.read_text(encoding="ascii"))
	parsed   = parse(source)
	resolved = resolve(parsed, solve(parsed))
	plain    = generate_py(parsed, resolved, "clash").module

	with pytest.raises(SystemExit) as raised:
		single.inline(plain, "clash")

	assert "View" in str(raised.value)
