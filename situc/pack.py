"""The packed layout image `situc pack` emits (26.33, decision 0026).

One responsibility: turn a resolved schema into the byte image described by
`std/image.situ`, so that a walker in another project can read a format it
was not compiled against.

Nothing here walks an image. That boundary is decision 0026's and it is what
keeps `situc`'s own promises true: an offset is a constant, an operation is
absent rather than refused, generated code never allocates. An interpreter
can make none of those three claims, so it lives somewhere else and this
module only ever writes.

The expression bytecode is section 10's language and nothing more. That
language is total -- no calls, no recursion, no iteration, no floating point
-- so the encoding is a postfix stack machine with a fixed operand width and
no jumps. A walker's evaluator is a loop over a switch with no way to not
terminate, which is the property that makes shipping one to a radio
defensible.

Expressions are compiled from the AST rather than from `Placement.size_expr`,
which holds *source text* for consumers that render it. Reading that back
would be re-parsing our own output, which section 25 forbids and which would
put a second parser in the one component decision 0026 wants checked.
"""

from __future__ import annotations

import struct as _struct
from collections.abc import Callable
from dataclasses import dataclass, field

from situc import ast, traverse
from situc.capability import DOMAINS, Axis
from situc.layout import Placement
from situc.resolve import ResolvedSchema, ResolvedStruct

MAGIC		= b"SITU"
FORMAT_VERSION	= 1
NONE		= 0xFFFFFFFF
HEADER_BYTES	= 40
STRUCT_BYTES	= 12
PLACEMENT_BYTES	= 32

FLAG_METADATA	= 1 << 0	# header.flags

#: `image_placement.flags`
OFFSET_KNOWN	= 1 << 0
FRAME_RELATIVE	= 1 << 1
SIZE_FIXED	= 1 << 2

#: `image_kind`, matching the enum in std/image.situ. A kind the walker does
#: not know is an error there rather than a guess, which is why the schema
#: declares `default = error`.
KIND = {
	"field": 0, "reserved": 1, "marker": 2, "region": 3,
	"variant": 4, "tlv": 5, "indexed": 6, "opaque": 7,
}

ENDIAN = {None: 0, ast.Endian.BIG: 1, ast.Endian.LITTLE: 2, ast.Endian.NATIVE: 3}
BIT_ORDER = {None: 0, ast.BitOrder.MSB_FIRST: 1, ast.BitOrder.LSB_FIRST: 2}


class PackError(Exception):
	"""An expression or placement the image cannot represent yet.

	Raised rather than encoded as a sentinel: a walker that reads a zero
	where a size expression should be computes the wrong length silently,
	and the whole point of the image is that the solving already happened.
	"""


# ---------------------------------------------------------------------------
# The expression bytecode
# ---------------------------------------------------------------------------

#: One byte each, operands little-endian and fixed width. No jumps: section
#: 10 has no control flow, so a program is a straight line and its length is
#: its own bound.
class Op:
	END		= 0x00
	PUSH		= 0x01		# + i64
	FIELD		= 0x02		# + u32 placement index
	REMAINING	= 0x03
	SIZE		= 0x04		# + u32 placement index
	OFFSET		= 0x05		# + u32 placement index
	COUNT		= 0x06		# + u32 placement index
	ADD		= 0x10
	SUB		= 0x11
	MUL		= 0x12
	DIV		= 0x13
	MOD		= 0x14
	AND		= 0x15
	OR		= 0x16
	XOR		= 0x17
	SHL		= 0x18
	SHR		= 0x19
	NEG		= 0x1A
	NOT		= 0x1B
	EQ		= 0x20
	NE		= 0x21
	LT		= 0x22
	LE		= 0x23
	GT		= 0x24
	GE		= 0x25
	LAND		= 0x26
	LOR		= 0x27
	MIN		= 0x30
	MAX		= 0x31
	ALIGN_UP	= 0x32


BINARY = {
	"+": Op.ADD, "-": Op.SUB, "*": Op.MUL, "/": Op.DIV, "%": Op.MOD,
	"&": Op.AND, "|": Op.OR, "^": Op.XOR, "<<": Op.SHL, ">>": Op.SHR,
	"==": Op.EQ, "!=": Op.NE, "<": Op.LT, "<=": Op.LE, ">": Op.GT,
	">=": Op.GE, "&&": Op.LAND, "||": Op.LOR,
}

