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
class Until(Node):
	"""`until "\\r\\n"` -- where a delimited member stops (section 8.6.1).

	The delimiter is bytes rather than text: a schema may frame on `0x00` as
	readily as on CRLF, and the lexer's string literal already carries either.

	`quoted` and `escape` are how a protocol says the delimiter may appear
	inside the content after all. Without one of them the content simply may
	not contain the delimiter, which is not a restriction situ invented --
	content that did would be unrepresentable, since writing it back would
	produce different framing. See doc/decision/0020-delimited-data.md.

	`cap` bounds the scan. A delimiter that is not there makes an unbounded
	read on a buffer whose end is the only thing stopping it, and an embedded
	caller usually wants the smaller promise.
	"""

	span: Span
	delimiter: bytes
	quoted: int | None	= None
	escape: int | None	= None
	cap: Expr | None	= None

	@property
	def is_relaxed(self) -> bool:
		"""Whether the delimiter may occur in the content.

		When it may, two byte sequences can encode one value and the field is
		NonCanonical. When it may not, the exclusion is enforced and the round
		trip is total.
		"""
		return self.quoted is not None or self.escape is not None


@dataclass(frozen=True)
class While(Node):
	"""`while (separator == 0x2D)` -- a run that ends after an element that
	fails a predicate (section 8.6.6).

	Different from `until` in the quantifier, which is the whole of it.
	`until` asks about the position *before* each element: is the terminator
	standing where an element would start. `while` asks about the element
	just read. Two real protocols wanted the second and neither could be
	written with the first: SMTP's multiline reply ends after the line whose
	separator is a space, and an IPv6 extension chain ends after the header
	whose `next_header` names an upper-layer protocol.

	The predicate reads the element's own fields, and nothing else. It cannot
	see the enclosing struct, because the enclosing struct's later members are
	placed *after* this run and asking about them would be circular.

	A `while` run is never empty: the first element is parsed before the
	predicate is evaluated. Whether the run is there at all is a different
	question, and a `variant` is what asks it.
	"""

	span: Span
	predicate: Expr
	cap: Expr | None = None


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
	until: Until | None		= None
	#: `T x[] while (cond)` -- a run ending after the element that fails it.
	repeat: While | None		= None
	#: `decimal u32 n until ":"` -- the value is written as digits rather than
	#: stored as bits. 10 or 16; None for an ordinary scalar. The scalar type
	#: gives the value's domain, not its width in the buffer, because a text
	#: number's width in the buffer depends on the number (section 8.6.2).
	radix: int | None		= None
	#: `u8 pixels[n] at hdr.pixel_offset` -- the member sits where a field
	#: says, measured from the start of the message rather than from the
	#: member before it. Distinct from `pin`, which asserts the offset the
	#: solver computed and never places anything.
	#:
	#: Such a member is a *reference* rather than a member in the ordinary
	#: sense: it contributes nothing to the enclosing struct's extent, and
	#: nothing is placed after it.
	located: Expr | None		= None


@dataclass(frozen=True)
class Reserved(Member):
	"""`reserved u3 [must_be_zero];` -- a first-class declaration, not an
	annotation on an unnamed field (section 8.8)."""

	span: Span
	type_ref: TypeRef
	array: ArraySpec | None		= None
	attrs: tuple[Attr, ...]		= ()
	until: Until | None		= None
	#: `preamble u8[4] = "WOZ2";` -- the bytes this run is pinned to (0052).
	#: A preamble is a reserved run whose content is stated rather than
	#: governed by a policy, so it shares every other property: anonymous,
	#: therefore no accessor, and checked on validate.
	#:
	#: `None` is an ordinary `reserved`. The two are one node because the
	#: only thing that differs is what the bytes must be, and a separate
	#: node would have duplicated placement, layout and every backend's
	#: no-accessor rule to say so.
	pinned: bytes | None		= None


@dataclass(frozen=True)
class Pad(Member):
	"""`pad_to(4);` -- explicit padding to the next multiple of n bytes,
	measured from the message base (section 8.4, decision 0043).

	Its size is the solver's, not the schema's: `align_up(offset, n) - offset`,
	a constant where the offset is static and a computed length where it is
	not. Padding is `must_be_zero` unless `[preserve]`, because a sender that
	varies it varies bytes the format calls fixed (8.8)."""

	span: Span
	to: int
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


