"""AST node definitions.

The AST is the single source of truth (project.md section 25): it is built once
from the source text and every later pass reads it. Nodes carry spans because
every diagnostic has to point at source.

Nodes hold what the author wrote, not what it means. Resolution of type names,
evaluation of expressions and computation of layout all happen in later passes
against this tree, never by re-parsing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from situc.diagnostics import Span
from situc.types import ScalarType


class Node:
	"""Base for every node. Subclasses are dataclasses carrying a `span`."""

	span: Span


# ---------------------------------------------------------------------------
# Expressions (project.md section 10)
#
# Parsed here, evaluated in phase 2 (constants and pins) and phase 5 (interval
# arithmetic over field references).
# ---------------------------------------------------------------------------


class Expr(Node):
	pass


@dataclass(frozen=True)
class IntLiteral(Expr):
	span: Span
	value: int
	text: str


@dataclass(frozen=True)
class StringLiteral(Expr):
	span: Span
	value: str


@dataclass(frozen=True)
class NameRef(Expr):
	"""A bare identifier."""

	span: Span
	name: str


@dataclass(frozen=True)
class Access(Expr):
	"""A dotted step: `hdr.length`, `MsgType.hello`, `recs[].value`.

	A path is a chain of these rather than a list of names, because an index can
	appear in the middle of one: `Message.recs[].value` is a capability path the
	requirement predicates of section 16 have to name.
	"""

	span: Span
	base: Expr
	name: str


@dataclass(frozen=True)
class Remaining(Expr):
	"""`remaining`: to the end of the enclosing frame."""

	span: Span


@dataclass(frozen=True)
class Call(Expr):
	"""`size(X)`, `offset(X)`, `align_up(x, n)`, and the capability predicates.

	The expression language has no user-defined functions (section 10); this is
	the fixed builtin set, and which names are legal is decided by the pass that
	evaluates the call, not here.
	"""

	span: Span
	name: str
	args: tuple[Expr, ...]


@dataclass(frozen=True)
class Unary(Expr):
	span: Span
	op: str
	operand: Expr


@dataclass(frozen=True)
class Binary(Expr):
	span: Span
	op: str
	left: Expr
	right: Expr


@dataclass(frozen=True)
class Index(Expr):
	"""`recs[]` or `recs[3]`: an element reference inside a capability path."""

	span: Span
	base: Expr
	index: Expr | None


# ---------------------------------------------------------------------------
# Shared pieces
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Attr(Node):
	"""One entry of an attribute list: a bare flag or `key = value`."""

	span: Span
	name: str
	value: Expr | None = None


@dataclass(frozen=True)
class ArraySpec(Node):
	"""`[N]`, `[expr]`, `[remaining]` or `[]`.

	An absent `size` is the empty form `[]`, legal only where a count comes from
	elsewhere (an `indexed` region, phase 6).
	"""

	span: Span
	size: Expr | None


@dataclass(frozen=True)
class TypeRef(Node):
	"""A scalar type or a named user type.

	`scalar` is set when the name resolved in the scalar table; otherwise this
	is a reference to a struct or enum declared elsewhere, resolved in a later
	pass.
	"""

	span: Span
	name: str
	scalar: ScalarType | None = None

	@property
	def is_scalar(self) -> bool:
		return self.scalar is not None


# ---------------------------------------------------------------------------
# Struct members
# ---------------------------------------------------------------------------


class Member(Node):
	pass


@dataclass(frozen=True)
class Field(Member):
	span: Span
	name: str
	type_ref: TypeRef
	array: ArraySpec | None		= None
	pin: Expr | None		= None
	attrs: tuple[Attr, ...]		= ()


@dataclass(frozen=True)
class Reserved(Member):
	"""`reserved u3 [must_be_zero];` -- a first-class declaration, not an
	annotation on an unnamed field (section 8.8)."""

	span: Span
	type_ref: TypeRef
	array: ArraySpec | None		= None
	attrs: tuple[Attr, ...]		= ()


@dataclass(frozen=True)
class PositionalBlock(Member):
	"""`positional { ... }`: a locally-checked staticness guarantee.

	Redundant by default, since positional layout is the default. It exists so
	the compiler can defend a region the author wants to stay static
	(section 9.2).
	"""

	span: Span
	members: tuple[Member, ...]


# ---------------------------------------------------------------------------
# Declarations
# ---------------------------------------------------------------------------


class Decl(Node):
	pass


class TargetKind(Enum):
	BUFFER	= "buffer"
	MMIO	= "mmio"


class Endian(Enum):
	BIG	= "big"
	LITTLE	= "little"
	NATIVE	= "native"


class BitOrder(Enum):
	MSB_FIRST = "msb_first"
	LSB_FIRST = "lsb_first"


@dataclass(frozen=True)
class TargetDirective(Decl):
	span: Span
	kind: TargetKind


@dataclass(frozen=True)
class EndianDirective(Decl):
	span: Span
	endian: Endian


@dataclass(frozen=True)
class BitOrderDirective(Decl):
	span: Span
	bit_order: BitOrder


@dataclass(frozen=True)
class ImportDirective(Decl):
	span: Span
	path: str


@dataclass(frozen=True)
class ConstDecl(Decl):
	span: Span
	name: str
	value: Expr


class EnumDefault(Enum):
	ERROR	= "error"
	PASS	= "pass"


@dataclass(frozen=True)
class EnumMember(Node):
	span: Span
	name: str
	value: Expr


@dataclass(frozen=True)
class EnumDecl(Decl):
	span: Span
	name: str
	backing: TypeRef
	members: tuple[EnumMember, ...]
	default: EnumDefault | None = None

	@property
	def effective_default(self) -> EnumDefault:
		"""Unknown values are rejected unless the schema says otherwise
		(section 8.7): the safe option is the silent one."""
		return self.default or EnumDefault.ERROR


@dataclass(frozen=True)
class EndianMarkerDecl(Decl):
	"""A byte-order marker: the TIFF `II`/`MM` pattern (section 8.3).

	Distinct from `endian native`, and deliberately so. Host order with no
	marker is non-canonical because the encoding depends on the machine.
	A marker travels with the data, so exactly one encoding is valid once the
	marker is known -- and endianness never changes extent, so this costs
	nothing on the offset or size axes.
	"""

	span: Span
	name: str
	backing: TypeRef
	little: Expr
	big: Expr


@dataclass(frozen=True)
class MarkerField(Member):
	"""`endian_marker byte_order;` -- the marker's own storage in a struct."""

	span: Span
	name: str
	attrs: tuple[Attr, ...] = ()


@dataclass(frozen=True)
class StructDecl(Decl):
	span: Span
	name: str
	members: tuple[Member, ...]
	attrs: tuple[Attr, ...] = ()


class RequirementKind(Enum):
	REQUIRE	= "require"	# build-time gate; failure is an error
	ASSERT	= "assert"	# same check; failure is a warning


@dataclass(frozen=True)
class Requirement(Decl):
	span: Span
	kind: RequirementKind
	expr: Expr


@dataclass
class Schema(Node):
	"""One parsed source file."""

	span: Span
	decls: list[Decl] = field(default_factory=list)

	def structs(self) -> list[StructDecl]:
		return [decl for decl in self.decls if isinstance(decl, StructDecl)]

	def enums(self) -> list[EnumDecl]:
		return [decl for decl in self.decls if isinstance(decl, EnumDecl)]

	def consts(self) -> list[ConstDecl]:
		return [decl for decl in self.decls if isinstance(decl, ConstDecl)]

	def requirements(self) -> list[Requirement]:
		return [decl for decl in self.decls if isinstance(decl, Requirement)]

	def markers(self) -> list[EndianMarkerDecl]:
		return [decl for decl in self.decls if isinstance(decl, EndianMarkerDecl)]
