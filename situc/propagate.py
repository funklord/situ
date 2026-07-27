"""The propagation table of project.md section 11.3, as data.

Invariant 1 of section 26: this table is data, not code. Adding a construct
means adding a row and a test, never editing scattered conditionals. Every rule
below is therefore a `Rule` value, and `apply` is the only interpreter.

A row records not just what it weakens but why, because the same rows are read
twice: once to compute a vector, and once to explain one. A blame chain is a
list of rule applications, so a rule with no explanation would produce a
diagnostic with no root cause -- which section 26 invariant 3 calls a bug.

Rows for constructs belonging to later phases are a pure addition here. The
lattice never learns what a codec is; it reads rules.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from situc import ast
from situc.capability import Axis, Value, Vector, rank
from situc.diagnostics import Span
from situc.layout import BITS_PER_BYTE, Placement
from situc.types import ScalarType

MAX_ALIGN = 8
ATOMIC_WIDTHS = frozenset({8, 16, 32, 64})


@dataclass(frozen=True)
class Effect:
	"""One axis a rule sets, and the phrase that explains it."""

	axis: Axis
	value: Value
	because: str


@dataclass(frozen=True)
class Rule:
	"""One row of the section 11.3 table."""

	name: str
	construct: str
	effects: tuple[Effect, ...]
	remedy: str = ""
	# When set, a weakening from this rule points at the construct that caused
	# it rather than at the field that suffers it. Section 17: a blame chain
	# names the root cause and its source location.
	blames_cause: bool = False


@dataclass(frozen=True)
class Weakening:
	"""A rule that fired on a particular field.

	Carries the span so a blame chain can point at source, which is what turns
	"offset is Dynamic" into a diagnostic rather than a fact.
	"""

	rule: Rule
	effect: Effect
	span: Span
	subject: str


@dataclass
class Resolved:
	"""A placement with its vector and the reasons that vector is what it is."""

	placement: Placement
	vector: Vector
	weakenings: list[Weakening] = field(default_factory=list)

	def blame(self, axis: Axis) -> list[Weakening]:
		"""Every rule that weakened one axis, in the order they fired."""
		return [w for w in self.weakenings if w.effect.axis is axis]


# ---------------------------------------------------------------------------
# The table
#
# Ordered as section 11.3 orders it. `applies` decides whether a row fires for
# a given placement; keeping the predicate beside the row is what lets the row
# stay data rather than becoming a branch somewhere else.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Row:
	rule: Rule
	applies: Callable[[Context], bool]


@dataclass(frozen=True)
class Context:
	"""Everything a row needs to decide whether it fires."""

	placement: Placement
	scalar: ScalarType | None
	is_aggregate: bool
	struct_attrs: tuple[ast.Attr, ...]
	enum_default_pass: bool
	reserved_unknown: bool
	# The signature of the codec transforming this placement, if any. The
	# lattice reads properties and nothing else (section 13.3): no rule below
	# may learn what an algorithm actually does.
	codec: ast.CodecDecl | None = None
	# The schema declared `strictness = lenient` (section 14.5).
	lenient: bool = False


def _align_value(placement: Placement) -> Value:
	"""Alignment is a property of a known offset.

	A dynamically placed field has no alignment the compiler can promise: where
	it lands depends on how long the bytes before it turn out to be. Unaligned
	is the honest answer and the weaker one.
	"""
	if placement.offset_bits is None:
		return Value("Unaligned")

	alignment = alignment_of(placement.offset_bits)
	return Value("Aligned", (str(alignment),)) if alignment else Value("Unaligned")


def alignment_of(offset_bits: int) -> int:
	"""The strongest power-of-two byte boundary an offset satisfies."""
	if offset_bits % BITS_PER_BYTE != 0:
		return 0

	byte = offset_bits // BITS_PER_BYTE
	if byte == 0:
		return MAX_ALIGN

	alignment = 1
	while alignment < MAX_ALIGN and byte % (alignment * 2) == 0:
		alignment *= 2
	return alignment


def _is_converted_scalar(context: Context) -> bool:
	scalar    = context.scalar
	placement = context.placement
	return (scalar is not None
	        and not scalar.is_bit_packed
	        and scalar.bits > BITS_PER_BYTE
	        and placement.kind != "marker"
	        and placement.marker is None
	        and placement.endian is not ast.Endian.NATIVE)


def _is_marker_scoped(context: Context) -> bool:
	"""A multi-byte field whose byte order arrives with the data."""
	scalar = context.scalar
	return (context.placement.marker is not None
	        and context.placement.kind != "marker"
	        and scalar is not None
	        and not scalar.is_bit_packed
	        and scalar.bits > BITS_PER_BYTE)


def _is_marker_field(context: Context) -> bool:
	return context.placement.kind == "marker"


def _is_bit_field(context: Context) -> bool:
	return context.scalar is not None and context.scalar.is_bit_packed


def _straddles(context: Context) -> bool:
	position = context.placement.bit_position
	return position is not None and position.straddles


def _is_unaligned_multibyte(context: Context) -> bool:
	"""A multi-byte scalar the compiler cannot promise is on its boundary.

	Either it is known to be misaligned, or its offset is not known at all --
	and an unknown offset is not an aligned one. `_align_value` already reports
	Unaligned for a dynamic offset; this is the same fact reaching the atomic
	axis, which it did not before, so a field that moved behind a
	variable-length member kept a single-instruction access it can no longer be
	guaranteed.
	"""
	scalar = context.scalar
	if scalar is None or scalar.is_bit_packed or scalar.bits <= BITS_PER_BYTE:
		return False
	if context.placement.offset_bits is None:
		return True
	width_bytes = scalar.bits // BITS_PER_BYTE
	return alignment_of(context.placement.offset_bits) < min(width_bytes, MAX_ALIGN)


def _is_aggregate_or_array(context: Context) -> bool:
	"""A field holding more than one value.

	Deliberately narrow: the unaligned and bit-field cases have rows of their
	own, and a catch-all here would attach the wrong reason to a plain scalar's
	blame chain.
	"""
	return (context.is_aggregate
	        or context.scalar is None
	        or context.placement.array_count is not None)


def _is_odd_width_scalar(context: Context) -> bool:
	scalar = context.scalar
	return (scalar is not None
	        and not scalar.is_bit_packed
	        and scalar.bits not in ATOMIC_WIDTHS)


def _has_dynamic_offset(context: Context) -> bool:
	return context.placement.offset_bits is None


def _is_frame_relative(context: Context) -> bool:
	return (context.placement.frame_relative
	        and context.placement.offset_bits is not None)


def _is_bounded_size(context: Context) -> bool:
	"""A member whose extent is a range.

	Constructs with a row of their own are excluded, because that row states
	the same two axes with a reason specific to the construct. A varint claims
	InPlaceSlack rather than Shifting, since a value that re-encodes to the same
	length moves nothing (section 8.1.1); a variant names its arms and costs
	equalizing them. Letting this row fire as well would meet the values to the
	same place but bury the reason, and the reason is the product.
	"""
	placement = context.placement
	return (not _owns_its_mutate(placement)
	        and not _has_unequal_arms(context)
	        and placement.size_max_bits is not None
	        and placement.size_max_bits != placement.size_bits)


# Constructs whose own row states the mutate axis with a reason specific to
# them. The generic size rows below must not restate it: the values would often
# meet to the same place, but the specific reason is the product.
OWNS_MUTATE = frozenset({"tlv", "opaque"})


def _owns_its_mutate(placement: Placement) -> bool:
	return placement.kind in OWNS_MUTATE or placement.varint is not None


def _is_unbounded_size(context: Context) -> bool:
	return context.placement.size_max_bits is None


def _is_array(placement: Placement) -> bool:
	return placement.array_count is not None or placement.sized_by is not None


def _has_dynamic_elements(context: Context) -> bool:
	"""An array whose element type is itself variable-sized.

	Element k cannot be found without walking the k-1 before it, so access
	drops to Sequential (section 11.3). Section 8.1.1 says the same thing about
	an array of varints in particular, and it is the same rule: what matters is
	that the element width is not known, not what makes it unknown.
	"""
	placement = context.placement

	if placement.kind == "element":
		return placement.size_max_bits != placement.size_bits

	# Only a real array has elements to walk. An `opaque` region has no
	# interior at all, and an `indexed` one is the exception the construct
	# exists for: the offset table means element N is one indirection away
	# however wide the elements are, so access stays Random (section 9.3).
	if placement.kind != "field":
		return False

	return _is_array(placement) and placement.element_bits is None


def _is_varint(context: Context) -> bool:
	return context.placement.varint is not None


def _is_non_minimal_varint(context: Context) -> bool:
	return context.placement.varint is not None and not context.placement.varint_minimal


def _inside_codec(context: Context) -> bool:
	"""A member of a coded region, as opposed to the region itself."""
	return context.codec is not None and context.placement.kind != "coded"


def _codec_region(context: Context) -> bool:
	return context.codec is not None and context.placement.kind == "coded"


def _interior_in_place(context: Context) -> bool:
	"""Length-preserving, byte-granular, linearly seekable: the CTR-mode case.

	Re-transforming a single byte range is possible, so a same-size field write
	stays in place. This is the row the whole transform design exists to reach.
	"""
	codec = context.codec
	return (_inside_codec(context)
	        and codec is not None
	        and codec.expansion is ast.Expansion.PRESERVING
	        and codec.seekable is ast.Seekable.LINEAR
	        and codec.granularity is ast.Granularity.BYTE)


def _interior_block_slack(context: Context) -> bool:
	codec = context.codec
	return (_inside_codec(context)
	        and codec is not None
	        and codec.expansion is ast.Expansion.PRESERVING
	        and codec.granularity is ast.Granularity.BLOCK)


def _interior_permuted(context: Context) -> bool:
	codec = context.codec
	return (_inside_codec(context)
	        and codec is not None
	        and codec.expansion is ast.Expansion.PRESERVING
	        and codec.seekable is ast.Seekable.PERMUTED)


def _interior_whole_region(context: Context) -> bool:
	"""Length-preserving but not seekable: offsets survive, mutation does not."""
	codec = context.codec
	return (_inside_codec(context)
	        and codec is not None
	        and codec.expansion is ast.Expansion.PRESERVING
	        and codec.seekable is ast.Seekable.NONE
	        and codec.granularity is not ast.Granularity.BLOCK)


def _needs_decode_first(context: Context) -> bool:
	"""Not systematic and not length-preserving: nothing is readable in place."""
	codec = context.codec
	return (_inside_codec(context)
	        and codec is not None
	        and not codec.systematic
	        and codec.expansion is not ast.Expansion.PRESERVING)


def _not_deterministic(context: Context) -> bool:
	"""A transform that may encode the same input more than one way.

	Section 14.4 lists this among the sources of non-canonicity, and it is the
	one that arrives from a property signature rather than from a construct: no
	amount of positional layout around the region makes up for a codec that can
	produce two outputs for one input.
	"""
	codec = context.codec
	return codec is not None and not codec.deterministic


def _is_systematic(context: Context) -> bool:
	codec = context.codec
	return _inside_codec(context) and codec is not None and codec.systematic


def _not_invertible(context: Context) -> bool:
	codec = context.codec
	return (context.codec is not None and codec is not None
	        and not codec.invertible)


def _error_propagating(context: Context) -> bool:
	codec = context.codec
	return (_codec_region(context) and codec is not None
	        and codec.error_propagating)


def _is_tlv(context: Context) -> bool:
	return context.placement.kind == "tlv"


def _tlv_preserves_unknown(context: Context) -> bool:
	return context.placement.tlv_unknown == "preserve"


def _tlv_has_no_ordering(context: Context) -> bool:
	"""TLV items are self-describing, so they may appear in any order.

	{A, B} and {B, A} encode the same content, which means the format admits
	more than one encoding of a value unless the schema pins the order. This is
	inherent to the construct, not to any policy on it.
	"""
	return context.placement.kind == "tlv" and not context.placement.tlv_ordered


def _tlv_tag_is_non_minimal(context: Context) -> bool:
	return (context.placement.kind == "tlv"
	        and context.placement.tlv_tag_varint is not None
	        and not context.placement.tlv_tag_minimal)


# Protobuf wire types. 2 is length-prefixed; the rest carry a scalar directly.
LENGTH_PREFIXED_WIRE = 2
SCALAR_WIRES = frozenset({0, 1, 5})


def _tlv_allows_packed_and_unpacked(context: Context) -> bool:
	"""A repeated scalar that may be written either way.

	Protobuf lets a repeated scalar field appear as several scalar items or as
	one length-prefixed item holding all of them. Both are legal for the same
	content, which is a cause of non-canonicity independent of ordering or
	unknown-field retention. It is visible exactly where the dispatch accepts a
	length-prefixed wire type alongside a scalar one, with duplicates allowed.
	"""
	placement = context.placement
	if placement.kind != "tlv" or placement.tlv_duplicates != "allowed":
		return False

	wires = set(placement.tlv_wire_types)
	return LENGTH_PREFIXED_WIRE in wires and bool(wires & SCALAR_WIRES)


def _tlv_allows_unordered_duplicates(context: Context) -> bool:
	"""Duplicates without an ordering rule: the same content, several encodings."""
	return (context.placement.tlv_duplicates == "allowed"
	        and not context.placement.tlv_ordered)


def _is_opaque(context: Context) -> bool:
	return context.placement.kind == "opaque"


def _is_indexed(context: Context) -> bool:
	return context.placement.kind == "indexed"


def _has_unequal_arms(context: Context) -> bool:
	sizes = {size for _, size in context.placement.arm_sizes}
	return len(sizes) > 1


def _is_covered(context: Context) -> bool:
	"""Bytes some tag authenticates (section 14.2).

	The tag itself is excluded: well-formedness has already established it sits
	outside its own coverage, and a tag that reported itself covered would make
	finalize look like it invalidated its own output.
	"""
	return (bool(context.placement.covered_by)
	        and context.placement.kind not in ("tag", "checksum"))


def _is_verify_gated(context: Context) -> bool:
	placement = context.placement
	return placement.sealed_by is not None and not placement.unverified_ok


def _reads_unverified(context: Context) -> bool:
	placement = context.placement
	return placement.sealed_by is not None and placement.unverified_ok


def _is_tag(context: Context) -> bool:
	return context.placement.kind in ("tag", "checksum")


def _is_lenient(context: Context) -> bool:
	return context.lenient


def _is_host_dependent(context: Context) -> bool:
	scalar = context.scalar
	return (context.placement.endian is ast.Endian.NATIVE
	        and scalar is not None
	        and scalar.bits > BITS_PER_BYTE)


TABLE: tuple[Row, ...] = (
	Row(
		rule = Rule(
			name      = "non-native-endian-scalar",
			construct = "a multi-byte scalar in a declared byte order",
			effects   = (Effect(Axis.REPR, Value("ValueConverted"),
			                    "the value is not the memory: reading it is a "
			                    "byte swap on a host of the other order"),),
			remedy    = "no pointer accessor is generated for this field; use the "
			            "by-value getter",
		),
		applies = _is_converted_scalar,
	),
	Row(
		rule = Rule(
			name      = "endian-marker-scope",
			construct = "a field whose byte order comes from an `endian_marker`",
			effects   = (),		# filled in below; the parameter is the marker
			remedy    = "",
		),
		applies = _is_marker_scoped,
	),
	Row(
		rule = Rule(
			name      = "bit-field",
			construct = "a bit-packed field",
			effects   = (
				Effect(Axis.REPR, Value("ValueConverted"),
				       "the value has to be shifted and masked out of its "
				       "containing byte"),
				Effect(Axis.ATOMIC, Value("NonAtomic"),
				       "writing it is a read-modify-write of the containing byte"),
				Effect(Axis.ALIGN, Value("Unaligned"),
				       "its address is not a byte address"),
			),
			remedy    = "widen the field to a whole number of bytes to regain "
			            "atomic access",
		),
		applies = _is_bit_field,
	),
	Row(
		rule = Rule(
			name      = "straddling-bit-field",
			construct = "a bit field crossing a byte boundary",
			# Section 11.3 gives this row "as above, plus atomic := NonAtomic
			# and a warning". It assigns no further axis, and inventing one
			# would be exactly the guess section 0 rule 4 forbids -- so the row
			# records the second, worse reason for the same value.
			effects   = (Effect(Axis.ATOMIC, Value("NonAtomic"),
			                    "the write spans two bytes, so it is a "
			                    "multi-byte read-modify-write"),),
			remedy    = "reorder the fields so this one fits inside a byte",
		),
		applies = _straddles,
	),
	Row(
		rule = Rule(
			name      = "unaligned-multi-byte-scalar",
			construct = "a multi-byte scalar not known to be on its boundary",
			effects   = (Effect(Axis.ATOMIC, Value("NonAtomic"),
			                    "an unaligned word access faults on some targets "
			                    "and is split on others, and an offset that is "
			                    "not known cannot be known to be aligned"),),
			remedy    = "reorder the preceding fields, or insert `reserved` "
			            "padding, to land this field on its natural boundary; "
			            "where the offset itself is dynamic, move the field "
			            "ahead of the variable-length member instead",
		),
		applies = _is_unaligned_multibyte,
	),
	Row(
		rule = Rule(
			name      = "aggregate-or-array",
			construct = "a struct-typed field or an array",
			effects   = (Effect(Axis.ATOMIC, Value("NonAtomic"),
			                    "a multi-field update is never atomic in v0"),),
			remedy    = "",
		),
		applies = _is_aggregate_or_array,
	),
	Row(
		rule = Rule(
			name      = "odd-width-scalar",
			construct = "a scalar whose width is not a machine word",
			effects   = (Effect(Axis.ATOMIC, Value("NonAtomic"),
			                    "no single load or store covers 24, 40, 48 or 56 "
			                    "bits, so the access is split"),),
			remedy    = "widen the field to 8, 16, 32 or 64 bits if atomic "
			            "access matters more than the bytes saved",
		),
		applies = _is_odd_width_scalar,
	),
	Row(
		rule = Rule(
			name      = "dynamic-predecessor",
			construct = "a dynamically sized member earlier in the same frame",
			effects   = (
				Effect(Axis.OFFSET, Value("Dynamic"),
				       "the bytes before this field vary in length, so its "
				       "position is not known until parse time"),
				Effect(Axis.ADDRESS, Value("Unstable"),
				       "a pointer to it is invalidated by any write that "
				       "changes the length of what precedes it"),
			),
			remedy    = "move the variable-length member after this one; that "
			            "costs nothing and restores a static offset to "
			            "everything between them",
			blames_cause = True,
		),
		applies = _has_dynamic_offset,
	),
	Row(
		rule = Rule(
			name      = "frame-relative",
			construct = "a member of a frame, addressed from the frame base",
			# The address effect is attached per placement: an element of a
			# fixed array at a known offset is frame-relative in its offset but
			# nothing can move it, so its address is still Stable.
			effects   = (Effect(Axis.OFFSET, Value("FrameStatic"),
			                    "the offset is fixed relative to the frame base, "
			                    "which is itself found once"),),
			remedy    = "acquire a view of the frame once; the fields inside it "
			            "are then constant offsets from its base",
		),
		applies = _is_frame_relative,
	),
	Row(
		rule = Rule(
			name      = "bounded-size",
			construct = "a member whose length comes from an earlier field",
			effects   = (
				Effect(Axis.SIZE, Value("Bounded"),
				       "the extent is a range, not a number, so a caller must "
				       "size buffers for the worst case"),
				Effect(Axis.MUTATE, Value("Shifting"),
				       "writing a different length moves every member after "
				       "this one"),
			),
			remedy    = "pin the length with `[must_eq = N]` to make it fixed, "
			            "or `[max = N]` to bound the worst case",
		),
		applies = _is_bounded_size,
	),
	Row(
		rule = Rule(
			name      = "unbounded-size",
			construct = "a member with no upper bound on its length",
			effects   = (),		# see _unbounded_effects
			remedy    = "give the driving length field a `[max = N]`, which makes "
			            "the region statically allocatable",
		),
		applies = _is_unbounded_size,
	),
	Row(
		rule = Rule(
			name      = "dynamic-element-type",
			construct = "an array whose element type is variable-sized",
			effects   = (Effect(Axis.ACCESS, Value("Sequential"),
			                    "element N cannot be found without walking the "
			                    "N-1 elements before it"),),
			remedy    = "give the element type a fixed size, or use an `indexed` "
			            "region so an offset table makes access O(1)",
		),
		applies = _has_dynamic_elements,
	),
	# -- section 13.5, propagation through a transform ----------------------
	#
	# Every row below reads the property signature and nothing else. That is
	# the decidability rule of 13.3, and it is what lets phase 12 add derived
	# codecs without disturbing any of this: a derived codec arrives as a
	# signature like any other.
	Row(
		rule = Rule(
			name      = "codec-not-invertible",
			construct = "a codec with no inverse",
			effects   = (Effect(Axis.MUTATE, Value("Immutable"),
			                    "the transform cannot be undone, so the region "
			                    "can be read but never written back"),),
			remedy    = "declare `invertible;` on the codec if it does have an "
			            "inverse; otherwise the region is genuinely read-only",
		),
		applies = _not_invertible,
	),
	Row(
		rule = Rule(
			name      = "codec-needs-decode",
			construct = "a codec that is neither systematic nor length-preserving",
			effects   = (
				Effect(Axis.STAGE, Value("TransformTime"),
				       "the interior does not exist until the transform has run, "
				       "so nothing can be read before decoding"),
				Effect(Axis.ACCESS, Value("Sequential"),
				       "the decoder produces the region in order"),
			),
			remedy    = "a `systematic` codec leaves the data verbatim at "
			            "computable offsets, which allows reads with no decode "
			            "at all",
		),
		applies = _needs_decode_first,
	),
	Row(
		rule = Rule(
			name      = "codec-whole-region-rewrite",
			construct = "a length-preserving codec that is not seekable",
			effects   = (Effect(Axis.MUTATE, Value("RewriteRequired"),
			                    "interior offsets survive, but any write "
			                    "re-transforms the whole region"),),
			remedy    = "a seekable codec re-transforms only the byte range that "
			            "changed; CTR mode is seekable where CBC is not",
		),
		applies = _interior_whole_region,
	),
	Row(
		rule = Rule(
			name      = "codec-block-granularity",
			construct = "a length-preserving codec with block granularity",
			effects   = (Effect(Axis.MUTATE, Value("InPlaceSlack"),
			                    "a write re-transforms the containing block "
			                    "rather than the whole region"),),
			remedy    = "byte granularity narrows that to the bytes that changed",
		),
		applies = _interior_block_slack,
	),
	Row(
		rule = Rule(
			name      = "codec-permuted",
			construct = "a codec whose output positions are a permutation",
			effects   = (Effect(Axis.ADDRESS, Value("Unstable"),
			                    "the position map is a bijection but not "
			                    "monotone, so no contiguous span can be handed "
			                    "out"),),
			remedy    = "random access survives the permutation; sequential "
			            "prefetch does not",
		),
		applies = _interior_permuted,
	),
	Row(
		rule = Rule(
			name      = "tlv",
			construct = "a `tlv` region",
			effects   = (
				Effect(Axis.ACCESS, Value("Sequential"),
				       "items are found by walking from the start; lookup by "
				       "tag is O(n)"),
				Effect(Axis.ADDRESS, Value("Unstable"),
				       "no item keeps a stable address across any mutation of "
				       "the region"),
				Effect(Axis.MUTATE, Value("InPlaceSlack"),
				       "an item is rewritten in place only at the same size, "
				       "and an append needs slack"),
			),
			remedy    = "for a tag that is read on every message, a `positional` "
			            "field gives O(1) access instead of an O(n) scan "
			            "(project.md section 18.2)",
		),
		applies = _is_tlv,
	),
	Row(
		rule = Rule(
			name      = "tlv-unordered-items",
			construct = "a `tlv` region with no ordering rule",
			effects   = (Effect(Axis.CANONICAL, Value("NonCanonical"),
			                    "items are self-describing, so the same content "
			                    "can be written with them in any order"),),
			remedy    = "declare an ordering rule on the region, which is what "
			            "makes a tag-based format signable at all",
		),
		applies = _tlv_has_no_ordering,
	),
	Row(
		rule = Rule(
			name      = "tlv-non-minimal-tag",
			construct = "a `tlv` region whose tag type accepts non-minimal encodings",
			effects   = (Effect(Axis.CANONICAL, Value("NonCanonical"),
			                    "the tag itself has more than one encoding, so "
			                    "two byte sequences carry the same item"),),
			remedy    = "declare `minimal;` on the varint type used as `tag_type`",
		),
		applies = _tlv_tag_is_non_minimal,
	),
	Row(
		rule = Rule(
			name      = "tlv-packed-and-unpacked",
			construct = "a `tlv` region accepting both packed and unpacked "
			            "encodings of a repeated value",
			effects   = (Effect(Axis.CANONICAL, Value("NonCanonical"),
			                    "a repeated value can be written as several "
			                    "scalar items or as one length-prefixed item, so "
			                    "the same content has more than one encoding"),),
			remedy    = "accept one form or the other, not both: drop the "
			            "length-prefixed wire type from the dispatch, or the "
			            "scalar ones",
		),
		applies = _tlv_allows_packed_and_unpacked,
	),
	Row(
		rule = Rule(
			name      = "tlv-unknown-preserve",
			construct = "a `tlv` region with `unknown = preserve`",
			effects   = (Effect(Axis.CANONICAL, Value("NonCanonical"),
			                    "unknown items are carried through unchanged, so "
			                    "the encoding admits content the schema does not "
			                    "describe"),),
			remedy    = "use `unknown = error`, which is the default, and version "
			            "the schema explicitly rather than retaining unknowns "
			            "(project.md section 19)",
		),
		applies = _tlv_preserves_unknown,
	),
	Row(
		rule = Rule(
			name      = "tlv-unordered-duplicates",
			construct = "a `tlv` region with `duplicate_tags = allowed` and no "
			            "ordering rule",
			effects   = (Effect(Axis.CANONICAL, Value("NonCanonical"),
			                    "the same content can be written with the "
			                    "duplicates in any order, so it has more than "
			                    "one encoding"),),
			remedy    = "declare an ordering rule alongside `duplicate_tags = "
			            "allowed`, or use `duplicate_tags = error`",
		),
		applies = _tlv_allows_unordered_duplicates,
	),
	Row(
		rule = Rule(
			name      = "opaque",
			construct = "an `opaque` region",
			effects   = (
				Effect(Axis.ACCESS, Value("Sequential"),
				       "the region has no interior schema, so there is nothing "
				       "to address inside it"),
				Effect(Axis.MUTATE, Value("RewriteRequired"),
				       "the whole region is replaced, and only by something of "
				       "the same size"),
			),
			remedy    = "give the region an interior schema to regain field "
			            "access, or leave it opaque and treat it as bytes",
		),
		applies = _is_opaque,
	),
	Row(
		rule = Rule(
			name      = "indexed",
			construct = "an `indexed` region",
			# Access deliberately stays Random: one indirection through the
			# table reaches element N whatever the elements weigh, which is the
			# whole reason to pay for the table.
			effects   = (Effect(Axis.ADDRESS, Value("FrameStable"),
			                    "an element is reached through the offset table, "
			                    "so its address holds only while the table does"),),
			remedy    = "insertion is not an operation here: every offset after "
			            "the insertion point would have to move. Rebuild the "
			            "region instead; element mutation stays in place",
		),
		applies = _is_indexed,
	),
	Row(
		rule = Rule(
			name      = "variant-unequal-arms",
			construct = "a variant whose arms are not the same size",
			effects   = (),		# costed per placement; see _variant_effects
			remedy    = "add `[equalize]` to pad every arm to the largest, which "
			            "restores static offsets after the variant at the cost of "
			            "the padding",
		),
		applies = _has_unequal_arms,
	),
	Row(
		rule = Rule(
			name      = "varint",
			construct = "a variable-length integer",
			effects   = (
				Effect(Axis.SIZE, Value("Bounded"),
				       "one byte per seven payload bits, so the extent is a "
				       "range and a caller must size for the worst case"),
				Effect(Axis.MUTATE, Value("InPlaceSlack"),
				       "a new value that encodes to the same length stays in "
				       "place; any other length moves everything after it"),
				Effect(Axis.ALIGN, Value("Unaligned"),
				       "the field has no fixed width, so it has no natural "
				       "boundary to sit on"),
				Effect(Axis.ATOMIC, Value("NonAtomic"),
				       "the value spans a number of bytes that is not known "
				       "until it is read"),
				Effect(Axis.REPR, Value("ValueConverted"),
				       "the value is base-128 groups with continuation bits, "
				       "not the bytes"),
			),
			remedy    = "if the field carries a `max` constraint, a fixed-width "
			            "scalar is usually free: a varint costs two bytes across "
			            "most of the range of a `max = 1500` field anyway, and "
			            "`u16` restores static offsets for everything after it",
		),
		applies = _is_varint,
	),
	Row(
		rule = Rule(
			name      = "non-minimal-varint",
			construct = "a varint type without `minimal`",
			effects   = (Effect(Axis.CANONICAL, Value("NonCanonical"),
			                    "non-minimal varint encodings are accepted, so "
			                    "the same value has more than one encoding"),),
			remedy    = "declare `minimal;` on the varint type, which is required "
			            "for `require canonical`",
		),
		applies = _is_non_minimal_varint,
	),
	Row(
		rule = Rule(
			name      = "endian-native",
			construct = "`endian native`",
			effects   = (Effect(Axis.CANONICAL, Value("NonCanonical"),
			                    "the encoding depends on the host, so the same "
			                    "value has more than one valid byte sequence"),),
			remedy    = "declare `endian big` or `endian little`, or use an "
			            "`endian_marker` if the byte order must travel with the data",
		),
		applies = _is_host_dependent,
	),
	Row(
		rule = Rule(
			name      = "reserved-unknown",
			construct = "`reserved [unknown]`",
			effects   = (Effect(Axis.CANONICAL, Value("NonCanonical"),
			                    "unvalidated bits are a malleability surface: the "
			                    "same value can be encoded with any of them set"),),
			remedy    = "use `[must_be_zero]`, which is the default, or "
			            "`[preserve]` to carry the bits through without accepting "
			            "them as free",
		),
		applies = lambda context: context.reserved_unknown,
	),
	Row(
		rule = Rule(
			name      = "enum-default-pass",
			construct = "an enum with `default = pass`",
			effects   = (Effect(Axis.CANONICAL, Value("NonCanonical"),
			                    "unknown values are accepted and preserved, so "
			                    "the encoding admits values the schema does not name"),),
			remedy    = "use `default = error`, which is the default, and add a "
			            "version discriminant if the enum has to grow",
		),
		applies = lambda context: context.enum_default_pass,
	),
	Row(
		rule = Rule(
			name      = "codec-not-deterministic",
			construct = "a codec that is not `deterministic`",
			effects   = (Effect(Axis.CANONICAL, Value("NonCanonical"),
			                    "the same input may encode more than one way, so "
			                    "the bytes do not follow from the value"),),
			remedy    = "declare `deterministic;` on the codec if it is, which "
			            "`require canonical` needs; a randomised or padded mode "
			            "is genuinely not canonical and cannot be made so from "
			            "the schema side",
		),
		applies = _not_deterministic,
	),
	Row(
		rule = Rule(
			name      = "covered-by-tag",
			construct = "bytes covered by an authentication tag",
			effects   = (),		# named per placement; see _coverage_effects
			remedy    = "move the field outside the covering region if it has to "
			            "stay freely writable -- which is why real protocols put "
			            "routing headers and hop counters outside coverage -- or "
			            "accept the recomputation and assert it with "
			            "`require in_place_dirty(...)`",
		),
		applies = _is_covered,
	),
	Row(
		rule = Rule(
			name      = "verify-gated",
			construct = "the interior of a sealed region",
			effects   = (Effect(Axis.STAGE, Value("VerifyGated"),
			                    "no view into the interior exists until the tag "
			                    "verifies, so parsing attacker-controlled "
			                    "plaintext before authenticating it is "
			                    "unrepresentable rather than discouraged"),),
			remedy    = "",
		),
		applies = _is_verify_gated,
	),
	Row(
		rule = Rule(
			name      = "allow-unverified-read",
			construct = "`sealed(...) [allow_unverified_read]`",
			effects   = (Effect(Axis.STAGE, Value("TransformTime"),
			                    "the stage gate of section 14.3 is waived here: "
			                    "the interior is reachable before the tag "
			                    "verifies, on attacker-controlled bytes"),),
			remedy    = "drop `[allow_unverified_read]` unless the protocol "
			            "genuinely cannot verify first; it is the single "
			            "highest-value security property in the design",
		),
		applies = _reads_unverified,
	),
	Row(
		rule = Rule(
			name      = "tag-field",
			construct = "an authentication tag or checksum",
			effects   = (Effect(Axis.MUTATE, Value("Immutable"),
			                    "its value is whatever the algorithm computes "
			                    "over the bytes it covers, so it is written by "
			                    "finalize and by nothing else"),),
			remedy    = "",
		),
		applies = _is_tag,
	),
	Row(
		rule = Rule(
			name      = "strictness-lenient",
			construct = "`strictness = lenient`",
			effects   = (Effect(Axis.CANONICAL, Value("NonCanonical"),
			                    "the parser accepts what the schema does not "
			                    "describe, so more than one byte sequence "
			                    "carries the same value"),),
			remedy    = "`strictness = strict`, which is the default, and a "
			            "version discriminant where the format has to grow "
			            "(project.md section 19)",
		),
		applies = _is_lenient,
	),
	Row(
		rule = Rule(
			name      = "secret-field",
			construct = "a `[secret]` field",
			effects   = (Effect(Axis.SECRECY, Value("Secret"),
			                    "debug accessors are suppressed and the storage "
			                    "is zeroized"),),
			remedy    = "",
		),
		applies = lambda context: _has_attr(context.placement.attrs, "secret"),
	),
)


def _has_attr(attrs: tuple[ast.Attr, ...], name: str) -> bool:
	return any(attr.name == name for attr in attrs)


# ---------------------------------------------------------------------------
# Applying the table
# ---------------------------------------------------------------------------


def apply(context: Context) -> Resolved:
	"""Run every row against one placement.

	The base vector states what the layout already knows -- offset, size and
	alignment are facts rather than weakenings -- and the table supplies the
	rest. A field that fires no row is at the strongest value on every axis,
	which is the identity row of section 11.3.
	"""
	placement = context.placement
	vector    = _base_vector(placement)
	weakenings: list[Weakening] = []

	for row in TABLE:
		if not row.applies(context):
			continue

		effects = row.rule.effects
		if row.rule.name == "endian-marker-scope":
			effects = _marker_effects(context)
		elif row.rule.name == "dynamic-predecessor":
			effects = _predecessor_effects(context)
		elif row.rule.name == "frame-relative":
			effects = _frame_effects(context)
		elif row.rule.name == "variant-unequal-arms":
			effects = _variant_effects(context)
		elif row.rule.name == "unbounded-size":
			effects = _unbounded_effects(context)
		elif row.rule.name == "covered-by-tag":
			effects = _coverage_effects(context)

		for effect in effects:
			effect  = _parameterise(effect, context.placement)
			current = vector.get(effect.axis)
			# Meet, not assignment: a rule never strengthens an axis another
			# rule already weakened. An effect at the same strength still
			# records its weakening, because two constructs can independently
			# cost the same capability and a blame chain wants both.
			if rank(effect.axis, effect.value) < rank(effect.axis, current):
				continue

			vector = vector.with_value(effect.axis, effect.value)
			weakenings.append(Weakening(
				rule    = row.rule,
				effect  = effect,
				span    = (placement.dynamic_cause_span or placement.span)
				          if row.rule.blames_cause else placement.span,
				subject = placement.path,
			))

	return Resolved(placement=placement, vector=vector, weakenings=weakenings)


def _coverage_effects(context: Context) -> tuple[Effect, ...]:
	"""Name the tags, because "covered" without them is not actionable.

	Only the `auth` axis moves. Writing a covered field is still a store to the
	same bytes at the same offset, so nothing about `mutate` changes -- what
	changes is that a tag now has to be recomputed, and that obligation is what
	this axis records. Section 14.2 turns on the distinction: in-place mutation
	is *possible* and it *invalidates coverage*, and a design that conflated the
	two would have nothing left to say about the difference.
	"""
	tags   = context.placement.covered_by
	listed = ", ".join(f"`{tag}`" for tag in tags)

	if len(tags) == 1:
		subject = f"tag {listed} authenticates"
		stale   = "the tag"
	else:
		subject = f"tags {listed} authenticate"
		stale   = "them"

	return (Effect(Axis.AUTH, Value("Covered", tags),
	               f"{subject} these bytes, so writing them leaves {stale} stale "
	               "until finalize recomputes it"),)


def _unbounded_effects(context: Context) -> tuple[Effect, ...]:
	effects = [Effect(Axis.SIZE, Value("Unbounded"),
	                  "nothing in the schema limits how many bytes this can "
	                  "occupy, so it cannot be statically allocated")]

	if not _owns_its_mutate(context.placement):
		effects.append(Effect(Axis.MUTATE, Value("Shifting"),
		                      "changing its length moves everything after it"))

	return tuple(effects)


def _variant_effects(context: Context) -> tuple[Effect, ...]:
	"""Cost the arms, so the advisor's equalization suggestion is concrete.

	Section 18.2 wants the padding cost of equalizing the arms, not just the
	news that they differ. It is the difference between each arm and the
	largest, which is exactly what padding to the largest would add.
	"""
	sizes   = context.placement.arm_sizes
	largest = max(size for _, size in sizes)
	worst   = max(largest - size for _, size in sizes) // BITS_PER_BYTE

	detail = ", ".join(f"`{name}` {size // BITS_PER_BYTE}" for name, size in sizes)

	return (
		Effect(Axis.SIZE, Value("Bounded"),
		       f"the extent is whichever arm is selected: {detail} bytes; "
		       f"equalizing them would cost up to {worst} bytes of padding"),
		Effect(Axis.MUTATE, Value("Shifting"),
		       "selecting a different arm changes the extent, moving every "
		       "member after the variant"),
	)


def _frame_effects(context: Context) -> tuple[Effect, ...]:
	effects = [Effect(Axis.OFFSET, Value("FrameStatic"),
	                  "the offset is fixed relative to the frame base, which is "
	                  "itself found once")]

	if context.placement.frame_base_dynamic:
		effects.append(Effect(
			Axis.ADDRESS, Value("FrameStable"),
			"a pointer stays valid only while the frame's base does not move"))

	return tuple(effects)


def _predecessor_effects(context: Context) -> tuple[Effect, ...]:
	"""Name the member that actually caused the loss.

	"offset is Dynamic" is a fact; "`opts` has size Bounded(0, 1500), which is
	why everything after it moved" is a diagnostic.
	"""
	placement = context.placement
	cause     = placement.dynamic_cause
	size      = placement.dynamic_cause_size

	if cause is None:
		blame = "an earlier member's length is not fixed"
	else:
		blame = f"`{cause}` has size {size}, so the bytes before this field vary"

	return (
		Effect(Axis.OFFSET, Value("Dynamic"), blame),
		Effect(Axis.ADDRESS, Value("Unstable"),
		       "a pointer to it is invalidated by any write that changes the "
		       "length of what precedes it"),
	)


def _parameterise(effect: Effect, placement: Placement) -> Effect:
	"""Fill in a value's parameters from the placement it applies to.

	The table states which value an axis takes; the numbers belong to the field,
	not to the rule, so they are attached here rather than duplicated per row.
	"""
	if effect.value.params:
		return effect

	if effect.axis is Axis.OFFSET and effect.value.base == "FrameStatic":
		assert placement.offset_bits is not None
		return Effect(effect.axis,
		              Value("FrameStatic", (render_offset(placement.offset_bits),)),
		              effect.because)

	if effect.axis is Axis.SIZE and effect.value.base == "Bounded":
		assert placement.size_max_bits is not None
		return Effect(effect.axis, Value("Bounded", (
			render_size(placement.size_bits),
			render_size(placement.size_max_bits))), effect.because)

	return effect


def _marker_effects(context: Context) -> tuple[Effect, ...]:
	"""The two axes a byte-order marker moves, and only those.

	Endianness never changes extent, so `offset` and `size` are untouched. That
	is the saving grace of the construct and the reason it is cheap to support
	(project.md section 8.3).
	"""
	marker = context.placement.marker
	assert marker is not None

	return (
		Effect(Axis.REPR, Value("ConditionallyConverted", (marker,)),
		       f"the byte swap is a parse-time branch on `{marker}`, so the value "
		       "is not the memory and no pointer accessor is generated"),
		Effect(Axis.CANONICAL, Value("CanonicalGiven", (marker,)),
		       f"two byte sequences encode the same value at the format level, "
		       f"but exactly one does given `{marker}`"),
	)


def _base_vector(placement: Placement) -> Vector:
	"""Facts the layout established, before any rule fires.

	Offsets and sizes start at their strongest values and are weakened by the
	rows above. Starting anywhere else would let a construct strengthen an axis,
	which invariant 2 forbids.
	"""
	vector = Vector()

	if placement.offset_bits is not None:
		vector = vector.with_value(
			Axis.OFFSET,
			Value("AbsoluteStatic", (render_offset(placement.offset_bits),)))

	if placement.size_max_bits == placement.size_bits:
		vector = vector.with_value(
			Axis.SIZE, Value("Fixed", (render_size(placement.size_bits),)))

	# Always stored, so the map renders `Aligned(8)` rather than a bare
	# `Aligned` for a field that happens to sit at the strongest boundary.
	return vector.with_value(Axis.ALIGN, _align_value(placement))


def render_offset(offset_bits: int) -> str:
	"""A byte offset, or `byte:bit` when the field does not start on a byte.

	Never truncated. A sub-byte offset printed as a plain byte number would be
	a lie every later pass would inherit (project.md section 26.2).
	"""
	byte, bit = divmod(offset_bits, BITS_PER_BYTE)
	return f"0x{byte:02X}" if bit == 0 else f"0x{byte:02X}:{bit}"


def render_size(size_bits: int) -> str:
	byte, bit = divmod(size_bits, BITS_PER_BYTE)
	return str(byte) if bit == 0 else f"{size_bits}bit"
