"""Capability axis values.

Phase 2 populates the five axes decidable from a static layout alone: `offset`,
`size`, `align`, `repr` and `atomic`. The lattice itself -- meet, the ordering,
and the propagation table of section 11.3 as data -- arrives in phase 3. What
lives here is the vocabulary, spelled exactly as project.md section 11.1 spells
it, so the committed map can be read against the specification.

The one rule that shapes everything below: no construct may strengthen an axis.
Where a value is uncertain, the weaker one is correct.
"""

from __future__ import annotations

from dataclasses import dataclass

from situc import ast
from situc.layout import BITS_PER_BYTE, Placement
from situc.types import ScalarKind

# The widest scalar, and so the strongest alignment worth reporting.
MAX_ALIGN = 8

ATOMIC_WIDTHS = frozenset({8, 16, 32, 64})


@dataclass(frozen=True)
class Vector:
	"""One field's capability vector, restricted to the phase 2 axes."""

	offset: str
	size: str
	align: str
	repr: str
	atomic: str

	def items(self) -> list[tuple[str, str]]:
		return [
			("offset", self.offset),
			("size",   self.size),
			("align",  self.align),
			("repr",   self.repr),
			("atomic", self.atomic),
		]


def render_offset(offset_bits: int) -> str:
	"""A byte offset, or `byte:bit` when the field does not start on a byte.

	Never truncated to bytes. A sub-byte offset that printed as a plain byte
	number would be a lie, and section 26.2 asks specifically that it be
	reported as such.
	"""
	byte, bit = divmod(offset_bits, BITS_PER_BYTE)
	return f"0x{byte:02X}" if bit == 0 else f"0x{byte:02X}:{bit}"


def render_size(size_bits: int) -> str:
	byte, bit = divmod(size_bits, BITS_PER_BYTE)
	if bit == 0:
		return str(byte)
	return f"{size_bits}bit"


def alignment_of(offset_bits: int) -> int:
	"""The strongest power-of-two byte boundary this offset satisfies.

	A property of the offset alone, as section 11.1 defines the axis: alignment
	relative to the message base. `aligned(X, n)` then passes for every n up to
	this value, which is what a schema author is asking about.
	"""
	if offset_bits % BITS_PER_BYTE != 0:
		return 0

	byte = offset_bits // BITS_PER_BYTE
	if byte == 0:
		return MAX_ALIGN

	alignment = 1
	while alignment < MAX_ALIGN and byte % (alignment * 2) == 0:
		alignment *= 2
	return alignment


# Weakening order per axis, strongest first (section 11.1). The full lattice
# with meet over every axis is phase 3; these two are the ones an aggregate
# needs today.
REPR_ORDER   = ("MemoryIdentical", "ValueConverted", "ConditionallyConverted")
ATOMIC_ORDER = ("AtomicWord", "NonAtomic")


def meet(values: list[str], order: tuple[str, ...]) -> str:
	"""The weakest of a set of values on one axis.

	A struct's vector is the meet of its members' (section 11.2). Meet is the
	worst case, never the best: nothing may strengthen a capability.
	"""
	if not values:
		return order[-1]
	return max(values, key=lambda value: order.index(_base(value)))


def _base(value: str) -> str:
	return value.split("(", 1)[0]


def derive(placement: Placement, members: list[Vector] | None = None) -> Vector:
	"""Compute the phase 2 axes for one placement.

	`members` is supplied for a field whose type is a struct: its `repr` and
	`atomic` are then the meet of its members' rather than anything it claims
	for itself. Its offset, size and alignment remain its own.
	"""
	aggregate = members is not None

	if aggregate:
		assert members is not None
		representation = meet([vector.repr for vector in members], REPR_ORDER)
		# A multi-field update is never atomic in v0, whatever the members say.
		atomicity = "NonAtomic"
	else:
		representation = _repr(placement)
		atomicity      = _atomic(placement)

	return Vector(
		offset = f"AbsoluteStatic({render_offset(placement.offset_bits)})",
		size   = f"Fixed({render_size(placement.size_bits)})",
		align  = _align(placement),
		repr   = representation,
		atomic = atomicity,
	)


def _align(placement: Placement) -> str:
	"""Alignment relative to the message base.

	A bit-packed field is always Unaligned, whatever byte it starts in: its
	address is not a byte address, so there is no aligned access to be had. The
	uniform answer is also the honest one -- reporting `Aligned(8)` for the
	first bit of a byte and `Unaligned` for the second would suggest a
	difference that does not exist for the caller.
	"""
	scalar = placement.scalar
	if scalar is not None and scalar.is_bit_packed:
		return "Unaligned"

	alignment = alignment_of(placement.offset_bits)
	return f"Aligned({alignment})" if alignment else "Unaligned"


def _repr(placement: Placement) -> str:
	"""Whether the value is literally the bytes.

	Reported host-independently, which is what makes the map committable: the
	same schema must produce the same map on every machine, or `map --check`
	would fail on a build host that differs from a developer's.

	A multi-byte scalar with a declared byte order is therefore reported
	ValueConverted even though it happens to be MemoryIdentical on a host of
	that order. That is the conservative direction -- weaker, never stronger --
	and it is the correct value on any host that does not match.
	"""
	scalar = placement.scalar

	if scalar is None:
		return "ValueConverted"

	# Bit packing always converts: the value has to be shifted and masked out of
	# its containing byte (section 11.3).
	if scalar.is_bit_packed:
		return "ValueConverted"

	# A single byte has no byte order, so `byte`, `u8` and `i8` are the bytes
	# whatever the host does.
	if scalar.bits <= BITS_PER_BYTE:
		return "MemoryIdentical"

	if placement.endian is ast.Endian.NATIVE:
		return "MemoryIdentical"

	return "ValueConverted"


def _atomic(placement: Placement) -> str:
	"""Whether a single instruction can carry the whole access.

	Section 11.1 is categorical about bit fields: writing one is a
	read-modify-write of the containing byte, so it is never atomic. Multi-field
	updates are never atomic in v0 either, so aggregates and arrays are out.
	"""
	scalar = placement.scalar

	if scalar is None or placement.array_count is not None:
		return "NonAtomic"

	if scalar.is_bit_packed:
		return "NonAtomic"

	if scalar.bits not in ATOMIC_WIDTHS:
		return "NonAtomic"

	# A word-sized access is only single-instruction when it is naturally
	# aligned; an unaligned one faults or is split on the targets that matter.
	width_bytes = scalar.bits // BITS_PER_BYTE
	if alignment_of(placement.offset_bits) < width_bytes:
		return "NonAtomic"

	return "AtomicWord"
