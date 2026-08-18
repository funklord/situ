"""Join the layout to the propagation table, producing capability vectors.

The layout solver says where the bytes are. The propagation table says what
that costs. This module is the seam: it builds the context each table row needs
and collects the result, so neither side has to know about the other.

Keeping it separate is what lets phase 7 add derived codecs without touching
the lattice: a new construct contributes context, and the table gains a row.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from situc import ast
from situc.capability import Axis, Value, Vector, meet_all
from situc.diagnostics import error
from situc.layout import BITS_PER_BYTE, Placement, SchemaLayout, StructLayout
from situc.propagate import Context, Resolved, apply


@dataclass
class ResolvedStruct:
	name: str
	layout: StructLayout
	entries: list[Resolved]	= field(default_factory=list)
	vector: Vector		= field(default_factory=Vector)


@dataclass
class ResolvedSchema:
	structs: dict[str, ResolvedStruct]	= field(default_factory=dict)
	layout: SchemaLayout			= field(default_factory=SchemaLayout)

	def find(self, path: str) -> Resolved | None:
		head, _, _ = path.partition(".")
		struct = self.structs.get(head)
		if struct is None:
			return None
		for entry in struct.entries:
			if entry.placement.path == path:
				return entry
		return None

	def find_struct(self, name: str) -> ResolvedStruct | None:
		return self.structs.get(name)

	def paths(self) -> list[str]:
		return [entry.placement.path
		        for struct in self.structs.values()
		        for entry in struct.entries]


def resolve(schema: ast.Schema, layout: SchemaLayout) -> ResolvedSchema:
	structs = {decl.name: decl for decl in schema.structs()}
	enums   = {decl.name: decl for decl in schema.enums()}
	codecs  = {decl.name: decl for decl in schema.codecs()}
	lenient = schema.strictness is ast.Strictness.LENIENT
	result  = ResolvedSchema(layout=layout)

	for name, struct_layout in layout.structs.items():
		decl = structs[name]
		_check_host_dependence(decl, struct_layout)
		_check_required_alignment(decl, struct_layout)
		_check_secret_is_not_layout_bearing(decl, struct_layout)
		_check_transform_tag_order(decl, struct_layout)

		entries = [
			apply(_context(placement, decl, structs, enums, codecs, lenient))
			for placement in struct_layout.placements
		]
		_meet_aggregates(entries, structs)

		result.structs[name] = ResolvedStruct(
			name    = name,
			layout  = struct_layout,
			entries = entries,
			# A struct's vector is the meet of its members', plus whatever the
			# struct construct itself imposes -- which for a plain struct is
			# nothing (section 11.2).
			vector  = _struct_vector(struct_layout, entries),
		)

	return result


def _meet_aggregates(entries: list[Resolved], structs: dict[str, ast.StructDecl]) -> None:
	"""Give a struct-typed field the meet of the members it contains.

	Section 11.2: a struct's vector is the meet of its members' plus whatever
	the struct construct itself imposes. A field of type `Flags` is
	ValueConverted because everything inside `Flags` is, even though the field
	itself declares no byte order.

	The solver has already flattened nested members into this list with dotted
	paths, so the members of `Header.flags` are the entries prefixed
	`Header.flags.`.
	"""
	by_path = {entry.placement.path: entry for entry in entries}

	for entry in entries:
		if entry.placement.type_name not in structs:
			continue

		prefix  = entry.placement.path + "."
		members = [other.vector for path, other in by_path.items()
		           if path.startswith(prefix)]
		if not members:
			continue

		# Offset, size and alignment stay the field's own: they say where it
		# sits, which its members cannot weaken.
		merged = meet_all(members)
		for axis in (Axis.OFFSET, Axis.SIZE, Axis.ALIGN):
			merged = _force(merged, axis, entry.vector.get(axis))

		entry.vector = entry.vector.meet(merged)


def _force(vector: Vector, axis: Axis, value: Value) -> Vector:
	"""Set an axis regardless of direction, for axes a meet must not touch."""
	kept = tuple((held, held_value) for held, held_value in vector.values
	             if held is not axis)
	return Vector(kept + ((axis, value),))


def _struct_vector(layout: StructLayout, entries: list[Resolved]) -> Vector:
	"""A struct type's own vector.

	`offset` and `align` are dropped back to their strongest values: a type is
	not placed anywhere, so those axes describe a field of this type rather than
	the type itself, and reporting a member's alignment against the type would
	be meaningless.
	"""
	vector = meet_all([entry.vector for entry in entries])

	vector = _force(vector, Axis.OFFSET, Value("AbsoluteStatic"))
	vector = _force(vector, Axis.ALIGN, Value("Aligned"))
	vector = _force(vector, Axis.SIZE, Value("Fixed", (
		str(layout.size_bytes) if layout.is_byte_sized else f"{layout.size_bits}bit",)))
	return vector


def _context(placement: Placement, decl: ast.StructDecl,
		structs: dict[str, ast.StructDecl],
		enums: dict[str, ast.EnumDecl],
		codecs: dict[str, ast.CodecDecl],
		lenient: bool) -> Context:
	enum = enums.get(placement.type_name)

	return Context(
		placement         = placement,
		scalar            = placement.scalar,
		is_aggregate      = placement.type_name in structs,
		struct_attrs      = decl.attrs,
		enum_default_pass = (enum is not None
		                     and enum.effective_default is ast.EnumDefault.PASS),
		reserved_unknown  = (placement.kind == "reserved"
		                     and _has_attr(placement.attrs, "unknown")),
		codec             = codecs.get(placement.codec or ""),
		lenient           = lenient,
	)


def _check_host_dependence(decl: ast.StructDecl, layout: StructLayout) -> None:
	"""`endian native` has to be reached deliberately (section 11.3).

	Host-order encoding is legitimate for in-memory and same-machine IPC
	formats, and catastrophic for anything that leaves the machine. The
	attribute makes the choice greppable and puts it in the capability map.
	"""
	if _has_attr(decl.attrs, "allow_host_dependent"):
		return

	for placement in layout.placements:
		scalar = placement.scalar
		if (placement.endian is ast.Endian.NATIVE
				and scalar is not None
				and scalar.bits > BITS_PER_BYTE):
			raise error(
				f"`{placement.name}` is host-order without `[allow_host_dependent]`",
				placement.span,
				label = "multi-byte scalar in `endian native`",
				notes = [
					"host-order encoding makes the struct non-canonical: the "
					"same value has a different byte sequence on a machine of "
					"the other order",
					"`native` is the order of the machine the generated code is "
					"compiled for, which is not necessarily the peer's: a server "
					"writing frames for a weaker client wants the client's order "
					"named outright, not its own",
					f"add `[allow_host_dependent]` to `struct {decl.name}` if "
					"this format never leaves the machine",
					"otherwise declare `endian big` or `endian little`, or use "
					"an `endian_marker` so the order travels with the data "
					"(project.md section 8.3)",
				],
			)


TAG_ORDER = ("after", "before")


def _transform_covers(layout: StructLayout,
		region: Placement) -> list[Placement]:
	"""The placements a `coded` region's transform runs over (14.1a)."""
	return [placement for placement in layout.placements
	        if placement is region
	        or (placement.name in region.coded_covers
	            and "." not in placement.path.partition(".")[2].partition(".")[2])]


