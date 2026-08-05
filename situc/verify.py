"""Check that real bytes conform to a schema, generating nothing.

Situ's usual bargain is that the schema becomes the code. A project that
cannot take that bargain -- because its callers hold owned structs rather
than views, or because its build may not gain a code generator -- can still
take a smaller one: **the schema as a specification**, checked in CI against
bytes the existing hand-written implementation produced.

That is what this is for. `situc wire --check` and `situc map --check`
already hold a schema to its own committed contracts, which catches an edit
that moves a field. Neither of them looks at a single real byte, so neither
notices the case that matters more: the schema and the implementation
disagreeing from the day the schema was written.

The corpus is the arbiter. Bytes the other implementation emitted are the
only thing in the arrangement that is not somebody's opinion about the
format -- which is the same argument `tests/unit/oracles.py` makes, one layer
out and available to a project that generates nothing.

Nothing is written to disk and nothing is compiled. The Python accessors are
built in memory and executed there, so an adopter's build gains a check and
no artifacts.
"""

from __future__ import annotations

import sys
import types
from dataclasses import dataclass
from pathlib import Path

from situc import ast
from situc.codegen import python as generate_py
from situc.codegen.c.vectors import Case, parse_vectors
from situc.diagnostics import Source
from situc.resolve import ResolvedSchema


@dataclass
class Outcome:
	"""One vector, and what the schema made of it."""

	case: Case
	#: None where the bytes conformed; otherwise what refused them.
	refusal: str | None = None

	@property
	def ok(self) -> bool:
		return self.refusal is None


def runtime_source() -> str:
	"""The Python runtime the generated module imports.

	Beside the package when situ is installed, and up two directories in the
	source tree. Both are checked rather than one being assumed, because the
	whole point of this command is that it runs where a build system does not.
	"""
	here = Path(__file__).resolve().parent
	for candidate in (here / "_runtime" / "situ_runtime.py",
	                  here.parent / "runtime" / "python" / "situ_runtime.py"):
		if candidate.is_file():
			return candidate.read_text(encoding="ascii")

	raise SystemExit(
		"situc: cannot find situ_runtime.py, which `verify` needs to read "
		"bytes without generating code")


def _module(schema: ast.Schema, resolved: ResolvedSchema, name: str) -> object:
	"""The accessors, built and executed in memory.

	`exec` rather than a temporary file and an import: this command exists so
	that a project can check a corpus without its build acquiring a code
	generator, and writing generated Python into somebody's tree -- even a
	temporary directory -- is the thing it is avoiding.
	"""
	runtime = types.ModuleType("situ_runtime")
	exec(runtime_source(), runtime.__dict__)           # noqa: S102
	sys.modules["situ_runtime"] = runtime

	try:
		built = types.ModuleType(name)
		exec(generate_py.generate(schema, resolved, name).module,   # noqa: S102
		     built.__dict__)
		return built
	finally:
		del sys.modules["situ_runtime"]


def check(schema: ast.Schema, resolved: ResolvedSchema, name: str,
		vectors: Source) -> list[Outcome]:
	"""Every vector in `vectors`, against the schema.

	A vector conforms when the schema can acquire a view over exactly its
	bytes and `validate` accepts them. Both halves matter and they fail
	differently: a view that cannot be acquired means the bytes are the wrong
	length for the layout, and a validator that refuses means they are the
	right length and say something the schema forbids.
	"""
	module   = _module(schema, resolved, name)
	found: list[Outcome] = []

	for case in parse_vectors(vectors):
		held = getattr(module, case.struct, None)
		if held is None:
			found.append(Outcome(case, f"no struct `{case.struct}` in this schema"))
			continue

		found.append(Outcome(case, _refusal(module, held, case.data)))

	return found


def _refusal(module: object, held: object, data: bytes) -> str | None:
	"""What stopped these bytes, or None."""
	message = getattr(module, "Message")

	try:
		# A fixed-size struct's `at` takes no length: its extent is the
		# constant, and offering one is a TypeError rather than a check.
		fixed = getattr(held, "SIZE_BYTES", 0)
		view  = (held.at(message(bytearray(data)), 0)   # type: ignore[attr-defined]
		         if fixed else
		         held.at(message(bytearray(data)), 0, len(data)))  # type: ignore[attr-defined]
		view.validate()
	except Exception as refused:                        # noqa: BLE001
		return f"{type(refused).__name__}: {refused}"

	return None


def render(found: list[Outcome], schema_path: str, vectors_path: str) -> str:
	"""The report, in the shape section 17 asks diagnostics to take."""
	failed = [one for one in found if not one.ok]

	lines = []
	for one in failed:
		lines.append(f"error: {one.case.struct} `{one.case.name}` does not "
		             f"conform")
		lines.append(f"   --> {vectors_path}:{one.case.line}")
		lines.append(f"    = {one.refusal}")
		lines.append(f"    = {len(one.case.data)} bytes, from an implementation "
		             f"that is not this schema")
		lines.append("")

	if not found:
		lines.append(f"situc: {vectors_path} holds no vectors")
	elif failed:
		lines.append(f"situc: {len(failed)} of {len(found)} vectors do not "
		             f"conform to {schema_path}")
	else:
		lines.append(f"situc: {len(found)} vectors conform to {schema_path}")

	return "\n".join(lines) + "\n"
