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
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from situc import ast, traverse
from situc.capability import DOMAINS, Axis
from situc.diagnostics import SituError
from situc.expr import evaluate
from situc.layout import Placement
from situc.relation import Refused as RelationRefused
from situc.relation import plan as plan_relation
from situc.resolve import ResolvedSchema, ResolvedStruct

MAGIC		= b"SITU"
FORMAT_VERSION	= 2
NONE		= 0xFFFFFFFF
HEADER_BYTES	= 20
SECTION_BYTES	= 16
STRUCT_BYTES	= 16
PLACEMENT_BYTES	= 48
ARM_BYTES	= 24
DELIMITER_BYTES	= 32
REGION_BYTES	= 16
CODEC_BYTES	= 4
VARINT_BYTES	= 12
TLV_BYTES	= 12
INDEX_BYTES	= 16
MARKER_BYTES	= 16
CONSTRAINT_BYTES = 16
ENUM_VALUE_BYTES = 16
VERSION_BYTES		= 8
RELATION_BYTES		= 24
RELATION_MUST_BYTES	= 8

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
SECTION_MARKERS		= 14
SECTION_CONSTRAINTS	= 15
SECTION_ENUM_VALUES	= 16
SECTION_VERSIONS	= 17
SECTION_RELATIONS	= 18
SECTION_RELATION_MUSTS	= 19
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
#: A `tag` or `checksum`. The differ asks it `present=<0|1>` rather than for
#: bytes or a value, and a walker with no way to tell rendered keystore's
#: `tag` as a sixteen-byte run (26.82).
IS_TAG			= 1 << 6

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
	# A relation's two parameters are usually the same struct, so a
	# placement index alone does not say which message to read it out
	# of. Only relation programs emit this (26.95).
	ARG_FIELD	= 0x07		# + u8 parameter, u32 placement index
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
#: A relation path resolves to the parameter it names and the placement
#: within that parameter's struct.
RelationResolver = Callable[[str], "tuple[int, int] | None"]


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

	def compile_relation(self, expr: ast.Expr,
	                     resolve_arg: "RelationResolver") -> None:
		"""Append a program for one `must`, over two parameters (26.95).

		Separate from `compile` rather than a flag on it, because the two
		resolve a path differently and share only the operator table. Here a
		path names a parameter and a member of it, so `resolve_arg` answers
		with both and the program says which message each load reads.

		`situc.relation` has already refused everything this cannot emit --
		calls, unknown operators, a bare parameter -- so a node reaching the
		fallthrough is a compiler bug rather than a schema error.
		"""
		if isinstance(expr, ast.IntLiteral):
			self.emit(Op.PUSH, expr.value, "<q")
			return
		if isinstance(expr, (ast.NameRef, ast.Access)):
			path = _path_of(expr)
			found = resolve_arg(path) if path else None
			if found is None:
				raise PackError(f"no placement for `{path or expr}`")
			arg, index = found
			self.code.append(Op.ARG_FIELD)
			self.code.append(arg)
			self.code += _struct.pack("<I", index)
			return
		if isinstance(expr, ast.Unary):
			self.compile_relation(expr.operand, resolve_arg)
			op = UNARY.get(expr.op, ...)
			if op is ...:
				raise PackError(f"unary `{expr.op}` in a relation")
			if op is not None:
				self.emit(op)
			return
		if isinstance(expr, ast.Binary):
			op = BINARY.get(expr.op)
			if op is None:
				raise PackError(f"`{expr.op}` in a relation")
			self.compile_relation(expr.left, resolve_arg)
			self.compile_relation(expr.right, resolve_arg)
			self.emit(op)
			return
		raise PackError(f"a relation cannot hold {type(expr).__name__}")

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
	relations: int			= 0
	#: path -> why, for every expression that could not be encoded.
	unencodable: dict[str, str]	= field(default_factory=dict)
	#: family -> how many placements carry it. Reported whether or not the
	#: image encodes it, so the two numbers can be compared.
	carried: dict[str, int]		= field(default_factory=dict)
	#: family -> how many placements carry it and the image drops it.
	unencoded: dict[str, int]	= field(default_factory=dict)