def _check_transform_tag_order(decl: ast.StructDecl,
		layout: StructLayout) -> None:
	"""Whether a tag covers the transformed or untransformed bytes (14.1b).

	Only where a `covers` clause reaches bytes some tag also covers. Then two
	orders are coherent and they disagree about what is on the wire:

	- `after`  -- the tag is computed first, over untransformed bytes, and the
	  transform goes on top. QUIC's header protection: the AEAD's associated
	  data is the *unprotected* header, and the mask is applied afterwards.
	- `before` -- the transform runs first and the tag covers its output, so
	  the tag authenticates what a peer actually reads off the wire.

	Situ settles orderings that are data dependencies with one terminating
	answer -- decision 0011 does exactly that for nested tags, innermost
	first, because no other order converges. This one has two answers that
	both terminate, and which is right is the protocol's choice rather than a
	consequence of the structure. So it is 17.0's case instead: an ambiguity
	the schema must resolve, and an error until it does.

	The wrong choice is undetectable at run time by construction -- both
	produce a message of the same length with the same fields in the same
	places, and the peer that disagrees reports a failed tag rather than a
	misordered transform. That is exactly the class 17.0 exists for.
	"""
	for region in layout.placements:
		if region.kind != "coded" or not region.coded_covers:
			continue

		tags = sorted({tag for placement in _transform_covers(layout, region)
		               for tag in placement.covered_by})
		order = next((attr for attr in region.attrs
		              if attr.name == "tag_order"), None)

		if order is not None and not tags:
			raise error(
				f"`{region.name}` has `tag_order` and no tag covers what it"
				" transforms",
				region.span,
				label = "nothing to be ordered against",
				notes = ["the attribute says whether a tag covers this"
				         " transform's input or its output, and here no tag"
				         " covers either",
				         "drop it: an attribute that decides nothing is a"
				         " construct whose meaning is silently nothing"
				         " (project.md section 14.1b)"],
			)

		if not tags:
			continue

		named = ", ".join(f"`{one}`" for one in tags)
		if order is None:
			raise error(
				f"`{region.name}` transforms bytes that {named} covers, and"
				" the schema does not say in which order",
				region.span,
				label = "add `[tag_order = after]` or `[tag_order = before]`",
				notes = ["`after`: the tag is computed first and the transform"
				         " goes on top, so the tag covers untransformed bytes"
				         " -- QUIC's header protection, whose associated data"
				         " is the unprotected header",
				         "`before`: the transform runs first and the tag"
				         " covers its output, so the tag authenticates what a"
				         " peer reads off the wire",
				         "both orders produce the same bytes in the same"
				         " places, so a peer that disagrees reports a failed"
				         " tag rather than a misordered transform -- which is"
				         " why this is an error rather than a default"
				         " (project.md sections 14.1b and 17.0)"],
			)

		value = order.value
		# A bare identifier is the spelling the grammar gives here; a quoted
		# one is accepted rather than reported as an unknown value, since the
		# author plainly meant the word they wrote.
		spelled = (value.name if isinstance(value, ast.NameRef)
		           else value.value if isinstance(value, ast.StringLiteral)
		           else None)
		if spelled not in TAG_ORDER:
			raise error(
				f"`{region.name}` has an unknown `tag_order`",
				order.span,
				label = f"expected `after` or `before`, found `{spelled}`",
				notes = ["`after` puts the transform on top of the tag,"
				         " `before` puts it underneath (14.1b)"],
			)


