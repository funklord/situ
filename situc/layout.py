"""The layout solver: byte-exact placement of every field.

Offsets and sizes are tracked in **bits** throughout, and reported in bytes only
where the value is byte-aligned. project.md section 26.2 is emphatic about this:
sub-byte codecs arrive in phase 12 and a region may then begin at a bit offset,
so retrofitting bit phase into a byte-based solver is a rewrite. Carrying the
factor of eight from the start costs nothing.

Situ inserts no implicit padding (section 8.4). A byte-aligned type that would
land mid-byte is therefore an error rather than something silently nudged into
place: the schema says what the bytes are, and the solver's job is to agree or
complain.
"""

from __future__ import annotations

from math import lcm

from dataclasses import dataclass, field, replace
from enum import Enum
from typing import TypeVar

from situc import ast, unparse
from situc.diagnostics import SituError, Span, error
from situc.expr import Env, Interval, build_env, evaluate, interval_of, scalar_interval
from situc.invariant import paths_in
from situc.types import BITS_PER_DIGIT, ScalarType, lookup

BITS_PER_BYTE = 8


@dataclass(frozen=True)
class BitPosition:
	"""Where a bit-packed field sits inside its byte.

	The linear bit offset is the same under either bit order; what changes is
	which physical bit of the byte the field's most significant bit lands on.
	`shift` is how far left the value sits in the containing byte, which is what
	a generated accessor needs.
	"""

	byte: int
	offset_in_byte: int
	width: int
	shift: int
	straddles: bool


@dataclass(frozen=True)
class TagPart:
	"""One part decoded out of a raw tag, as schema source over `tag`.

	Source rather than a node, for the reason `Arm.source` and `discriminant`
	are: the expression's shape is the schema's and identical in every target,
	so what a backend needs is the arithmetic and its own name for the tag.
	"""

	name: str
	source: str


@dataclass(frozen=True)
class ValueRule:
	"""How one item's value extent is found, for one wire type.

	`label` is None for the dispatch's `default`. `kind` is one of "fixed",
	"prefixed", "self_delimiting" or "error", which are section 9.5's four
	ways for a value to say where it ends -- the last of them by refusing.
	"""

	label: int | None
	kind: str
	size: int | None		= None
	length_type: str | None		= None


@dataclass(frozen=True)
class KnownTag:
	"""A tag the schema names, and what it says an item with it carries."""

	tag: int
	name: str
	wire: int | None		= None
	type_name: str | None		= None
	repeated: bool			= False


@dataclass(frozen=True)
class TlvGrammar:
	"""How a tlv region's items are found (section 9.5).

	Everything a walk needs and nothing about policy: the policies say whether
	the region can be canonical, and this says where the next item starts.
	Held as one record rather than four fields on `Placement`, because it is
	one thing -- a region either describes its items or does not.
	"""

	tag_decode: tuple[TagPart, ...]		= ()
	#: Which decoded part the value dispatch selects on. None for the simple
	#: form, whose every value is sized the same way.
	selector: str | None			= None
	rules: tuple[ValueRule, ...]		= ()
	known: tuple[KnownTag, ...]		= ()
	#: The simple form's `length_type = u8`: one length before every value.
	length_type: str | None			= None
	#: Which decoded part a `known` key matches, or None for the raw tag
	#: (decision 0023).
	identity: str | None			= None

	def rule_for(self, wire: int) -> ValueRule | None:
		"""The arm an item with this wire type takes, `default` last."""
		for rule in self.rules:
			if rule.label == wire:
				return rule
		return next((rule for rule in self.rules if rule.label is None), None)

	@property
	def walkable(self) -> bool:
		"""Whether this says enough for a walk to find the next item."""
		return bool(self.rules) or self.length_type is not None


@dataclass(frozen=True)
class IndexTable:
	"""An `indexed` region's offset table, as a walk needs it (section 9.3).

	The table is `count` entries of `entry_bits`, and the elements follow it.
	Reaching element N is one read of entry N plus `base`, which is what buys
	Random access over elements that need not be the same size.
	"""

	#: Width of one table entry, in bits. Always a whole number of bytes.
	entry_bits: int
	#: Where the count comes from, as a member path. None where it is a
	#: literal, which `count_fixed` then carries.
	count_path: str | None		= None
	count_fixed: int | None		= None
	#: What an offset is measured from (decision 0024).
	base: str			= "region"
	#: The member `base` names, for `base == "member"`.
	base_member: str | None		= None
	#: The element type, where it is a struct this backend can frame.
	element: str | None		= None


@dataclass(frozen=True)
class Arm:
	"""One `case` of a variant, as a walk needs it.

	`value` is the discriminant folded to an integer, because `case K.a:` has
	to become a comparison in four languages and each spells an enum member
	differently. Resolving it here means no backend needs an enum-name
	renderer; `source` is kept so the generated code can still say `K.a` in
	the comment beside the number.

	`member` is the path of the member this arm selects, and None for
	`default: error` -- an arm that selects nothing, whose extent is not zero
	but undefined, there being no such message.
	"""

	source: str | None
	value: int | None
	member: str | None


@dataclass(frozen=True)
class Placement:
	"""One field or reserved region, resolved."""

	path: str
	name: str
	kind: str			# "field", "reserved" or "marker"
	type_name: str
	# None once something dynamic precedes this member: the offset exists, it
	# is just not knowable until parse time.
	offset_bits: int | None
	size_bits: int			# the lower bound, and the exact size when fixed
	scalar: ScalarType | None
	endian: ast.Endian | None
	bit_order: ast.BitOrder | None
	span: Span
	attrs: tuple[ast.Attr, ...]	= ()
	marker: str | None		= None
	array_count: int | None		= None
	element_bits: int | None	= None
	bit_position: BitPosition | None = None
	# None means unbounded above. Equal to size_bits when the size is fixed.
	size_max_bits: int | None	= None
	# The offset is measured from a frame base rather than the message base,
	# which is what an array element or a member of a dynamically placed struct
	# gets. Section 12.2: this is the island of staticness.
	frame_relative: bool		= False
	# The member whose value drives this one's size, for blame.
	sized_by: str | None		= None
	# Set when the field's type is a varint, which the propagation table reads
	# to attach the right reasons.
	varint: str | None		= None
	varint_minimal: bool		= True
	# The earlier member that made this one's offset dynamic, and where it is
	# declared. Section 17 asks a blame chain to name the root cause and point
	# at it, not at the field that suffers.
	dynamic_cause: str | None	= None
	dynamic_cause_span: Span | None	= None
	dynamic_cause_size: str | None	= None
	# Whether the frame this member is measured against is itself placed
	# dynamically. An element of a fixed array at a known offset is
	# frame-relative in its offset but nothing can move it, so its address is
	# still Stable; an element of an array after a variable-length member is not.
	frame_base_dynamic: bool	= False
	# For a variant: each arm's name and worst-case size, so the advisor can
	# cost equalizing them.
	arm_sizes: tuple[tuple[str, int], ...] = ()
	#: For a variant: the discriminant, as schema source, and one entry per
	#: arm -- the value it matches (None for `default`) against the path of
	#: the member it selects (None for `default: error`, which has none).
	#:
	#: `arm_sizes` is the worst case, which is what the advisor costs. This is
	#: what a *walk* needs: how long this instance is, which is whichever arm
	#: the discriminant picked, and the picking has to be in the generated
	#: code rather than in the compiler.
	discriminant: str | None		= None
	arm_cases: tuple[Arm, ...]		= ()
	# For a tlv region: the policies that decide whether it can be canonical.
	tlv_unknown: str | None		= None
	tlv_duplicates: str | None	= None
	tlv_ordered: bool		= False
	# The varint used as the tag type, and whether it demands minimal
	# encodings. A non-minimal tag is a cause of non-canonicity in its own
	# right, independent of anything the items do.
	tlv_tag_varint: str | None	= None
	tlv_tag_minimal: bool		= True
	tlv_wire_types: tuple[int, ...]	= ()
	# How the region's items are found. Empty until the front end read the
	# item grammar, which is why nothing walked one for a long time.
	tlv_grammar: TlvGrammar | None	= None
	# For an `indexed` region: the offset table, so a backend can walk it.
	index_table: IndexTable | None	= None
	# The codec transforming this region, or the one whose region contains it.
	codec: str | None		= None
	# The authenticated and sealed regions this member sits inside, outermost
	# first, plus the region itself when it is one. Coverage is resolved from
	# this: a tag covers a region, and a member is covered when one of the
	# regions it sits in is (section 14.1).
	regions: tuple[str, ...]	= ()
	# Set on a member inside a sealed region, naming it. What makes the
	# interior VerifyGated -- the stage gate of section 14.3.
	sealed_by: str | None		= None
	# The region carries `[allow_unverified_read]`, which is the loud, greppable
	# way out of the stage gate rather than a quiet one.
	unverified_ok: bool		= False
	# For a tag or checksum placement: the regions it covers, after inference.
	tag_covers: tuple[str, ...]	= ()
	# The tags covering these bytes. Written after the whole struct is placed,
	# because a tag is usually declared after the regions it covers.
	covered_by: tuple[str, ...]	= ()
	#: The invariant that maintains this field, if one does. Such a field
	#: is not the author's to write: only a recompute may.
	derived_by: str | None		= None
	#: `at hdr.pixel_offset`: the member sits where this field says, measured
	#: from the start of the message. It joins no offset chain and contributes
	#: nothing to the enclosing extent -- see section 9.8.
	located: str | None		= None
	#: `until "\r\n"`: the member ends at the first occurrence of these bytes,
	#: found by scanning rather than computed. Everything after it has
	#: `offset = Scanned` rather than `Dynamic` -- a search that can fail,
	#: not an addition that cannot (docs/decisions/0020-delimited-data.md).
	delimiter: bytes | None		= None
	#: How the delimiter is made inert inside the content, where a protocol
	#: admits it there. Both cost `canonical = NonCanonical`, because two byte
	#: sequences then encode one value.
	delimiter_quote: int | None	= None
	delimiter_escape: int | None	= None
	#: A bound on the scan, from `until D max N`.
	delimiter_cap: int | None	= None
	#: The array's size expression as source, where it is not a bare field
	#: reference. `sized_by` holds a path and holds nothing for
	#: `data[(len + 1) * 8 - 2]`, so a backend reading only that emitted a
	#: length of zero for one of the commonest shapes there is -- a length
	#: field counted in units rather than bytes.
	size_expr: str | None		= None
	#: `while (cond)`: the run ends after the element that fails this.
	#: Held as source rather than as a tree, because every consumer of it
	#: either renders it or hands it to a backend that renders it.
	repeat_while: str | None	= None
	#: A bound on the number of elements, from `while (...) max N`.
	repeat_cap: int | None		= None
	#: `decimal`/`hex`: the base the value is written in. The `scalar` beside
	#: it gives the value's domain rather than its width in the buffer, which
	#: for a text number depends on the number (section 8.6.2).
	radix: int | None		= None
	@property
	def radix_max(self) -> int | None:
		"""The largest value this text number can hold.

		Not the type's maximum. `decimal u16 code[3]` is three digits, so it
		holds 0..999 whatever `u16` would allow -- and a range check written
		against the type would accept a value the field cannot represent.
		"""
		if self.radix is None or self.scalar is None:
			return None

		limit = (1 << self.scalar.bits) - 1
		if self.array_count is None:
			return limit
		return min(limit, int(self.radix ** self.array_count) - 1)

	#: `[minimal]`: leading zeros are refused, so one value has one spelling.
	#: Without it "007" and "7" are the same number written two ways, which is
	#: what `canonical` exists to report.
	radix_minimal: bool		= False
	#: `[trim]`: whitespace at either end is framing, not value. The member's
	#: *span* is unchanged -- the bytes are still there and still partition
	#: the struct -- but the value they carry is what is left after it.
	trimmed: bool			= False
	#: `[case_insensitive]`: two spellings differing only in ASCII case are
	#: one token, which is what an HTTP header name is.
	case_insensitive: bool		= False
	#: `[since = N]`: the member is present from protocol version N onward
	#: (section 19.4). Its offset is still static -- `[since]` is append-only
	#: by construction, so nothing before it can move -- and what varies is
	#: whether the bytes are there at all.
	since: int | None		= None
	#: The member whose value says which version this is, for a struct that
	#: has one. Copied onto every member so a backend has it to hand.
	version_field: str | None	= None
	#: The earlier member whose scan made this one's offset Scanned, for blame.
	scan_cause: str | None		= None
	scan_cause_span: Span | None	= None
	# Set on a field of a `register`: how the bus lets it be reached, and what
	# touching it does besides move a value (section 15.2).
	access_mode: ast.AccessMode | None	= None
	on_read: ast.SideEffect			= ast.SideEffect.NONE
	on_write: ast.SideEffect		= ast.SideEffect.NONE
	# The register this field belongs to, if any.
	register: ast.RegisterInfo | None	= None

	@property
	def is_fixed_size(self) -> bool:
		return self.size_max_bits == self.size_bits

	@property
	def has_static_offset(self) -> bool:
		return self.offset_bits is not None

	@property
	def is_byte_aligned(self) -> bool:
		return self.offset_bits is not None and self.offset_bits % BITS_PER_BYTE == 0

	@property
	def offset_bytes(self) -> int:
		assert self.offset_bits is not None, "offset is dynamic"
		return self.offset_bits // BITS_PER_BYTE


