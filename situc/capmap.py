"""Render a solved schema as the capability map.

The map is meant to be committed and diffed (project.md section 18.1), so the
format is optimised for that over prettiness. Two consequences:

- One entry per line, no nesting. A nested field appears as a full path.
- The path column is padded to a fixed width rather than to the longest name in
  the file. Fitting the column to the content would reflow every line of a
  block whenever a long name is added, burying the real change.

The committed map doubles as the protocol documentation that projects always
want and never have, which is the other reason it is readable text.
"""

from __future__ import annotations

from pathlib import PurePath

from situc import ast
from situc.capability import Vector, derive
from situc.layout import BITS_PER_BYTE, SchemaLayout, StructLayout

FORMAT_VERSION = 0

# Phase 2 resolves these five (section 26.2). The rest arrive with the lattice.
AXES = ("offset", "size", "align", "repr", "atomic")

PATH_WIDTH = 38


def render(schema: ast.Schema, layout: SchemaLayout, path: str) -> str:
	# Only the file name is recorded. A map committed beside its schema must be
	# byte-identical however it was generated, and an invocation path would make
	# it depend on the caller's working directory.
	lines = [
		f"# situ capability map v{FORMAT_VERSION}",
		f"# schema: {PurePath(path).name}",
		f"# axes:   {' '.join(AXES)}",
		"#",
		"# Offsets are byte values, or byte:bit where a field does not start on",
		"# a byte boundary. Sizes are bytes unless suffixed with `bit`.",
	]

	aggregates = _aggregate_paths(schema, layout)

	for name in sorted(layout.structs):
		lines.append("")
		lines.extend(_struct(layout.structs[name], aggregates))

	return "\n".join(lines) + "\n"


def _struct(layout: StructLayout, aggregates: set[str]) -> list[str]:
	size  = (f"{layout.size_bytes}" if layout.is_byte_sized
	         else f"{layout.size_bits}bit")
	lines = [f"struct {layout.name} size={size}"]

	# Members are derived first so an aggregate can be given the vectors it has
	# to meet over. Nested paths are the ones prefixed with the aggregate's own
	# path, which the solver has already flattened into this list.
	vectors = {
		placement.path: derive(placement)
		for placement in layout.placements
		if placement.path not in aggregates
	}

	for placement in layout.placements:
		if placement.path in aggregates:
			members = [
				vector for path, vector in vectors.items()
				if path.startswith(placement.path + ".")
			]
			vector = derive(placement, members)
		else:
			vector = vectors[placement.path]

		lines.append(_entry(placement.path, vector))

	return lines


def _entry(path: str, vector: Vector) -> str:
	axes = " ".join(f"{name}={value}" for name, value in vector.items())
	return f"  {path.ljust(PATH_WIDTH)} {axes}".rstrip()


def _aggregate_paths(schema: ast.Schema, layout: SchemaLayout) -> set[str]:
	"""Paths whose type is a struct rather than a scalar or enum.

	Their vector is the meet of their members', so they claim no representation
	or atomicity of their own.
	"""
	structs = {decl.name for decl in schema.structs()}
	return {
		placement.path
		for struct in layout.structs.values()
		for placement in struct.placements
		if placement.type_name in structs
	}


def summary(layout: SchemaLayout) -> str:
	"""A one-line-per-struct digest, for humans rather than for diffing."""
	lines = []
	for name in sorted(layout.structs):
		struct = layout.structs[name]
		if struct.is_byte_sized:
			lines.append(f"{name}: {struct.size_bytes} bytes, "
			             f"{len(struct.placements)} entries")
		else:
			lines.append(f"{name}: {struct.size_bits} bits "
			             f"({struct.size_bits % BITS_PER_BYTE} short of a byte), "
			             f"{len(struct.placements)} entries")
	return "\n".join(lines) + "\n"
