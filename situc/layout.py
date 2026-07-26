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

from dataclasses import dataclass, field, replace
from enum import Enum
from typing import TypeVar

from situc import ast
from situc.diagnostics import SituError, Span, error
from situc.expr import Env, Interval, build_env, evaluate, interval_of, scalar_interval
from situc.types import ScalarType, lookup

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
	# The codec transforming this region, or the one whose region contains it.
	codec: str | None		= None

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
	# Fields that exist only after a transform has run. Recorded so a reference
	# to one gets the decidability diagnostic of section 13.3 rather than a
	# bare "not in scope".
	behind_codec: dict[str, str] = field(default_factory=dict)


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
		self.file_scope = _file_scope(schema)

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
		scope  = self.file_scope.narrow(decl.attrs, self.result.env)
		layout = StructLayout(name=name, size_bits=0, span=decl.span)

		# `fields` accumulates as the walk proceeds, so a size expression can
		# only ever name a field declared before it. That is the
		# no-forward-reference rule of section 10, enforced by construction
		# rather than by a check.
		state  = Walk(fields={}, cursor=Extent(0, 0))
		self.place_members(decl, decl.members, scope, layout, name, state)

		layout.size_bits     = state.cursor.lo
		layout.size_max_bits = state.cursor.hi
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
			elif isinstance(member, ast.Coded):
				self.place_coded(decl, member, scope, layout, prefix, state)
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
		env    = self.result.env.with_layout(self.result.lookup).with_fields(state.fields)
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

	def place_coded(self, decl: ast.StructDecl, region: ast.Coded, scope: Scope,
			layout: StructLayout, prefix: str, state: Walk) -> None:
		"""A region whose bytes are the output of a transform (section 13.5).

		The interior is laid out as though untransformed, because that is what
		it is once decoded; the region's own extent is the interior's extent put
		through the codec's expansion. The lattice reads the property signature
		and nothing else -- this function never learns what the algorithm does.
		"""
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

		layout.placements.insert(slot, Placement(
			path          = f"{prefix}.{region.name}",
			name          = region.name,
			kind          = "coded",
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
			dynamic_cause      = state.cause[0] if state.cause else None,
			dynamic_cause_span = state.cause[1] if state.cause else None,
			dynamic_cause_size = state.cause[2] if state.cause else None,
		))

		# Everything the region contains carries the codec, so the propagation
		# table can read it without walking back up the tree.
		for index in range(slot + 1, len(layout.placements)):
			held = layout.placements[index]
			layout.placements[index] = replace(held, codec=region.codec)

		if state.cause is None and extent.hi != extent.lo:
			state.cause = (region.name, region.span, _render_extent(extent))

		state.cursor = cursor.advance(extent)

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
		env      = self.result.env.with_layout(self.result.lookup).with_fields(state.fields)
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
			endian        = None,
			bit_order     = scope.bit_order,
			span          = member.span,
			sized_by      = _path_of(count_expr),
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

		low: int | None  = None
		high: int | None = 0
		arm_sizes: list[tuple[str, int]] = []

		for arm in variant.arms:
			if arm.member is None:
				# `error` rejects the message and `opaque` consumes the rest;
				# neither contributes a fixed extent of its own.
				if arm.is_opaque:
					high = None
				continue

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

		self.check_alignment(decl, member, scalar, cursor, element)
		position = self.bit_position(scalar, local, cursor, element)

		layout.placements.append(Placement(
			path           = path,
			name           = name,
			kind           = "field" if isinstance(member, ast.Field) else "reserved",
			type_name      = member.type_ref.name,
			offset_bits    = cursor.lo if cursor.is_exact else None,
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
			array_count    = count.value() if count.is_point and member.array else None,
			element_bits   = element.lo if element.is_point else None,
			bit_position   = position,
			frame_relative = False,
			sized_by       = self.sizing_field(member),
			dynamic_cause      = state.cause[0] if state.cause else None,
			dynamic_cause_span = state.cause[1] if state.cause else None,
			dynamic_cause_size = state.cause[2] if state.cause else None,
		))

		if isinstance(member, ast.Field) and member.type_ref.scalar is None:
			self.absorb_nested(member, layout, path, cursor, element,
			                   layout.placements[-1])

		self.record_interval(member, scalar, state, path, name)

		if member.array is not None and isinstance(member.array.size, ast.Remaining):
			state.closed_by = name

		if state.cause is None and total.hi != total.lo:
			state.cause = (name, member.span, _render_extent(total))

		state.cursor = cursor.advance(total)

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

			layout.placements.append(Placement(
				path           = f"{path}{suffix}{tail}",
				name           = inner.name,
				kind           = inner.kind,
				type_name      = inner.type_name,
				offset_bits    = offset,
				size_bits      = inner.size_bits,
				size_max_bits  = inner.size_max_bits,
				scalar         = inner.scalar,
				endian         = inner.endian,
				bit_order      = inner.bit_order,
				span           = inner.span,
				attrs          = inner.attrs,
				marker         = inner.marker,
				array_count    = inner.array_count,
				element_bits   = inner.element_bits,
				bit_position   = inner.bit_position,
				frame_relative = framed or inner.frame_relative,
				sized_by       = inner.sized_by,
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
			return member.type_ref.scalar

		enum = self.enums.get(member.type_ref.name)
		if enum is not None:
			return enum.backing.scalar

		return None

	def element_extent(self, member: ast.Field | ast.Reserved,
			scope: Scope) -> Interval:
		"""The size of one element, which may itself be a range for a frame."""
		type_ref = member.type_ref

		if type_ref.scalar is not None:
			self.check_directives(member, type_ref.scalar, scope)
			return Interval.point(type_ref.scalar.bits)

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
			raise error(
				"an array needs a size here",
				member.array.span,
				label = "expected a length",
				notes = ["the empty form `[]` is only legal inside an `indexed` "
				         "region, which arrives in phase 6"],
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

		env   = self.result.env.with_layout(self.result.lookup).with_fields(state.fields)
		count = interval_of(member.array.size, env)

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
		env = self.result.env.with_layout(self.result.lookup)

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

	assert codec.ratio is not None
	numerator, denominator = codec.ratio

	def scale(bits: int) -> int:
		return -(-bits * numerator // denominator)	# ceil

	if codec.expansion is ast.Expansion.RATIO_EXACT:
		return Interval(scale(interior.lo),
		                None if interior.hi is None else scale(interior.hi))

	# ratio_bounded: worst case known, actual data-dependent.
	return Interval(interior.lo,
	                None if interior.hi is None else scale(interior.hi))


def _arm_name(arm: ast.VariantArm) -> str:
	if arm.member is None:
		return "default"
	if isinstance(arm.member, ast.Field):
		return arm.member.name
	return "arm"


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


def _file_scope(schema: ast.Schema) -> Scope:
	endian: ast.Endian | None       = None
	bit_order: ast.BitOrder | None  = None

	for decl in schema.decls:
		if isinstance(decl, ast.EndianDirective):
			endian = decl.endian
		elif isinstance(decl, ast.BitOrderDirective):
			bit_order = decl.bit_order

	return Scope(endian, bit_order)


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
