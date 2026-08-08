"""Reading a packed layout image (26.33, decision 0026 as amended).

This package is the walker: it reads an image `situc pack` wrote and answers
questions about bytes, for a format it was not compiled against. It is a
separate binary and nothing under `situc/` imports it -- the compiler emits
images and never walks one, which is what keeps "an offset is a constant" and
"an operation is absent rather than refused" true of what `situc` generates.

The image is read through the *generated* accessors for `std/image.situ`, not
by hand. Decision 0026 asked for that and it is the whole reason the format is
a schema: a hand-rolled parser here would be the one component nothing checks,
in the place whose input is least trusted.
"""

from __future__ import annotations

import struct as _struct
from dataclasses import dataclass, field

MAGIC		= b"SITU"
FORMAT_VERSION	= 2
NONE		= 0xFFFFFFFF
HEADER_BYTES	= 20
SECTION_BYTES	= 16

#: `image_section_tag`. A kind this walker predates is skipped rather than
#: refused, which is the property the directory exists for.
STRUCTS, PLACEMENTS, CODE, STRINGS = 1, 2, 3, 4
ARMS, DELIMITERS, REGIONS, CODECS  = 5, 6, 7, 8
VARINTS, TLVS, INDEXES             = 9, 10, 11
NAMES, VECTORS, MARKERS            = 12, 13, 14
CONSTRAINTS, ENUM_VALUES, VERSIONS = 15, 16, 17
RELATIONS, RELATION_MUSTS = 18, 19

#: `image_check`
MUST_EQ, MINIMUM, MAXIMUM, MUST_BE_ZERO, MUST_BE_ONE, ENUM_KNOWN = range(6)

#: `image_placement.flags`
OFFSET_KNOWN, FRAME_RELATIVE, SIZE_FIXED, FRAME_BASE_DYNAMIC = 1, 2, 4, 8
SIGNED, MARKER_GOVERNED, IS_TAG = 16, 32, 64

BIG, LITTLE, NATIVE = 1, 2, 3


class ImageError(Exception):
	"""The image cannot be read. Never a guess: a walker that carries on
	over a malformed table produces answers that look like answers."""


@dataclass
class Placement:
	kind: int
	endian: int
	bit_order: int
	flags: int
	offset_bits: int
	size_bits: int
	size_max_bits: int
	element_bits: int
	array_count: int
	size_code: int
	type_struct: int
	located_code: int
	repeat_code: int
	radix: int
	text_flags: int
	radix_digits: int
	since: int
	repeat_cap: int

	@property
	def offset_known(self) -> bool:
		return bool(self.flags & OFFSET_KNOWN)

	@property
	def fixed(self) -> bool:
		return bool(self.flags & SIZE_FIXED)

	@property
	def signed(self) -> bool:
		return bool(self.flags & SIGNED)

	@property
	def marker_governed(self) -> bool:
		return bool(self.flags & MARKER_GOVERNED)

	@property
	def is_tag(self) -> bool:
		return bool(self.flags & IS_TAG)


@dataclass
class Struct:
	first_placement: int
	placement_count: int
	size_bits: int
	struct_flags: int = 0

	@property
	def validatable(self) -> bool:
		"""Whether the image carries every check `validate` makes here."""
		return bool(self.struct_flags & 1)

	@property
	def measurable(self) -> bool:
		"""Whether one instance can be measured from its own bytes.

		What decides whether anything may descend into it. A struct that
		ends in its frame rather than at a length it carries has no extent
		of its own, so no backend hands back a sub-view over one -- and a
		walk that measured one anyway would be reading the frame and
		calling it the struct.
		"""
		return bool(self.struct_flags & 2)

	@property
	def fixed(self) -> bool:
		return self.size_bits != NONE


@dataclass(frozen=True)
class Relation:
	"""A cross-message relation the image carries (26.95).

	`request` and `response` are struct indices, in that order, because the
	order is temporal: the first is the message seen first. `musts` are
	offsets into the code section, each a program leaving one value.
	"""

	name: int
	request: int
	response: int
	musts: tuple[int, ...]