def _ast_members(
		schema: ast.Schema) -> dict[str, ast.Field | ast.Opaque | ast.Reserved]:
	"""`struct.member` -> the AST field, for the expressions it carries.

	Nested as well as top level. A variant's arm and a region's interior hold
	fields with size expressions of their own, and reading only one level
	deep left them without one: `label.body.text` is `u8 text[rest]`, and
	packing it with no size program made a dnsname label one byte long and
	a walk of them run off the end of the buffer (26.84).
	"""
	found: dict[str, ast.Field | ast.Opaque | ast.Reserved] = {}

	# A reserved member is named by the compiler rather than by the schema,
	# and `layout.reserved_count` numbers them per *struct* in declaration
	# order -- flattened, so one inside a region is still the struct's Nth.
	# Reconstructing the name is what lets a reserved run be looked up at
	# all: every one of the 24 in this tree is `struct.<reservedN>`, checked
	# rather than assumed.
	def walk(prefix: str, members: Sequence[object], owner: str,
			counter: list[int]) -> None:
		for member in members:
			name = getattr(member, "name", None)
			path = f"{prefix}.{name}" if name else prefix
			# `default: opaque rest[nlmsg_len - 16]` is an `Opaque`, not a
			# `Field`, and carries its size expression directly rather than
			# through an array. Recording only fields left netlink's default
			# arm with no size program, so a message whose type names no arm
			# measured the variant as zero and put the attributes on top of
			# the body -- one attribute where C, placing them past a
			# 900-megabyte `rest`, counted none.
			# A reserved run is named by the compiler rather than by the
			# schema -- `<reserved0>`, `<reserved1>` -- and carries its
			# length the same way a field does. Missing it left cpio's
			# padding measuring zero, so the two runs it pads with were
			# never checked for being zero and `cpio_entry` said OK where
			# every backend said CONSTRAINT.
			if isinstance(member, ast.Reserved):
				path = f"{owner}.<reserved{counter[0]}>"
				counter[0] += 1
			if isinstance(member, (ast.Field, ast.Opaque, ast.Reserved)):
				found[path] = member
			# A variant holds its arms; a region and a positional block hold
			# their members. Both are reached by the same walk rather than
			# by naming every construct that can contain a field.
			for arm in getattr(member, "arms", ()):
				held = getattr(arm, "member", None)
				if held is not None:
					walk(path, (held,), owner, counter)

			# An `authenticated` region does not extend the path, because
			# `layout.place_authenticated` does not either: its members sit
			# at the offsets they would have had without it and keep the
			# enclosing struct's namespace, which is why 5.3 addresses
			# `Packet.hdr.seq` and not `Packet.authenticated.hdr.seq`. A
			# `coded` or `sealed` region *is* a namespace and does extend it.
			#
			# Extending it here recorded `u.s.payload` for a placement whose
			# path is `u.payload`, so the lookup below missed and the member
			# got no size program. A counted run with no program measures
			# zero, so a walk of `u8 payload[length - 8]` inside a covered
			# region reported a malformed message as valid where every
			# backend answered BOUNDS -- the walker's worst failure shape,
			# and invisible until a covered region held a member whose
			# bounds C would reject.
			inner = prefix if isinstance(member, ast.Authenticated) else path
			walk(inner, getattr(member, "members", ()), owner, counter)


	for decl in schema.decls:
		if isinstance(decl, ast.StructDecl):
			walk(decl.name, decl.members, decl.name, [0])
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