class IndexBase(Enum):
	"""What an offset in the table is measured from (decision 0024).

	`REGION` is the default and the safe one: an offset that cannot mean
	anything outside the region is one the region's own extent bounds. The
	other two can name bytes anywhere in the message, so they are declared.
	"""

	#: From the start of the indexed region -- the table's own first byte.
	REGION  = "region"
	#: From the start of the message, which is what `at expr` means (9.8).
	MESSAGE = "message"
	#: From the start of a member declared before the region.
	MEMBER  = "member"


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
	#: What the offsets are measured from. `base` names it; absent means the
	#: region itself, which is the only choice that cannot reach outside it.
	base: IndexBase		= IndexBase.REGION
	#: The member `base` names, for `IndexBase.MEMBER` and nothing else.
	base_member: str | None	= None

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


# ---------------------------------------------------------------------------
# The item grammar of a `tlv` region (section 9.5)
#
# A tlv region says how to find its items: how to decode a raw tag into named
# parts, and how each part decides where the value ends. That is a grammar, and
# for a long time it was kept as the verbatim source text of three arguments,
# "interpreted by the pass that needs them" -- a pass nobody wrote. What read
# it instead was a hand-written walk in the C runtime with protobuf's `tag >> 3`
# and its four wire types baked in, which is a second description of the format
# the schema already describes. These nodes are the first description.
# ---------------------------------------------------------------------------


class ValueRule(Node):
	"""How one item's value extent is found, once its wire type is known."""


@dataclass(frozen=True)
class SelfDelimiting(ValueRule):
	"""`self_delimiting`: the value carries its own extent.

	Legal only for a type that does -- a varint, a nul-terminated run. Section
	9.5 says so and `wellformed` holds it to that, because a fixed-width type
	declared self-delimiting would make the walk read a length that is not
	there.
	"""

	span: Span


@dataclass(frozen=True)
class FixedValue(ValueRule):
	"""`8`: a literal byte count."""

	span: Span
	size: int


@dataclass(frozen=True)
class PrefixedValue(ValueRule):
	"""`prefixed(pb_varint)`: a length in that type, then that many bytes."""

	span: Span
	length_type: str


@dataclass(frozen=True)
class RejectValue(ValueRule):
	"""`error`: a wire type this schema does not describe.

	A rejection, not a gap. Protobuf's groups (wire types 3 and 4) are the
	worked example: the walk must stop rather than guess an extent.
	"""

	span: Span


@dataclass(frozen=True)
class ValueCase(Node):
	"""One arm of the `value_size` dispatch. A `label` of None is `default`."""

	span: Span
	label: int | None
	rule: ValueRule


@dataclass(frozen=True)
class ValueSize(Node):
	"""`value_size = switch (wire) { case 0: self_delimiting, ... }`

	`selector` names a part `tag_decode` produces, and nothing else: the
	dispatch happens after the tag is decoded and before the value is read, so
	the only thing in scope is what the tag decoded to.
	"""

	span: Span
	selector: str
	cases: tuple[ValueCase, ...]

	def default(self) -> ValueCase | None:
		return next((case for case in self.cases if case.label is None), None)


@dataclass(frozen=True)
class TagPart(Node):
	"""One named part decoded out of a raw tag: `field = tag >> 3`.

	`value` is an expression over `tag` alone. The parts are what the rest of
	the region's grammar may name.
	"""

	span: Span
	name: str
	value: Expr