UNARY = {"-": Op.NEG, "~": Op.NOT, "+": None}

#: `size`, `offset` and `count` take a path; `min`, `max` and `align_up` take
#: values and are ordinary operators with a name.
PATH_CALLS  = {"size": Op.SIZE, "offset": Op.OFFSET, "count": Op.COUNT}
VALUE_CALLS = {"min": Op.MIN, "max": Op.MAX, "align_up": Op.ALIGN_UP}


def _path_of(expr: ast.Expr) -> str | None:
	"""The dotted path an expression names, or None if it is not one."""
	if isinstance(expr, ast.NameRef):
		return expr.name
	if isinstance(expr, ast.Access):
		base = _path_of(expr.base)
		return None if base is None else f"{base}.{expr.name}"
	if isinstance(expr, ast.Index):
		return _path_of(expr.base)
	return None


#: A path to a placement index, or None where the image does not name it.
Resolver = Callable[[str | None], int | None]


class Program:
	"""A growing bytecode buffer, so a caller can emit several and share one."""

	def __init__(self) -> None:
		self.code = bytearray()

	def emit(self, op: int, operand: int | None = None,
	         width: str = "<I") -> None:
		self.code.append(op)
		if operand is not None:
			self.code += _struct.pack(width, operand)

	def compile(self, expr: ast.Expr, resolve_path: Resolver,
	            consts: dict[str, int] | None = None) -> None:
		"""Append a postfix program computing `expr`.

		`resolve_path` maps a dotted path to a placement index, and returns
		None where the path is not one this image names. `consts` maps a
		`const` name to its value: a const is a compile-time constant, so it
		becomes a literal here rather than a load a walker would have to
		resolve against a table it does not carry.
		"""
		known: dict[str, int] = consts or {}
		if isinstance(expr, ast.IntLiteral):
			self.emit(Op.PUSH, expr.value, "<q")
			return
		if isinstance(expr, ast.Remaining):
			self.emit(Op.REMAINING)
			return
		if isinstance(expr, (ast.NameRef, ast.Access, ast.Index)):
			path = _path_of(expr)
			if path is not None and path in known:
				self.emit(Op.PUSH, known[path], "<q")
				return
			index = resolve_path(path) if path else None
			if index is None:
				raise PackError(f"no placement for `{path or expr}`")
			self.emit(Op.FIELD, index)
			return
		if isinstance(expr, ast.Unary):
			self.compile(expr.operand, resolve_path, known)
			op = UNARY.get(expr.op, ...)
			if op is ...:
				raise PackError(f"unary `{expr.op}`")
			if op is not None:		# unary `+` is the identity
				self.emit(op)
			return
		if isinstance(expr, ast.Binary):
			op = BINARY.get(expr.op)
			if op is None:
				raise PackError(f"binary `{expr.op}`")
			self.compile(expr.left, resolve_path, known)
			self.compile(expr.right, resolve_path, known)
			self.emit(op)
			return
		if isinstance(expr, ast.Call):
			self._call(expr, resolve_path, known)
			return
		raise PackError(f"{type(expr).__name__} is not in section 10")

	def _call(self, expr: ast.Call, resolve_path: Resolver,
	          consts: dict[str, int]) -> None:
		if expr.name in PATH_CALLS:
			if len(expr.args) != 1:
				raise PackError(f"`{expr.name}` takes one path")
			path  = _path_of(expr.args[0])
			index = resolve_path(path) if path else None
			if index is None:
				raise PackError(f"no placement for `{path}` in `{expr.name}`")
			self.emit(PATH_CALLS[expr.name], index)
			return
		if expr.name in VALUE_CALLS:
			if len(expr.args) != 2:
				raise PackError(f"`{expr.name}` takes two values")
			for arg in expr.args:
				self.compile(arg, resolve_path, consts)
			self.emit(VALUE_CALLS[expr.name])
			return
		raise PackError(f"`{expr.name}` is not a section 10 builtin")


# ---------------------------------------------------------------------------
# The image
# ---------------------------------------------------------------------------

