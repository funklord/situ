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

from situc import ast, layers
from situc.capability import DOMAINS, Axis, Value, Vector
from situc.layout import BITS_PER_BYTE, StructLayout
from situc.resolve import ResolvedSchema

FORMAT_VERSION = 1

# Always shown: these describe where the bytes are, which is the question the
# map exists to answer.
CORE_AXES = (Axis.OFFSET, Axis.SIZE, Axis.ALIGN, Axis.REPR, Axis.ATOMIC)

PATH_WIDTH = 38


def _layer_lines(schema: ast.Schema) -> list[str]:
	"""The two layer scalars, and only where they rise above `view` (0032).

	Silent at the default, which is this file's own rule for every axis: a
	value at its strongest says nothing, so a map committed before the ladder
	existed is byte-identical to one generated after it.

	They are here rather than per struct because they are facts about the
	whole schema, and they are in the map rather than a new artifact because
	the map is what `--check` already holds to its committed copy. That is
	what makes deleting a relation a visible regression to a consumer who
	never built the rung that would emit one.
	"""
	floor = layers.floor(schema)
	reach = layers.reach(schema)
	if floor == "view" and reach == "view":
		return []
	return ["#",
	        f"# layers: floor={floor} reach={reach}"]


def _codec_lines(schema: ast.Schema) -> list[str]:
	"""Record every codec, and which of them are trusted rather than proven.

	Section 13.1: a tier-1 codec can lie, because its declaration is trusted and
	unverified. Marking it in the map is what lets a reviewer see which
	capability conclusions rest on an assertion. A signature with no binding is
	marked too -- it is not an error, but nothing implements it yet.
	"""
	codecs = schema.codecs()
	if not codecs:
		return []

	bound = {impl.codec: impl for impl in schema.impls()}
	lines = ["", "# codecs. `trusted` means the properties are declared and",
	         "# unverified: run `situc gen-codec-tests` to falsify a lying one."]

	for codec in sorted(codecs, key=lambda decl: decl.name):
		impl = bound.get(codec.name)
		if impl is None:
			status = "unbound"
		elif impl.kind is ast.ImplKind.DERIVED:
			status = "derived"
		else:
			status = "trusted"

		properties = " ".join(_codec_properties(codec))
		lines.append(f"codec {codec.name} {status} {properties}".rstrip())

	return lines


def _codec_properties(codec: ast.CodecDecl) -> list[str]:
	shown = []

	if codec.expansion is ast.Expansion.PRESERVING:
		shown.append("length_preserving")
	elif codec.expansion is ast.Expansion.FIXED_ADD:
		shown.append(f"expansion=+{codec.expansion_add}")
	elif codec.expansion is ast.Expansion.UNBOUNDED:
		shown.append("expansion=unbounded")
	elif codec.ratio is not None:
		shown.append(f"expansion={codec.expansion.value}"
		             f"({codec.ratio[0]},{codec.ratio[1]})")

	shown.append(f"seekable={codec.seekable.value}")
	granularity = codec.granularity.value
	if codec.granularity_size is not None:
		granularity += f"({codec.granularity_size})"
	shown.append(f"granularity={granularity}")

	for flag in ("systematic", "authenticated", "invertible", "deterministic",
	             "error_propagating"):
		if getattr(codec, flag):
			shown.append(flag)

	return shown


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
		"#",
		"# A struct's size is a bare number where it is fixed, `min..max` where",
		"# it is bounded, and `min..` where nothing bounds it.",
	]

	lines.extend(_layer_lines(schema))
	lines.extend(_codec_lines(schema))

	for name in sorted(resolved.structs):
		lines.append("")
		lines.extend(_struct(name, resolved))

	return "\n".join(lines) + "\n"


def _struct_size(layout: StructLayout) -> str:
	"""What the struct spans, which is a range whenever it is not fixed.

	`size_bytes` alone is the *minimum*, and this printed it with nothing to
	say so: `struct proto_message size=0` for a struct with no upper bound at
	all, and `struct udp_header size=8` for one that runs to 65535. Fifty of
	the tree's 158 structs understated themselves that way -- and the line
	directly below each one prints `size=Bounded(8, 65535)` for a field, so
	the same token meant a byte count on one line and a range on the next.

	Written as `min..max`, or `min..` where nothing bounds it, so a reader who
	sees a bare number knows the struct is fixed.
	"""
	if not layout.is_byte_sized:
		return f"{layout.size_bits}bit"

	low = layout.size_bytes
	if layout.size_max_bits is None:
		return f"{low}.."
	if layout.size_max_bits == layout.size_bits:
		return f"{low}"
	return f"{low}..{layout.size_max_bits // BITS_PER_BYTE}"


def _struct(name: str, resolved: ResolvedSchema) -> list[str]:
	struct  = resolved.structs[name]
	layout  = struct.layout
	size    = _struct_size(layout)

	# A register's address and access width belong in the map: they are what
	# decides every mutate value under it, so a reader asking why a setter is
	# missing should not have to open the schema to find them.
	kind = "struct"
	extra = ""
	if layout.register is not None:
		register = layout.register
		kind  = "register"
		where = ("" if register.address is None
		         else f" @ 0x{register.address:02X}")
		extra = (f"{where} access_width={register.access_width}"
		         f"{' no_rmw' if register.no_rmw else ''}")

	lines = [f"{kind} {name}{extra} size={size} "
	         f"{_axes(struct.vector, core=False)}".rstrip()]
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
