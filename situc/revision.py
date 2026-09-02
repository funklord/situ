"""Capability changes between two schema revisions (project.md section 18.3).

Intended for code review and CI, which is what shapes it. A textual diff of two
schemas says which lines moved; this says what moved cost. A field that quietly
went from `InPlaceFixed` to `Shifting` is a line of schema and a fleet-wide
performance change, and the point of running this in CI is that the second fact
arrives at the same time as the first.

Regressions and improvements are both reported, and only regressions set the
exit status. A revision that gains a capability is worth seeing and is not worth
failing a build over.
"""

from __future__ import annotations

from dataclasses import dataclass

from situc.capability import (NUMERIC_STRENGTH, Axis, Value, is_at_least,
                             rank)
from situc.layout import BITS_PER_BYTE
from situc.resolve import ResolvedSchema


@dataclass(frozen=True)
class Change:
	"""One path, one axis, and which way it went."""

	path: str
	axis: Axis | None
	before: Value | None
	after: Value | None
	kind: str		# "weakened", "strengthened", "added", "removed"

	@property
	def is_regression(self) -> bool:
		"""A lost capability, or a path that stopped existing.

		A removed field is a regression whatever it was: every caller of its
		accessor stops compiling, which is a harder break than any axis moving.
		"""
		return self.kind in ("weakened", "removed")

	def render(self) -> str:
		if self.kind == "added":
			return f"  + {self.path}"
		if self.kind == "removed":
			return f"  - {self.path}"

		assert self.axis is not None and self.before is not None
		assert self.after is not None
		# Always old on the left. A direction marker that reversed the reading
		# order would make every line need parsing twice.
		mark = {"weakened": "!", "strengthened": "+", "moved": "~"}[self.kind]
		return (f"  {mark} {self.path}: {self.axis.value} "
		        f"{self.before.render()} -> {self.after.render()}")


def compare(old: ResolvedSchema, new: ResolvedSchema) -> list[Change]:
	"""Every capability difference, weakenings first.

	Paths are matched by name. Situ has no field numbers -- position carries
	identity and the name is the identity (section 4) -- so a renamed field is
	a removal and an addition, which is the truth: nothing that called the old
	accessor calls the new one.
	"""
	before = {entry.placement.path: entry
	          for struct in old.structs.values() for entry in struct.entries}
	after  = {entry.placement.path: entry
	          for struct in new.structs.values() for entry in struct.entries}

	changes: list[Change] = []

	for path in sorted(set(before) - set(after)):
		changes.append(Change(path, None, None, None, "removed"))

	for path in sorted(set(after) - set(before)):
		changes.append(Change(path, None, None, None, "added"))

	for path in sorted(set(before) & set(after)):
		for axis in Axis:
			was = before[path].vector.get(axis)
			now = after[path].vector.get(axis)
			if was == now:
				continue

			# Same base, different parameters -- an offset that moved, a bound
			# that changed -- is a change in the layout rather than in the
			# capability. Reported, but as neither direction.
			if rank(axis, now) > rank(axis, was):
				kind = "weakened"
			elif rank(axis, now) < rank(axis, was):
				kind = "strengthened"
			else:
				kind = _parameter_change(axis, was, now)

			changes.append(Change(path, axis, was, now, kind))

	return sorted(changes, key=_ordering)


def _parameter_change(axis: Axis, was: Value, now: Value) -> str:
	"""A bound that grew is a regression; one that shrank is not.

	`Bounded(0, 1500)` becoming `Bounded(0, 4096)` keeps the axis where it was
	and costs every caller 2.5 KB of buffer, which is the kind of change this
	tool exists to surface.

	**Three outcomes, not two.** It used to be `weakened if worse else
	strengthened`, so *not measurably worse* was reported as *better*:
	`Bounded(2, 10)` becoming `Bounded(4, 10)` doubles the minimum extent,
	leaves the maximum where it was, and was printed under "Improvements".
	Neither value is stronger there -- `is_at_least` answers True in both
	directions -- and "moved" is the answer the offset case already gives
	(26.187).

	**An axis whose parameters carry strength is compared on them.** `align`
	is in `NUMERIC_STRENGTH` and `capability.py` says why: "`Aligned(8)` is
	stronger than `Aligned(2)`". Falling through to "moved" reported a real
	weakening as neither direction, and `situc build` refuses the very same
	edit when a `require aligned(...)` names it -- so the tool that exists to
	catch a regression in review called it a layout change and exited 0.
	"""
	if axis in NUMERIC_STRENGTH and was.params and now.params:
		if is_at_least(axis, now, was) and not is_at_least(axis, was, now):
			return "strengthened"
		if is_at_least(axis, was, now) and not is_at_least(axis, now, was):
			return "weakened"
		return "moved"

	if axis is Axis.SIZE and was.params and now.params:
		worse, better = _worst(now), _worst(was)
		if worse > better:
			return "weakened"
		if worse < better:
			return "strengthened"
		return "moved"		# the worst case did not move

	return "moved"


def _worst(value: Value) -> int:
	"""The largest extent this value admits, in **bits**.

	Bits because that is the unit both spellings share: `render_size` writes
	a whole number of bytes as `2` and anything else as `12bit`, and
	`"12bit".isdigit()` is False -- so both sides of a sub-byte change used to
	collapse to 0, `0 > 0` was False, and a field that grew from four bits to
	six was reported as an improvement. The same edit one byte wide is
	reported as a regression, which is the tell: the verdict turned on
	whether the size divided by eight.
	"""
	tail = value.params[-1]
	if tail.endswith("bit"):
		head = tail[:-3]
		return int(head) if head.isdigit() else 0
	return int(tail) * BITS_PER_BYTE if tail.isdigit() else 0


def _ordering(change: Change) -> tuple[int, str, str]:
	order = {"removed": 0, "weakened": 1, "moved": 2, "strengthened": 3, "added": 4}
	return (order[change.kind], change.path, change.axis.value if change.axis else "")


def render(changes: list[Change]) -> str:
	if not changes:
		return "No capability change.\n"

	regressions = [change for change in changes if change.is_regression]
	gains       = [change for change in changes if change.kind == "strengthened"]

	lines: list[str] = []
	for title, kind in (("Regressions", None), ("Layout changes", "moved"),
	                    ("Improvements", "strengthened"), ("Added", "added")):
		if kind is None:
			group = [change for change in changes if change.is_regression]
		else:
			group = [change for change in changes if change.kind == kind]
		if not group:
			continue
		if lines:
			lines.append("")
		lines.append(f"{title}:")
		lines.extend(change.render() for change in group)

	lines.append("")
	lines.append(f"{len(regressions)} regression(s), {len(gains)} improvement(s).")
	return "\n".join(lines) + "\n"


def to_dict(change: Change) -> dict[str, object]:
	return {
		"path":   change.path,
		"axis":   change.axis.value if change.axis else None,
		"before": change.before.render() if change.before else None,
		"after":  change.after.render() if change.after else None,
		"kind":   change.kind,
		"regression": change.is_regression,
	}