@dataclass(frozen=True)
class KnownTag(Node):
	"""One entry of the `known` map: a tag number given a name and a type.

	Two forms, both from section 9.5. The simple one is `0x01 : Mtu` and gives
	only a name; the general one is
	`1 : { name = user_id, wire = 0, type = pb_varint }` and pins the wire type
	an item with this tag must carry.
	"""

	span: Span
	tag: int
	name: str
	wire: int | None       = None
	type_name: str | None  = None
	#: `type = u8[]` -- the value is a run of that type rather than one of it.
	repeated: bool         = False


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
	#
	# Derived from `value_size` rather than scanned for separately. It used to
	# be the only thing read out of the dispatch, by a pass that counted `case`
	# labels while skipping their bodies.
	wire_types: tuple[int, ...]	= ()
	#: How a raw tag decodes into named parts. Empty for the simple form,
	#: whose tag is the whole of the tag.
	tag_decode: tuple[TagPart, ...]	= ()
	#: How the value's extent is found. None for the simple form, which says
	#: it with `length_type` instead.
	value_size: ValueSize | None	= None
	#: The tags this schema names, in the order written.
	known: tuple[KnownTag, ...]	= ()
	#: `tag_identity = field`: which decoded part a `known` key matches.
	#: See doc/decision/0023-tlv-tag-identity.md. None where it was not
	#: written, which `identity_part` resolves and `wellformed` refuses where
	#: it cannot be.
	identity: str | None		= None

	def identity_part(self) -> str | None:
		"""The part a `known` key matches, or None for the raw tag.

		Declared where more than one part could be meant, inferred where only
		one could. Guessing is what this exists to avoid: matching on a wire
		type where a field number was meant returns the wrong item, and
		nothing about the message says so.
		"""
		if self.identity is not None:
			return self.identity
		if len(self.tag_decode) == 1:
			return self.tag_decode[0].name
		return None

	def part(self, name: str) -> TagPart | None:
		return next((part for part in self.tag_decode if part.name == name), None)

	def rule_for(self, wire: int) -> ValueRule | None:
		"""The dispatch arm an item with this wire type takes, `default` last."""
		for case in self.value_size.cases if self.value_size else ():
			if case.label == wire:
				return case.rule
		default = self.value_size.default() if self.value_size else None
		return default.rule if default else None

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
	rather than the only one. See doc/decision/0009-coded-regions.md.
	"""

	span: Span
	name: str
	codec: str
	args: tuple[Attr, ...]
	members: tuple[Member, ...]
	attrs: tuple[Attr, ...] = ()
	#: `coded pn(hp) covers(first) { u8 number[..]; }` -- regions *outside*
	#: this one whose bytes the transform also runs over, beyond the region's
	#: own extent (section 14.1a).
	#:
	#: Additive, not a replacement: the codec sees the union of this region
	#: and everything named here. Empty is the ordinary case and means the
	#: region covers only itself -- unlike a tag, where an empty `covers` is
	#: inference over every region in the struct. There is nothing to infer
	#: here, because a transform that reached beyond its own bytes without
	#: being told to would be a surprise rather than a default.
	#:
	#: Header protection is what this is for: QUIC masks the first byte and
	#: the packet number under one operation, and the two are not adjacent.
	covers: tuple[str, ...] = ()
	#: `coded body(dot_stuffing) until "\r\n.\r\n" { ... }` -- a region whose
	#: extent is found by scanning rather than computed from its interior.
	#:
	#: Scan first, then decode. That is the order the protocols that need this
	#: specify: SMTP's dot-stuffing protects the terminator, so the sequence
	#: is unambiguous in the *encoded* bytes and would not be in the decoded
	#: ones (section 13.6).
	until: Until | None = None


@dataclass(frozen=True)
class Authenticated(Member):
	"""`authenticated { ... }`: plaintext covered by a tag (section 14.1).

	The block transforms nothing: its members are laid out exactly where they
	would have been without it, and they stay in the enclosing struct's
	namespace, which is why 5.3 addresses `Packet.hdr.seq` and not
	`Packet.authenticated.hdr.seq`. All the block does is name a region a tag
	can cover, which is why it carries a name at all.
	"""

	span: Span
	name: str
	members: tuple[Member, ...]
	attrs: tuple[Attr, ...] = ()


@dataclass(frozen=True)
class Sealed(Member):
	"""`sealed(codec, nonce = ref) { ... }`: encrypted and covered.

	`coded` plus authentication, exactly as decision 0009 planned it: the
	transform half is shared with `Coded` down to the layout function, and this
	construct adds tag coverage and the `VerifyGated` stage of section 14.3.
	Unlike `authenticated`, the interior is a region with its own namespace --
	the bytes there are the codec's output, not the struct's.
	"""

	span: Span
	name: str
	codec: str
	args: tuple[Attr, ...]
	members: tuple[Member, ...]
	attrs: tuple[Attr, ...] = ()


class TagKind(Enum):
	TAG      = "tag"
	CHECKSUM = "checksum"


@dataclass(frozen=True)
class TagField(Member):
	"""`tag u8[16] covers(a, b);` (section 14.1).

	`checksum` is the same construct with a non-cryptographic algorithm: it
	shares the entire coverage and dirty-bit mechanism, because the question
	"which bytes does this field have to be recomputed for" does not depend on
	whether the answer is a MAC or a CRC.

	An empty `covers` is inference, not absence: every authenticated and sealed
	region in the enclosing struct, in declaration order.
	"""

	span: Span
	name: str
	type_ref: TypeRef
	array: ArraySpec
	covers: tuple[str, ...]		= ()
	kind: TagKind			= TagKind.TAG
	attrs: tuple[Attr, ...]		= ()
	#: `checksum u16 sum covers(hdr, payload) prefix(udp_pseudo);` -- a struct
	#: whose bytes the algorithm runs over *before* this message's, and which
	#: this message does not contain (section 14.2a).
	#:
	#: TCP's and UDP's checksums are the case. They cover a pseudo-header made
	#: of the source and destination addresses, the protocol number and the
	#: transport length -- two of which belong to the IP layer, which is why
	#: the kernel's `csum_tcpudp_nofold` takes `saddr` and `daddr` as
	#: arguments rather than reading them out of the datagram.
	#:
	#: A pseudo-header is a byte layout, which is what situ describes; what
	#: situ cannot do is fill one in from this message alone. So the clause
	#: names a declared struct and the generated code says how many bytes the
	#: caller has to hand over and in what shape. Computing the sum was
	#: already the caller's (14.1), so this widens *which bytes are covered*
	#: and nothing else.
	prefix: str | None		= None

	@property
	def infers_coverage(self) -> bool:
		return not self.covers


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
	FILE	= "file"


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
	#: `target file append`. Written rather than implied, because it is not
	#: true of files as such: it makes the top-level extent growable and turns
	#: every address `Unstable`, since a resize invalidates every outstanding
	#: pointer. Six of the seven file-format examples are not growable (0047).
	append: bool = False


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
class NamespaceDecl(Decl):
	"""`namespace outer { ... }` (doc/decision/0012-namespaces.md).

	A namespace scopes type names and nothing else. It is not a struct: a
	struct is a byte layout, so wrapping declarations in one would change the
	wire format, and the whole point here is to organise names without touching
	bytes.

	Flattened away immediately after parsing -- every declaration inside comes
	out with a qualified name, and no later pass learns that namespaces exist.
	"""

	span: Span
	name: str
	decls: list[Decl]


class Strictness(Enum):
	STRICT  = "strict"
	LENIENT = "lenient"


@dataclass(frozen=True)
class StrictnessDirective(Decl):
	"""`strictness = lenient;` (section 14.5).

	`strict` is the default and needs no directive; the whole point of the
	construct is that relaxing has to be written down. `lenient` sets
	`canonical = NonCanonical` for the schema, because accepting what the schema
	does not describe means more than one byte sequence encodes a value.
	"""

	span: Span
	strictness: Strictness


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
	#: Base-128, low group first, continuation bit set on every byte but the
	#: last. DWARF's, protobuf's.
	LEB128 = "leb128"
	#: Base-128, high group first, otherwise the same. ASN.1's identifier
	#: octets, MIDI's delta times, SQLite's record varints.
	BE128  = "be128"


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
	#: A declared ceiling on the encoded length, where the format sets one
	#: shorter than seven bits per byte would need. See `terminal_bits`.
	declared_max_bytes: int | None = None

	@property
	def max_bytes(self) -> int:
		"""Worst-case encoded length.

		Seven payload bits per byte unless the format says otherwise, which
		some do: SQLite's varint stops at nine bytes where seven-bit groups
		would need ten.
		"""
		if self.declared_max_bytes is not None:
			return self.declared_max_bytes
		return (self.max_bits + 6) // 7

	@property
	def terminal_bits(self) -> int:
		"""How many bits the last permitted byte carries.

		The bytes before it carry seven each, so the last carries whatever is
		left. Usually that is seven or fewer and the byte looks like any other;
		where it is exactly eight there is no spare bit for a continuation
		flag, and the byte is read whole. That is SQLite's ninth byte, and it
		falls out of the arithmetic rather than being a second flag to declare.
		"""
		return self.max_bits - 7 * (self.max_bytes - 1)

	@property
	def terminal_is_whole(self) -> bool:
		"""Whether the last permitted byte carries all eight of its bits."""
		return self.terminal_bits == 8


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
	RATIO_PADDED   = "ratio_padded"		# a:b, rounded up to a whole group
	RATIO_BOUNDED  = "ratio_bounded"	# worst case known, actual data-dependent
	UNBOUNDED      = "unbounded"


class KernelFamily(Enum):
	"""The section 13.4 families.

	The reassuring result of that survey: essentially every line code, FEC,
	scrambler and framing code in practical use is one of these or a pipeline
	of them, which is what bounds the tier-2 design.
	"""

	TABLE       = "table"
	POLYNOMIAL  = "polynomial"
	LINEAR      = "linear_block"
	SHIFT       = "shift_register"
	PERMUTATION = "permutation"
	STUFFING    = "stuffing"


@dataclass(frozen=True)
class Kernel(Node):
	"""A description an implementation and a signature are both derived from.

	Held as the arguments the author wrote. What each family means is
	`situc/kernels.py`'s to decide, and what it implies about capabilities is
	derived there rather than declared here -- a kernel the compiler generates
	the code for is a kernel whose properties it can compute, which is the
	entire difference between tier 1 and tier 2 (section 13.1).
	"""

	span: Span
	family: KernelFamily
	args: tuple[Attr, ...] = ()

	def argument(self, name: str) -> Expr | None:
		for arg in self.args:
			if arg.name == name:
				return arg.value
		return None

	def flag(self, name: str) -> bool:
		return any(arg.name == name and arg.value is None for arg in self.args)


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
	# The tier-2 description, when there is one. A codec with a kernel has its
	# properties derived rather than declared, and anything the author also
	# wrote must agree with what the kernel implies.
	kernel: Kernel | None		= None
	# `a |> b |> c`: the stages, in order. Properties compose pointwise and
	# conservatively (section 13.4).
	pipeline: tuple[str, ...]	= ()
	# The sizes the primitive produces and requires, in bytes (decision 0038).
	# `None` is "not stated" rather than zero: an extern codec's implementation
	# belongs to somebody else, and an author who does not know its tag width
	# must still be able to declare the codec. Where one is stated it is
	# checked against the field the schema declares, and where it is not,
	# nothing is checked and the schema is no worse off than before.
	#
	# Not derivable from `expansion`, which answers a different question. Where
	# a codec appends its overhead the two coincide; where the tag is a
	# separate field the codec is length-preserving, expansion is zero, and it
	# says nothing about the tag beside it.
	tag_bytes: int | None		= None
	nonce_bytes: int | None		= None


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


class AccessMode(Enum):
	"""SystemRDL's vocabulary, borrowed rather than reinvented (section 15.2)."""

	RW       = "rw"
	RO       = "ro"
	WO       = "wo"
	W1C      = "w1c"
	W0C      = "w0c"
	W1S      = "w1s"
	W0S      = "w0s"
	RC       = "rc"		# read-to-clear
	RS       = "rs"		# read-to-set
	WO_ONCE  = "wo_once"
	RSVD     = "rsvd"

	@property
	def readable(self) -> bool:
		return self not in (AccessMode.WO, AccessMode.WO_ONCE, AccessMode.RSVD)

	@property
	def writable(self) -> bool:
		"""Whether the bus offers a write for this field at all.

		`rc` and `rs` are *read*-triggered: reading clears or sets, and there
		is no write side. They were writable here because the test was "not
		read-only", so both generated a setter that wrote a whole word of
		ones with one bit cleared -- an operation the hardware does not have,
		aimed at a field nobody can write.
		"""
		return self not in (AccessMode.RO, AccessMode.RSVD,
		                    AccessMode.RC, AccessMode.RS)

	@property
	def reading_has_an_effect(self) -> bool:
		"""Whether *reading* changes the field, from the mode alone.

		`rc` and `rs` say the same thing `on_read = clear` says; SystemRDL
		spells it two ways and situ understood only one, so a register that
		declared read-to-clear in the vocabulary rather than in the
		side-effect clause came out `effect = Pure` -- the axis reporting a
		destructive read as a pure one.
		"""
		return self in (AccessMode.RC, AccessMode.RS)

	@property
	def is_assignment(self) -> bool:
		"""Whether a write means "store this value".

		A `w1c` field is written with a 1 to clear it, so `set(false)` would be
		a lie about what the bus does. Section 15.3 asks for `clear_error()`
		instead, and this is what decides that.
		"""
		return self in (AccessMode.RW, AccessMode.WO, AccessMode.WO_ONCE)


