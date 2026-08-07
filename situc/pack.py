"""The packed layout image `situc pack` emits (26.33, decision 0026).

One responsibility: turn a resolved schema into the byte image described by
`std/image.situ`, so that a walker can read a format it was not compiled
against.

Nothing here walks an image. That boundary is decision 0026's and it is what
keeps `situc`'s own promises true: an offset is a constant, an operation is
absent rather than refused, generated code never allocates. An interpreter
can make none of those three claims, so it is a separate binary -- in this
repository since 0026's amendment, so that it can join the differential
check, but never linked into `situc`. This module only ever writes.

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
FORMAT_VERSION	= 2
NONE		= 0xFFFFFFFF
HEADER_BYTES	= 20
SECTION_BYTES	= 16
STRUCT_BYTES	= 12
PLACEMENT_BYTES	= 48
ARM_BYTES	= 24
DELIMITER_BYTES	= 32
REGION_BYTES	= 12
CODEC_BYTES	= 4
VARINT_BYTES	= 12
TLV_BYTES	= 12
INDEX_BYTES	= 16

FLAG_METADATA	= 1 << 0	# header.flags

#: `image_section_tag` in std/image.situ. A walker keeps what it knows and
#: skips the rest, which is what lets a section be added without a version.
SECTION_STRUCTS		= 1
SECTION_PLACEMENTS	= 2
SECTION_CODE		= 3
SECTION_STRINGS		= 4
SECTION_ARMS		= 5
SECTION_DELIMITERS	= 6
SECTION_REGIONS		= 7
SECTION_CODECS		= 8
SECTION_VARINTS		= 9
SECTION_TLVS		= 10
SECTION_INDEXES		= 11
SECTION_NAMES		= 12
SECTION_VECTORS		= 13

#: `image_placement.flags`
OFFSET_KNOWN		= 1 << 0
FRAME_RELATIVE		= 1 << 1
SIZE_FIXED		= 1 << 2
FRAME_BASE_DYNAMIC	= 1 << 3
#: Whether the value is signed. Absent from the first version of the format,
#: and the walker found it the first time it read a BMP: `i32 width` came
#: back as 3136328947 where C said -1158638349, the same bits under two
#: readings. An image that cannot say which is not a description of a
#: layout (26.81).
SIGNED			= 1 << 4
#: The byte order is an endian marker's, read from the message. netlink is
#: the format whose byte order is the sending machine's, and a walker reading
#: this record's static endian disagreed with C about `nlmsg_seq` (26.81).
MARKER_GOVERNED		= 1 << 5

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

#: Every construct a placement can carry that a walk needs, and how to see
#: it. One table, read by the encoders below and by the coverage report, so
#: that a family cannot be encoded without being counted or counted without
#: being encoded.
#:
#: The first version of this file reported only expressions, and said
#: "nothing dropped" over an image that carried no delimiter, no variant arm
#: and no index table. That is the vacuous pass this project keeps finding,
#: written into the instrument meant to prevent it: a count is only evidence
#: once you know what it counted.
CONSTRUCTS: tuple[tuple[str, Callable[[Placement], bool]], ...] = (
	("region",     lambda p: bool(p.regions)),
	("delimiter",  lambda p: p.delimiter is not None),
	("radix",      lambda p: p.radix is not None),
	("variant",    lambda p: bool(p.arm_cases)),
	("codec",      lambda p: p.codec is not None),
	("repeat",     lambda p: p.repeat_while is not None),
	("located",    lambda p: p.located is not None),
	("varint",     lambda p: p.varint is not None),
	("tlv",        lambda p: p.tlv_grammar is not None),
	("indexed",    lambda p: p.index_table is not None),
)

#: Which of those the image actually encodes. Everything not named here is
#: reported as missing rather than passed over, and moving a family into the
#: image means moving its name in here -- which is what makes the report
#: retire itself as the format grows.
ENCODED: frozenset[str] = frozenset({
	"region", "delimiter", "radix", "variant", "codec",
	"repeat", "located", "varint", "tlv", "indexed",
})


@dataclass
class Coverage:
	"""What the image carries, said positively.

	26.76's lesson, applied here before it can go wrong: a packer that
	silently emits `none` for every construct it could not encode produces an
	image that loads, walks, and is wrong. So the counts are reported and the
	tests assert them rather than asserting that nothing raised.
	"""

	structs: int			= 0
	placements: int			= 0
	expressions: int		= 0
	#: path -> why, for every expression that could not be encoded.
	unencodable: dict[str, str]	= field(default_factory=dict)
	#: family -> how many placements carry it. Reported whether or not the
	#: image encodes it, so the two numbers can be compared.
	carried: dict[str, int]		= field(default_factory=dict)
	#: family -> how many placements carry it and the image drops it.
	unencoded: dict[str, int]	= field(default_factory=dict)


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


class _Pool:
	"""The core string pool: the strings a walk needs in order to function.

	Codec and varint encoding names live here rather than in the metadata
	tail, because a walker cannot dispatch a transform it cannot identify.
	The tail carries the strings needed only to print.
	"""

	def __init__(self) -> None:
		self.data: bytearray = bytearray()
		self._at: dict[str, int] = {}

	def intern(self, text: str) -> int:
		if text not in self._at:
			self._at[text] = len(self.data)
			self.data.extend(text.encode("ascii", "replace") + b"\0")
		return self._at[text]


def _arm_fields(arm: object) -> tuple[int, str | None, int]:
	"""One variant arm as (case value, selected path, kind bits).

	`default:` carries no value, and `default: error` selects nothing. Both
	are bits rather than sentinels so a walker never has to know which
	integer means "no case".
	"""
	value    = getattr(arm, "value", None)
	selected = getattr(arm, "path", None) or getattr(arm, "member", None)
	kind     = 0
	if value is None:
		kind |= 1				# the default arm
	if selected is None:
		kind |= 2				# `default: error`
	return (int(value) if value is not None else 0,
	        selected if isinstance(selected, str) else None, kind)


def _policy(name: str | None) -> int:
	"""A tlv policy as a small integer, 0 meaning unstated."""
	return {None: 0, "error": 1, "skip": 2, "keep": 3,
	        "first": 4, "last": 5}.get(name, 0)


def _index_base(placement: Placement) -> int:
	"""Where an `indexed` region's offsets are measured from (decision 0024)."""
	table = placement.index_table
	base  = getattr(table, "base", None)
	return {"region": 0, "message": 1}.get(str(base), 2)


