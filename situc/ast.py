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
	# Verbatim source of a value the parser retains but does not interpret --
	# a `{ ... }` map of known tags, a `switch (...)` over wire types. Keeping
	# the text is what lets the AST stay a faithful record of the schema even
	# where a later phase owns the meaning.
	raw: str | None    = None


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
class VariantArm(Node):
	"""One `case` of a variant, or its `default`.

	`member` is None for `default: error;`, which rejects an unknown
	discriminant rather than accepting it -- the default default, and
	deliberately so (section 14.5).
	"""

	span: Span
	value: Expr | None		# None for the default arm
	member: Member | None
	is_error: bool			= False
	is_opaque: bool			= False

	@property
	def is_default(self) -> bool:
		return self.value is None


@dataclass(frozen=True)
class Variant(Member):
	"""A discriminated union selected by an already-parsed field (section 9.6).

	The discriminant must be parsed strictly before the variant in layout
	order; a forward reference is an error. Unless every arm is the same size,
	the variant makes everything after it dynamic -- which the advisor points
	out, along with the padding cost of equalizing them.
	"""

	span: Span
	name: str
	discriminant: Expr
	arms: tuple[VariantArm, ...]
	attrs: tuple[Attr, ...]	= ()

	def members_of(self) -> list[Member]:
		"""The members the arms declare, skipping the policy-only arms."""
		return [arm.member for arm in self.arms if arm.member is not None]

	@property
	def default_arm(self) -> VariantArm | None:
		for arm in self.arms:
			if arm.is_default:
				return arm
		return None


@dataclass(frozen=True)
class Opaque(Member):
	"""A region with a size but no interior schema (section 9.4).

	Deliberately collapses structural capability in exchange for flexibility:
	treat-as-bytes, whole-region replace if same size, no interior access. An
	opaque region can later gain structure via a stage transition, which is how
	sealed payloads work.
	"""

	span: Span
	name: str
	size: Expr
	attrs: tuple[Attr, ...] = ()


@dataclass(frozen=True)
class Indexed(Member):
	"""An offset table followed by elements, FlatBuffers style (section 9.3).

	Buys O(1) random access through one indirection, and elements that need not
	be fixed size. Insertion is not supported: the offsets would shift.
	"""

	span: Span
	name: str
	args: tuple[Attr, ...]
	members: tuple[Member, ...]

	def argument(self, name: str) -> Expr | None:
		for arg in self.args:
			if arg.name == name:
				return arg.value
		return None


class UnknownPolicy(Enum):
	ERROR    = "error"
	SKIP     = "skip"
	PRESERVE = "preserve"


class DuplicatePolicy(Enum):
	ERROR   = "error"
	ALLOWED = "allowed"


@dataclass(frozen=True)
class Tlv(Member):
	"""A schema-free region of tag-length-value items (section 9.5).

	Capabilities: sequential iteration, append if slack exists, lookup by tag
	O(n), no stable addressing across any mutation, item mutation in place only
	if same size.

	`unknown` and `duplicate_tags` both default to `error`, deliberately: an
	unknown tag or a repeated one is a malleability surface, and accepting one
	silently is what situ refuses to do (section 14.5).
	"""

	span: Span
	name: str
	args: tuple[Attr, ...]
	unknown: UnknownPolicy		= UnknownPolicy.ERROR
	duplicates: DuplicatePolicy	= DuplicatePolicy.ERROR
	ordered: bool			= False
	attrs: tuple[Attr, ...]		= ()
	# Wire types the `value_size` dispatch accepts. Protobuf's packed-versus-
	# unpacked ambiguity is visible here and nowhere else: a repeated scalar
	# may be written as several scalar items or as one length-prefixed one.
	wire_types: tuple[int, ...]	= ()

	def argument(self, name: str) -> Expr | None:
		for arg in self.args:
			if arg.name == name:
				return arg.value
		return None


