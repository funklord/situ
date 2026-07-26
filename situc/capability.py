"""The capability lattice: axes, values, weakening order and meet.

This is the core of the compiler (project.md section 11). Every other pass
exists to feed it or to report its results, and where a design decision
elsewhere conflicts with keeping it sound and decidable, the lattice wins.

The axes are independent. Each is a lattice with a defined weakening order, and
the vector is a product lattice: incomparable vectors exist and that is fine.
The compiler never needs a total order, only meet.

**Nothing strengthens a capability.** Every construct either leaves an axis
alone or weakens it. If an implementation seems to need the other direction,
the axis definition is wrong -- stop and ask.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Axis(Enum):
	SIZE		= "size"
	OFFSET		= "offset"
	ACCESS		= "access"
	MUTATE		= "mutate"
	ADDRESS		= "address"
	ALIGN		= "align"
	REPR		= "repr"
	ATOMIC		= "atomic"
	CANONICAL	= "canonical"
	STAGE		= "stage"
	AUTH		= "auth"
	SECRECY		= "secrecy"
	EFFECT		= "effect"


# Domains, strongest first, exactly as section 11.1 orders them. This is the
# normative table; the code below reads it rather than restating it.
#
# `stage` is the one axis that increases rather than weakens, so it is listed
# in the direction of less usable and treated uniformly with the rest.
#
# `auth` is not truly ordered -- it is a set-valued tag identity -- but meet
# needs some order, and Covered is the more constrained of the two: mutating
# covered bytes marks a tag dirty. Phase 8 owns the real treatment.
DOMAINS: dict[Axis, tuple[str, ...]] = {
	Axis.SIZE:	("Fixed", "Bounded", "Unbounded"),
	Axis.OFFSET:	("AbsoluteStatic", "FrameStatic", "Dynamic"),
	Axis.ACCESS:	("Random", "Sequential"),
	Axis.MUTATE:	("InPlaceFixed", "InPlaceSlack", "Shifting",
			 "RewriteRequired", "Immutable"),
	Axis.ADDRESS:	("Stable", "FrameStable", "Unstable"),
	Axis.ALIGN:	("Aligned", "Unaligned"),
	Axis.REPR:	("MemoryIdentical", "ValueConverted", "ConditionallyConverted"),
	Axis.ATOMIC:	("AtomicWord", "NonAtomic"),
	Axis.CANONICAL:	("Canonical", "CanonicalGiven", "NonCanonical"),
	Axis.STAGE:	("CompileTime", "ParseTime", "TransformTime", "VerifyGated"),
	Axis.AUTH:	("Uncovered", "Covered"),
	Axis.SECRECY:	("Public", "Secret"),
	Axis.EFFECT:	("Pure", "EffectOnRead", "EffectOnWrite", "EffectBoth"),
}

# Axes whose parameter carries strength rather than identity. `Aligned(8)` is
# stronger than `Aligned(2)`; `AbsoluteStatic(0x04)` is not stronger or weaker
# than `AbsoluteStatic(0x08)`, it is simply a different offset.
NUMERIC_STRENGTH = frozenset({Axis.ALIGN})


@dataclass(frozen=True)
class Value:
	"""One axis value, with any parameters it carries."""

	base: str
	params: tuple[str, ...] = ()

	def render(self) -> str:
		if not self.params:
			return self.base
		return f"{self.base}({', '.join(self.params)})"

	def __str__(self) -> str:
		return self.render()


def rank(axis: Axis, value: Value) -> int:
	"""Position in the weakening order; larger is weaker."""
	domain = DOMAINS[axis]
	if value.base not in domain:
		raise ValueError(f"`{value.base}` is not a value of axis `{axis.value}`")
	return domain.index(value.base)


def is_at_least(axis: Axis, actual: Value, required: Value) -> bool:
	"""Whether `actual` is at least as strong as `required` on one axis."""
	actual_rank   = rank(axis, actual)
	required_rank = rank(axis, required)

	if actual_rank != required_rank:
		return actual_rank < required_rank

	if axis in NUMERIC_STRENGTH and actual.params and required.params:
		return int(actual.params[0]) >= int(required.params[0])

	return True


def meet_values(axis: Axis, left: Value, right: Value) -> Value:
	"""The weaker of two values on one axis.

	Meet is the worst case, never the best. Where both sides sit at the same
	base but carry a strength-bearing parameter, the smaller parameter wins.
	"""
	if rank(axis, left) != rank(axis, right):
		return left if rank(axis, left) > rank(axis, right) else right

	if axis in NUMERIC_STRENGTH and left.params and right.params:
		return left if int(left.params[0]) <= int(right.params[0]) else right

	return left


@dataclass(frozen=True)
class Vector:
	"""One field's capability vector.

	Axes absent from `values` are at their strongest, which is the identity
	element: a fixed-size, byte-aligned, host-order scalar weakens nothing.
	"""

	values: tuple[tuple[Axis, Value], ...] = ()

	def get(self, axis: Axis) -> Value:
		for held, value in self.values:
			if held is axis:
				return value
		return Value(DOMAINS[axis][0])

	def with_value(self, axis: Axis, value: Value) -> Vector:
		"""Set an axis, refusing to strengthen it.

		The refusal is an assertion rather than a diagnostic because it is an
		implementation error, not a schema error: invariant 2 of section 26 says
		no construct may strengthen a capability.
		"""
		current = self.get(axis)
		if rank(axis, value) < rank(axis, current):
			raise ValueError(
				f"cannot strengthen {axis.value} from {current} to {value}; "
				"no construct may strengthen a capability (project.md section 26)")

		kept = tuple((held, held_value) for held, held_value in self.values
		             if held is not axis)
		return Vector(kept + ((axis, value),))

	def meet(self, other: Vector) -> Vector:
		"""Pointwise worst case. A struct's vector is the meet of its members'."""
		result = Vector()
		for axis in Axis:
			result = result.with_value(
				axis, meet_values(axis, self.get(axis), other.get(axis)))
		return result

	def is_at_least(self, other: Vector) -> bool:
		return all(is_at_least(axis, self.get(axis), other.get(axis)) for axis in Axis)

	def items(self) -> list[tuple[Axis, Value]]:
		return [(axis, self.get(axis)) for axis in Axis]


def strongest() -> Vector:
	"""The identity vector: every axis at its strongest value."""
	return Vector()


def meet_all(vectors: list[Vector]) -> Vector:
	result = strongest()
	for vector in vectors:
		result = result.meet(vector)
	return result