@dataclass
class Coverage:
	"""What the image carries, said positively.

	26.76's lesson, applied here before it can go wrong: a packer that
	silently emits `none` for every expression it could not compile produces
	an image that loads, walks, and is wrong. So the counts are reported and
	the tests assert them rather than asserting that nothing raised.
	"""

	structs: int			= 0
	placements: int			= 0
	expressions: int		= 0
	#: path -> why, for every expression that could not be encoded.
	unencodable: dict[str, str]	= field(default_factory=dict)


def _ast_members(schema: ast.Schema) -> dict[str, ast.Field]:
	"""`struct.member` -> the AST field, for the expressions it carries.

	Only one level deep, which is where an array size is written. A nested
	struct's own members are found under that struct's own name.
	"""
	found: dict[str, ast.Field] = {}
	for decl in schema.decls:
		if not isinstance(decl, ast.StructDecl):
			continue
		for member in decl.members:
			if isinstance(member, ast.Field):
				found[f"{decl.name}.{member.name}"] = member
	return found


def _kind_of(placement: Placement) -> int:
	"""Which `image_kind` a placement is.

	`Placement.kind` distinguishes field, reserved and marker; the region
	shapes are told apart by what the placement carries, the same way
	`traverse.classify` tells them apart for a backend.
	"""
	if placement.index_table is not None:
		return KIND["indexed"]
	if placement.tlv_grammar is not None or placement.tlv_unknown is not None:
		return KIND["tlv"]
	if placement.arm_cases:
		return KIND["variant"]
	if placement.regions and placement.path.endswith(placement.regions[-1]):
		return KIND["region"]
	return KIND.get(placement.kind, KIND["field"])


def _u32(value: int | None) -> int:
	return NONE if value is None else min(value, NONE - 1)


def pack(schema: ast.Schema, resolved: ResolvedSchema,
         metadata: bool = False) -> tuple[bytes, Coverage]:
	"""Emit the image for one resolved schema.

	Returns the bytes and what went into them. The coverage is returned
	rather than logged because a caller that cannot say how much of the
	schema it encoded has not checked anything.
	"""
	coverage = Coverage()
	members  = _ast_members(schema)

	# A stable index for every placement, and for every struct. Declaration
	# order, so that two runs over one schema produce one image -- the
	# property `situc wire --check` and a committed image both rest on.
	order:  list[tuple[str, ResolvedStruct]] = list(resolved.structs.items())
	struct_index = {name: i for i, (name, _) in enumerate(order)}

	rows: list[tuple[str, Placement]] = []
	spans: list[tuple[int, int]] = []
	for name, rstruct in order:
		first = len(rows)
		for entry in traverse.own_entries(rstruct):
			rows.append((name, entry.placement))
		spans.append((first, len(rows) - first))

	placement_index = {p.path: i for i, (_, p) in enumerate(rows)}

	# `member.field` inside a struct means the `field` of whatever struct
	# `member` is typed as -- `image.structs[header.struct_count]` reaches
	# `image_header.struct_count`. Walking the type chain is how a walker
	# will do it too; matching the text against placement paths is not, and
	# that was the first version, which resolved nothing for ten schemas.
	own_of = {name: {p.path.split(".")[-1]: p
	                 for _, p in rows if _ == name}
	          for name, _ in order}

	def resolve_in(owner: str, parts: list[str]) -> int | None:
		here = own_of.get(owner, {}).get(parts[0])
		if here is None:
			return None
		if len(parts) == 1:
			return placement_index.get(here.path)
		return resolve_in(here.type_name, parts[1:])

	def resolve_path(path: str | None, owner: str | None = None) -> int | None:
		if path is None:
			return None
		if path in placement_index:
			return placement_index[path]
		parts = path.split(".")
		if owner is not None:
			found = resolve_in(owner, parts)
			if found is not None:
				return found
		for name, _ in order:			# a path written from the root
			if parts[0] == name and len(parts) > 1:
				found = resolve_in(name, parts[1:])
				if found is not None:
					return found
		return None

	# A `const` is a compile-time constant, so it becomes a literal in the
	# bytecode. A walker carries no constant table and should not need one.
	consts = {decl.name: decl.value.value
	          for decl in schema.decls
	          if isinstance(decl, ast.ConstDecl)
	          and isinstance(decl.value, ast.IntLiteral)}

	# -- the bytecode, first, because a placement record points into it --
	program = Program()
	code_at: dict[str, int] = {}
	for owner, placement in rows:
		field = members.get(placement.path)
		expr  = field.array.size if field is not None and field.array else None
		if expr is None:
			continue
		start = len(program.code)
		try:
			program.compile(expr, lambda p: resolve_path(p, owner), consts)
		except PackError as why:
			del program.code[start:]
			coverage.unencodable[placement.path] = str(why)
			continue
		program.emit(Op.END)
		code_at[placement.path] = start
		coverage.expressions += 1

	# -- section offsets --
	struct_off    = HEADER_BYTES
	placement_off = struct_off + len(order) * STRUCT_BYTES
	code_off      = placement_off + len(rows) * PLACEMENT_BYTES
	tail_off      = code_off + len(program.code)
	meta_off      = tail_off if metadata else 0

	out = bytearray()
	out += MAGIC
	out += _struct.pack("<HH", FORMAT_VERSION, FLAG_METADATA if metadata else 0)
	out += _struct.pack("<III", len(order), len(rows), len(program.code))
	out += _struct.pack("<IIII", struct_off, placement_off, code_off, meta_off)
	image_bytes_at = len(out)
	out += _struct.pack("<I", 0)			# patched once the tail is known
	assert len(out) == HEADER_BYTES, len(out)

	for (name, rstruct), (first, count) in zip(order, spans):
		size = (rstruct.layout.size_bits
		        if rstruct.layout.is_fixed_size else None)
		out += _struct.pack("<III", first, count, _u32(size))

	for owner, placement in rows:
		flags = 0
		if placement.offset_bits is not None:
			flags |= OFFSET_KNOWN
		if placement.frame_relative:
			flags |= FRAME_RELATIVE
		if placement.size_max_bits == placement.size_bits:
			flags |= SIZE_FIXED
		out += _struct.pack(
			"<BBBB",
			_kind_of(placement),
			ENDIAN.get(placement.endian, 0),
			BIT_ORDER.get(placement.bit_order, 0),
			flags,
		)
		out += _struct.pack(
			"<IIIIIII",
			_u32(placement.offset_bits),
			_u32(placement.size_bits),
			_u32(placement.size_max_bits),
			_u32(placement.element_bits),
			_u32(placement.array_count),
			_u32(code_at.get(placement.path)),
			_u32(struct_index.get(placement.type_name)),
		)

	out += bytes(program.code)
	if metadata:
		out += _metadata(order, rows, resolved, tail_off)

	out[image_bytes_at:image_bytes_at + 4] = _struct.pack("<I", len(out))
	coverage.structs    = len(order)
	coverage.placements = len(rows)
	return bytes(out), coverage