@dataclass(frozen=True)
class Coded(Member):
	"""A region transformed by a codec, with an interior schema (section 13.5).

	The general form. `sealed` (phase 8) is this plus authentication: a codec
	over a region is a transform question, and encryption is one instance of it
	rather than the only one. See docs/decisions/0009-coded-regions.md.
	"""

	span: Span
	name: str
	codec: str
	args: tuple[Attr, ...]
	members: tuple[Member, ...]
	attrs: tuple[Attr, ...] = ()


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


class VarintEncoding(Enum):
	LEB128 = "leb128"


class VarintTransform(Enum):
	ZIGZAG = "zigzag"


@dataclass(frozen=True)
class VarintDecl(Decl):
	"""A variable-length integer type (section 8.1.1).

	Required rather than optional: describing protobuf is impossible without
	them. The capability consequences are severe and have to be reported
	clearly, because this is exactly the construct users reach for without
	understanding the cost.
	"""

	span: Span
	name: str
	encoding: VarintEncoding
	max_bits: int
	minimal: bool
	transform: VarintTransform | None = None

	@property
	def max_bytes(self) -> int:
		"""Worst-case encoded length: seven payload bits per byte."""
		return (self.max_bits + 6) // 7


class Seekable(Enum):
	"""The class of a codec's output-position function (section 13.2)."""

	LINEAR    = "linear"		# position is monotone in the input position
	PERMUTED  = "permuted"		# a bijection, but not monotone: interleavers
	BLOCKWISE = "blockwise"		# positions hold within a block
	NONE      = "none"


class Granularity(Enum):
	"""The minimum independently transformable unit."""

	BIT    = "bit"
	SYMBOL = "symbol"
	BYTE   = "byte"
	BLOCK  = "block"
	STREAM = "stream"


class Expansion(Enum):
	"""How output extent follows input extent."""

	PRESERVING     = "length_preserving"
	FIXED_ADD      = "add"			# expansion = +N
	RATIO_EXACT    = "ratio_exact"		# a:b exactly, so offsets stay linear
	RATIO_BOUNDED  = "ratio_bounded"	# worst case known, actual data-dependent
	UNBOUNDED      = "unbounded"


@dataclass(frozen=True)
class CodecDecl(Decl):
	"""A transform's property signature (section 13.2).

	The signature is the interface between both codec tiers and everything
	downstream: the capability lattice consumes property signatures and nothing
	else. That is what makes adding derived codecs in phase 12 purely additive,
	and what lets an implementation be swapped without the schema changing.

	A tier-1 signature is trusted and unverified, so it can lie. The capability
	map marks it `trusted` for exactly that reason, and `gen-codec-tests` emits
	the tests that would catch a lying one.
	"""

	span: Span
	name: str
	expansion: Expansion		= Expansion.PRESERVING
	expansion_add: int		= 0
	ratio: tuple[int, int] | None	= None
	seekable: Seekable		= Seekable.NONE
	granularity: Granularity	= Granularity.STREAM
	granularity_size: int | None	= None
	systematic: bool		= False
	authenticated: bool		= False
	invertible: bool		= False
	deterministic: bool		= False
	error_propagating: bool		= False
	has_kernel: bool		= False


class ImplKind(Enum):
	DERIVED = "derived"
	EXTERN  = "extern"


@dataclass(frozen=True)
class ImplDecl(Decl):
	"""Binds an implementation to a signature (section 13.1).

	Separate from the signature so a hand-tuned assembly routine, a DMA-driven
	hardware unit or a vendor library can replace the default without changing
	one byte of the capability map.
	"""

	span: Span
	codec: str
	kind: ImplKind
	symbol: str | None = None


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

	def varints(self) -> list[VarintDecl]:
		return [decl for decl in self.decls if isinstance(decl, VarintDecl)]

	def codecs(self) -> list[CodecDecl]:
		return [decl for decl in self.decls if isinstance(decl, CodecDecl)]

	def impls(self) -> list[ImplDecl]:
		return [decl for decl in self.decls if isinstance(decl, ImplDecl)]