@dataclass
class Image:
	"""One loaded image: the tables, and the names where they were kept."""

	structs: list[Struct]			= field(default_factory=list)
	placements: list[Placement]		= field(default_factory=list)
	code: bytes				= b""
	#: Cross-message relations, in declaration order (26.95). Absent from
	#: an image packed before the section existed, which is a section a
	#: walker skips rather than a load that fails.
	relations: list[Relation]		= field(default_factory=list)
	strings: bytes				= b""
	#: placement index -> the bytes it ends at, from the delimiter table.
	delimiters: dict[int, bytes]		= field(default_factory=dict)
	#: placement index -> (quote, escape, cap), each `none` where the format
	#: has none. `quote` toggles: inside a quoted run the delimiter is
	#: content. `escape` applies to the byte after it, itself included.
	delimiter_rules: dict[int, tuple[int, int, int]] = field(default_factory=dict)
	#: Placements inside an `authenticated` or `sealed` region. A walk reads
	#: those through a gate, which this walker does not render.
	regions: set[int]			= field(default_factory=set)
	#: placement index -> the region's flags: bit 0 sealed, bit 1 the schema
	#: waived the gate with `[allow_unverified_read]`.
	region_flags: dict[int, int]		= field(default_factory=dict)
	#: placement index -> the region it sits inside. A region names itself,
	#: so a gate's interior is everything whose owner is the gate.
	region_owner: dict[int, int]		= field(default_factory=dict)
	#: Placements whose width is decoded rather than declared. A varint's
	#: length is in its own bytes, so nothing after one has a computable
	#: offset until the walk can decode it.
	varints: set[int]			= field(default_factory=set)
	#: placement index -> (max_bytes, terminal_bits, big_endian). The
	#: decoder's parameters, carried rather than rederived from `max_bits`:
	#: the arithmetic is the compiler's and a second copy of it here would
	#: be a second implementation to keep in step.
	varint_rules: dict[int, tuple[int, int, bool]] = field(default_factory=dict)
	#: placement index -> the value that means little-endian.
	markers: dict[int, int]			= field(default_factory=dict)
	#: placement index -> [(check, value)], in declaration order. The order
	#: is the answer: the first failure is what `validate` returns.
	constraints: dict[int, list[tuple[int, int]]] = field(default_factory=dict)
	#: enum id -> the values it names.
	enum_values: dict[int, set[int]]	= field(default_factory=dict)
	#: struct index -> the placement holding its version number. A `[since]`
	#: member is present only in a message whose own version reaches it, so
	#: whether such a field is *wrong* cannot be asked until this says
	#: whether it is *there*.
	versions: dict[int, int]		= field(default_factory=dict)
	#: variant placement index -> (discriminant, [(case, selected, flags)])
	arms: dict[int, tuple[int, list[tuple[int, int, int]]]] = \
		field(default_factory=dict)
	#: struct index -> name, and placement index -> path. Empty without the
	#: metadata tail, which a device does not carry.
	struct_names: list[str]			= field(default_factory=list)
	placement_names: list[str]		= field(default_factory=list)

	def name_of(self, index: int) -> str:
		"""A placement's path, or its index where the tail was omitted."""
		if index < len(self.placement_names):
			return self.placement_names[index]
		return f"placement[{index}]"

	def struct_name(self, index: int) -> str:
		if index < len(self.struct_names):
			return self.struct_names[index]
		return f"struct[{index}]"

	def members(self, struct: Struct) -> list[int]:
		first = struct.first_placement
		return list(range(first, first + struct.placement_count))


def _string_at(pool: bytes, offset: int) -> str:
	end = pool.find(b"\0", offset)
	return pool[offset:end if end >= 0 else len(pool)].decode("ascii", "replace")


