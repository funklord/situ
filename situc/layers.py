"""The layer ladder, and which rung a schema needs (decision 0032).

Six rungs, and the two scalars the capability map carries: the *floor* is the
lowest rung that can emit a schema at all, the *reach* the highest it has
content for. Both are properties of constructs rather than of an invocation,
which is what lets one committed map serve every rung.

This module exists because three callers wanted the same answer -- `capmap`
prints it, the CLI validates against it, and `require no_alloc(X)` is
discharged from it -- and the first of them had it privately. A second copy
of "which constructs need storage" is exactly the drift `relation.py` and
`traverse.py` were each created to stop.
"""

from __future__ import annotations

from situc import ast
from situc.resolve import ResolvedSchema

#: Low rung first. `--layer` is a choice over this list and the order is the
#: ladder, so a rung emits everything the rungs before it emit.
LAYERS = ("view", "edit", "relate", "frame", "converse", "drive")


def unbounded_codecs(schema: ast.Schema) -> set[str]:
	"""Codecs whose output extent cannot be known without transforming.

	Case E of `doc/decision/0031-where-allocation-is-unavoidable.md`, and
	the only one of the five that leaves no choice: there is no
	measure-then-allocate pass, because the measure pass *is* the work.
	"""
	return {codec.name for codec in schema.codecs()
	        if codec.expansion is ast.Expansion.UNBOUNDED}


def _walk(members: tuple[ast.Member, ...]) -> list[ast.Member]:
	"""Every member, and everything nested under it.

	Six `ast.Member` subclasses hold nested members. Five spell the field
	`members` and **`Variant` alone spells it `arms`**, so a `getattr` keyed
	on the common name reaches five of the six -- the parser's
	`_widen_byte_enum_fields` met the same fact and says so at length.

	Here it decided `floor()`. A region with an unbounded codec inside a
	variant ARM was invisible, so `allocating` returned nothing and the
	schema was reported emittable at rung 1:

	    member form   allocating=['one.body']   floor=edit
	    arm form      allocating=[]             floor=view

	That is a wrong answer rather than a missing check -- `no_alloc(X)` is
	one of the four predicates section 16 has the compiler name (26.434).
	"""
	found: list[ast.Member] = []
	for member in members:
		found.append(member)
		found.extend(_walk(getattr(member, "members", ())))
		found.extend(_walk(tuple(
			arm.member for arm in getattr(member, "arms", ())
			if arm.member is not None)))
	return found


def allocating(schema: ast.Schema) -> set[str]:
	"""`struct.member` for every region that cannot be emitted at rung 1.

	A path rather than a bare name, because two structs may each have a
	region called `body` and only one of them may need storage.
	"""
	unbounded = unbounded_codecs(schema)
	if not unbounded:
		return set()

	found: set[str] = set()
	for struct in schema.structs():
		for member in _walk(struct.members):
			if (isinstance(member, (ast.Coded, ast.Sealed))
					and member.codec in unbounded):
				name = getattr(member, "name", None) or member.codec
				found.add(f"{struct.name}.{name}")
	return found


def floor(schema: ast.Schema) -> str:
	"""The lowest rung that can emit this schema."""
	return "edit" if allocating(schema) else "view"


def reach(schema: ast.Schema) -> str:
	"""The highest rung this schema has content for."""
	if any(relation.attrs for relation in schema.relations()):
		return "drive"
	return "relate" if schema.relations() else "view"


def allocates(schema: ast.Schema, resolved: ResolvedSchema, path: str) -> bool:
	"""Whether anything at or under `path` needs storage rung 1 cannot give.

	This is what makes `no_alloc(X)` a question rather than a tautology.
	Section 16 recorded it as one of four predicates the compiler names and
	cannot decide, on the grounds that generated code never allocates so it
	always holds -- true until the ladder gave `--layer edit` somewhere for
	the answer to be no.
	"""
	needy = allocating(schema)
	if not needy:
		return False

	if path in needy:
		return True
	# A struct is asked about by name, and a member by `struct.member`, so a
	# prefix match answers both without the caller saying which it meant.
	return any(one == path or one.startswith(f"{path}.") for one in needy)