@dataclass
class StructLayout:
	name: str
	size_bits: int
	placements: list[Placement]	= field(default_factory=list)
	span: Span | None		= None
	reserved_count: int		= 0
	# None means unbounded. A struct is a frame exactly when these differ:
	# some member's size is not known until parse time (section 9.1).
	size_max_bits: int | None	= None
	# Set when this struct was written as a `register` (section 15.2).
	register: ast.RegisterInfo | None = None

	@property
	def is_frame(self) -> bool:
		return self.size_max_bits != self.size_bits

	@property
	def is_fixed_size(self) -> bool:
		return not self.is_frame

	@property
	def is_byte_sized(self) -> bool:
		return self.size_bits % BITS_PER_BYTE == 0

	@property
	def size_bytes(self) -> int:
		return self.size_bits // BITS_PER_BYTE

	@property
	def size_max_bytes(self) -> int | None:
		if self.size_max_bits is None:
			return None
		return self.size_max_bits // BITS_PER_BYTE


@dataclass
class SchemaLayout:
	structs: dict[str, StructLayout] = field(default_factory=dict)
	env: Env			= field(default_factory=Env)

	def lookup(self, builtin: str, path: str) -> int | None:
		"""Answer `size(X)`, `offset(X)` and `count(X)` over solved structs.

		Sizes and offsets are answered in bytes, because that is the unit a
		schema author writes them in. A value that is not a whole number of
		bytes is refused rather than rounded.
		"""
		head, _, rest = path.partition(".")
		layout = self.structs.get(head)
		if layout is None:
			return None

		if not rest:
			if builtin == "size":
				if not layout.is_byte_sized or layout.is_frame:
					return None
				return layout.size_bytes
			if builtin == "offset":
				return 0
			return None

		placement = self.find(path)
		if placement is None:
			return None

		if builtin == "size":
			if not placement.is_fixed_size:
				return None
			bits = placement.size_bits
			return None if bits % BITS_PER_BYTE else bits // BITS_PER_BYTE
		if builtin == "offset":
			offset = placement.offset_bits
			if offset is None or offset % BITS_PER_BYTE:
				return None
			return offset // BITS_PER_BYTE
		return placement.array_count

	def explain(self, builtin: str, path: str) -> tuple[str, str]:
		"""Why `lookup` had no answer: the message, and the label under it.

		`lookup` returns None for four different reasons and the caller
		reported all of them as "unknown path" -- so `size(udp_header)` on a
		struct that is plainly declared said it was not declared, when what is
		true is that it no longer *has* one size. Three of those four are
		facts about the layout rather than about the name, and a reader told
		the wrong one goes looking for a typo (invariant 18).
		"""
		unknown = (f"unknown path `{path}`", "not a declared struct or field")

		head, _, rest = path.partition(".")
		layout = self.structs.get(head)
		if layout is None:
			return unknown

		if not rest:
			if builtin != "size":
				return (f"`{builtin}` takes a member path, not a struct",
				        "a struct has no offset or count of its own")
			if layout.is_frame:
				return (f"`{head}` has no single size: its extent depends on "
				        "the data", "a variable-length struct")
			if not layout.is_byte_sized:
				return (f"`{head}` is not a whole number of bytes",
				        "a bit-packed struct")
			return unknown

		placement = self.find(path)
		if placement is None:
			return unknown

		if builtin == "size":
			if not placement.is_fixed_size:
				return (f"`{path}` has no single size: the data decides it",
				        "a variable-length member")
			return (f"`{path}` is not a whole number of bytes",
			        "a bit-packed member")
		if builtin == "offset":
			if placement.offset_bits is None:
				return (f"`{path}` has no single offset: it is placed at "
				        "run time", "a dynamically placed member")
			return (f"`{path}` does not start on a byte boundary",
			        "a bit-packed member")
		return (f"`{path}` is not a counted array", "no count to report")

	def find(self, path: str) -> Placement | None:
		head, _, _ = path.partition(".")
		layout = self.structs.get(head)
		if layout is None:
			return None
		for placement in layout.placements:
			if placement.path == path:
				return placement
		return None


# ---------------------------------------------------------------------------
# Scope: endian and bit order
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Scope:
	"""The endian and bit order in force, and where each was set.

	Both are scoped: a file-level directive, overridable per struct, overridable
	per field (section 8.3). Absent is distinct from defaulted -- section 17.0
	makes a missing directive an error at the point a field actually needs one,
	rather than picking a silent default that is undetectable at runtime.
	"""

	endian: ast.Endian | None	= None
	bit_order: ast.BitOrder | None	= None
	# Set by `[endian = from(marker)]`. Distinct from `endian` being None:
	# the byte order is known, it is just not known until parse time.
	marker: str | None		= None

	def narrow(self, attrs: tuple[ast.Attr, ...], env: Env) -> Scope:
		endian    = self.endian
		bit_order = self.bit_order
		marker    = self.marker

		for attr in attrs:
			if attr.name == "endian":
				resolved = _endian_attr(attr)
				if isinstance(resolved, str):
					marker = resolved
					endian = None
				else:
					endian = resolved
					marker = None
			elif attr.name == "bit_order":
				bit_order = _attr_enum(attr, ast.BitOrder, "bit order")

		return Scope(endian, bit_order, marker)

	@property
	def has_byte_order(self) -> bool:
		return self.endian is not None or self.marker is not None


def _endian_attr(attr: ast.Attr) -> ast.Endian | str:
	"""Resolve `[endian = ...]`, which may name an order or defer to a marker."""
	if (isinstance(attr.value, ast.Call) and attr.value.name == "from"
			and len(attr.value.args) == 1
			and isinstance(attr.value.args[0], ast.NameRef)):
		return attr.value.args[0].name

	return _attr_enum(attr, ast.Endian, "endianness")


EnumT = TypeVar("EnumT", bound=Enum)


def _attr_enum(attr: ast.Attr, enum: type[EnumT], described: str) -> EnumT:
	if attr.value is None:
		raise error(f"`{attr.name}` needs a value", attr.span,
		            label = f"expected `{attr.name} = <{described}>`")

	if not isinstance(attr.value, ast.NameRef):
		raise error(f"expected a {described}", attr.value.span)

	for candidate in enum:
		if candidate.value == attr.value.name:
			return candidate

	options = ", ".join(f"`{item.value}`" for item in enum)
	raise error(
		f"unknown {described} `{attr.value.name}`",
		attr.value.span,
		label = f"expected one of {options}",
	)


@dataclass(frozen=True)
class Extent:
	"""A running bit offset that may have stopped being a single number.

	`lo == hi` means the cursor is still exact and every member so far has had
	a fixed size. Once they differ, every subsequent member's offset is dynamic
	-- which is the locality rule of section 11.3, expressed as a value rather
	than as a flag somebody has to remember to set.
	"""

	lo: int
	hi: int | None

	@property
	def is_exact(self) -> bool:
		return self.hi == self.lo

	def advance(self, size: Interval) -> Extent:
		hi = None if self.hi is None or size.hi is None else self.hi + size.hi
		return Extent(self.lo + size.lo, hi)


@dataclass
class Walk:
	"""Solver state threaded through one struct's members."""

	fields: dict[str, Interval]
	cursor: Extent
	# Set once a `[remaining]` member has been placed: nothing may follow it.
	closed_by: str | None = None
	# The first member whose size was not fixed, which is what every later
	# member's Dynamic offset traces back to.
	cause: tuple[str, Span, str] | None = None
	# The first member found by scanning for a delimiter. Separate from
	# `cause` because it is a strictly stronger claim: a dynamic offset is
	# arithmetic over values already read and cannot fail, while reaching a
	# member past a scan means searching, and the delimiter may not be there
	# (docs/decisions/0020-delimited-data.md).
	scan: tuple[str, Span] | None = None
	# Fields that exist only after a transform has run. Recorded so a reference
	# to one gets the decidability diagnostic of section 13.3 rather than a
	# bare "not in scope".
	behind_codec: dict[str, str] = field(default_factory=dict)
	# The authenticated and sealed regions enclosing the members being placed,
	# outermost first.
	regions: tuple[str, ...] = ()
	# The innermost sealed region, if any, and whether it opted out of the
	# stage gate.
	sealed_by: str | None = None
	unverified_ok: bool   = False


# ---------------------------------------------------------------------------
# The solver
# ---------------------------------------------------------------------------


def solve(schema: ast.Schema) -> SchemaLayout:
	env    = build_env(schema)
	result = SchemaLayout(env=env)
	solver = Solver(schema, result)
	solver.run()
	return result