def _assemble(sections: list[tuple[int, bytes, int]], metadata: bool) -> bytes:
	"""Header, directory, then the section bodies in directory order.

	Two passes: the directory's size is known from the section count, so the
	bodies' offsets are known before anything is written. Nothing is patched
	afterwards except the total length, which cannot be known earlier.
	"""
	live = [(kind, blob, stride) for kind, blob, stride in sections]
	body_at = HEADER_BYTES + len(live) * SECTION_BYTES

	directory, bodies, at = bytearray(), bytearray(), body_at
	for kind, blob, stride in live:
		count = len(blob) // stride if stride else 0
		directory += _struct.pack("<IIII", kind, at, count, stride)
		bodies    += blob
		at        += len(blob)

	out = bytearray()
	out += MAGIC
	out += _struct.pack("<HH", FORMAT_VERSION, FLAG_METADATA if metadata else 0)
	out += _struct.pack("<I", body_at + len(bodies))
	out += _struct.pack("<II", len(live), HEADER_BYTES)
	assert len(out) == HEADER_BYTES, len(out)
	out += directory
	out += bodies
	return bytes(out)


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

	# `at expr` and `while (cond)` are expressions too, and a walker needs
	# both: one says where a member is, the other when a run stops.
	located_at: dict[str, int] = {}
	repeat_at:  dict[str, int] = {}
	# A `while` predicate is asked about the element just parsed, so its
	# names resolve in the *element's* struct rather than in the struct that
	# holds the run: `while (nla_len >= 4)` is netlink's `nla_ok`, and
	# `nla_len` is a field of the attribute, not of the message.
	picker: tuple[tuple[dict[str, int], Callable[[ast.Field], ast.Expr | None],
	                    bool], ...] = (
		(located_at, lambda f: f.located, False),
		(repeat_at, lambda f: f.repeat.predicate if f.repeat else None, True),
	)
	for target, pick, scope in picker:
		for owner, placement in rows:
			field = members.get(placement.path)
			expr  = pick(field) if field is not None else None
			if expr is None:
				continue
			where = placement.type_name if scope else owner
			start = len(program.code)
			try:
				program.compile(expr, lambda p: resolve_path(p, where), consts)
			except PackError as why:
				del program.code[start:]
				coverage.unencodable[placement.path] = str(why)
				continue
			program.emit(Op.END)
			target[placement.path] = start
			coverage.expressions += 1

	# -- the side tables, and the core strings a walk needs to function --
	strings = _Pool()
	sections: list[tuple[int, bytes, int]] = []

	structs_blob = bytearray()
	for (name, rstruct), (first, count) in zip(order, spans):
		size = (rstruct.layout.size_bits
		        if rstruct.layout.is_fixed_size else None)
		structs_blob += _struct.pack("<III", first, count, _u32(size))

	# The codec table is built first: a region record points into it.
	codec_index: dict[str, int] = {}
	codecs_blob = bytearray()
	for _, placement in rows:
		for named in (placement.codec, *placement.tag_covers[:0]):
			if named and named not in codec_index:
				codec_index[named] = len(codec_index)
				codecs_blob += _struct.pack("<I", strings.intern(named))

	varint_index: dict[str, int] = {}
	varints_blob = bytearray()
	arms_blob    = bytearray()
	delims_blob  = bytearray()
	regions_blob = bytearray()
	tlvs_blob    = bytearray()
	index_blob   = bytearray()

	for at, (owner, placement) in enumerate(rows):
		if placement.varint is not None:
			varint_index.setdefault(placement.varint, len(varint_index))
			varints_blob += _struct.pack(
				"<IIBBxx", at, strings.intern(placement.varint),
				min(placement.size_max_bits or 64, 255),
				1 if placement.varint_minimal else 0)
		for arm in placement.arm_cases:
			value, chosen, kind = _arm_fields(arm)
			arms_blob += _struct.pack(
				"<IIqB7x", at, _u32(placement_index.get(chosen or "")),
				value, kind)
		if placement.delimiter is not None:
			raw = placement.delimiter[:15]
			delims_blob += _struct.pack(
				"<IIIIB15s", at,
				_u32(placement.delimiter_quote),
				_u32(placement.delimiter_escape),
				_u32(placement.delimiter_cap),
				len(raw), raw)
		if placement.regions:
			flags = (1 if placement.sealed_by else 0) \
				| (2 if placement.unverified_ok else 0)
			regions_blob += _struct.pack(
				"<IIB3x", at,
				_u32(codec_index.get(placement.codec or "")), flags)
		if placement.tlv_grammar is not None:
			regions = (1 if placement.tlv_ordered else 0)
			tlvs_blob += _struct.pack(
				"<IIBBBx", at,
				_u32(varint_index.get(placement.tlv_tag_varint or "")),
				regions,
				_policy(placement.tlv_unknown),
				_policy(placement.tlv_duplicates))
		if placement.index_table is not None:
			entry_bytes = traverse.index_entry_bytes(placement)
			index_blob += _struct.pack(
				"<IIIB3x", at,
				_u32(None if entry_bytes is None else entry_bytes * 8),
				NONE, _index_base(placement))

	sections.append((SECTION_STRUCTS, bytes(structs_blob), STRUCT_BYTES))
	sections.append((SECTION_PLACEMENTS, b"", PLACEMENT_BYTES))	# filled below
	sections.append((SECTION_CODE, bytes(program.code), 1))
	for kind, blob, stride in (
			(SECTION_ARMS, arms_blob, ARM_BYTES),
			(SECTION_DELIMITERS, delims_blob, DELIMITER_BYTES),
			(SECTION_REGIONS, regions_blob, REGION_BYTES),
			(SECTION_CODECS, codecs_blob, CODEC_BYTES),
			(SECTION_VARINTS, varints_blob, VARINT_BYTES),
			(SECTION_TLVS, tlvs_blob, TLV_BYTES),
			(SECTION_INDEXES, index_blob, INDEX_BYTES)):
		if blob:
			sections.append((kind, bytes(blob), stride))

	placements_blob = bytearray()
	for owner, placement in rows:
		flags = 0
		if placement.offset_bits is not None:
			flags |= OFFSET_KNOWN
		if placement.frame_relative:
			flags |= FRAME_RELATIVE
		if placement.size_max_bits == placement.size_bits:
			flags |= SIZE_FIXED
		if placement.frame_base_dynamic:
			flags |= FRAME_BASE_DYNAMIC
		if placement.scalar is not None and placement.scalar.signed:
			flags |= SIGNED
		if placement.marker is not None:
			flags |= MARKER_GOVERNED
		text = (1 if placement.radix_minimal else 0) \
			| (2 if placement.trimmed else 0) \
			| (4 if placement.case_insensitive else 0)
		placements_blob += _struct.pack(
			"<BBBB",
			_kind_of(placement),
			ENDIAN.get(placement.endian, 0),
			BIT_ORDER.get(placement.bit_order, 0),
			flags,
		)
		placements_blob += _struct.pack(
			"<IIIIIIIIIBBHHH",
			_u32(placement.offset_bits),
			_u32(placement.size_bits),
			_u32(placement.size_max_bits),
			_u32(placement.element_bits),
			_u32(placement.array_count),
			_u32(code_at.get(placement.path)),
			_u32(struct_index.get(placement.type_name)),
			_u32(located_at.get(placement.path)),
			_u32(repeat_at.get(placement.path)),
			placement.radix or 0,
			text,
			min(placement.array_count or 0, 0xFFFF) if placement.radix else 0,
			min(placement.since or 0, 0xFFFF),
			0,
		)
	sections[1] = (SECTION_PLACEMENTS, bytes(placements_blob), PLACEMENT_BYTES)

	if strings.data:
		sections.append((SECTION_STRINGS, bytes(strings.data), 1))
	if metadata:
		sections.extend(_metadata(order, rows, resolved))

	out = _assemble(sections, metadata)
	coverage.structs    = len(order)
	coverage.placements = len(rows)
	for family, present in CONSTRUCTS:
		count = sum(1 for _, placement in rows if present(placement))
		if not count:
			continue
		coverage.carried[family] = count
		if family not in ENCODED:
			coverage.unencoded[family] = count
	return bytes(out), coverage