class SideEffect(Enum):
	NONE    = "none"
	CLEAR   = "clear"
	POP     = "pop"
	TRIGGER = "trigger"


@dataclass(frozen=True)
class RegisterInfo:
	"""What makes a struct a register rather than a buffer layout (15.2).

	A register is a struct: a fixed-width container of fields at an offset, and
	the same solver places it and the same lattice costs it. What it adds is an
	address, a bus access width, and the fact that reaching it is a
	transaction rather than a memory access.
	"""

	address: int | None
	width: int
	access_width: int
	volatile: bool = True		# implicit under `target mmio` (15.1)
	no_rmw: bool   = False


@dataclass(frozen=True)
class StructDecl(Decl):
	span: Span
	name: str
	members: tuple[Member, ...]
	attrs: tuple[Attr, ...] = ()
	# Set when this was written as a `register`. Everything else about a struct
	# holds; codegen reads this to decide it is emitting bus transactions
	# rather than buffer accessors.
	register: RegisterInfo | None = None


class RequirementKind(Enum):
	REQUIRE	= "require"	# build-time gate; failure is an error
	ASSERT	= "assert"	# same check; failure is a warning


@dataclass(frozen=True)
class Requirement(Decl):
	span: Span
	kind: RequirementKind
	expr: Expr