def _measurable(resolved: ResolvedSchema, struct: ResolvedStruct) -> bool:
	"""Whether a sub-view over one instance of `struct` exists.

	The same question `situ_packet_body_connect_view` answers and
	`situ_packet_body_publish_view` does not: a struct that ends in its
	frame carries no length of its own, so nothing can hand back a view
	bounded by it. Recorded in the image rather than re-derived by each
	reader, because a walker deciding this for itself would be deciding it
	differently -- which is what made every `publish` packet BOUNDS where C
	said OK.
	"""
	return struct.layout.is_fixed_size \
		or traverse.has_computable_extent(resolved.structs, struct)


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

	# A variant's arms name members that are *not* the struct's own entries --
	# `label.body.text` sits under the variant, not beside it -- so they have
	# no index until they are put in the table. They go after every struct's
	# own members and are not counted in any struct's span, so a walker
	# iterating members sees exactly what it did before and an arm record has
	# something to point at. Found by rendering arms: the table simply did
	# not contain them (26.82).
	own_count = len(rows)
	for name, rstruct in order:
		for entry in rstruct.entries:
			placement = entry.placement
			if any(placement.path == held.path for _, held in rows):
				continue
			wanted = any(arm.member == placement.path
			             for _, other in rows for arm in other.arm_cases)
			# A sealed region's interior is nested under the region for the
			# same reason an arm's member is nested under the variant, and
			# needs the same treatment: the gate exists to hand out those
			# members, so an image that cannot name them describes a gate
			# with nothing behind it.
			wanted = wanted or placement.sealed_by is not None
			# A *coded* region's interior needs it for a different reason.
			# Nothing hands those members out -- they are the transform's
			# input, not bytes on the wire -- but the region's extent is
			# their extent through the expansion, and a size expression can
			# only name a member the table contains. Without them the region
			# measured zero and everything after it was placed on top of it.
			wanted = wanted or any(
				placement.path.startswith(held.path + ".")
				and held.codec is not None
				for _, held in rows)
			if wanted:
				rows.append((name, placement))

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
	schema_enums = {decl.name: decl for decl in schema.enums()}

	varint_decls = {decl.name: decl for decl in schema.decls
	                if isinstance(decl, ast.VarintDecl)}

	codec_decls = {decl.name: decl for decl in schema.decls
	               if isinstance(decl, ast.CodecDecl)}

	consts = {decl.name: decl.value.value
	          for decl in schema.decls
	          if isinstance(decl, ast.ConstDecl)
	          and isinstance(decl.value, ast.IntLiteral)}

	# -- the bytecode, first, because a placement record points into it --
	program = Program()
	code_at: dict[str, int] = {}
	for owner, placement in rows:
		field = members.get(placement.path)
		expr: ast.Expr | None
		if isinstance(field, ast.Opaque):
			expr = field.size
		elif isinstance(field, (ast.Field, ast.Reserved)) and field.array:
			expr = field.array.size
		else:
			expr = None
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

	# A coded or sealed region's extent is its interior's extent put through
	# the codec's expansion (13.5), and nothing in the region's own bytes can
	# say how many there are -- they are the transform's output. The rule is
	# `traverse.region_extent`, which the C backend already asks, so this
	# renders the same arithmetic into bytecode rather than forming a second
	# opinion about `ratio_padded`'s rounding.
	#
	# Without it a walk gave the region a length of zero and placed
	# everything after it on top of it: `coded_run.trailer` came out at byte
	# 1 where C puts it at byte 7, and the struct had to defer `validate`
	# entirely rather than answer wrongly.
	#
	# None where the expansion has no closed form -- a bounded ratio, an
	# unbounded one, or a run inside the region, whose elements would have to
	# be read out of ciphertext to be measured. Those still defer, and so
	# does every backend.
	for owner, placement in rows:
		# The region itself, not its interior. A member inside one carries
		# the same `codec`, so asking that alone measured `content`'s own
		# (empty) interior through the expansion and recorded the answer
		# under the interior's name -- a program that returned zero for a
		# member whose length is `n`.
		if placement.kind not in ("coded", "sealed"):
			continue
		# A region that ends at a delimiter is measured by the scan, not by
		# the expansion, and the delimiter table already carries it. smtp's
		# DATA body is both at once -- stuffed *and* terminated -- and the
		# scan runs on the encoded bytes precisely because the stuffing is
		# what protects the terminator. Asking the expansion there reported
		# an expression the image had failed to carry, when the length was
		# never going to come from arithmetic.
		if placement.delimiter is not None:
			continue
		rule = traverse.region_extent(resolved.structs[owner], placement,
		                              codec_decls.get(placement.codec or ""),
		                              resolved.structs)
		if rule is None:
			coverage.unencodable[placement.path] = \
				"the codec's expansion has no closed form"
			continue
		start = len(program.code)
		program.emit(Op.PUSH, rule.constant, "<q")
		missing = next((inside.path for inside in rule.variable
		                if inside.path not in placement_index), None)
		if missing is not None:
			# Said out loud rather than skipped. The first version of this
			# dropped the program and recorded nothing, so the region kept
			# measuring zero and the coverage report called it encoded.
			del program.code[start:]
			coverage.unencodable[placement.path] = \
				f"the interior member `{missing}` is not in the table"
			continue
		for inside in rule.variable:
			program.emit(Op.SIZE, placement_index[inside.path])
			program.emit(Op.ADD)
		if True:
			if rule.kind == "add":
				program.emit(Op.PUSH, rule.add, "<q")
				program.emit(Op.ADD)
			elif rule.kind == "ratio":
				program.emit(Op.PUSH, rule.out, "<q")
				program.emit(Op.MUL)
				program.emit(Op.PUSH, rule.into, "<q")
				program.emit(Op.DIV)
			elif rule.kind == "padded":
				# A partial group still costs a whole one, so this is a
				# ceiling division and not a division: `(x + g - 1) / g * go`.
				program.emit(Op.PUSH, rule.group_in - 1, "<q")
				program.emit(Op.ADD)
				program.emit(Op.PUSH, rule.group_in, "<q")
				program.emit(Op.DIV)
				program.emit(Op.PUSH, rule.group_out, "<q")
				program.emit(Op.MUL)
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
	picker: tuple[tuple[dict[str, int],
	                    Callable[[ast.Field | ast.Opaque | ast.Reserved],
	                             ast.Expr | None],
	                    bool], ...] = (
		# Both are `Field` properties; an `Opaque` has neither, so it is
		# asked for neither.
		(located_at, lambda f: getattr(f, "located", None), False),
		(repeat_at, lambda f: (f.repeat.predicate
		                       if isinstance(f, ast.Field) and f.repeat
		                       else None), True),
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

	# `validate` is the one probe that cannot be rendered by halves: a
	# partial one answers OK where the schema says no. So the constraints go
	# in, and a struct is flagged only where *every* check `traverse` says
	# it makes is one this image carries -- the same taxonomy the C backend
	# emits from, rather than a second opinion about what a check is.
	constraints_blob = bytearray()
	enum_blob        = bytearray()
	enum_ids: dict[str, int] = {}
	# A nested member is `validate` called through, so the parent can answer
	# exactly when the child can. That is a fixed point rather than one
	# pass: a struct three deep settles only after the one below it does, so
	# iterate until nothing changes. Bounded by the number of structs, since
	# each round can only ever turn a flag off.
	# Which member names each struct's version, by struct *name*, so the
	# loop below can ask before the version table is built by index.
	version_of: dict[str, int | None] = {}
	for name, rstruct in order:
		version_member = next((entry.placement.version_field
		                       for entry in traverse.own_entries(rstruct)
		                       if entry.placement.version_field), None)
		version_of[name] = (
			None if version_member is None
			else placement_index.get(f"{name}.{version_member}"))

	validatable: dict[str, bool] = {name: True for name, _ in order}
	nests: dict[str, set[str]] = {}
	for name, rstruct in order:
		whole = True
		for entry in traverse.own_entries(rstruct):
			placement = entry.placement
			at = placement_index.get(placement.path)
			kind = traverse.classify_check(rstruct, placement, set(resolved.structs))
			# A coded region's extent is its interior through the codec's
			# expansion. The image carries that now, as a size program on
			# the region -- but only where the expansion has a closed form.
			# A bounded ratio, an unbounded one, or a run inside the region
			# leaves the length genuinely unknowable without decoding, so a
			# walk would place what follows optimistically where C's
			# accessor refuses. Those still defer, and so does every
			# backend.
			if placement.kind in ("coded", "sealed") \
					and placement.path not in code_at \
					and placement.delimiter is None:
				whole = False
				continue
			# A `[since]` member is checked only where the message's own
			# version admits it, and the image now names the member that
			# says so. Without one there is nothing to ask -- checking
			# unconditionally said CONSTRAINT for a v1 message carrying
			# none of the fields a v3 one would -- so a `[since]` member
			# in a struct with no version field still defers.
			if placement.since is not None \
					and version_of.get(name) is None:
				whole = False
				continue
			if kind is traverse.Check.NOTHING or at is None:
				continue
			if kind is traverse.Check.RESERVED:
				# Four policies (8.8), and only two are checks.
				# `preserve` says copy the bits, `unknown` says they mean
				# nothing this schema knows -- so nothing is checked and
				# nothing is promised. Emitting `must_be_zero` for those
				# said CONSTRAINT where every backend said OK.
				policies = {a.name for a in placement.attrs}
				if policies & {"preserve", "unknown"}:
					continue
				# `must_be_one` means every bit, not the number one: C
				# compares a reserved `u4` against 0xF. The width is the
				# packer's to know, so the expected value is carried rather
				# than derived -- a walk that read the check kind and
				# assumed 1 passed exactly the messages the schema refuses,
				# and only for a field narrower than eight bits.
				if "must_be_one" in policies:
					if placement.size_bits is None \
							or not 0 < placement.size_bits <= 63:
						whole = False
						continue
					constraints_blob += _struct.pack(
						"<IqBxxx", at, (1 << placement.size_bits) - 1, 4)
					continue
				constraints_blob += _struct.pack("<IqBxxx", at, 0, 3)
				continue
			# A fixed-width text number, whose digits are a check of their
			# own and whose spelling is another. Then whatever `CONSTRAINED`
			# would have asked of it, which is the half both forms of the
			# construct were losing: `classify_check` called this an array,
			# so cpio's `[min = 70701, max = 70702]` reached no backend and
			# no image. The two records are packed in C's order -- the
			# spelling, then the number -- because the order is the answer
			# and not just the verdict.
			if kind is traverse.Check.TEXT_NUMBER:
				if placement.scalar is None or placement.radix is None:
					whole = False
					continue
				if placement.radix_minimal:
					constraints_blob += _struct.pack(
						"<IqBxxx", at, placement.radix, 10)
				constraints_blob += _struct.pack(
					"<IqBxxx", at, (1 << placement.scalar.bits) - 1, 9)

			if kind in (traverse.Check.CONSTRAINED,
			            traverse.Check.TEXT_NUMBER):
				for attr in placement.attrs:
					code = {"must_eq": 0, "min": 1, "max": 2}.get(attr.name)
					if code is None or attr.value is None:
						continue
					# Folded rather than read off the literal.
					# `[must_eq = hardware_type.ethernet]` and
					# `[must_eq = PROTOCOL_VERSION]` are an enum member and
					# a const, and reading `.value` off either gives None --
					# so arp and telemetry deferred `validate` over a bound
					# the compiler had already resolved. `situc.expr` is
					# what the C backend folds it with, and
					# `resolved.layout.env` is the environment it uses, so
					# this is the same answer rather than a second one.
					try:
						held = evaluate(attr.value, resolved.layout.env)
					except SituError:
						whole = False
						continue
					constraints_blob += _struct.pack(
						"<IqBxxx", at, int(held), code)
				held_enum = schema_enums.get(placement.type_name or "")
				# Only where the enum *rejects* an unknown value.
				# `default = pass` admits one by design (section 8.7), so
				# `validate` emits no membership check and a walker that
				# made one said CONSTRAINT where every backend said OK --
				# `image_section_tag` is exactly that, and it is `pass` so
				# that a walker can read an image from a later situc.
				# An unstated default *is* `error`: section 8.7 makes
				# rejecting an unknown value "the default default, and
				# deliberately so". Reading `None` as `pass` emitted no
				# membership check for every enum in the tree that does not
				# spell one out -- which is most of them -- and answered OK
				# where four backends answered CONSTRAINT.
				if held_enum is not None \
						and held_enum.default is ast.EnumDefault.PASS:
					held_enum = None
				if held_enum is not None:
					if held_enum.name not in enum_ids:
						enum_ids[held_enum.name] = len(enum_ids)
						for member in held_enum.members:
							named_value = getattr(member.value, "value", None)
							if named_value is None:
								whole = False
								continue
							enum_blob += _struct.pack(
								"<IqI", enum_ids[held_enum.name],
								int(named_value), 0)
					constraints_blob += _struct.pack(
						"<IqBxxx", at, enum_ids[held_enum.name], 5)
				continue
			if kind is traverse.Check.REPEATED:
				# A run is only a *check* where it carries one. 87 of the
				# 106 in this tree carry none -- `ipv4_address`'s octets
				# validate to `return SITU_OK;` and nothing else -- so
				# deferring every struct holding an array gave up sixty of
				# them for a check that mostly is not there. What does
				# check is an encoding, a nul terminator, or digits.
				# A run the *message* sizes carries one check -- that the
				# declared length fits the frame -- and the walk derives
				# that from the layout it already has. What it cannot
				# derive is an encoding, a nul terminator, or digits.
				# A fixed-width text number carries no check at all, which
				# is worth stating because it looks like it should carry
				# the same two a delimited one does. cpio's whole header is
				# ASCII octal with `[min]` and `[max]` on two of its
				# fields, and `situ_cpio_header_validate` is `return
				# SITU_OK;` -- the digits are parsed where they are read and
				# nowhere else. Deferring over the radix gave up cpio
				# entirely for a check no backend makes.
				#
				# `nul_terminated` and `encoding` do carry one, but only
				# where the member has a static offset and a declared
				# count: the check names the bytes it scans, and a run the
				# message sizes has neither. That is C's own condition, so
				# cpio's `name[header.namesize] [nul_terminated]` is
				# unchecked and `edges`' `name[16]` is not.
				# A reserved *run*: every byte zero, over a length the
				# message computes. The scalar form is `must_be_zero` and
				# is a value comparison; this one cannot be, which is why
				# it is a kind of its own. `preserve` and `unknown` say
				# nothing is checked, as they do for a scalar.
				if placement.kind == "reserved":
					policies = {a.name for a in placement.attrs}
					if policies & {"preserve", "unknown"}:
						continue
					if "must_be_one" in policies:
						whole = False	# no run of ones in the tree yet
						continue
					constraints_blob += _struct.pack("<IqBxxx", at, 0, 13)
					continue

				# `nul_terminated` and `encoding` carry a check only where
				# the member has a static offset and a declared count: the
				# check names the bytes it scans, and a run the message
				# sizes has neither. That is C's own condition, so cpio's
				# `name[header.namesize] [nul_terminated]` is unchecked and
				# `edges`' `name[16]` is not.
				text_attrs = {a.name for a in placement.attrs}
				if text_attrs & {"encoding", "nul_terminated"} \
						and placement.offset_bits is not None \
						and placement.array_count:
					if "nul_terminated" in text_attrs:
						constraints_blob += _struct.pack(
							"<IqBxxx", at, 0, 11)
					encoding = next(
						(a for a in placement.attrs
						 if a.name == "encoding"), None)
					if encoding is not None:
						spelling = getattr(encoding.value, "name", None)
						if spelling not in ("ascii", "utf8"):
							whole = False
							continue
						constraints_blob += _struct.pack(
							"<IqBxxx", at,
							0 if spelling == "ascii" else 1, 12)
					continue
				# A length the message declares has to fit the frame, and
				# that is the only check a plain run carries. `remaining`
				# is what is left by definition and a `while` run stops at
				# the frame, so neither is checked -- asking anyway said
				# BOUNDS for netlink and ipv6ext where C said OK.
				#
				# Asked of the AST rather than of `sized_by`, which holds a
				# path and holds nothing for `payload[length - 8]`: that is
				# a length the message declares whatever arithmetic is
				# wrapped round it, and reading `sized_by` missed it.
				field = members.get(placement.path)
				sized = (field.array.size
				         if isinstance(field, ast.Field) and field.array
				         else None)
				if sized is not None and isinstance(field, ast.Field) \
						and not isinstance(sized, ast.Remaining) \
						and field.repeat is None:
					constraints_blob += _struct.pack("<IqBxxx", at, 0, 6)
				continue
			if kind is traverse.Check.DELIMITED:
				# The delimiter has to be there, and for a plain delimited
				# member that is the whole of it.
				#
				# `[encoding = ascii]` adds nothing here, which is worth
				# saying because it looks like it should. The check needs a
				# static offset and a declared count to name the bytes it
				# would scan, and a member that runs to a delimiter has
				# neither -- so no backend emits one, and `http`'s
				# `method[] until " " [encoding = ascii]` is checked for its
				# terminator and not for its characters. Deferring over it
				# gave up six structs for a check nothing makes.
				#
				# A text number carries two more. Its bytes have to parse
				# in its radix and fit the scalar's domain -- C calls the
				# getter and returns whatever it returns, which for a byte
				# that is not a digit, or a value too large, is
				# CONSTRAINT -- and `[minimal]` forbids a second spelling
				# of a number that already has one.
				if placement.radix is not None and placement.scalar is None:
					whole = False
					continue

				# 8.6.3 again: a run of records ends where the terminator
				# stands in for a record, and is not a member that ends at
				# a delimiter. Asking a run whether it was terminated said
				# CONSTRAINT for `kv_block` where C said OK.
				if placement.type_name in resolved.structs:
					continue
				constraints_blob += _struct.pack("<IqBxxx", at, 0, 7)

				# The encoding, over the content the scan found. All four
				# backends check this now; until they did, `[encoding =
				# ascii]` on a delimited member was a claim the schema made
				# and nothing tested.
				spelled = next((a for a in placement.attrs
				                if a.name == "encoding"), None)
				if spelled is not None:
					how = getattr(spelled.value, "name", None)
					if how not in ("ascii", "utf8"):
						whole = False
						continue
					constraints_blob += _struct.pack(
						"<IqBxxx", at, 0 if how == "ascii" else 1, 12)

				# In C's order, which is the answer and not just the
				# verdict: the terminator first, then the spelling, then
				# the parse. A field of non-digits that also runs off the
				# end is CONSTRAINT for the terminator, and reordering
				# these would answer for a different reason.
				if placement.radix is not None \
						and placement.scalar is not None:
					if placement.radix_minimal:
						constraints_blob += _struct.pack(
							"<IqBxxx", at, placement.radix, 10)
					constraints_blob += _struct.pack(
						"<IqBxxx", at,
						(1 << placement.scalar.bits) - 1, 9)
					# ...and then whatever the schema declared about the
					# number. Every backend emits these after the parse now,
					# and none of them did before: a delimited text number's
					# `[min]` and `[max]` were dropped by the same early
					# return in four backends, so `999` passed `[min = 200,
					# max = 599]` everywhere including here.
					for attr in placement.attrs:
						code = {"must_eq": 0, "min": 1,
						        "max": 2}.get(attr.name)
						if code is None or attr.value is None:
							continue
						try:
							held = evaluate(attr.value, resolved.layout.env)
						except SituError:
							whole = False
							continue
						constraints_blob += _struct.pack(
							"<IqBxxx", at, int(held), code)
				continue
			if kind is traverse.Check.DISCRIMINANT:
				# The only permissive shape is a `default:` arm that
				# *selects a member*: netlink's `default: opaque rest[...]`
				# takes any discriminant and hands back the bytes, so there
				# is nothing to refuse.
				#
				# Everything else refuses, including a variant that writes
				# no default clause at all. 14.5 makes `error` the default
				# default, so its absence is a rejection rather than a
				# permission -- and reading the absence the other way
				# deferred `validate` for every variant in the tree that
				# simply listed its cases. `edges`' `equalized` and
				# `arm_run` and icmp's `icmp_message` are all that shape,
				# and all three of C's validators emit the check.
				#
				# This is the same mistake 26.89 records for an *enum*
				# whose default is unstated, one construct along, and it
				# was found the same way: by asking what the four backends
				# emit rather than what the AST happens to contain.
				if any(arm.value is None and arm.member is not None
				       for arm in placement.arm_cases):
					continue
				constraints_blob += _struct.pack("<IqBxxx", at, 0, 8)
				# A struct-typed arm runs its own `validate` when it is
				# the one selected, so this struct can answer only when
				# each of those can. They join the fixed point below.
				for arm in placement.arm_cases:
					held_at = placement_index.get(arm.member or "")
					if held_at is None:
						continue
					typed = rows[held_at][1].type_name
					if typed and typed in resolved.structs \
							and _measurable(resolved,
							                resolved.structs[typed]):
						nests.setdefault(name, set()).add(typed)
				continue
			if kind is traverse.Check.NESTED and placement.type_name:
				nests.setdefault(name, set()).add(placement.type_name)
				continue
			whole = False		# a check this image does not carry yet
		validatable[name] = whole

	for _ in range(len(order) + 1):
		changed = False
		for name, _rstruct in order:
			if not validatable[name]:
				continue
			for inner in nests.get(name, ()):
				if not validatable.get(inner, False):
					validatable[name] = False
					changed = True
					break
		if not changed:
			break

	structs_blob = bytearray()
	for (name, rstruct), (first, count) in zip(order, spans):
		size = (rstruct.layout.size_bits
		        if rstruct.layout.is_fixed_size else None)
		flags = 1 if validatable.get(name) else 0
		if _measurable(resolved, rstruct):
			flags |= 2
		structs_blob += _struct.pack("<IIII", first, count, _u32(size),
		                             flags)

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
	markers_blob = bytearray()

	# The `little` constant a marker is compared against. A walker reads the
	# field big-endian and asks whether it equals this; without it the marker
	# is a number with no question attached.
	marker_decls = {decl.name: decl for decl in schema.decls
	                if isinstance(decl, ast.EndianMarkerDecl)}

	for at, (owner, placement) in enumerate(rows):
		if placement.kind == "marker":
			marker_decl = marker_decls.get(placement.type_name or "")
			little = getattr(getattr(marker_decl, "little", None), "value", None)
			if little is not None:
				markers_blob += _struct.pack("<IqI", at, int(little), 0)
		if placement.varint is not None:
			varint_index.setdefault(placement.varint, len(varint_index))
			varint_decl = varint_decls.get(placement.varint)
			big  = (varint_decl is not None
			        and varint_decl.encoding is ast.VarintEncoding.BE128)
			varints_blob += _struct.pack(
				"<IIBBBx", at, strings.intern(placement.varint),
				min(varint_decl.max_bytes if varint_decl else 10, 255),
				min(varint_decl.terminal_bits if varint_decl else 7, 255),
				(1 if placement.varint_minimal else 0) | (2 if big else 0))
		# The discriminant is what makes an arm answerable: without it a
		# walker has the cases and no way to say which one this message
		# selected, which is the whole question the probe asks.
		selects = resolve_path(placement.discriminant, owner) \
			if placement.discriminant else None
		for arm in placement.arm_cases:
			value, chosen, arm_kind = _arm_fields(arm)
			arms_blob += _struct.pack(
				"<IIqIB3x", at, _u32(placement_index.get(chosen or "")),
				value, _u32(selects), arm_kind)
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
			# `region_at`, not `owner`: this loop already binds `owner` to
			# the struct's name, and the shadow made the index a string to
			# every reader including mypy.
			region_at = next(
				(i for i, (_, held) in enumerate(rows)
				 if held.path.endswith("." + placement.regions[-1])
				 and held.regions
				 and held.regions[-1] == placement.regions[-1]
				 and held.path.count(".") <= placement.path.count(".")),
				None)
			regions_blob += _struct.pack(
				"<IIIB3x", at, _u32(region_at),
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

	# Which member carries each struct's version. `Placement.version_field`
	# already names it -- the layout resolves `[version = ver]` onto every
	# member of the struct -- so this is the name turned into an index, not a
	# second reading of the attribute.
	versions_blob = bytearray()
	for shape, (name, _rstruct) in enumerate(order):
		carries = version_of.get(name)
		if carries is not None:
			versions_blob += _struct.pack("<II", shape, carries)

	# -- cross-message relations (26.95) --------------------------------
	#
	# Emitted after every other program so that appending to `program.code`
	# here cannot move an offset another section already recorded. The
	# struct ids are positions in `order`, which is what every other
	# reference into the struct table already means.
	shape_of  = {name: index for index, (name, _) in enumerate(order)}
	relations_blob  = bytearray()
	musts_blob      = bytearray()

	for decl in schema.relations():
		params = {param.name: param for param in decl.params}
		shapes = [shape_of.get(param.type_name) for param in decl.params]
		if any(shape is None for shape in shapes):
			coverage.unencodable[f"relation {decl.name}"] = \
				"a parameter has no struct in this image"
			continue

		def resolve_arg(path: str,
		                params: dict[str, ast.RelationParam] = params
		                ) -> tuple[int, int] | None:
			head, _, rest = path.partition(".")
			param = params.get(head)
			if param is None or not rest:
				return None
			index = resolve_in(param.type_name, rest.split("."))
			if index is None:
				return None
			return (list(params).index(head), index)

		# `compile_relation` trusts `situc.relation` to have refused
		# everything it cannot emit, and that stopped being true when a
		# relation learned to compare arrays: the program would carry a
		# scalar read of a run, which the walker refuses at evaluation time
		# rather than here. The image says what it can answer, so a relation
		# it cannot is recorded as unencodable instead of encoded wrongly.
		#
		# The four compiled backends emit it. Teaching the image would mean a
		# new opcode in three implementations -- the packer, `walker/vm.py`
		# and `walker/c/situ_walk.c` -- with the drift test that ties them
		# together, which is its own piece of work rather than a line here.
		try:
			if any(one.bytes_equal is not None
			       for one in plan_relation(decl, resolved)):
				coverage.unencodable[f"relation {decl.name}"] = (
					"compares arrays, and the image's expression VM reads "
					"scalars")
				continue
		except RelationRefused:
			# Not expressible at all; the backends have said so already and
			# this is not the place to repeat the reason.
			continue

		first = len(musts_blob) // RELATION_MUST_BYTES
		try:
			for must in decl.body:
				at = len(program.code)
				program.compile_relation(must.expr, resolve_arg)
				program.emit(Op.END)
				musts_blob += _struct.pack("<II", at, 0)
		except PackError as why:
			coverage.unencodable[f"relation {decl.name}"] = str(why)
			del musts_blob[first * RELATION_MUST_BYTES:]
			continue

		relations_blob += _struct.pack(
			"<IIIIII", strings.intern(decl.name), shapes[0] or 0,
			shapes[1] or 0, first, len(decl.body), 0)
		coverage.relations += 1

	sections.append((SECTION_STRUCTS, bytes(structs_blob), STRUCT_BYTES))
	sections.append((SECTION_PLACEMENTS, b"", PLACEMENT_BYTES))	# filled below
	sections.append((SECTION_CODE, bytes(program.code), 1))
	for section, blob, stride in (
			(SECTION_ARMS, arms_blob, ARM_BYTES),
			(SECTION_DELIMITERS, delims_blob, DELIMITER_BYTES),
			(SECTION_REGIONS, regions_blob, REGION_BYTES),
			(SECTION_CODECS, codecs_blob, CODEC_BYTES),
			(SECTION_VARINTS, varints_blob, VARINT_BYTES),
			(SECTION_TLVS, tlvs_blob, TLV_BYTES),
			(SECTION_INDEXES, index_blob, INDEX_BYTES),
			(SECTION_MARKERS, markers_blob, MARKER_BYTES),
			(SECTION_CONSTRAINTS, constraints_blob, CONSTRAINT_BYTES),
			(SECTION_ENUM_VALUES, enum_blob, ENUM_VALUE_BYTES),
			(SECTION_VERSIONS, versions_blob, VERSION_BYTES),
			(SECTION_RELATIONS, relations_blob, RELATION_BYTES),
			(SECTION_RELATION_MUSTS, musts_blob, RELATION_MUST_BYTES)):
		if blob:
			sections.append((section, bytes(blob), stride))

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
		if placement.tag_covers:
			flags |= IS_TAG
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
			min(placement.repeat_cap or 0, 0xFFFF),
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
