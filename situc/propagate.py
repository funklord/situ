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


def _is_fixed_point(context: Context) -> bool:
	return context.scalar is not None and context.scalar.is_fixed_point


def _is_bcd(context: Context) -> bool:
	return context.scalar is not None and context.scalar.is_bcd


def _is_derived(context: Context) -> bool:
	return context.placement.derived_by is not None


def _is_nul_terminated(context: Context) -> bool:
	return any(attr.name == "nul_terminated"
	           for attr in context.placement.attrs)


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

	`opaque` and `tlv` are *not* excluded, though they own their mutate
	effect: excluding the whole row dropped the size effect with it, so an
	`opaque` region whose extent the data decides kept the default `Fixed` --
	rendered without a number, which is the tell that no rule ever set it. The
	unbounded row beside this one has always emitted the size effect and
	withheld only the mutate one; this one had drifted a construct behind it.
	"""
	placement = context.placement
	return (not _has_unequal_arms(context)
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


def _is_declared_non_canonical(context: Context) -> bool:
	return any(attr.name == "non_canonical"
	           for attr in context.placement.attrs)


def _is_repeat_while(context: Context) -> bool:
	return context.placement.repeat_while is not None


def _is_delimited(context: Context) -> bool:
	return context.placement.delimiter is not None


def _is_text_number(context: Context) -> bool:
	return context.placement.radix is not None


def _is_loose_text_number(context: Context) -> bool:
	"""A text number that accepts more than one spelling of a value.

	`007` and `7` are the same number, and so are `FF` and `ff`. That is
	exactly what `canonical` reports, and decision 0020 argued the axis has
	more to say about text than about binary -- then the first text construct
	shipped without using it.
	"""
	placement = context.placement
	if placement.radix is None or placement.radix_minimal:
		return False

	# A fixed-width text number is canonical without asking. `007` is not a
	# second spelling of `7` in a three-digit field -- it is the only one,
	# because `7` alone does not fit the field and the parse refuses a space.
	# The padding is forced rather than optional, which is the whole
	# difference between this and a delimited number.
	return placement.array_count is None


def _is_uncapped_scan(context: Context) -> bool:
	placement = context.placement
	return placement.delimiter is not None and placement.delimiter_cap is None


def _is_relaxed_delimiter(context: Context) -> bool:
	"""The delimiter may occur in the content, so a value has two spellings.

	Without a relaxation the content simply may not contain the delimiter --
	which is checked on parse, and is what keeps the field Canonical.
	"""
	placement = context.placement
	return (placement.delimiter is not None
	        and (placement.delimiter_quote is not None
	             or placement.delimiter_escape is not None))


def _is_versioned(context: Context) -> bool:
	return context.placement.since is not None


def _is_trimmed(context: Context) -> bool:
	return context.placement.trimmed


def _is_case_insensitive(context: Context) -> bool:
	return context.placement.case_insensitive


def _is_data_placed(context: Context) -> bool:
	return context.placement.located is not None


def _is_past_a_scan(context: Context) -> bool:
	return context.placement.scan_cause is not None


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


def _indexes_outside_the_region(context: Context) -> bool:
	"""An index table whose offsets are bounded by nothing the frame knows.

	The message base and nothing else. A member base measures from a member of
	this same struct, so an element is still inside the frame and the check at
	the frame boundary still covers it -- it reaches outside the *region* and
	not outside the frame, which is a weaker statement than this axis makes.
	`example/sqlite` is what made the distinction concrete: a cell pointer is
	measured from the start of the page, which is a member of the page.
	"""
	table = context.placement.index_table
	return table is not None and table.base == "message"


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


# -- the MMIO target (section 15.3) -----------------------------------------


def _register_of(context: Context) -> ast.RegisterInfo | None:
	return context.placement.register


def _needs_rmw(context: Context) -> bool:
	"""A field narrower than the bus access that reaches it.

	Writing it means reading the word, changing some bits and writing it back.
	Whether that is *safe* is a separate question, which the two rows below
	answer; this only says the read is necessary.
	"""
	register = _register_of(context)
	if register is None or context.placement.kind == "reserved":
		return False
	return context.placement.size_bits < register.access_width


def _reads_have_effects(context: Context) -> bool:
	placement = context.placement
	register  = _register_of(context)
	if register is None:
		return False
	return register.no_rmw or placement.on_read is not ast.SideEffect.NONE


def _rmw_is_unsafe(context: Context) -> bool:
	"""Section 15.3's headline interaction, and the reason for the chapter.

	`access_width = 32` plus a one-bit field means a single-bit write needs a
	read-modify-write; `no_rmw` or a read with side effects means that read is
	not something the generated code may perform. Together they make the field
	`RewriteRequired`, so no setter is generated and the caller composes the
	whole word.
	"""
	return _needs_rmw(context) and _reads_have_effects(context)


def _is_partial_word(context: Context) -> bool:
	"""A register field that is not the whole bus access.

	The memory here is a bus word, and a narrower field is shifted and masked
	out of one. So the value is not the bytes however wide the field is: a `u8`
	at bit 3 of a 32-bit register is as converted as a `u3` is, which the
	buffer rules would not have said.
	"""
	register  = _register_of(context)
	placement = context.placement
	if register is None:
		return False
	return (placement.size_bits != register.access_width
	        or placement.offset_bits is None
	        or placement.offset_bits % register.access_width != 0)


def _is_read_only(context: Context) -> bool:
	mode = context.placement.access_mode
	return mode is not None and not mode.writable


def _register_effect(context: Context) -> bool:
	placement = context.placement
	return (placement.register is not None
	        and (placement.on_read is not ast.SideEffect.NONE
	             or placement.on_write is not ast.SideEffect.NONE))


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
			name      = "derived-field",
			construct = "a field an invariant maintains",
			effects   = (
				Effect(Axis.MUTATE, Value("Immutable"),
				       "an invariant decides this field's value, so writing it "
				       "directly would make the schema's own statement false"),
			),
			remedy    = "write what it derives from and call the generated "
			            "recompute, which is the one thing that may set it",
		),
		applies = _is_derived,
	),
	Row(
		rule = Rule(
			name      = "declared-non-canonical",
			construct = "a schema saying its encoding is not canonical",
			effects   = (),		# the reason is the schema's; see below
			remedy    = "",
		),
		applies = _is_declared_non_canonical,
	),
	Row(
		rule = Rule(
			name      = "repeat-while",
			construct = "a run that ends after the element failing a condition",
			effects   = (
				Effect(Axis.ACCESS, Value("Sequential"),
				       "how many elements there are is not in the schema and "
				       "not stated in the data either: it is whichever one "
				       "first fails the condition, so element N is reached by "
				       "reading the N-1 before it"),
				Effect(Axis.MUTATE, Value("Shifting"),
				       "an element whose length changes moves every element "
				       "after it, and so does one that starts or stops "
				       "satisfying the condition"),
			),
			remedy    = "`while (...) max N` bounds the walk, which makes the "
			            "run statically allocatable; a count field ahead of it "
			            "would make it `Random` instead, at the cost of a "
			            "number the format has to carry",
		),
		applies = _is_repeat_while,
	),
	Row(
		rule = Rule(
			name      = "delimited-member",
			construct = "a member that ends at a delimiter",
			effects   = (
				Effect(Axis.MUTATE, Value("Shifting"),
				       "the length is wherever the delimiter turns out to be, "
				       "so a longer value needs more room and the bytes after "
				       "it have to move to make it"),
			),
			# The size axis is deliberately not set here. `unbounded-size`
			# already reports an absent upper bound and `until D max N` gives
			# one, so claiming Unbounded from this row would have reported a
			# capped scan as unbounded -- the row would have contradicted the
			# remedy it names.
			remedy    = "`until D max N` bounds the scan, which makes the member "
			            "statically allocatable and turns a missing delimiter "
			            "into an error instead of a read to the end of the buffer",
		),
		applies = _is_delimited,
	),
	Row(
		rule = Rule(
			name      = "text-number",
			construct = "a number written as digits rather than stored as bits",
			effects   = (
				Effect(Axis.REPR, Value("TextConverted"),
				       "the value is not the memory and the conversion can "
				       "fail: a byte swap is total, and `12x4` is not a "
				       "number"),
			),
			remedy    = "read it through the generated getter, which returns an "
			            "error rather than a value where the digits are not "
			            "digits; there is no pointer accessor that could hand "
			            "back a number",
		),
		applies = _is_text_number,
	),
	Row(
		rule = Rule(
			name      = "non-minimal-text-number",
			construct = "a text number that accepts leading zeros",
			effects   = (
				Effect(Axis.CANONICAL, Value("NonCanonical"),
				       "`007` and `7` are the same value written two ways, and "
				       "for hexadecimal so are `FF` and `ff`, so the bytes do "
				       "not follow from the number"),
			),
			remedy    = "`[minimal]` refuses a leading zero on parse, which buys "
			            "one spelling per value back -- the same thing it means "
			            "on a varint",
		),
		applies = _is_loose_text_number,
	),
	Row(
		rule = Rule(
			name      = "unbounded-scan",
			construct = "a delimited member with no cap on the scan",
			effects   = (
				Effect(Axis.EFFECT, Value("EffectOnRead"),
				       "reading it walks the buffer to the delimiter, so the "
				       "cost of a read depends on the data rather than the "
				       "schema"),
			),
			remedy    = "`until D max N` bounds the walk",
		),
		applies = _is_uncapped_scan,
	),
	Row(
		rule = Rule(
			name      = "relaxed-delimiter",
			construct = "a delimited member whose delimiter may occur in its content",
			effects   = (
				Effect(Axis.CANONICAL, Value("NonCanonical"),
				       "a quoted or escaped delimiter is inert, so the same "
				       "value has more than one spelling"),
			),
			remedy    = "drop `[quoted]` or `[escape]` if the protocol does not "
			            "need them; without one the content may not contain the "
			            "delimiter at all, which is checked on parse and buys a "
			            "single spelling back",
		),
		applies = _is_relaxed_delimiter,
	),
	Row(
		rule = Rule(
			name      = "versioned-member",
			construct = "a member present only from a given protocol version",
			effects   = (
				Effect(Axis.STAGE, Value("ParseTime"),
				       "whether these bytes are here at all is a value in the "
				       "data, so nothing can reach them before the version "
				       "field has been read"),
			),
			remedy    = "read it through the generated accessor, which returns "
			            "a version error rather than the bytes that happen to "
			            "follow; there is no unconditional getter, because "
			            "there is no unconditional field",
		),
		applies = _is_versioned,
	),
	Row(
		rule = Rule(
			name      = "trimmed-value",
			construct = "a value with optional whitespace around it",
			effects   = (
				Effect(Axis.CANONICAL, Value("NonCanonical"),
				       "` 5`, `5` and `5  ` carry the same value, so the bytes "
				       "do not follow from it"),
			),
			remedy    = "emit no padding on write and refuse it on parse, which "
			            "gets a single encoding back -- at the cost of rejecting "
			            "messages the format permits",
		),
		applies = _is_trimmed,
	),
	Row(
		rule = Rule(
			name      = "case-insensitive-token",
			construct = "a token compared without regard to case",
			effects   = (
				Effect(Axis.CANONICAL, Value("NonCanonical"),
				       "`Content-Length` and `content-length` are one token "
				       "with two spellings, so the bytes do not follow from "
				       "the value"),
			),
			remedy    = "compare through the generated `_eq`, which folds ASCII "
			            "case the way the schema says; `memcmp` against a "
			            "literal is the bug this attribute exists to name",
		),
		applies = _is_case_insensitive,
	),
	Row(
		rule = Rule(
			name      = "data-placed",
			construct = "a member the data positions, rather than the members before it",
			effects   = (
				Effect(Axis.OFFSET, Value("DataPlaced"),
				       "where it starts is a number in the message rather "
				       "than the sum of what precedes it, so nothing about "
				       "the frame says where it is or that it is inside one"),
				Effect(Axis.ADDRESS, Value("Unstable"),
				       "a pointer to it moves whenever the field holding its "
				       "offset is written, which is a single field far away "
				       "from the bytes that move"),
			),
			remedy = "a member placed after the one before it keeps a static "
			         "or dynamic offset, and the bounds check at the frame "
			         "boundary covers it; an offset the message chooses has "
			         "to be checked on every use",
		),
		applies = _is_data_placed,
	),
	Row(
		rule = Rule(
			name      = "scanned-predecessor",
			construct = "a member found by scanning for a delimiter earlier in the frame",
			effects   = (
				Effect(Axis.OFFSET, Value("Scanned"),
				       "reaching this field means searching for the delimiter "
				       "that ends an earlier one, which is linear in the "
				       "distance to it and can fail when the delimiter is not "
				       "there"),
				Effect(Axis.ACCESS, Value("Sequential"),
				       "there is no arithmetic that finds this field: every "
				       "member between it and the frame base has to be walked"),
				Effect(Axis.ADDRESS, Value("Unstable"),
				       "a pointer to it moves whenever anything before it "
				       "changes length, which delimited content does freely"),
			),
			remedy = "put the fixed-offset members before the first delimited "
			         "one; everything ahead of a scan keeps a static offset, "
			         "and that costs nothing but declaration order",
			blames_cause = True,
		),
		applies = _is_past_a_scan,
	),
	Row(
		rule = Rule(
			name      = "nul-terminated",
			construct = "a nul-terminated field",
			effects   = (
				Effect(Axis.CANONICAL, Value("NonCanonical"),
				       "the declared size is the capacity, so the bytes after "
				       "the terminator do not affect the value and many byte "
				       "sequences encode the same one"),
			),
			remedy    = "zero the padding on write and require it on parse to "
			            "get a single encoding back, or drop the attribute and "
			            "treat the field as the fixed-width bytes it is",
		),
		applies = _is_nul_terminated,
	),
	Row(
		rule = Rule(
			name      = "fixed-point",
			construct = "a fixed-point field",
			effects   = (
				Effect(Axis.REPR, Value("ValueConverted"),
				       "the stored integer is the value scaled by a power of "
				       "two, so reading the number it means is a shift"),
			),
			remedy    = "the accessors hand back the stored integer and the "
			            "header carries the scale; no floating point is "
			            "generated, because the target may have none",
		),
		applies = _is_fixed_point,
	),
	Row(
		rule = Rule(
			name      = "bcd",
			construct = "a packed binary-coded decimal field",
			effects   = (
				Effect(Axis.REPR, Value("ValueConverted"),
				       "each nibble is a decimal digit, so the value is "
				       "decoded rather than read"),
			),
			remedy    = "use an integer field if the wire format allows one; "
			            "BCD costs a decode on every access and can hold bit "
			            "patterns that are not numbers",
		),
		applies = _is_bcd,
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
			),		# withheld per construct; see _bounded_effects
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
			name      = "indexed-outside-the-region",
			construct = "an `indexed` region whose offsets are measured from "
			            "the start of the message",
			# The construct's own row leaves address at FrameStable, which is
			# true while an offset cannot name a byte outside the region: the
			# region's extent bounds it, and the frame check covers it. It
			# stops being true the moment `base` names the message or an
			# earlier member (decision 0024). Section 9.8 makes exactly this
			# argument for `at expr`, and an index table is the same shape with
			# a table in front of it.
			effects   = (Effect(Axis.ADDRESS, Value("Unstable"),
			                    "an offset measured from outside the region "
			                    "can name any byte in the message, so nothing "
			                    "about the region says an element is inside it "
			                    "and a write anywhere may move one"),),
			remedy    = "measure from the region, which is the default: an "
			            "offset that cannot leave the region is bounded by the "
			            "region's own extent, and the check at the boundary "
			            "covers it",
		),
		applies = _indexes_outside_the_region,
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
			name      = "register-partial-word",
			construct = "a register field narrower than the bus access",
			effects   = (
				Effect(Axis.REPR, Value("ValueConverted"),
				       "the value is shifted and masked out of a bus word, so "
				       "it is not the memory and no pointer to it exists"),
				Effect(Axis.ATOMIC, Value("NonAtomic"),
				       "the word arrives in one transaction, but isolating the "
				       "field from it does not"),
			),
			remedy    = "a field occupying the whole `access_width` is read and "
			            "written as one transaction with no masking at all",
		),
		applies = _is_partial_word,
	),
	Row(
		rule = Rule(
			name      = "register-read-only",
			construct = "a register field the bus does not let you write",
			effects   = (Effect(Axis.MUTATE, Value("Immutable"),
			                    "the hardware drives this field; a write to it "
			                    "either does nothing or does something else"),),
			remedy    = "",
		),
		applies = _is_read_only,
	),
	Row(
		rule = Rule(
			name      = "register-rmw-unsafe",
			construct = "a partial-width field in a register whose reads are "
			            "not free",
			effects   = (),		# costed per placement; see _rmw_effects
			remedy    = "compose the whole word and write it once, which the "
			            "generated builder does; or widen `access_width` to the "
			            "field if the bus allows a narrower transaction",
		),
		applies = _rmw_is_unsafe,
	),
	Row(
		rule = Rule(
			name      = "register-side-effect",
			construct = "a register field whose access has a side effect",
			effects   = (),		# named per placement; see _effect_effects
			remedy    = "a field with `on_read` other than `none` cannot be read "
			            "twice for the same value; read it once and keep it",
		),
		applies = _register_effect,
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
		if row.rule.name == "declared-non-canonical":
			effects = _declared_effects(context)
		elif row.rule.name == "endian-marker-scope":
			effects = _marker_effects(context)
		elif row.rule.name == "dynamic-predecessor":
			effects = _predecessor_effects(context)
		elif row.rule.name == "frame-relative":
			effects = _frame_effects(context)
		elif row.rule.name == "variant-unequal-arms":
			effects = _variant_effects(context)
		elif row.rule.name == "bounded-size":
			effects = _bounded_effects(context)
		elif row.rule.name == "unbounded-size":
			effects = _unbounded_effects(context)
		elif row.rule.name == "covered-by-tag":
			effects = _coverage_effects(context)
		elif row.rule.name == "register-rmw-unsafe":
			effects = _rmw_effects(context)
		elif row.rule.name == "register-side-effect":
			effects = _effect_effects(context)

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


def _rmw_effects(context: Context) -> tuple[Effect, ...]:
	"""Say which two facts combined, because either alone would be fine.

	A narrow field in a wide access is ordinary; a register whose reads have
	side effects is ordinary. It is the pair that removes the setter, and a
	diagnostic naming only one of them would send the reader to change the
	wrong thing.
	"""
	placement = context.placement
	register  = placement.register
	assert register is not None

	why = ("`no_rmw` is declared" if register.no_rmw
	       else f"`on_read = {placement.on_read.value}` makes the read destructive")

	return (Effect(Axis.MUTATE, Value("RewriteRequired"),
	                f"the field is {placement.size_bits} of "
	                f"{register.access_width} bits, so writing it alone would "
	                f"need a read-modify-write, and {why}"),)


def _effect_effects(context: Context) -> tuple[Effect, ...]:
	placement = context.placement
	reads     = placement.on_read is not ast.SideEffect.NONE
	writes    = placement.on_write is not ast.SideEffect.NONE

	if reads and writes:
		value, detail = "EffectBoth", (f"reading it {placement.on_read.value}s and "
		                               f"writing it {placement.on_write.value}s")
	elif reads:
		value, detail = "EffectOnRead", f"reading it {placement.on_read.value}s"
	else:
		value, detail = "EffectOnWrite", f"writing it {placement.on_write.value}s"

	return (Effect(Axis.EFFECT, Value(value),
	               f"{detail}, so the access is not a pure load or store and "
	               "may not be repeated, reordered or elided"),)


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


def _row(name: str) -> Rule:
	"""One rule by name, so a computed effect list starts from the table.

	Copying the effects into the function would be two statements of one rule,
	which is invariant 1 with the table one level in.
	"""
	return next(row.rule for row in TABLE if row.rule.name == name)


def _bounded_effects(context: Context) -> tuple[Effect, ...]:
	"""The size effect always, the mutate effect only where nothing owns it.

	The same split `_unbounded_effects` has always made, arrived at from the
	other direction: this row used to withhold *both* by not applying at all,
	which left `opaque` and `tlv` regions claiming a fixed size they have not
	got.
	"""
	effects = [effect for effect in _row("bounded-size").effects
	           if effect.axis is not Axis.MUTATE
	           or not _owns_its_mutate(context.placement)]
	return tuple(effects)


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

	# An arm with no upper bound takes the variant's with it. A `Bounded`
	# here is a promise of two numbers, and the parameterisation that fills
	# them in asserts rather than guessing -- so a variant one of whose arms
	# ends in `[remaining]` crashed the compiler on an assertion instead of
	# reporting anything. MQTT's PUBLISH is that shape: its payload is the
	# rest of the packet, and it sits in a switch beside CONNACK's two bytes.
	#
	# Unbounded is also the true answer, and it is the *weaker* one, which is
	# the direction invariant 2 permits: nothing in the schema limits how many
	# bytes the selected arm can occupy.
	if context.placement.size_max_bits is None:
		return (
			Effect(Axis.SIZE, Value("Unbounded"),
			       f"the extent is whichever arm is selected: {detail} bytes,"
			       " and one of them has no upper bound"),
			Effect(Axis.MUTATE, Value("Shifting"),
			       "selecting a different arm changes the extent, moving "
			       "every member after the variant"),
		)

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


def _declared_effects(context: Context) -> tuple[Effect, ...]:
	"""A weakening the schema states because the lattice cannot derive it.

	Section 11.5. Everything else on this axis is inferred from a construct
	situ understands -- a capacity-sized string, optional whitespace, a
	non-minimal varint -- and some formats are non-canonical for reasons that
	are nowhere in the layout. A DNS name may be spelled uncompressed or as a
	pointer to any earlier occurrence of any suffix of it, which is many byte
	sequences for one value and not a fact about where the bytes are.

	Safe by construction, and that is the whole argument for allowing it:
	invariant 2 says no construct may *strengthen* an axis, so a declaration
	that can only move down the lattice cannot make the map claim more than
	situ can back. It can only make it claim less, which is what an honest
	schema does when it knows something the compiler does not.

	The reason is required. A weakening with no reason is a shrug, and the
	blame chain is where a reader finds out why a field costs what it does.
	"""
	from situc.unparse import expr_to_source

	for attr in context.placement.attrs:
		if attr.name != "non_canonical":
			continue
		reason = ("no reason given" if attr.value is None
		          else expr_to_source(attr.value).strip('"'))
		return (Effect(Axis.CANONICAL, Value("NonCanonical"),
		               f"the schema says so: {reason}"),)
	return ()


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