@dataclass(frozen=True)
class Invariant(Decl):
	"""`invariant s.total == size(s.hdr) + size(s.body);` (open question 3).

	A derived field and what it derives from. The left side names one field,
	which stops being the author's to write; the right side is an expression
	over the same struct, which every field in it now has an obligation to.

	That is deliberately the shape a tag already has (14.2). Writing a field a
	tag covers leaves the tag stale; writing a field an invariant reads leaves
	the invariant stale, and the same dirty bit, the same refusal to transmit
	and the same explicit recompute serve both. The machinery was not built for
	this and did not need changing to carry it.
	"""

	span: Span
	derived: str			# the field the invariant maintains
	expr: Expr			# what it equals


@dataclass(frozen=True)
class Must(Node):
	"""One run-time constraint in a relation body (26.95, decision 0030).

	`must` rather than `require`, and the distinction is load-bearing. Section
	16 fixes `require` as a *build-time* capability gate whose failure is a
	compile error; this is a check over two values at run time. Reusing the
	word would give it two meanings in a language whose stated rule is one
	word per concept, and the run-time vocabulary already has a root:
	`must_eq` and `must_be_zero`, whose failures are `SITU_ERR_CONSTRAINT`
	exactly as this one's is.
	"""

	span: Span
	expr: Expr