class Solver:
	def __init__(self, schema: ast.Schema, result: SchemaLayout) -> None:
		self.schema  = schema
		self.result  = result
		self.structs = {decl.name: decl for decl in schema.structs()}
		self.enums   = {decl.name: decl for decl in schema.enums()}
		self.markers = {decl.name: decl for decl in schema.markers()}
		self.varints = {decl.name: decl for decl in schema.varints()}
		self.codecs  = {decl.name: decl for decl in schema.codecs()}
		self.scopes  = _scopes(schema)

	def run(self) -> None:
		# Recursion is already rejected, so a plain post-order walk terminates.
		for name in self.structs:
			self.layout_of(name)

		self.check_pins()

	def layout_of(self, name: str) -> StructLayout:
		existing = self.result.structs.get(name)
		if existing is not None:
			return existing

		decl   = self.structs[name]
		scope  = self.scopes[name].narrow(decl.attrs, self.result.env)
		layout = StructLayout(name=name, size_bits=0, span=decl.span,
		                      register=decl.register)

		# `fields` accumulates as the walk proceeds, so a size expression can
		# only ever name a field declared before it. That is the
		# no-forward-reference rule of section 10, enforced by construction
		# rather than by a check.
		state  = Walk(fields={}, cursor=Extent(0, 0))
		self.place_members(decl, decl.members, scope, layout, name, state)
		self.resolve_coverage(decl, layout)
		self.resolve_invariants(decl, layout)

		layout.size_bits     = state.cursor.lo
		layout.size_max_bits = state.cursor.hi

		# A versioned struct is as small as its first version and as large as
		# its last, so its extent is a range even though every member in it
		# sits at a static offset. Both facts are true at once, and only
		# because `[since]` is append-only: nothing moves, and what varies is
		# how much of the struct is there (section 19.4).
		earliest = _first_versioned(layout)
		if earliest is not None:
			layout.size_bits = earliest
		self.result.structs[name] = layout
		return layout

	def place_members(self, decl: ast.StructDecl, members: tuple[ast.Member, ...],
			scope: Scope, layout: StructLayout, prefix: str, state: Walk) -> None:
		for index, member in enumerate(members):
			if isinstance(member, ast.PositionalBlock):
				# A positional block is a staticness assertion, not a frame: its
				# members keep accumulating into the enclosing struct, and
				# anything dynamic inside one is an error (section 9.2).
				before = state.cursor
				self.place_members(decl, member.members, scope, layout, prefix, state)
				if state.cursor.hi != state.cursor.lo and before.hi == before.lo:
					raise error(
						"a `positional` block cannot contain a dynamic member",
						member.span,
						label = "staticness is asserted here",
						notes = ["`positional` exists so the compiler defends a "
						         "region the author wants to stay static "
						         "(project.md section 9.2)",
						         "move the dynamic member outside the block"],
					)
			elif isinstance(member, ast.Variant):
				self.place_variant(decl, member, scope, layout, prefix, state)
			elif isinstance(member, ast.Opaque):
				self.place_opaque(member, scope, layout, prefix, state)
			elif isinstance(member, ast.Tlv):
				self.place_tlv(member, scope, layout, prefix, state)
			elif isinstance(member, (ast.Coded, ast.Sealed)):
				self.place_coded(decl, member, scope, layout, prefix, state)
			elif isinstance(member, ast.Authenticated):
				self.place_authenticated(decl, member, scope, layout, prefix, state)
			elif isinstance(member, ast.TagField):
				self.place_tag(member, scope, layout, prefix, state)
			elif isinstance(member, ast.Indexed):
				self.place_indexed(decl, member, scope, layout, prefix, state)
			elif isinstance(member, ast.MarkerField):
				self.place_marker(decl, member, scope, layout, prefix, state)
			elif isinstance(member, (ast.Field, ast.Reserved)):
				last = index == len(members) - 1
				self.place_one(decl, member, scope, layout, prefix, state, last)

	def place_opaque(self, member: ast.Opaque, scope: Scope,
			layout: StructLayout, prefix: str, state: Walk) -> None:
		"""A sized region with no interior schema (section 9.4)."""
		env    = self.result.env.with_layout(self.result.lookup, self.result.explain).with_fields(state.fields)
		size   = interval_of(member.size, env)
		cursor = state.cursor

		if size.lo < 0:
			raise error("an opaque region may not have a negative size",
			            member.size.span, label = f"range is {size.render()}")

		bits = Interval(size.lo * BITS_PER_BYTE,
		                None if size.hi is None else size.hi * BITS_PER_BYTE)

		layout.placements.append(Placement(
			path          = f"{prefix}.{member.name}",
			name          = member.name,
			kind          = "opaque",
			type_name     = "opaque",
			offset_bits   = cursor.lo if cursor.is_exact else None,
			size_bits     = bits.lo,
			size_max_bits = bits.hi,
			scalar        = None,
			endian        = None,
			bit_order     = scope.bit_order,
			span          = member.span,
			attrs         = member.attrs,
			sized_by      = _path_of(member.size),
			dynamic_cause      = state.cause[0] if state.cause else None,
			dynamic_cause_span = state.cause[1] if state.cause else None,
			dynamic_cause_size = state.cause[2] if state.cause else None,
		))

		if state.cause is None and bits.hi != bits.lo:
			state.cause = (member.name, member.span, _render_extent(bits))

		state.cursor = cursor.advance(bits)

	def place_coded(self, decl: ast.StructDecl, region: ast.Coded | ast.Sealed,
			scope: Scope, layout: StructLayout, prefix: str, state: Walk) -> None:
		"""A region whose bytes are the output of a transform (section 13.5).

		The interior is laid out as though untransformed, because that is what
		it is once decoded; the region's own extent is the interior's extent put
		through the codec's expansion. The lattice reads the property signature
		and nothing else -- this function never learns what the algorithm does.

		`sealed` comes here too: it is `coded` plus authentication (decision
		0009), and the transform half is identical down to this function. What
		it adds is the coverage stamp and the stage gate below.
		"""
		sealed = isinstance(region, ast.Sealed)
		codec = self.codecs.get(region.codec)
		assert codec is not None, "wellformed rejects an unknown codec"

		cursor = state.cursor
		slot   = len(layout.placements)

		# The interior is placed at the region's base. Its offsets are what the
		# decoded bytes look like, which is what a caller addresses.
		inner = Walk(fields=dict(state.fields), cursor=cursor, cause=state.cause)
		self.place_members(decl, region.members, scope, layout,
		                   f"{prefix}.{region.name}", inner)

		# The interior's fields are deliberately not merged back into the
		# enclosing scope. Section 13.3: the expression language may not
		# reference transform output, because a schema that could branch on a
		# decoded value would make "is this in-place mutable?" undecidable.
		for name in inner.fields:
			if name not in state.fields:
				state.behind_codec[name] = region.codec
				state.behind_codec[f"{region.name}.{name}"] = region.codec

		interior_lo = inner.cursor.lo - cursor.lo
		interior_hi = (None if inner.cursor.hi is None or cursor.hi is None
		               else inner.cursor.hi - cursor.hi)
		interior    = Interval(interior_lo, interior_hi)
		extent      = _expand(codec, interior)

		# A delimited region's extent is the scan, not the interior put
		# through the expansion. The two answer different questions: the
		# expansion says how much the transform *could* grow the interior to,
		# and the delimiter says where the encoded bytes actually stop. Only
		# the second is on the wire (section 13.6).
		until = getattr(region, "until", None)
		if until is not None:
			extent = self.delimited_extent(region, until, state)

		layout.placements.insert(slot, Placement(
			path          = f"{prefix}.{region.name}",
			name          = region.name,
			kind          = "sealed" if sealed else "coded",
			type_name     = region.codec,
			offset_bits   = cursor.lo if cursor.is_exact else None,
			size_bits     = extent.lo,
			size_max_bits = extent.hi,
			scalar        = None,
			endian        = None,
			bit_order     = scope.bit_order,
			span          = region.span,
			attrs         = region.attrs,
			codec         = region.codec,
			delimiter     = until.delimiter if until is not None else None,
			delimiter_cap = (evaluate(until.cap, self.result.env)
			                 if until is not None and until.cap is not None
			                 else None),
			dynamic_cause      = state.cause[0] if state.cause else None,
			dynamic_cause_span = state.cause[1] if state.cause else None,
			dynamic_cause_size = state.cause[2] if state.cause else None,
		))

		# Everything the region contains carries the codec, so the propagation
		# table can read it without walking back up the tree.
		for index in range(slot + 1, len(layout.placements)):
			held = layout.placements[index]
			layout.placements[index] = replace(held, codec=region.codec)

		if sealed:
			# The region's own placement is stamped along with its interior: a
			# tag covers the sealed bytes, and the region is those bytes.
			self.stamp_region(layout, slot, region.name,
			                  sealed_by     = region.name,
			                  unverified_ok = _has_attr(region.attrs,
			                                            "allow_unverified_read"))

		if state.cause is None and extent.hi != extent.lo:
			state.cause = (region.name, region.span, _render_extent(extent))

		state.cursor = cursor.advance(extent)

	def place_authenticated(self, decl: ast.StructDecl, region: ast.Authenticated,
			scope: Scope, layout: StructLayout, prefix: str, state: Walk) -> None:
		"""Plaintext covered by a tag (section 14.1).

		The block transforms nothing and opens no scope: its members accumulate
		into the enclosing struct at the offsets they would have had anyway, and
		keep the struct's namespace. All it contributes is a name for a span of
		bytes, so that a tag can say it covers them.
		"""
		cursor = state.cursor
		slot   = len(layout.placements)

		self.place_members(decl, region.members, scope, layout, prefix, state)

		size_lo = state.cursor.lo - cursor.lo
		size_hi = (None if state.cursor.hi is None or cursor.hi is None
		           else state.cursor.hi - cursor.hi)

		layout.placements.insert(slot, Placement(
			path          = f"{prefix}.{region.name}",
			name          = region.name,
			kind          = "authenticated",
			type_name     = "authenticated",
			offset_bits   = cursor.lo if cursor.is_exact else None,
			size_bits     = size_lo,
			size_max_bits = size_hi,
			scalar        = None,
			endian        = None,
			bit_order     = scope.bit_order,
			span          = region.span,
			attrs         = region.attrs,
			dynamic_cause      = state.cause[0] if state.cause else None,
			dynamic_cause_span = state.cause[1] if state.cause else None,
			dynamic_cause_size = state.cause[2] if state.cause else None,
		))

		self.stamp_region(layout, slot, region.name)

	def stamp_region(self, layout: StructLayout, start: int, name: str,
			sealed_by: str | None = None, unverified_ok: bool = False) -> None:
		"""Record which regions a run of placements sits inside.

		Prepended rather than assigned, so nesting comes out outermost-first:
		the inner region has already stamped itself by the time the outer one
		runs. The innermost sealed region wins the stage gate for the same
		reason.
		"""
		for index in range(start, len(layout.placements)):
			held = layout.placements[index]
			layout.placements[index] = replace(
				held,
				regions       = (name,) + held.regions,
				sealed_by     = held.sealed_by or sealed_by,
				unverified_ok = held.unverified_ok or unverified_ok,
			)

	def resolve_invariants(self, decl: ast.StructDecl,
			layout: StructLayout) -> None:
		"""Coverage, for a derived field rather than a tag (open question 3).

		The shape is the tag's exactly. A field the expression reads is covered
		by the invariant, so writing it leaves something stale and the same
		dirty bit says so. The field the invariant maintains is not the
		author's to write at all, which the `derived_by` marker below turns
		into a refusal in every backend without any of them knowing what an
		invariant is.
		"""

		invariants = [held for held in self.schema.invariants()
		              if held.derived.partition(".")[0] == decl.name]
		if not invariants:
			return

		depends: dict[str, list[str]] = {}
		derived: dict[str, str] = {}

		for held in invariants:
			field = held.derived.partition(".")[2]
			name  = f"invariant {field}"
			derived[field] = name
			for path in paths_in(held.expr):
				read = path.partition(".")[2]
				if read:
					depends.setdefault(read, []).append(name)

		for index, placed in enumerate(layout.placements):
			local = placed.path.partition(".")[2]

			if local in derived:
				layout.placements[index] = replace(
					placed, derived_by=derived[local])
			elif local in depends:
				layout.placements[index] = replace(
					placed,
					covered_by=tuple(sorted(set(placed.covered_by)
					                        | set(depends[local]))))

	def resolve_coverage(self, decl: ast.StructDecl, layout: StructLayout) -> None:
		"""Join tags to the bytes they cover, once the whole struct is placed.

		Deferred to here because a tag is normally declared after the regions it
		covers -- 5.3 puts it last -- so at the moment the regions are placed
		there is nothing yet to join them to.

		Well-formedness has already established that every named region exists
		and that coverage is disjoint or nested, so this only has to apply what
		those checks allowed.
		"""
		from situc.wellformed import auth_regions, coverage_of, tag_fields

		regions = auth_regions(decl.members)
		if not regions:
			return

		# region name -> the tags covering it. A region may appear under more
		# than one tag when coverage nests (decision 0011).
		covering: dict[str, list[str]] = {}
		# Innermost first is narrowest first: coverage is disjoint or nested, so
		# a tag covering fewer regions is the inner one. That is the order the
		# generated code must recompute in, because an inner tag's own bytes are
		# input to the outer one.
		order: dict[str, tuple[int, int]] = {}

		for position, tag in enumerate(tag_fields(decl.members)):
			covers = coverage_of(tag, regions)
			order[tag.name] = (len(covers), position)
			for region in covers:
				covering.setdefault(region, []).append(tag.name)

		for index, held in enumerate(layout.placements):
			tags = {name for region in held.regions
			        for name in covering.get(region, ())}
			if not tags:
				continue

			layout.placements[index] = replace(
				held, covered_by=tuple(sorted(tags, key=lambda name: order[name])))

		for index, held in enumerate(layout.placements):
			if held.kind not in ("tag", "checksum"):
				continue
			tag = next(field for field in tag_fields(decl.members)
			           if field.name == held.name)
			layout.placements[index] = replace(
				held, tag_covers=coverage_of(tag, regions))

	def place_tag(self, member: ast.TagField, scope: Scope, layout: StructLayout,
			prefix: str, state: Walk) -> None:
		"""An authentication tag or a checksum (section 14.1).

		Laid out as the byte string it is. What makes it a tag rather than an
		array is its coverage, which is resolved once the whole struct is placed
		-- a tag is normally declared after the regions it covers, so it cannot
		be resolved here.
		"""
		env    = self.result.env.with_layout(self.result.lookup, self.result.explain).with_fields(state.fields)
		cursor = state.cursor
		scalar = member.type_ref.scalar

		if scalar is None or scalar.is_bit_packed:
			raise error(
				f"a {member.kind.value} must be a whole-byte scalar type",
				member.type_ref.span,
				label = f"`{member.type_ref.name}` is not",
				notes = ["a tag is a byte string produced by an algorithm, so it "
				         "has no bit-level structure to describe",
				         "`tag u8[16];` for a 128-bit tag"],
			)

		count = interval_of(member.array.size, env) if member.array.size else Interval(0, 0)
		if not count.is_point or count.lo <= 0:
			raise error(
				f"a {member.kind.value} needs a constant length",
				member.array.span,
				label = f"length is {count.render()}",
				notes = ["the algorithm fixes the tag width, so a data-dependent "
				         "one describes no algorithm at all"],
			)

		bits = count.lo * scalar.bits

		layout.placements.append(Placement(
			path          = f"{prefix}.{member.name}",
			name          = member.name,
			kind          = member.kind.value,
			type_name     = member.type_ref.name,
			offset_bits   = cursor.lo if cursor.is_exact else None,
			size_bits     = bits,
			size_max_bits = bits,
			scalar        = scalar,
			endian        = None,
			bit_order     = scope.bit_order,
			span          = member.span,
			attrs         = member.attrs,
			array_count   = count.lo,
			element_bits  = scalar.bits,
			regions       = state.regions,
			dynamic_cause      = state.cause[0] if state.cause else None,
			dynamic_cause_span = state.cause[1] if state.cause else None,
			dynamic_cause_size = state.cause[2] if state.cause else None,
		))

		state.cursor = cursor.advance(Interval(bits, bits))

	def place_tlv(self, member: ast.Tlv, scope: Scope, layout: StructLayout,
			prefix: str, state: Walk) -> None:
		"""A schema-free run of tag-length-value items (section 9.5).

		Nothing about its extent is knowable here: the items are whatever the
		data holds. It is Unbounded, and everything after it is Dynamic.
		"""
		cursor     = state.cursor
		tag_type   = member.argument("tag_type")
		tag_varint = (self.varints.get(tag_type.name)
		              if isinstance(tag_type, ast.NameRef) else None)

		layout.placements.append(Placement(
			path          = f"{prefix}.{member.name}",
			name          = member.name,
			kind          = "tlv",
			type_name     = "tlv",
			offset_bits   = cursor.lo if cursor.is_exact else None,
			size_bits     = 0,
			size_max_bits = None,
			scalar        = None,
			endian        = None,
			bit_order     = scope.bit_order,
			span          = member.span,
			attrs         = member.attrs,
			tlv_unknown   = member.unknown.value,
			tlv_duplicates = member.duplicates.value,
			tlv_ordered   = member.ordered,
			tlv_tag_varint = tag_varint.name if tag_varint else None,
			tlv_tag_minimal = tag_varint.minimal if tag_varint else True,
			tlv_wire_types = member.wire_types,
			tlv_grammar   = _tlv_grammar(member),
			dynamic_cause      = state.cause[0] if state.cause else None,
			dynamic_cause_span = state.cause[1] if state.cause else None,
			dynamic_cause_size = state.cause[2] if state.cause else None,
		))

		if state.cause is None:
			state.cause = (member.name, member.span, "Unbounded")

		state.cursor = cursor.advance(Interval(0, None))

	def place_indexed(self, decl: ast.StructDecl, member: ast.Indexed,
			scope: Scope, layout: StructLayout, prefix: str, state: Walk) -> None:
		"""An offset table then elements (section 9.3).

		The table is `count` entries of `offset_type`; the elements follow. The
		indirection is what buys O(1) access to an element whose size is not
		fixed, and it is why insertion is unsupported: every later offset would
		have to move.
		"""
		env      = self.result.env.with_layout(self.result.lookup, self.result.explain).with_fields(state.fields)
		cursor   = state.cursor
		element  = member.members[0]

		offset_type = member.argument("offset_type")
		if offset_type is None or not isinstance(offset_type, ast.NameRef):
			raise error(
				"an `indexed` region needs `offset_type`",
				member.span,
				label = "expected `offset_type = u16` or similar",
				notes = ["the table's entry width decides how far the region can "
				         "reach, so it cannot be inferred"],
			)

		width = lookup(offset_type.name)
		if width is None or width.is_bit_packed:
			raise error(
				f"`{offset_type.name}` is not a whole-byte scalar",
				offset_type.span,
				label = "invalid offset type",
			)

		count_expr = member.argument("count")
		if count_expr is None:
			raise error(
				"an `indexed` region needs `count`",
				member.span,
				label = "expected `count = <field>`",
				notes = ["the table's length has to come from somewhere the "
				         "parser can read before it"],
			)

		count = interval_of(count_expr, env)
		table = Interval(
			count.lo * width.bits,
			None if count.hi is None else count.hi * width.bits)

		# The elements themselves: reached through the table, so their extent
		# is whatever remains rather than something this pass can total.
		layout.placements.append(Placement(
			path          = f"{prefix}.{member.name}",
			name          = member.name,
			kind          = "indexed",
			type_name     = element.type_ref.name if isinstance(element, ast.Field)
			                else "indexed",
			offset_bits   = cursor.lo if cursor.is_exact else None,
			size_bits     = table.lo,
			size_max_bits = None,
			scalar        = None,
			# The table's entries are scalars in the region's byte order. It
			# was None here, so a backend reading an entry had nothing to ask
			# and defaulted -- which reads a big-endian table little end
			# first and yields a plausible offset.
			endian        = scope.endian,
			bit_order     = scope.bit_order,
			span          = member.span,
			sized_by      = _path_of(count_expr),
			index_table   = IndexTable(
				entry_bits  = width.bits,
				count_path  = _path_of(count_expr),
				count_fixed = count.lo if count.is_point else None,
				base        = member.base.value,
				base_member = member.base_member,
				element     = (element.type_ref.name
				               if isinstance(element, ast.Field) else None),
			),
			dynamic_cause      = state.cause[0] if state.cause else None,
			dynamic_cause_span = state.cause[1] if state.cause else None,
			dynamic_cause_size = state.cause[2] if state.cause else None,
		))

		if state.cause is None:
			state.cause = (member.name, member.span, "Unbounded")

		state.cursor = cursor.advance(Interval(table.lo, None))

	def place_variant(self, decl: ast.StructDecl, variant: ast.Variant,
			scope: Scope, layout: StructLayout, prefix: str, state: Walk) -> None:
		"""A variant occupies the extent of whichever arm is selected.

		Section 9.6: the size is the size of the selected arm, so unless every
		arm is the same size the variant makes everything after it dynamic. The
		arms are laid out at the same base, because exactly one of them is
		present.
		"""
		self.check_discriminant(variant, state)

		cursor = state.cursor
		if cursor.is_exact and cursor.lo % BITS_PER_BYTE != 0:
			raise error(
				f"variant `{variant.name}` must start on a byte boundary",
				variant.span,
				label = f"lands {cursor.lo % BITS_PER_BYTE} bits into a byte",
			)

		# The variant's own entry is inserted here, before its arms, so the map
		# reads container-then-contents like every other aggregate.
		slot = len(layout.placements)

		from situc.unparse import expr_to_source	# circular at module scope

		low: int | None  = None
		high: int | None = 0
		arm_sizes: list[tuple[str, int]] = []
		arm_cases: list[Arm] = []

		for arm in variant.arms:
			source = None if arm.value is None else expr_to_source(arm.value)
			value  = (None if arm.value is None
			          else evaluate(arm.value, self.result.env))

			if arm.member is None:
				# `error` rejects the message and `opaque` consumes the rest;
				# neither contributes a fixed extent of its own.
				if arm.is_opaque:
					high = None
				arm_cases.append(Arm(source, value, None))
				continue

			arm_cases.append(Arm(source, value,
			                     f"{prefix}.{variant.name}.{_arm_name(arm)}"))
			extent = self.arm_extent(decl, arm, scope, layout, prefix, variant, state)
			low    = extent.lo if low is None else min(low, extent.lo)
			if extent.hi is None or high is None:
				high = None
			else:
				high = max(high, extent.hi)
				arm_sizes.append((_arm_name(arm), extent.hi))

		total = Interval(low or 0, high)

		# `[equalize]` pads every arm to the largest, which is section 17.0's
		# explicit resolution for unequal arms: the ambiguity is accepted and
		# its consequence reported, or it is paid off in padding.
		if _has_attr(variant.attrs, "equalize"):
			if high is None:
				raise error(
					f"`[equalize]` needs every arm to have a known size",
					variant.span,
					label = "one arm is unbounded",
					notes = ["an `opaque` default arm has no size to pad to"],
				)
			total     = Interval(high, high)
			arm_sizes = []

		layout.placements.insert(slot, Placement(
			path          = f"{prefix}.{variant.name}",
			name          = variant.name,
			kind          = "variant",
			type_name     = "variant",
			offset_bits   = cursor.lo if cursor.is_exact else None,
			size_bits     = total.lo,
			size_max_bits = total.hi,
			scalar        = None,
			endian        = None,
			bit_order     = scope.bit_order,
			span          = variant.span,
			attrs         = variant.attrs,
			dynamic_cause      = state.cause[0] if state.cause else None,
			dynamic_cause_span = state.cause[1] if state.cause else None,
			dynamic_cause_size = state.cause[2] if state.cause else None,
			arm_sizes     = tuple(arm_sizes),
			discriminant  = expr_to_source(variant.discriminant),
			arm_cases     = tuple(arm_cases),
		))

		if state.cause is None and total.hi != total.lo:
			state.cause = (variant.name, variant.span, _render_extent(total))

		state.cursor = cursor.advance(total)

	def arm_extent(self, decl: ast.StructDecl, arm: ast.VariantArm, scope: Scope,
			layout: StructLayout, prefix: str, variant: ast.Variant,
			state: Walk) -> Interval:
		"""Lay one arm out at the variant's base and report its extent.

		Every arm starts where the variant starts: exactly one is present, so
		they overlay rather than follow one another.
		"""
		assert arm.member is not None

		arm_state = Walk(fields=dict(state.fields), cursor=state.cursor,
		                 cause=state.cause)
		before    = state.cursor

		self.place_members(decl, (arm.member,), scope, layout,
		                   f"{prefix}.{variant.name}", arm_state)

		lo = arm_state.cursor.lo - before.lo
		hi = (None if arm_state.cursor.hi is None or before.hi is None
		      else arm_state.cursor.hi - before.hi)
		return Interval(lo, hi)

	def check_not_behind_codec(self, expr: ast.Expr, state: Walk) -> None:
		"""Refuse an expression that names a field inside a coded region.

		The first of section 13.3's three prohibitions, and the one that keeps
		the whole system decidable: if a size or a discriminant could come from
		transform output, the compiler would have to run the transform to answer
		a capability question, and it never does.
		"""
		from situc.expr import path_text

		path = path_text(expr)
		if path is None:
			return

		codec = state.behind_codec.get(path)
		if codec is None:
			return

		raise error(
			f"`{path}` is inside a `{codec}` region and cannot be referenced here",
			expr.span,
			label = "transform output",
			notes = [
				"the expression language may not reference transform output "
				"(project.md section 13.3)",
				"the compiler reasons about property signatures, never about "
				"what a transform produces; a size that depended on decoded "
				"content would make in-place mutability undecidable",
				"move the field outside the region, or size this member from "
				"something outside it",
			],
		)

	def check_discriminant(self, variant: ast.Variant, state: Walk) -> None:
		"""The discriminant must be parsed strictly before the variant.

		Section 9.6 makes a forward reference an error: the selector has to be
		readable before the thing it selects, or nothing can be parsed at all.
		"""
		from situc.expr import path_text

		self.check_not_behind_codec(variant.discriminant, state)

		path = path_text(variant.discriminant)
		if path is None:
			raise error(
				f"variant `{variant.name}` needs a field as its discriminant",
				variant.discriminant.span,
				label = "expected a name such as `kind` or `hdr.type`",
			)

		if path not in state.fields:
			known = ", ".join(sorted(state.fields)[:6]) or "none"
			raise error(
				f"`{path}` is not readable before variant `{variant.name}`",
				variant.discriminant.span,
				label = "not declared yet",
				notes = ["the discriminant must be parsed strictly before the "
				         "variant in layout order (project.md section 9.6)",
				         f"fields in scope here: {known}"],
			)

	def place_marker(self, decl: ast.StructDecl, member: ast.MarkerField,
			scope: Scope, layout: StructLayout, prefix: str, state: Walk) -> None:
		"""A marker field is its backing type, read before any order is known.

		It carries no endianness of its own: the bytes are compared as a
		sequence, which is the only way to read a value whose byte order it is
		itself announcing.
		"""
		marker = self.markers.get(member.name)
		if marker is None:
			raise error(
				f"unknown endian marker `{member.name}`",
				member.span,
				label = "not declared",
				notes = ["declare it with `endian_marker " + member.name
				         + " : u16 { little = ..., big = ..., }`"],
			)

		scalar = marker.backing.scalar
		assert scalar is not None, "the parser rejects a non-scalar backing"

		cursor = state.cursor
		if cursor.is_exact and cursor.lo % BITS_PER_BYTE != 0:
			raise error(
				f"marker `{member.name}` must start on a byte boundary",
				member.span,
				label = f"lands {cursor.lo % BITS_PER_BYTE} bits into a byte",
			)

		layout.placements.append(Placement(
			path          = f"{prefix}.{member.name}",
			name          = member.name,
			kind          = "marker",
			type_name     = member.name,
			offset_bits   = cursor.lo if cursor.is_exact else None,
			size_bits     = scalar.bits,
			size_max_bits = scalar.bits,
			scalar        = scalar,
			endian        = None,
			bit_order     = scope.bit_order,
			span          = member.span,
			attrs         = member.attrs,
			element_bits  = scalar.bits,
		))

		state.fields[member.name] = scalar_interval(scalar.bits, signed=False)
		state.cursor = cursor.advance(Interval.point(scalar.bits))

	def place_one(self, decl: ast.StructDecl, member: ast.Field | ast.Reserved,
			scope: Scope, layout: StructLayout, prefix: str, state: Walk,
			last: bool) -> None:
		if state.closed_by is not None:
			raise error(
				f"nothing may follow `{state.closed_by}`",
				member.span,
				label = "declared after a `[remaining]` member",
				notes = [f"`{state.closed_by}` runs to the end of its frame, so "
				         "this member has no bytes to occupy",
				         "move it before the `[remaining]` member"],
			)

		local = scope.narrow(member.attrs, self.result.env)

		# A reserved region has no name, but the map still has to name it: an
		# entry that collided with the struct's own path would be unreadable and
		# would break `find`. Angle brackets cannot occur in an identifier, so
		# the synthesised name can never clash with a real one.
		if isinstance(member, ast.Field):
			name = member.name
		else:
			name = f"<reserved{layout.reserved_count}>"
			layout.reserved_count += 1

		path   = f"{prefix}.{name}"
		scalar = self.effective_scalar(member, local)
		cursor = state.cursor

		element = self.element_extent(member, local)
		count   = self.array_extent(member, state, last)
		total   = _multiply(element, count)

		if getattr(member, "repeat", None) is not None:
			total = self.repeated_extent(member, state)
		elif member.until is not None:
			total = self.delimited_extent(member, member.until, state)
		elif getattr(member, "radix", None) is not None and member.array is not None:
			# `decimal u16 code[3]` is three *digits*, which is three bytes.
			# The generic path multiplied the count by the scalar's width and
			# made it six -- because everywhere else `[n]` counts elements of
			# the declared type, and here the declared type is the value's
			# domain rather than its storage (8.6.2). Silently a field of the
			# wrong width, which every offset after it inherits.
			digits = count.value() if count.is_point else None
			if digits is not None:
				total = Interval.point(digits * BITS_PER_BYTE)

		self.check_alignment(decl, member, scalar, cursor, element)
		position = self.bit_position(scalar, local, cursor, element)

		layout.placements.append(Placement(
			path           = path,
			name           = name,
			kind           = "field" if isinstance(member, ast.Field) else "reserved",
			type_name      = member.type_ref.name,
			# None for a located member: its offset is whatever the field
			# says, and the cursor position where it was *written* is a
			# different number that reads like an answer.
			offset_bits    = (None if getattr(member, "located", None) is not None
			                  else cursor.lo if cursor.is_exact else None),
			size_bits      = total.lo,
			size_max_bits  = total.hi,
			scalar         = scalar,
			endian         = local.endian,
			bit_order      = local.bit_order,
			span           = member.span,
			attrs          = member.attrs,
			marker         = local.marker,
			varint         = (member.type_ref.name
			                  if member.type_ref.name in self.varints else None),
			varint_minimal = self._varint_minimal(member),
			# Not for a delimited member. `array_extent` returns one run for
			# `x[] until "D"` because that is what a run is, and recording it
			# here said "an array of exactly one element", which is false and
			# was believed: the classifier called it an ARRAY, `doc` labelled
			# it `x[1]` and drew a one-byte box, the dissector read one byte
			# and misaligned the rest of the packet, and gen-checks sized an
			# instance from it. Four consumers, one lie, and each of them
			# needed its own code to disbelieve it.
			# ...and a `while` run is the same lie with a different construct
			# in front of it. How many elements there are is whichever one
			# first fails the condition, which is not a number this knows.
			array_count    = (count.value()
			                  if count.is_point and member.array
			                  and member.until is None
			                  and getattr(member, "repeat", None) is None
			                  else None),
			element_bits   = element.lo if element.is_point else None,
			bit_position   = position,
			frame_relative = False,
			sized_by       = self.sizing_field(member),
			size_expr      = _size_source(member),
			access_mode    = _access_mode(member, decl),
			on_read        = _side_effect(member.attrs, "on_read"),
			on_write       = _side_effect(member.attrs, "on_write"),
			register       = decl.register,
			dynamic_cause      = state.cause[0] if state.cause else None,
			dynamic_cause_span = state.cause[1] if state.cause else None,
			dynamic_cause_size = state.cause[2] if state.cause else None,
			delimiter          = member.until.delimiter if member.until else None,
			repeat_while       = _repeat_source(member),
			repeat_cap         = self._repeat_cap(member),
			radix              = getattr(member, "radix", None),
			radix_minimal      = _has_attr(member.attrs, "minimal"),
			trimmed            = _has_attr(member.attrs, "trim"),
			since              = _since_of(member),
			version_field      = _version_field(decl),
			case_insensitive   = _has_attr(member.attrs, "case_insensitive"),
			delimiter_quote    = _delimiter_byte(member, "quoted"),
			delimiter_escape   = _delimiter_byte(member, "escape"),
			delimiter_cap      = self._scan_cap(member),
			scan_cause         = state.scan[0] if state.scan else None,
			scan_cause_span    = state.scan[1] if state.scan else None,
			located            = _located_source(member),
		))

		if member.until is not None and state.scan is None:
			# The scan begins *after* this member: reaching this one is still
			# arithmetic, and reaching anything past it is a search. Setting it
			# before the placement would have blamed the delimited member for
			# its own delimiter.
			state.scan = (name, member.span)

		if isinstance(member, ast.Field) and member.type_ref.scalar is None:
			self.absorb_nested(member, layout, path, cursor, element,
			                   layout.placements[-1])

		self.record_interval(member, scalar, state, path, name)

		if member.array is not None and isinstance(member.array.size, ast.Remaining):
			state.closed_by = name

		if getattr(member, "located", None) is not None:
			# A located member is a *reference*: it sits where a field says,
			# so it neither follows the member before it nor puts anything
			# after it. Advancing the cursor past it would place the next
			# member at an offset nothing means -- the sum of a running
			# position and an absolute one.
			#
			# It also cannot make anything after it dynamic, for the same
			# reason: what follows a located member is whatever followed the
			# member before it.
			return

		if state.cause is None and total.hi != total.lo:
			state.cause = (name, member.span, _render_extent(total))

		state.cursor = cursor.advance(total)

	def repeated_extent(self, member: ast.Field | ast.Reserved,
			state: Walk) -> Interval:
		"""How many bits a `while` run occupies (section 8.6.6).

		At least one element, because the first is parsed before the predicate
		is evaluated -- a `while` run is never empty, and whether the run is
		there at all is a `variant`'s question rather than this one's.

		Unbounded above unless `max N` caps the count, which is the only thing
		that can: how many elements there are is in the data.
		"""
		repeat = getattr(member, "repeat", None)
		assert repeat is not None

		# `self.structs` holds declarations; the laid-out element is in the
		# result, and asking the wrong one for a size is how this first
		# reached for an attribute a `StructDecl` does not have.
		element = self.result.structs.get(member.type_ref.name)
		floor   = element.size_bits if element is not None else BITS_PER_BYTE
		cap     = self._repeat_cap(member)

		if cap is None:
			return Interval(floor, None)

		ceiling = element.size_max_bits if element is not None else None
		return Interval(floor, None if ceiling is None else ceiling * cap)

	def _repeat_cap(self, member: ast.Field | ast.Reserved) -> int | None:
		repeat = getattr(member, "repeat", None)
		if repeat is None or repeat.cap is None:
			return None
		return evaluate(repeat.cap, self.result.env)

	def delimited_extent(self, member: ast.Member,
			until: ast.Until, state: Walk) -> Interval:
		"""How many bits a delimited member occupies, delimiter included.

		The delimiter is part of the member's extent even though it is not part
		of its value, for the same reason `nul_terminated` counts its capacity:
		members partition their struct's bytes exactly, and a delimiter nobody
		owned would be a hole between two members.

		The minimum is an empty content plus the delimiter. The maximum is the
		cap where one is given, and unbounded otherwise -- which is the honest
		answer, and the reason `until D max N` exists for callers who need a
		smaller promise than "as far as the buffer goes".
		"""
		floor = len(until.delimiter) * BITS_PER_BYTE

		if until.cap is None:
			return Interval(floor, None)

		env  = self.result.env.with_layout(self.result.lookup, self.result.explain).with_fields(state.fields)
		size = interval_of(until.cap, env)

		if size.hi is None or size.hi < len(until.delimiter):
			raise error(
				f"a scan capped below its own delimiter can never succeed",
				until.cap.span,
				label = f"cap is {size.render()} bytes",
				notes = [f"the delimiter is {len(until.delimiter)} byte(s), so "
				         f"a member of at most {size.hi} could not hold it",
				         "the cap bounds the whole member, delimiter included"],
			)

		return Interval(floor, size.hi * BITS_PER_BYTE)

	def _scan_cap(self, member: ast.Field | ast.Reserved) -> int | None:
		if member.until is None or member.until.cap is None:
			return None
		return evaluate(member.until.cap, self.result.env)

	def _varint_minimal(self, member: ast.Field | ast.Reserved) -> bool:
		varint = self.varints.get(member.type_ref.name)
		return True if varint is None else varint.minimal

	def record_interval(self, member: ast.Field | ast.Reserved,
			scalar: ScalarType | None, state: Walk, path: str, name: str) -> None:
		"""Make this member visible to the size expressions that follow it.

		Only scalars are recorded: a struct has no value, and an array's
		elements are not addressable from an expression.
		"""
		if not isinstance(member, ast.Field):
			return

		# An array has no single value, so nothing about it is nameable.
		if member.array is not None:
			return

		if member.type_ref.name in self.structs:
			self.record_nested_intervals(member, state, name)
			return

		# A varint is a value like any other, and its range is what `max_bits`
		# declares. It was recorded nowhere, so `u8 payload[n]` for a varint
		# `n` was refused with "no fields are in scope at this point" -- which
		# reads as though nothing preceded it. Section 9.7 calls describing
		# protobuf impossible without varints, and a length-prefixed field is
		# what a varint is usually for.
		varint = self.varints.get(member.type_ref.name)
		if varint is not None:
			state.fields[name] = self.constrain(
				scalar_interval(varint.max_bits,
				                signed=varint.transform is not None),
				member.attrs)
			return

		if scalar is None:
			return

		state.fields[name] = self.constrain(
			scalar_interval(scalar.bits, scalar.signed), member.attrs)

	def record_nested_intervals(self, member: ast.Field, state: Walk,
			name: str) -> None:
		"""A nested struct's scalars are visible as `member.field`.

		This is what lets `u8 opts[hdr.length]` work: `hdr` is a struct, and
		`length` is one of its fields.
		"""
		nested = self.result.structs.get(member.type_ref.name)
		if nested is None:
			return

		for inner in nested.placements:
			if inner.kind != "field" or inner.scalar is None:
				continue
			if inner.array_count is not None:
				continue
			tail = inner.path[len(nested.name) + 1 :]
			if "." in tail:
				continue
			state.fields[f"{name}.{tail}"] = self.constrain(
				scalar_interval(inner.scalar.bits, inner.scalar.signed), inner.attrs)

	def constrain(self, base: Interval, attrs: tuple[ast.Attr, ...]) -> Interval:
		"""Narrow a field's range by the constraints it declares.

		`[must_eq = 4]` collapses the interval to a point, which is what makes
		`x[hdr.n]` a fixed array rather than a dynamic one. Section 10 states
		this outright, and it is the whole reason a schema can pin a generic
		format into a static one.
		"""
		lo, hi = base.lo, base.hi
		env    = self.result.env

		for attr in attrs:
			if attr.value is None:
				continue
			try:
				value = evaluate(attr.value, env)
			except SituError:
				continue

			if attr.name == "must_eq":
				return Interval.point(value)
			if attr.name == "max":
				hi = value if hi is None else min(hi, value)
			elif attr.name == "min":
				lo = max(lo, value)

		return Interval(lo, hi)

	def sizing_field(self, member: ast.Field | ast.Reserved) -> str | None:
		"""The field whose value drives this member's size, for blame."""
		if member.array is None or member.array.size is None:
			return None
		if isinstance(member.array.size, ast.Remaining):
			return "remaining"
		from situc.expr import path_text
		return path_text(member.array.size)

	def absorb_nested(self, member: ast.Field, layout: StructLayout, path: str,
			cursor: Extent, element: Interval, parent: Placement) -> None:
		"""Copy a nested struct's placements into the parent, rebased.

		The map names every reachable field, so `Header.flags.urgent` has to
		appear with its offset relative to the outermost struct.

		Three cases, and the difference between them is the whole of section
		12.2. An array's elements are described once, frame-relative, because
		`recs[].value` names every element rather than one. A struct at a
		dynamic offset is likewise frame-relative: its interior is static
		relative to a base nobody knows yet, which is the island of staticness.
		A struct at a known offset simply adds.
		"""
		nested = self.result.structs.get(member.type_ref.name)
		if nested is None:
			return

		array  = member.array is not None
		suffix = "[]" if array else ""
		framed = array or not cursor.is_exact

		# The element base moves only when the array itself is dynamically
		# placed. A fixed array at a known offset is frame-relative in its
		# offsets but nothing can shift it.
		base_dynamic = not cursor.is_exact
		cause        = (parent.dynamic_cause, parent.dynamic_cause_span,
		                parent.dynamic_cause_size) if parent.dynamic_cause else None

		# The element itself gets an entry, not only its members. Section 11.4
		# names `Message.recs[]` as a thing with a size and an offset, because
		# that is what a capability requirement addresses.
		if array:
			layout.placements.append(Placement(
				path           = f"{path}[]",
				name           = f"{member.name}[]",
				kind           = "element",
				type_name      = member.type_ref.name,
				offset_bits    = 0,
				size_bits      = element.lo,
				size_max_bits  = element.hi,
				scalar         = None,
				endian         = None,
				bit_order      = None,
				span           = member.span,
				frame_relative = True,
				frame_base_dynamic = base_dynamic,
				dynamic_cause      = cause[0] if cause else None,
				dynamic_cause_span = cause[1] if cause else None,
				dynamic_cause_size = cause[2] if cause else None,
			))

		for inner in nested.placements:
			tail = inner.path[len(nested.name) :]

			if framed:
				offset = inner.offset_bits
			elif inner.offset_bits is None:
				offset = None
			else:
				offset = cursor.lo + inner.offset_bits

			# `replace` rather than a field list. What differs between a
			# member and the same member seen from its parent is the path, the
			# offset it is measured from, and what it cost to reach -- and
			# nothing else, because it is the same bytes.
			#
			# The list this replaces was hand-maintained and had fallen
			# behind by six fields, among them `repeat_while`: a run inside a
			# nested struct lost the fact that it was a run, and read
			# `access=Random` in the parent's map while the identical bytes
			# read `access=Sequential` under their own struct. It survived
			# because `array_count` was also being set to a false 1, and the
			# generic array row happened to reach the same answer.
			layout.placements.append(replace(
				inner,
				path           = f"{path}{suffix}{tail}",
				offset_bits    = offset,
				frame_relative = framed or inner.frame_relative,
				frame_base_dynamic = base_dynamic or inner.frame_base_dynamic,
				dynamic_cause      = cause[0] if cause else None,
				dynamic_cause_span = cause[1] if cause else None,
				dynamic_cause_size = cause[2] if cause else None,
			))

	# -- widths -----------------------------------------------------------

	def effective_scalar(self, member: ast.Field | ast.Reserved,
			scope: Scope) -> ScalarType | None:
		"""The scalar a field actually stores, seeing through an enum.

		An enum is its backing type as far as layout and representation go
		(section 8.7 makes the backing type mandatory for exactly this reason),
		so a `MsgType` field backed by `u8` must report the same `repr` and
		`atomic` as a plain `u8`. Only a struct-typed field has no scalar.
		"""
		if member.type_ref.scalar is not None:
			return self.narrowed(member, member.type_ref.scalar)

		enum = self.enums.get(member.type_ref.name)
		if enum is not None:
			return enum.backing.scalar

		return None

	def narrowed(self, member: ast.Field | ast.Reserved,
			scalar: ScalarType) -> ScalarType:
		"""A BCD field's declared width, where it declares one (8.1).

		`bcd<d>` is a nibble a digit, which is what a display wants and what a
		register file usually is not: a DS1307 spends the top bit of its
		seconds register on Clock Halt and two bits of its hours register on
		12/24 and PM, leaving seven and six bits of decimal under them. Every
		driver masks those off before decoding, which is the work a
		description exists to remove -- and `bcd2` at eight bits could not
		describe the register at all (26.35).

		`[bits = N]` narrows the *top* digit and leaves the rest whole, which
		is what the hardware does: three bits of tens above four of units is
		0..79, and the field's own `[max]` says which of those are meant.
		Everything else follows the ordinary bit-packing rules -- this is a
		seven-bit field, and the byte it shares is the schema's to lay out.
		"""
		attr = next((one for one in member.attrs if one.name == "bits"), None)
		if attr is None:
			return scalar

		if not scalar.is_bcd:
			raise error(
				f"`[bits]` is for a packed-decimal field, and "
				f"`{member.type_ref.name}` is not one",
				attr.span,
				label = "not a `bcd` type",
				notes = ["every other type carries its width in its name --"
				         " `u7` is seven bits",
				         "`bcd<d>` names digits rather than bits, which is why"
				         " it is the one that can be narrowed"],
			)

		if attr.value is None:
			raise error(
				"`[bits]` needs a width",
				attr.span,
				label = "no value",
				notes = [f"`{member.type_ref.name}` is {scalar.bits} bits;"
				         " say how many of them this field takes",
				         "for example `[bits = 7]`, which is a control bit"
				         " above two decimal digits"],
			)

		bits  = evaluate(attr.value, self.result.env)
		floor = (scalar.digits - 1) * BITS_PER_DIGIT

		if bits > scalar.bits:
			raise error(
				f"`{member.type_ref.name}` is {scalar.bits} bits and this asks"
				f" for {bits}",
				attr.span,
				label = "wider than the type",
				notes = ["`[bits]` narrows a packed-decimal field; padding it"
				         " out would be a different type"],
			)
		if bits <= floor:
			raise error(
				f"{bits} bits leaves no room for {scalar.digits} digits",
				attr.span,
				label = f"needs more than {floor}",
				notes = [f"the lower {scalar.digits - 1} digit(s) are a whole"
				         f" nibble each, which is {floor} bits",
				         "the top digit takes what is left, and cannot take"
				         " nothing"],
			)

		return replace(scalar, bits=bits)

	def element_extent(self, member: ast.Field | ast.Reserved,
			scope: Scope) -> Interval:
		"""The size of one element, which may itself be a range for a frame."""
		type_ref = member.type_ref

		if type_ref.scalar is not None:
			# `narrowed` first: a `bcd2 [bits = 7]` is a seven-bit field, and
			# asking the type for its width places it as eight and reports it
			# as straddling the byte it fits in.
			scalar = self.narrowed(member, type_ref.scalar)
			self.check_directives(member, scalar, scope)
			return Interval.point(scalar.bits)

		# A varint carries its own byte order in its encoding, so it needs no
		# `endian` in scope: the continuation bit decides the order.

		enum = self.enums.get(type_ref.name)
		if enum is not None:
			backing = enum.backing.scalar
			assert backing is not None, "parser rejects non-scalar enum backing"
			self.check_directives(member, backing, scope)
			return Interval.point(backing.bits)

		varint = self.varints.get(type_ref.name)
		if varint is not None:
			# Section 8.1.1: one to ceil(max_bits / 7) bytes. The lower bound is
			# one byte even for a zero value, because the continuation bit has
			# to be somewhere.
			return Interval(BITS_PER_BYTE, varint.max_bytes * BITS_PER_BYTE)

		if type_ref.name in self.structs:
			nested = self.layout_of(type_ref.name)
			return Interval(nested.size_bits, nested.size_max_bits)

		raise error(f"unknown type `{type_ref.name}`", type_ref.span, label="not declared")

	def array_extent(self, member: ast.Field | ast.Reserved, state: Walk,
			last: bool) -> Interval:
		"""How many elements, as a range.

		A single-point range is a fixed array however it was written, so a
		count driven by a `[must_eq]` field is as static as a literal.
		"""
		if member.array is None:
			return Interval.point(1)

		if member.array.size is None:
			# `until` and `while` are the other two things that can say where
			# an array stops, and both say it after the brackets rather than
			# inside them: the length is not a number the schema knows, it is
			# wherever the delimiter turns out to be (8.6.1) or wherever the
			# condition first fails (8.6.6).
			if member.until is not None or getattr(member, "repeat", None):
				return Interval.point(1)
			raise error(
				"an array needs a size here",
				member.array.span,
				label = "expected a length",
				notes = ["the empty form `[]` is only legal inside an `indexed` "
				         "region, or with `until` or `while` to say where it "
				         "stops"],
			)

		if isinstance(member.array.size, ast.Remaining):
			if not last:
				raise error(
					"`[remaining]` must be the last member of its frame",
					member.array.span,
					label = "runs to the end of the frame",
					notes = ["a member after this one would have no bytes to "
					         "occupy (project.md section 8.5)"],
				)
			# The frame's extent is not known here, and for a top-level struct
			# it is not known at all. Unbounded is the honest answer.
			return Interval(0, None)

		self.check_not_behind_codec(member.array.size, state)

		env   = self.result.env.with_layout(self.result.lookup, self.result.explain).with_fields(state.fields)
		count = interval_of(member.array.size, env)

		if not count.lo_known:
			# Distinct from the negative case, and worth its own sentence: the
			# solver has not shown this is negative, it has failed to show
			# that it is not. That used to read as `[0, inf]` and pass, the
			# widening having handed the check the answer it was looking for.
			raise error(
				"array length has no lower bound the solver can derive",
				member.array.size.span,
				label = "may be negative",
				notes = ["every field an expression reads has to be bounded for "
				         "its result to be (project.md section 8.5)",
				         "`[min = N]` on the fields it reads is what supplies one"],
			)

		if count.lo < 0:
			raise error(f"array length {count.render()} may be negative",
			            member.array.size.span, label="must be zero or more")

		if count.is_point and count.value() == 0:
			raise error(
				"array length is zero",
				member.array.size.span,
				label = "evaluates to 0",
				notes = ["a zero-length field occupies no bytes and can never be "
				         "read or written; remove it instead"],
			)

		return count

	# -- placement rules --------------------------------------------------

	def check_directives(self, member: ast.Field | ast.Reserved,
			scalar: ScalarType, scope: Scope) -> None:
		"""Section 17.0: a missing directive is an error, never a default.

		Endianness matters only for a scalar wider than a byte, and bit order
		only for one that packs. Demanding both everywhere would make trivial
		schemas noisy for no safety gain.
		"""
		if scalar.bits > BITS_PER_BYTE and not scope.has_byte_order:
			raise error(
				f"no endianness in scope for `{member.type_ref.name}`",
				member.span,
				label = "multi-byte scalar with no byte order",
				notes = ["add `endian big;` or `endian little;` at file level, or "
				         "`[endian = ...]` on the struct or the field",
				         "situ never guesses where the wrong choice is "
				         "undetectable at runtime (project.md section 17.0)"],
			)

		if scalar.is_bit_packed and scope.bit_order is None:
			raise error(
				f"no bit order in scope for `{member.type_ref.name}`",
				member.span,
				label = "sub-byte field with no bit order",
				notes = ["add `bit_order msb_first;` or `bit_order lsb_first;` at "
				         "file level, or `[bit_order = ...]` on the struct"],
			)

	def check_alignment(self, decl: ast.StructDecl, member: ast.Field | ast.Reserved,
			scalar: ScalarType | None, extent: Extent, element: Interval) -> None:
		"""Byte-aligned types must start on a byte boundary.

		Situ inserts no implicit padding (section 8.4), so a whole-byte type
		landing mid-byte cannot be nudged into place. Saying so is the whole
		point: the alternative is a layout that silently disagrees with the
		format being described.

		`scalar` is the effective one, so an `enum E : u4` packs exactly as a
		bare `u4` does. Reading the syntactic type here would make a sub-byte
		enum demand a byte boundary it can never have.
		"""
		packed = scalar is not None and scalar.is_bit_packed

		if not extent.is_exact:
			# A dynamic cursor cannot be checked here, and a bit-packed field
			# cannot be placed against one: its bit phase within the resolved
			# byte is not something this phase computes. Refused rather than
			# guessed -- a wrong bit offset is undetectable at runtime.
			if packed:
				raise error(
					f"`{member.type_ref.name}` is bit-packed and cannot follow a "
					"dynamically sized member",
					member.span,
					label = "no resolvable bit position",
					notes = [
						"the byte it lands in is only known at parse time, and "
						"bit phase across a dynamic boundary arrives with the "
						"sub-byte codecs of phase 12",
						"move this field before the dynamic member, or widen it "
						"to a whole number of bytes",
					],
				)
			return

		cursor = extent.lo
		width  = element.lo

		# Both checks below are about buffers, where the byte is the unit of
		# access and a field crossing one costs a read-modify-write. A register
		# is reached as a single access of `access_width` bits, so every field
		# in it is a bit range within one word: starting mid-byte costs nothing,
		# and crossing a byte boundary costs nothing either.
		if decl.register is not None:
			return

		if not packed and cursor % BITS_PER_BYTE != 0:
			stray = cursor % BITS_PER_BYTE
			raise error(
				f"`{member.type_ref.name}` must start on a byte boundary",
				member.span,
				label = f"lands {stray} bit{'s' if stray != 1 else ''} into byte "
				        f"{cursor // BITS_PER_BYTE}",
				notes = [
					f"the preceding bit fields occupy {stray} of 8 bits",
					"situ inserts no implicit padding (project.md section 8.4); "
					f"add `reserved u{BITS_PER_BYTE - stray};` to close the byte",
				],
			)

		if not packed:
			return

		first = cursor // BITS_PER_BYTE
		last  = (cursor + width - 1) // BITS_PER_BYTE
		if first == last or _has_attr(decl.attrs, "allow_straddle"):
			return

		raise error(
			f"bit field `{member.type_ref.name}` straddles a byte boundary",
			member.span,
			label = f"occupies bits {cursor % BITS_PER_BYTE}..{cursor % BITS_PER_BYTE + width - 1} "
			        f"from byte {first}",
			notes = [
				"straddling silently forces a multi-byte read-modify-write, so it "
				"has to be declared (project.md section 8.2)",
				f"add `[allow_straddle]` to `struct {decl.name}`, or reorder the "
				"fields so this one fits inside a byte",
			],
		)

	def bit_position(self, scalar: ScalarType | None, scope: Scope,
			extent: Extent, element: Interval) -> BitPosition | None:
		if scalar is None or not scalar.is_bit_packed or not extent.is_exact:
			return None

		cursor = extent.lo
		width  = element.lo
		local  = cursor % BITS_PER_BYTE
		first = cursor // BITS_PER_BYTE
		last  = (cursor + width - 1) // BITS_PER_BYTE

		# msb_first fills from the most significant bit down, so a field's shift
		# is what remains below it in the byte. lsb_first fills upward, so the
		# shift is the offset itself.
		if scope.bit_order is ast.BitOrder.MSB_FIRST:
			shift = BITS_PER_BYTE - local - width if width <= BITS_PER_BYTE - local else 0
		else:
			shift = local

		return BitPosition(
			byte           = first,
			offset_in_byte = local,
			width          = width,
			shift          = shift,
			straddles      = first != last,
		)

	# -- pins -------------------------------------------------------------

	def check_pins(self) -> None:
		"""A pin asserts the solved offset; it never places a field.

		This is the bug class field numbers were incidentally papering over
		(project.md section 4): insert a field above a pinned one and the pin
		catches the drift.
		"""
		env = self.result.env.with_layout(self.result.lookup, self.result.explain)

		for name, decl in self.structs.items():
			layout = self.result.structs[name]
			for member in _all_fields(decl.members):
				if member.pin is None:
					continue

				placement = _find(layout, f"{name}.{member.name}")
				assert placement is not None, "every field is placed"

				expected = evaluate(member.pin, env)
				self.check_pin(member, placement, expected)

	def check_pin(self, member: ast.Field, placement: Placement, expected: int) -> None:
		if placement.offset_bits is None:
			raise error(
				f"`{member.name}` has no static offset, so the pin cannot hold",
				member.pin.span if member.pin else member.span,
				label = f"pinned to {_hex(expected)}",
				notes = [
					f"a dynamically sized member precedes it"
					+ (f": `{placement.sized_by}` drives it" if placement.sized_by
					   else ""),
					"a pin asserts a compile-time offset; move the field before "
					"the dynamic member, or drop the pin",
				],
			)

		if not placement.is_byte_aligned:
			raise error(
				f"`{member.name}` is not at a byte offset, so the pin cannot hold",
				member.pin.span if member.pin else member.span,
				label = f"pinned to byte {expected}",
				notes = [
					f"solved offset is byte {placement.offset_bytes}, bit "
					f"{placement.offset_bits % BITS_PER_BYTE}",
					"`@` takes a byte offset (project.md section 27.5)",
				],
			)

		if placement.offset_bytes == expected:
			return

		drift = placement.offset_bytes - expected
		raise error(
			f"offset pin on `{member.name}` does not match the computed layout",
			member.pin.span if member.pin else member.span,
			label = f"pinned to {_hex(expected)}, solved to {_hex(placement.offset_bytes)}",
			notes = [
				f"the field sits {abs(drift)} byte{'s' if abs(drift) != 1 else ''} "
				f"{'later' if drift > 0 else 'earlier'} than the pin claims",
				"a pin asserts the solver's answer, it does not place the field "
				"(project.md section 4)",
				f"either correct the pin to {_hex(placement.offset_bytes)}, or fix "
				"the layout above this field",
			],
		)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _path_of(expr: ast.Expr) -> str | None:
	from situc.expr import path_text
	return path_text(expr)