def _metadata(order: list[tuple[str, ResolvedStruct]],
              rows: list[tuple[str, Placement]],
              resolved: ResolvedSchema, base: int) -> bytes:
	"""The optional tail: a string pool, the names, and the vectors.

	Separate from the core because 26.33 recorded that the two consumers pull
	opposite ways. A device that never prints a name does not carry one, and
	the core's layout is identical either way -- which is what stops this
	being two formats rather than one with a tail.
	"""
	pool  = bytearray()
	index: dict[str, int] = {}

	def intern(text: str) -> int:
		if text not in index:
			index[text] = len(pool)
			pool.extend(text.encode("ascii", "replace") + b"\0")
		return index[text]

	struct_names    = [intern(name) for name, _ in order]
	placement_names = [intern(p.path) for _, p in rows]

	vectors = bytearray()
	for owner, placement in rows:
		entry = resolved.find(placement.path)
		for axis in Axis:
			value  = entry.vector.get(axis) if entry is not None else None
			domain = [str(name) for name in DOMAINS[axis]]
			shown  = str(value) if value is not None else None
			vectors.append(domain.index(shown)
			               if shown is not None and shown in domain else 0xFF)

	head_bytes   = 20
	strings_at   = base + head_bytes
	names_at     = strings_at + len(pool)
	placements_at = names_at + len(struct_names) * 4
	vectors_at   = placements_at + len(placement_names) * 4

	out = bytearray()
	out += _struct.pack("<IIIII", len(pool), strings_at, names_at,
	                    placements_at, vectors_at)
	assert len(out) == head_bytes, len(out)
	out += pool
	for offset in struct_names:
		out += _struct.pack("<I", offset)
	for offset in placement_names:
		out += _struct.pack("<I", offset)
	out += vectors
	return bytes(out)