def _check_secret_is_not_layout_bearing(decl: ast.StructDecl,
		layout: StructLayout) -> None:
	"""A `[secret]` field may not decide where anything is (section 14.6).

	A length or a discriminant read from secret material leaks it: the extent of
	the message is visible to anyone counting bytes, whatever the encryption
	does. This is a side channel the schema can rule out entirely, so it does.
	"""
	secret = {placement.name for placement in layout.placements
	          if _has_attr(placement.attrs, "secret")}
	if not secret:
		return

	for placement in layout.placements:
		driver = placement.sized_by
		if driver is None or driver.partition(".")[0] not in secret:
			continue

		raise error(
			f"`{placement.name}` takes its size from the secret field "
			f"`{driver}`",
			placement.span,
			label = "layout depends on secret material",
			notes = [
				"the encoded length is visible to anyone counting bytes, so a "
				"secret-dependent length leaks the secret however strong the "
				"cipher is",
				f"drop `[secret]` from `{driver}` if it is not really secret, or "
				"size this member from a public field",
			],
		)


def _check_required_alignment(decl: ast.StructDecl, layout: StructLayout) -> None:
	"""`[require_aligned]` turns a misalignment into an error (section 8.4).

	Section 17.0 lists a field's alignment as an ambiguity when the target may
	fault on unaligned access: either the schema demands alignment, or it
	accepts the consequence. Absence of the attribute is the acceptance, and
	the align axis then records what was accepted. The attribute is the demand,
	and it has to actually refuse.
	"""
	from situc.propagate import alignment_of

	for placement in layout.placements:
		if not _has_attr(placement.attrs, "require_aligned"):
			continue

		scalar = placement.scalar
		if scalar is None or scalar.is_bit_packed:
			raise error(
				f"`{placement.name}` cannot be `[require_aligned]`",
				placement.span,
				label = "not a whole-byte scalar",
				notes = ["a bit-packed field has no byte address to align",
				         "widen it to a whole number of bytes, or drop the "
				         "attribute"],
			)

		natural = min(scalar.bits // BITS_PER_BYTE, 8)

		if placement.offset_bits is None:
			raise error(
				f"`{placement.name}` is `[require_aligned]` but has no static offset",
				placement.span,
				label = "placed after a dynamically sized member",
				notes = ["where it lands depends on the data, so alignment cannot "
				         "be promised at compile time",
				         "move it before the dynamic member, or drop the attribute "
				         "and handle the unaligned access"],
			)

		actual = alignment_of(placement.offset_bits)
		if actual >= natural:
			continue

		raise error(
			f"`{placement.name}` is `[require_aligned]` but lands at "
			f"{actual}-byte alignment",
			placement.span,
			label = f"needs {natural}-byte alignment",
			notes = [
				f"it sits at offset {placement.offset_bits // BITS_PER_BYTE}, and "
				f"a {scalar.bits}-bit scalar wants a multiple of {natural}",
				"reorder the preceding members, or insert `reserved` padding, to "
				"move it onto its natural boundary",
				"unaligned access faults on some targets and is split on others "
				"(project.md section 8.4)",
			],
		)


def _has_attr(attrs: tuple[ast.Attr, ...], name: str) -> bool:
	return any(attr.name == name for attr in attrs)