@dataclass(frozen=True)
class RelationParam(Node):
	"""One message a relation is stated over: `request: fzn_frame`."""

	span: Span
	name: str			# the name the body refers to it by
	type_name: str			# the struct, possibly `outer::Header`


@dataclass(frozen=True)
class Relation(Decl):
	"""`relation response_to(request: frame, response: frame) { ... }`

	A pure predicate over two views. It holds no state, allocates nothing, and
	does not know which messages exist -- the caller owns the pairing, and this
	answers only whether a pairing is well formed.

	**Parameter order is temporal**: the first is the message seen first. A
	dissector needs to say "response to frame N" and a fuzz harness needs to
	know which message to copy bytes *from*, so making order carry it means
	neither needs a second declaration and no relation can omit the fact.
	"""

	span: Span
	name: str
	params: tuple[RelationParam, ...]
	body: tuple[Must, ...]
	#: The exchange's retransmission and timing contract, where it states one
	#: (26.98). On the relation because the relation already identifies the
	#: exchange -- its equality constraints are the conversation key -- and
	#: both endpoints must agree on the policy, which is what makes it schema
	#: rather than a flag (0032).
	attrs: tuple[Attr, ...] = ()


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

	def invariants(self) -> list[Invariant]:
		return [decl for decl in self.decls if isinstance(decl, Invariant)]

	def relations(self) -> list[Relation]:
		return [decl for decl in self.decls if isinstance(decl, Relation)]

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

	@property
	def strictness(self) -> Strictness:
		"""Strict unless the schema says otherwise (section 14.5)."""
		for decl in self.decls:
			if isinstance(decl, StrictnessDirective):
				return decl.strictness
		return Strictness.STRICT
