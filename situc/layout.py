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

from dataclasses import dataclass, field
from enum import Enum
from typing import TypeVar

from situc import ast
from situc.diagnostics import Span, error
from situc.expr import Env, build_env, evaluate
from situc.types import ScalarType

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
	kind: str			# "field" or "reserved"
	type_name: str
	offset_bits: int
	size_bits: int
	scalar: ScalarType | None
	endian: ast.Endian | None
	bit_order: ast.BitOrder | None
	span: Span
	attrs: tuple[ast.Attr, ...]	= ()
	marker: str | None		= None
	array_count: int | None		= None
	element_bits: int | None	= None
	bit_position: BitPosition | None = None

	@property
	def is_byte_aligned(self) -> bool:
		return self.offset_bits % BITS_PER_BYTE == 0

	@property
	def offset_bytes(self) -> int:
		return self.offset_bits // BITS_PER_BYTE


@dataclass
class StructLayout:
	name: str
	size_bits: int
	placements: list[Placement]	= field(default_factory=list)
	span: Span | None		= None
	reserved_count: int		= 0

	@property
	def is_byte_sized(self) -> bool:
		return self.size_bits % BITS_PER_BYTE == 0

	@property
	def size_bytes(self) -> int:
		return self.size_bits // BITS_PER_BYTE


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
				return None if not layout.is_byte_sized else layout.size_bytes
			if builtin == "offset":
				return 0
			return None

		placement = self.find(path)
		if placement is None:
			return None

		if builtin == "size":
			bits = placement.size_bits
			return None if bits % BITS_PER_BYTE else bits // BITS_PER_BYTE
		if builtin == "offset":
			bits = placement.offset_bits
			return None if bits % BITS_PER_BYTE else bits // BITS_PER_BYTE
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
					marker, endian = resolved, None
				else:
					endian, marker = resolved, None
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

		# Registered before the walk so a nested struct that has already been
		# solved is reused rather than recomputed.
		cursor = self.place_members(decl, decl.members, scope, layout, prefix=name, cursor=0)
		layout.size_bits = cursor
		self.result.structs[name] = layout
		return layout

	def place_members(self, decl: ast.StructDecl, members: tuple[ast.Member, ...],
			scope: Scope, layout: StructLayout, prefix: str, cursor: int) -> int:
		for member in members:
			if isinstance(member, ast.PositionalBlock):
				# A positional block is a staticness assertion, not a frame:
				# its members keep accumulating into the enclosing struct.
				cursor = self.place_members(decl, member.members, scope, layout,
				                            prefix, cursor)
			elif isinstance(member, ast.MarkerField):
				cursor = self.place_marker(decl, member, scope, layout, prefix, cursor)
			elif isinstance(member, (ast.Field, ast.Reserved)):
				cursor = self.place_one(decl, member, scope, layout, prefix, cursor)
		return cursor

	def place_marker(self, decl: ast.StructDecl, member: ast.MarkerField,
			scope: Scope, layout: StructLayout, prefix: str, cursor: int) -> int:
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

		if cursor % BITS_PER_BYTE != 0:
			raise error(
				f"marker `{member.name}` must start on a byte boundary",
				member.span,
				label = f"lands {cursor % BITS_PER_BYTE} bits into a byte",
			)

		layout.placements.append(Placement(
			path         = f"{prefix}.{member.name}",
			name         = member.name,
			kind         = "marker",
			type_name    = member.name,
			offset_bits  = cursor,
			size_bits    = scalar.bits,
			scalar       = scalar,
			endian       = None,
			bit_order    = scope.bit_order,
			span         = member.span,
			attrs        = member.attrs,
			element_bits = scalar.bits,
		))
		return cursor + scalar.bits

	def place_one(self, decl: ast.StructDecl, member: ast.Field | ast.Reserved,
			scope: Scope, layout: StructLayout, prefix: str, cursor: int) -> int:
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
		width  = self.width_of(member, local)
		count  = self.array_count(member)
		scalar = self.effective_scalar(member, local)

		element_bits = width
		total_bits   = width if count is None else width * count

		self.check_alignment(decl, member, scalar, cursor, element_bits)
		position = self.bit_position(scalar, local, cursor, element_bits)

		layout.placements.append(Placement(
			path         = path,
			name         = name,
			kind         = "field" if isinstance(member, ast.Field) else "reserved",
			type_name    = member.type_ref.name,
			offset_bits  = cursor,
			size_bits    = total_bits,
			scalar       = scalar,
			endian       = local.endian,
			bit_order    = local.bit_order,
			span         = member.span,
			attrs        = member.attrs,
			marker       = local.marker,
			array_count  = count,
			element_bits = element_bits,
			bit_position = position,
		))

		if isinstance(member, ast.Field) and member.type_ref.scalar is None:
			self.absorb_nested(member, layout, path, cursor)

		return cursor + total_bits

	def absorb_nested(self, member: ast.Field, layout: StructLayout,
			path: str, base: int) -> None:
		"""Copy a nested struct's placements into the parent, rebased.

		The map names every reachable field, so `Header.flags.urgent` has to
		appear with its offset relative to the outermost struct. Arrays of
		structs are not expanded per element: `recs[].value` is one entry
		describing every element, because that is how a capability path names
		them (section 16).
		"""
		nested = self.result.structs.get(member.type_ref.name)
		if nested is None:
			return

		suffix = "[]" if member.array is not None else ""
		for inner in nested.placements:
			tail = inner.path[len(nested.name) :]
			layout.placements.append(Placement(
				path         = f"{path}{suffix}{tail}",
				name         = inner.name,
				kind         = inner.kind,
				type_name    = inner.type_name,
				offset_bits  = base + inner.offset_bits if not suffix else inner.offset_bits,
				size_bits    = inner.size_bits,
				scalar       = inner.scalar,
				endian       = inner.endian,
				bit_order    = inner.bit_order,
				span         = inner.span,
				attrs        = inner.attrs,
				marker       = inner.marker,
				array_count  = inner.array_count,
				element_bits = inner.element_bits,
				bit_position = inner.bit_position,
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

	def width_of(self, member: ast.Field | ast.Reserved, scope: Scope) -> int:
		type_ref = member.type_ref

		if type_ref.scalar is not None:
			self.check_directives(member, type_ref.scalar, scope)
			return type_ref.scalar.bits

		enum = self.enums.get(type_ref.name)
		if enum is not None:
			backing = enum.backing.scalar
			assert backing is not None, "parser rejects non-scalar enum backing"
			self.check_directives(member, backing, scope)
			return backing.bits

		nested = self.structs.get(type_ref.name)
		if nested is not None:
			return self.layout_of(type_ref.name).size_bits

		raise error(f"unknown type `{type_ref.name}`", type_ref.span, label="not declared")

	def array_count(self, member: ast.Field | ast.Reserved) -> int | None:
		if member.array is None:
			return None
		if member.array.size is None:
			raise error(
				"an array needs a size here",
				member.array.span,
				label = "expected a length",
				notes = ["the empty form `[]` is only legal inside an `indexed` "
				         "region, which arrives in phase 6"],
			)

		env   = self.result.env.with_layout(self.result.lookup)
		count = evaluate(member.array.size, env)

		if count < 0:
			raise error(f"array length {count} is negative", member.array.size.span,
			            label = "must be zero or more")
		if count == 0:
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
			scalar: ScalarType | None, cursor: int, width: int) -> None:
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
			cursor: int, width: int) -> BitPosition | None:
		if scalar is None or not scalar.is_bit_packed:
			return None

		local = cursor % BITS_PER_BYTE
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