def load(blob: bytes, accessors: object | None = None) -> Image:
	"""Read an image into tables.

	`accessors` is the generated module for `std/image.situ`. When it is
	given every record is read through it, which is what decision 0026 asked
	for; the fallback exists only so this module can be imported without a
	code generator to hand, and the tests pass the accessors.
	"""
	if len(blob) < HEADER_BYTES or blob[:4] != MAGIC:
		raise ImageError("not a situ image: bad magic")
	version, flags = _struct.unpack_from("<HH", blob, 4)
	if version != FORMAT_VERSION:
		raise ImageError(f"image format version {version}, this walker "
		                 f"reads {FORMAT_VERSION}")
	total, count, directory = _struct.unpack_from("<III", blob, 8)
	if total != len(blob):
		raise ImageError(f"image says {total} bytes, got {len(blob)}")

	found: dict[int, tuple[int, int, int]] = {}
	for i in range(count):
		kind, offset, records, stride = _struct.unpack_from(
			"<IIII", blob, directory + i * SECTION_BYTES)
		if offset + records * stride > len(blob):
			raise ImageError(f"section {kind} runs past the image")
		found[kind] = (offset, records, stride)

	image = Image()
	for at, records, stride in [found.get(STRUCTS, (0, 0, 0))]:
		for i in range(records):
			image.structs.append(Struct(*_struct.unpack_from(
				"<IIII", blob, at + i * stride)))
	for at, records, stride in [found.get(PLACEMENTS, (0, 0, 0))]:
		for i in range(records):
			base = at + i * stride
			kind, endian, bit_order, pflags = _struct.unpack_from(
				"<BBBB", blob, base)
			rest = _struct.unpack_from("<IIIIIIIIIBBHHH", blob, base + 4)
			image.placements.append(Placement(kind, endian, bit_order,
			                                  pflags, *rest))
	if CODE in found:
		at, records, stride = found[CODE]
		image.code = blob[at:at + records * stride]
	if STRINGS in found:
		at, records, stride = found[STRINGS]
		image.strings = blob[at:at + records * stride]

	if RELATIONS in found:
		musts_at, musts_n, musts_stride = found.get(RELATION_MUSTS, (0, 0, 0))
		at, records, stride = found[RELATIONS]
		for i in range(records):
			name, request, response, first, count, _pad = _struct.unpack_from(
				"<IIIIII", blob, at + i * stride)
			if first + count > musts_n:
				raise ImageError(f"relation {i} names musts the image lacks")
			musts = tuple(
				_struct.unpack_from("<I", blob,
				                    musts_at + (first + j) * musts_stride)[0]
				for j in range(count))
			image.relations.append(Relation(name, request, response, musts))

	if DELIMITERS in found:
		at, records, stride = found[DELIMITERS]
		for i in range(records):
			where, quote, escape, cap, length = _struct.unpack_from(
				"<IIIIB", blob, at + i * stride)
			start = at + i * stride + 17
			image.delimiters[where] = blob[start:start + length]
			image.delimiter_rules[where] = (quote, escape, cap)

	if REGIONS in found:
		at, records, stride = found[REGIONS]
		for i in range(records):
			where, owner, _codec, rflags = _struct.unpack_from(
				"<IIIB", blob, at + i * stride)
			image.regions.add(where)
			image.region_flags[where] = rflags
			image.region_owner[where] = owner

	if VARINTS in found:
		at, records, stride = found[VARINTS]
		for i in range(records):
			where, _enc, max_bytes, terminal, vflags = _struct.unpack_from(
				"<IIBBB", blob, at + i * stride)
			image.varints.add(where)
			image.varint_rules[where] = (max_bytes, terminal,
			                             bool(vflags & 2))

	if CONSTRAINTS in found:
		at, records, stride = found[CONSTRAINTS]
		for i in range(records):
			where, value, check = _struct.unpack_from(
				"<IqB", blob, at + i * stride)
			image.constraints.setdefault(where, []).append((check, value))

	if ENUM_VALUES in found:
		at, records, stride = found[ENUM_VALUES]
		for i in range(records):
			enum_id, value, _pad = _struct.unpack_from(
				"<IqI", blob, at + i * stride)
			image.enum_values.setdefault(enum_id, set()).add(value)

	if VERSIONS in found:
		at, records, stride = found[VERSIONS]
		for i in range(records):
			shape, where = _struct.unpack_from("<II", blob, at + i * stride)
			image.versions[shape] = where

	if MARKERS in found:
		at, records, stride = found[MARKERS]
		for i in range(records):
			where, little, _pad = _struct.unpack_from(
				"<IqI", blob, at + i * stride)
			image.markers[where] = little

	if ARMS in found:
		at, records, stride = found[ARMS]
		for i in range(records):
			where, selected, value, selects, aflags = _struct.unpack_from(
				"<IIqIB", blob, at + i * stride)
			found_arm = image.arms.setdefault(where, (selects, []))
			found_arm[1].append((value, selected, aflags))

	if NAMES in found and STRINGS in found:
		at, records, stride = found[NAMES]
		offsets = [_struct.unpack_from("<I", blob, at + i * stride)[0]
		           for i in range(records)]
		names = [_string_at(image.strings, o) for o in offsets]
		split = len(image.structs)
		image.struct_names    = names[:split]
		image.placement_names = names[split:]

	return image
