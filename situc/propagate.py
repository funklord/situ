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
	scalar = context.scalar
	if scalar is None or scalar.is_bit_packed or scalar.bits <= BITS_PER_BYTE:
		return False
	if context.placement.offset_bits is None:
		return False
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
	placement = context.placement
	return (placement.size_max_bits is not None
	        and placement.size_max_bits != placement.size_bits)


def _is_unbounded_size(context: Context) -> bool:
	return context.placement.size_max_bits is None


def _has_dynamic_elements(context: Context) -> bool:
	"""An array whose element type is itself variable-sized.

	Element k cannot be found without walking the k-1 before it, so access
	drops to Sequential (section 11.3).
	"""
	placement = context.placement
	return (placement.kind == "element"
	        and placement.size_max_bits != placement.size_bits)


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
			construct = "a multi-byte scalar at an offset below its natural alignment",
			effects   = (Effect(Axis.ATOMIC, Value("NonAtomic"),
			                    "an unaligned word access faults on some targets "
			                    "and is split on others"),),
			remedy    = "reorder the preceding fields, or insert `reserved` "
			            "padding, to land this field on its natural boundary",
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
			effects   = (
				Effect(Axis.SIZE, Value("Unbounded"),
				       "nothing in the schema limits how many bytes this can "
				       "occupy, so it cannot be statically allocated"),
				Effect(Axis.MUTATE, Value("Shifting"),
				       "changing its length moves everything after it"),
			),
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