def _expand(codec: ast.CodecDecl, interior: Interval) -> Interval:
	"""Output extent as a function of input extent (section 13.2).

	Pure property arithmetic: the expansion form decides this and nothing else
	does. An exact ratio keeps the output a linear function of the input, which
	is why interior offsets survive it and why the earlier draft's blanket
	"all ratios are unbounded" was wrong -- Manchester at 2:1 is the
	counterexample.
	"""
	if codec.expansion is ast.Expansion.PRESERVING:
		return interior

	if codec.expansion is ast.Expansion.FIXED_ADD:
		added = codec.expansion_add * BITS_PER_BYTE
		return Interval(interior.lo + added,
		                None if interior.hi is None else interior.hi + added)

	if codec.expansion is ast.Expansion.UNBOUNDED:
		return Interval(interior.lo, None)

	# A ratio may carry an addend as well, which only a pipeline produces: a
	# stage that appends parity followed by one that expands scales the parity
	# too. The form stays the ratio, because the form is what decides whether
	# interior positions survive, and the addend rides along in the arithmetic
	# (docs/decisions/0016-composed-expansion.md).
	assert codec.ratio is not None
	numerator, denominator = codec.ratio
	added = codec.expansion_add * BITS_PER_BYTE

	def scale(bits: int) -> int:
		return -(-bits * numerator // denominator) + added	# ceil, then the addend

	if codec.expansion is ast.Expansion.RATIO_EXACT:
		return Interval(scale(interior.lo),
		                None if interior.hi is None else scale(interior.hi))

	if codec.expansion is ast.Expansion.RATIO_PADDED:
		# A group is the smallest run of input that is both a whole number of
		# bytes and a whole number of symbols, so the group size follows from
		# the ratio rather than being declared: base64's six-bit symbols give
		# lcm(8, 6) = 24 input bits, and base32's five-bit ones give 40.
		group_in  = lcm(BITS_PER_BYTE, denominator)
		group_out = group_in // denominator * numerator

		def pad(bits: int) -> int:
			return -(-bits // group_in) * group_out + added

		return Interval(pad(interior.lo),
		                None if interior.hi is None else pad(interior.hi))

	# ratio_bounded: worst case known, actual data-dependent.
	return Interval(interior.lo,
	                None if interior.hi is None else scale(interior.hi))


def _arm_name(arm: ast.VariantArm) -> str:
	if arm.member is None:
		return "default"
	if isinstance(arm.member, ast.Field):
		return arm.member.name
	return "arm"


def _tlv_grammar(member: ast.Tlv) -> TlvGrammar:
	"""The region's item grammar, as a backend needs it.

	The AST nodes carry spans and the schema's own expression trees; a backend
	needs the arithmetic and the numbers. This is the same lowering `Arm` gets
	for a variant, for the same reason.
	"""
	length = member.argument("length_type")

	return TlvGrammar(
		tag_decode  = tuple(TagPart(part.name, unparse.expr_to_source(part.value))
		                    for part in member.tag_decode),
		selector    = member.value_size.selector if member.value_size else None,
		rules       = tuple(_value_rule(case) for case in
		                    (member.value_size.cases if member.value_size else ())),
		known       = tuple(KnownTag(tag.tag, tag.name, tag.wire, tag.type_name,
		                             tag.repeated) for tag in member.known),
		length_type = length.name if isinstance(length, ast.NameRef) else None,
		identity    = member.identity_part(),
	)


def _value_rule(case: ast.ValueCase) -> ValueRule:
	if isinstance(case.rule, ast.FixedValue):
		return ValueRule(case.label, "fixed", size=case.rule.size)
	if isinstance(case.rule, ast.PrefixedValue):
		return ValueRule(case.label, "prefixed",
		                 length_type=case.rule.length_type)
	if isinstance(case.rule, ast.SelfDelimiting):
		return ValueRule(case.label, "self_delimiting")
	return ValueRule(case.label, "error")


def _render_extent(size: Interval) -> str:
	lo = size.lo // BITS_PER_BYTE
	if size.hi is None:
		return f"Unbounded (at least {lo} bytes)"
	return f"Bounded({lo}, {size.hi // BITS_PER_BYTE})"


def _multiply(element: Interval, count: Interval) -> Interval:
	"""Total extent of `count` elements of `element` size."""
	if element.hi is None or count.hi is None:
		return Interval(element.lo * count.lo, None)
	return Interval(element.lo * count.lo, element.hi * count.hi)


def _scopes(schema: ast.Schema) -> dict[str, Scope]:
	"""The byte and bit order in force at each struct, by declaration position.

	A directive applies to what follows it and to nothing before it, which is
	what anyone reading top to bottom assumes. It used to apply to the whole
	file with the last one winning, so `endian native;` on line 3 silently
	rewrote the struct on line 2 -- a wrong answer that no diagnostic could
	reach, since both readings produce a valid layout.

	Positional scoping is also what lets one file describe a protocol whose
	layers disagree about byte order, without giving every struct in it an
	`[endian = ...]` attribute.
	"""
	endian: ast.Endian | None      = None
	bit_order: ast.BitOrder | None = None
	scopes: dict[str, Scope]       = {}

	for decl in schema.decls:
		if isinstance(decl, ast.EndianDirective):
			endian = decl.endian
		elif isinstance(decl, ast.BitOrderDirective):
			bit_order = decl.bit_order
		elif isinstance(decl, ast.StructDecl):
			scopes[decl.name] = Scope(endian, bit_order)

	return scopes


ACCESS_MODES = {mode.value: mode for mode in ast.AccessMode}
SIDE_EFFECTS = {effect.value: effect for effect in ast.SideEffect}


def _access_mode(member: ast.Field | ast.Reserved,
		decl: ast.StructDecl) -> ast.AccessMode | None:
	"""The access mode written on a register field.

	`rw` where a register field says nothing: it is the mode that claims least
	about the hardware, and a field the schema does not describe is one the
	compiler should not assume is special. Outside a register there is no mode
	at all -- bytes in a buffer are not reached over a bus.
	"""
	if decl.register is None or isinstance(member, ast.Reserved):
		return None

	for attr in member.attrs:
		mode = ACCESS_MODES.get(attr.name)
		if mode is not None:
			return mode
	return ast.AccessMode.RW


def _side_effect(attrs: tuple[ast.Attr, ...], name: str) -> ast.SideEffect:
	for attr in attrs:
		if attr.name != name:
			continue
		if isinstance(attr.value, ast.NameRef):
			effect = SIDE_EFFECTS.get(attr.value.name)
			if effect is not None:
				return effect
		raise error(f"`{name}` needs one of "
		            f"{', '.join(sorted(SIDE_EFFECTS))}",
		            attr.span, label="not a side effect")
	return ast.SideEffect.NONE


def _has_attr(attrs: tuple[ast.Attr, ...], name: str) -> bool:
	return any(attr.name == name for attr in attrs)


def _all_fields(members: tuple[ast.Member, ...]) -> list[ast.Field]:
	found: list[ast.Field] = []
	for member in members:
		if isinstance(member, ast.PositionalBlock):
			found.extend(_all_fields(member.members))
		elif isinstance(member, ast.Field):
			found.append(member)
	return found


def _find(layout: StructLayout, path: str) -> Placement | None:
	for placement in layout.placements:
		if placement.path == path:
			return placement
	return None


def _hex(value: int) -> str:
	return f"0x{value:02X}"


def _first_versioned(layout: StructLayout) -> int | None:
	"""Where the first `[since]` member starts, which is version 1's extent."""
	starts = [placement.offset_bits for placement in layout.placements
	          if placement.since is not None and placement.offset_bits is not None]
	return min(starts) if starts else None


def _since_of(member: ast.Field | ast.Reserved) -> int | None:
	from situc.wellformed import _since_of as read

	return read(member)


def _version_field(decl: ast.StructDecl) -> str | None:
	from situc.wellformed import _version_field as read

	return read(decl)


def _size_source(member: ast.Field | ast.Reserved) -> str | None:
	"""An array's size as source, when it is more than a field reference."""
	from situc.unparse import expr_to_source

	array = getattr(member, "array", None)
	if array is None or array.size is None:
		return None
	if isinstance(array.size, ast.Remaining) or _path_of(array.size) is not None:
		return None		# `[remaining]` and `[n]` are already handled

	# And a constant. `octets[4]` has no field path either, and reporting it
	# here made `traverse.classify` call a fixed array variable -- which is
	# the same fact `array_count` already carries, said a second time and
	# believed instead.
	if not paths_in(array.size):
		return None

	return expr_to_source(array.size)


def _located_source(member: ast.Field | ast.Reserved) -> str | None:
	from situc.unparse import expr_to_source

	located = getattr(member, "located", None)
	return None if located is None else expr_to_source(located)


def _repeat_source(member: ast.Field | ast.Reserved) -> str | None:
	from situc.unparse import expr_to_source

	repeat = getattr(member, "repeat", None)
	return None if repeat is None else expr_to_source(repeat.predicate)


def _delimiter_byte(member: ast.Field | ast.Reserved, name: str) -> int | None:
	"""The one byte `[quoted = '"']` or `[escape = '\\']` names.

	One byte, not a string: quoting is a state toggle and escaping applies to
	the byte after, and neither generalises to a sequence without becoming a
	grammar. A schema that needs more than this needs a parser, which is the
	line docs/decisions/0020-delimited-data.md draws.
	"""
	if member.until is None:
		return None

	for attr in member.attrs:
		if attr.name != name or attr.value is None:
			continue
		if not isinstance(attr.value, ast.StringLiteral):
			raise error(
				f"`{name}` takes a one-byte string",
				attr.span,
				label = "expected a string literal",
				notes = [f'`[{name} = "\\\\"]`, the byte that makes a delimiter '
				         "inert"],
			)
		raw = attr.value.value.encode("latin-1")
		if len(raw) != 1:
			raise error(
				f"`{name}` takes exactly one byte, not {len(raw)}",
				attr.span,
				label = f"{len(raw)} bytes",
				notes = ["quoting is a state toggle and escaping applies to the "
				         "byte after it; neither generalises to a sequence "
				         "without becoming a grammar"],
			)
		return raw[0]
	return None