def _metadata(order: list[tuple[str, ResolvedStruct]],
              rows: list[tuple[str, Placement]],
              resolved: ResolvedSchema) -> list[tuple[int, bytes, int]]:
	"""The optional tail, as sections: the names and the capability vectors.

	These are what a walk needs in order to be *read*, not in order to work,
	which is the line the core/tail split is drawn on. A device omits them and
	loses nothing it executes; a dissector asks for them and can print.

	Returned as directory entries rather than one blob so that the tail is
	made of ordinary sections: a walker that wants names and not vectors
	takes one and skips the other, with no tail-specific parsing at all.
	"""
	pool = _Pool()
	names = bytearray()
	for name, _ in order:
		names += _struct.pack("<I", pool.intern(name))
	for _, placement in rows:
		names += _struct.pack("<I", pool.intern(placement.path))

	vectors = bytearray()
	for owner, placement in rows:
		entry = resolved.find(placement.path)
		for axis in Axis:
			value  = entry.vector.get(axis) if entry is not None else None
			domain = [str(name) for name in DOMAINS[axis]]
			shown  = str(value) if value is not None else None
			vectors.append(domain.index(shown)
			               if shown is not None and shown in domain else 0xFF)

	out: list[tuple[int, bytes, int]] = [
		(SECTION_NAMES, bytes(names), 4),
		(SECTION_VECTORS, bytes(vectors), len(list(Axis))),
	]
	if pool.data:
		out.append((SECTION_STRINGS, bytes(pool.data), 1))
	return out
