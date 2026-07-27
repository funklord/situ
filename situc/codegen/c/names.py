"""C identifier construction, and the one hazard it carries.

A schema path is hierarchical and a C identifier is not, so every generated
name is a path flattened with underscores. That flattening is not injective:
`A.b_c` and `A_b.c` reach the same identifier, and so do an enum named `a_b`
and the sealed region `b` of a struct `a`. Left alone, the first sign of it is
the C compiler rejecting generated code, with a diagnostic that names a
function nobody wrote and no source location in the schema at all.

So the flattening is checked here, before anything is emitted. Nothing about
this is specific to how a schema spells its names: the check fires on two
constructs that collide, whatever convention either of them follows.
"""

from __future__ import annotations

from dataclasses import dataclass

from situc.diagnostics import Diagnostic, Label, Severity, SituError, Span
from situc.resolve import ResolvedSchema


def ident(*parts: str) -> str:
	"""Join name fragments into a C identifier.

	Every part goes through `c_name` on the way, so a caller cannot hand a
	namespace separator or a dotted path to the emitter and have it reach the
	output verbatim. That has to happen here rather than at the call sites:
	there are dozens of those, and one that forgot would emit a header no
	compiler accepts.
	"""
	return "_".join(c_name(part) for part in parts if part)


def macro(*parts: str) -> str:
	return ident(*parts).upper()


def c_name(path: str) -> str:
	"""A path rendered as a C identifier fragment.

	Namespace separators and nested paths both flatten to underscores, and the
	synthesised names of reserved regions are dropped: they have no accessor.
	"""
	return path.replace("::", "_").replace(".", "_").replace("[]", "")


@dataclass(frozen=True)
class Entity:
	"""One schema construct and the identifier stem it generates.

	The stem, not the full name: a field yields `_get`, `_set`, `_ptr` and more
	from one stem, so two constructs sharing a stem collide in every accessor
	derived from it. Checking stems needs no list of the suffixes in use, which
	is what keeps this from going stale when a phase adds another one.
	"""

	stem: str
	described: str
	span: Span


def entities(resolved: ResolvedSchema, prefix: str,
		declarations: list[tuple[str, str, Span]]) -> list[Entity]:
	"""Every construct that contributes a name, with where it was declared.

	Type declarations arrive from the schema rather than from the resolved
	layout, because a layout carries no span of its own that a diagnostic could
	point at.
	"""
	found = [Entity(ident(prefix, c_name(name)), f"{kind} `{name}`", span)
	         for kind, name, span in declarations]

	for name, struct in resolved.structs.items():
		for entry in struct.entries:
			placement = entry.placement
			# An element entry describes every element of an array at once and
			# generates nothing of its own, and a reserved region is validated
			# rather than exposed.
			if placement.kind in ("element", "reserved"):
				continue

			local = placement.path[len(name) + 1 :]
			found.append(Entity(
				ident(prefix, c_name(name), c_name(local)),
				f"`{placement.path}`",
				placement.span))

	return found


def check_collisions(resolved: ResolvedSchema, prefix: str,
		declarations: list[tuple[str, str, Span]]) -> list[Diagnostic]:
	"""Refuse a schema whose names cannot be told apart in C.

	Raises on a genuine collision, because the alternative is generated code
	that does not compile. Returns warnings for names that survive as functions
	but meet in the macro namespace, which is uppercased: those are legal, and
	they are worth saying out loud rather than enforcing, since which
	convention a schema uses is the author's business (section 25).
	"""
	found    = entities(resolved, prefix, declarations)
	warnings = []

	by_stem: dict[str, Entity] = {}
	for entity in found:
		previous = by_stem.get(entity.stem)
		if previous is not None:
			raise _collision(previous, entity)
		by_stem[entity.stem] = entity

	folded: dict[str, Entity] = {}
	for entity in found:
		previous = folded.get(entity.stem.upper())
		if previous is not None:
			warnings.append(_macro_collision(previous, entity))
		folded[entity.stem.upper()] = entity

	return warnings


def _collision(first: Entity, second: Entity) -> SituError:
	return SituError(Diagnostic(
		severity = Severity.ERROR,
		message  = f"{second.described} and {first.described} generate the same "
		           "C identifier",
		primary  = Label(second.span, f"generates `{second.stem}`"),
		labels   = [Label(first.span, "and so does this")],
		notes    = [
			f"a path flattens to underscores, so both reach `{second.stem}` and "
			"every accessor built from it",
			"rename either one, or put them in separate namespaces",
		],
	))


def _macro_collision(first: Entity, second: Entity) -> Diagnostic:
	return Diagnostic(
		severity = Severity.WARNING,
		message  = f"{second.described} and {first.described} differ only in case",
		primary  = Label(second.span, f"generates `{second.stem}`"),
		labels   = [Label(first.span, f"against `{first.stem}`")],
		notes    = [
			"their accessors are distinct, but macro names are uppercased, so "
			f"any macro derived from either reaches `{second.stem.upper()}`",
			"a size constant, an array count or a tag dirty bit would collide; "
			"the accessors would not",
		],
	)
