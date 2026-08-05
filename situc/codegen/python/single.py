"""Fold the runtime into the generated module, for one-file programs.

The Python backend emits a module that imports `situ_runtime`, which is the
right shape for anything installable and the wrong shape for a program whose
shipped artifact is **one file importing nothing outside the standard
library**. That constraint is not tidiness where it appears: a project
reporting it keeps a dpkg backend for embedded boxes with no `apt-get`, where
the deploy story is one `scp` and the repair story is a text editor on the
target, and its CI walks the AST for imports and fails on anything outside
`sys.stdlib_module_names`.

Two files fail that gate, and the second is a hard import rather than
something a reader can vendor by copy-paste without thought. So this inlines
the runtime -- and only the parts the schema actually reaches, because
copying all 608 lines into a module that uses forty of them is a different
way of being unusable.

WHAT MAKES THE TRIM SAFE. The runtime is a flat module: top-level classes,
functions and constants, and nothing conditional. So the reachable set is a
closure over "which top-level names does this definition mention", started
from the names the generated module imports. A definition is kept whole or
not at all; nothing is rewritten.

WHAT IT REFUSES. A schema whose own top-level names collide with a runtime
name gets a diagnostic rather than a module where one silently shadows the
other. `struct View` is a legal schema and an illegal thing to inline beside
`class View`.
"""

from __future__ import annotations

import ast as python_ast
import re
from pathlib import Path

#: Where the runtime lives, checked in both layouts for the reason
#: `situc/verify.py` checks two: this has to work from an installed situc as
#: well as from the source tree.
RUNTIME_PLACES = (
	Path(__file__).resolve().parents[2] / "_runtime" / "situ_runtime.py",
	Path(__file__).resolve().parents[3] / "runtime" / "python" / "situ_runtime.py",
)


def runtime_source() -> str:
	for candidate in RUNTIME_PLACES:
		if candidate.is_file():
			return candidate.read_text(encoding="ascii")

	raise SystemExit("situc: cannot find situ_runtime.py to inline")


def _imported(module: str) -> list[str]:
	"""The runtime names the generated module asks for."""
	found = re.search(r"^from situ_runtime import \(\n(.*?)^\)$",
	                  module, re.M | re.S)
	if found is None:
		return []

	names: list[str] = []
	for line in found.group(1).splitlines():
		names.extend(part.strip() for part in line.split(",") if part.strip())
	return names


def _defines(node: python_ast.stmt) -> list[str]:
	"""The top-level names one statement introduces."""
	if isinstance(node, (python_ast.ClassDef, python_ast.FunctionDef)):
		return [node.name]
	if isinstance(node, python_ast.Assign):
		return [t.id for t in node.targets if isinstance(t, python_ast.Name)]
	if isinstance(node, python_ast.AnnAssign) \
			and isinstance(node.target, python_ast.Name):
		return [node.target.id]
	return []


def _mentions(node: python_ast.stmt) -> set[str]:
	"""Every bare name a statement mentions, wherever it appears.

	Deliberately crude. A name used only in an annotation still counts,
	because `from __future__ import annotations` does not stop somebody
	calling `typing.get_type_hints` later, and a trimmer that drops a class
	because it is only ever a type is a trimmer that breaks at a distance.
	"""
	return {inner.id for inner in python_ast.walk(node)
	        if isinstance(inner, python_ast.Name)}


def _block(lines: list[str], node: python_ast.stmt) -> str:
	"""One definition's source, with the comment block above it.

	The comments are the point of taking source text rather than unparsing:
	this runtime explains why each helper bounds what it bounds, and an
	inlined copy that dropped the reasoning would be worse to read than the
	import it replaced.
	"""
	start = node.lineno - 1
	for held in getattr(node, "decorator_list", []):
		start = min(start, held.lineno - 1)

	while start > 0 and lines[start - 1].lstrip().startswith("#"):
		start -= 1

	assert node.end_lineno is not None
	return "\n".join(lines[start:node.end_lineno])


def inline(module: str, basename: str) -> str:
	"""The generated module with the runtime folded in.

	Only the reachable part, and the imports merged so the result is one file
	whose every import is a standard-library one.
	"""
	runtime = runtime_source()
	lines   = runtime.split("\n")
	tree    = python_ast.parse(runtime)

	provides: dict[str, python_ast.stmt] = {}
	for node in tree.body:
		for name in _defines(node):
			provides[name] = node

	wanted = _imported(module)
	if not wanted:
		return module

	missing = [name for name in wanted if name not in provides]
	if missing:
		raise SystemExit(
			f"situc: the generated module imports {missing} from the runtime "
			f"and the runtime does not define them; --single-file cannot "
			f"inline what it cannot find")

	# The closure. A definition drags in whatever it mentions, and the
	# runtime is flat, so this terminates on the definitions it has.
	keep: set[str] = set()
	queue          = list(wanted)
	while queue:
		name = queue.pop()
		if name in keep or name not in provides:
			continue
		keep.add(name)
		queue.extend(_mentions(provides[name]) - keep)

	clash = sorted(keep & _top_level_names(module))
	if clash:
		raise SystemExit(
			f"situc: this schema defines {clash}, which the runtime also "
			f"defines. One would shadow the other in a single file; rename "
			f"the struct or drop --single-file.")

	body = [_block(lines, node) for node in tree.body
	        if any(name in keep for name in _defines(node))]

	return _assemble(module, runtime, body, basename)


def _top_level_names(module: str) -> set[str]:
	found: set[str] = set()
	for node in python_ast.parse(module).body:
		found.update(_defines(node))
	return found


def _imports(source: str) -> list[str]:
	"""The plain import lines of a module, `__future__` excluded."""
	found = []
	for node in python_ast.parse(source).body:
		if isinstance(node, python_ast.ImportFrom) \
				and node.module == "__future__":
			continue
		if isinstance(node, (python_ast.Import, python_ast.ImportFrom)):
			if isinstance(node, python_ast.ImportFrom) \
					and node.module == "situ_runtime":
				continue
			found.append(python_ast.unparse(node))
	return found


def _assemble(module: str, runtime: str, body: list[str],
		basename: str) -> str:
	"""One file: header, merged imports, the runtime's reachable part, then
	the generated module with its import of the runtime removed."""
	rest = re.sub(r"^from situ_runtime import \(\n.*?^\)$\n", "",
	              module, count=1, flags=re.M | re.S)
	rest = "\n".join(line for line in rest.split("\n")
	                 if not _is_import(line))

	merged = sorted(set(_imports(module)) | set(_imports(runtime)))

	return "\n".join([
		f'"""Generated by situc from {basename}.situ -- do not edit.',
		"",
		"One file, importing nothing outside the standard library. The parts",
		"of situ's Python runtime this schema reaches are inlined below,",
		"with their own comments, followed by the accessors.",
		"",
		"Regenerate rather than edit:",
		"",
		f"    situc build {basename}.situ --target python --single-file",
		'"""',
		"",
		"from __future__ import annotations",
		"",
		*merged,
		"",
		"",
		"# -- situ runtime, inlined "
		+ "-" * 52,
		"",
		*[one + "\n" for one in body],
		"# -- generated accessors "
		+ "-" * 54,
		"",
		rest.lstrip("\n"),
	])


def _is_import(line: str) -> bool:
	stripped = line.strip()
	return stripped.startswith("import ") or (
		stripped.startswith("from ") and " import " in stripped)
