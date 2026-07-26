"""Render a solved schema as the capability map.

The map is meant to be committed and diffed (project.md section 18.1), so the
format is optimised for that over prettiness. Two consequences:

- One entry per line, no nesting. A nested field appears as a full path.
- The path column is padded to a fixed width rather than to the longest name in
  the file. Fitting the column to the content would reflow every line of a
  block whenever a long name is added, burying the real change.

Core axes are always present so a diff of one line reads on its own. Any other
axis appears only when it has been weakened, which is exactly when a reader
needs to see it -- and a field gaining a weakening shows up as a line gaining a
token rather than as a reflowed block.

The committed map doubles as the protocol documentation that projects always
want and never have, which is the other reason it is readable text.
"""

from __future__ import annotations

from pathlib import PurePath

from situc import ast
from situc.capability import DOMAINS, Axis, Value, Vector
from situc.resolve import ResolvedSchema

FORMAT_VERSION = 1

# Always shown: these describe where the bytes are, which is the question the
# map exists to answer.
CORE_AXES = (Axis.OFFSET, Axis.SIZE, Axis.ALIGN, Axis.REPR, Axis.ATOMIC)

PATH_WIDTH = 38


def render(schema: ast.Schema, resolved: ResolvedSchema, path: str) -> str:
	# Only the file name is recorded. A map committed beside its schema must be
	# byte-identical however it was generated, and an invocation path would make
	# it depend on the caller's working directory.
	lines = [
		f"# situ capability map v{FORMAT_VERSION}",
		f"# schema: {PurePath(path).name}",
		f"# core:   {' '.join(axis.value for axis in CORE_AXES)}",
		"#",
		"# Core axes are always shown. Any other axis appears only where it has",
		"# been weakened below its strongest value.",
		"#",
		"# Offsets are byte values, or byte:bit where a field does not start on",
		"# a byte boundary. Sizes are bytes unless suffixed with `bit`.",
	]

	for name in sorted(resolved.structs):
		lines.append("")
		lines.extend(_struct(name, resolved))

	return "\n".join(lines) + "\n"


def _struct(name: str, resolved: ResolvedSchema) -> list[str]:
	struct  = resolved.structs[name]
	layout  = struct.layout
	size    = f"{layout.size_bytes}" if layout.is_byte_sized else f"{layout.size_bits}bit"

	lines = [f"struct {name} size={size} {_axes(struct.vector, core=False)}".rstrip()]
	for entry in struct.entries:
		lines.append(_entry(entry.placement.path, entry.vector))
	return lines


def _entry(path: str, vector: Vector) -> str:
	return f"  {path.ljust(PATH_WIDTH)} {_axes(vector)}".rstrip()


def _axes(vector: Vector, core: bool = True) -> str:
	"""Core axes in their declared order, then any other weakened axis.

	The order is fixed rather than taken from the Axis enum, so a line reads the
	same way every time and a diff stays local to what actually changed.
	"""
	shown: list[tuple[Axis, Value]] = []

	if core:
		shown.extend((axis, vector.get(axis)) for axis in CORE_AXES)

	for axis, value in vector.items():
		if axis in CORE_AXES and core:
			continue
		if value.base != DOMAINS[axis][0]:
			shown.append((axis, value))

	return " ".join(f"{axis.value}={value.render()}" for axis, value in shown)


def summary(resolved: ResolvedSchema) -> str:
	"""A one-line-per-struct digest, for humans rather than for diffing."""
	lines = []
	for name in sorted(resolved.structs):
		struct = resolved.structs[name]
		layout = struct.layout
		size   = (f"{layout.size_bytes} bytes" if layout.is_byte_sized
		          else f"{layout.size_bits} bits")
		weakened = sorted({
			axis.value
			for entry in struct.entries
			for axis, value in entry.vector.items()
			if value.base != DOMAINS[axis][0]
		})
		note = f"; weakened: {', '.join(weakened)}" if weakened else ""
		lines.append(f"{name}: {size}, {len(struct.entries)} entries{note}")

	return "\n".join(lines) + "\n"
