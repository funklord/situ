"""Code generation backends.

C is primary (project.md section 20.1). Rust is phase 11 and must not be
started before the C backend has proven the lattice.
"""

from __future__ import annotations

from situc import ast, traverse
from situc.diagnostics import not_yet_implemented
from situc.layout import Placement
from situc.resolve import ResolvedSchema, ResolvedStruct
from situc.traverse import own_members


def refuse_parameters(schema: ast.Schema) -> None:
	"""Stop a build that would silently read a parameter off the buffer.

	One function for every generator that has not learned to pass an
	argument, for `traverse.parameters`' own reason: a copy of this refusal
	per generator is a copy to reword separately.

	It covered the four backends and the packer too until 2026-09-17, and
	those six now take their arguments (26.393 to 26.397). What is left are
	the generators that emit a SECOND artifact over a schema -- the differ,
	the C checks, fuzz and tamper harnesses, and the three `derived`
	emitters. Each builds calls of its own and would have to thread an
	argument through them; none is on a `situc build` path, so what a
	parameter costs there is `situc gen-checks` and its siblings declining,
	not a broken build.

	A whole-schema refusal rather than a note beside the parameter, because
	a note leaves every expression that READS it still emitting that read:
	`_over_fields` reads a member where it sits, and a parameter sits at the
	offset of the member after it. A size expression naming one compiles
	cleanly and measures the wrong byte, which is the shape 26.32 rates
	worst.
	"""
	held = traverse.parameters(schema)
	if not held:
		return

	struct, member = held[0]
	raise not_yet_implemented(
		f"`parameter {member.name}` in `{struct}`", member.span, 12,
		[
			"a parameter is an argument the caller supplies, and this "
			"generator does not pass one -- so a read of it would come "
			"off the buffer at its offset, which is where the member "
			"after it begins (decision 0050)",
			"the four `situc build` backends DO take their arguments, so "
			"a view for this schema is available; what declines here is "
			"the second artifact this command emits, which builds calls "
			"of its own",
		])


def arguments(struct: ResolvedStruct) -> list[Placement]:
	"""The `parameter`s a struct takes, in declaration order (0050).

	Here rather than in each layer generator for `traverse.parameters`' own
	reason: seven files ask it, and seven copies of "which members are
	arguments" is seven things to be wrong. Scalars by construction --
	`wellformed.check_parameters` refuses a struct-typed one -- so a caller
	may ask for the member's width without a second guard for a case the
	front end has already closed.
	"""
	return [held for held in own_members(struct)
	        if held.parameter and held.scalar is not None]


def takes_arguments(struct: ResolvedStruct) -> bool:
	"""Whether a view of this struct cannot be built without being told
	something the message does not carry."""
	return bool(arguments(struct))


def argued_sides(relation: ast.Relation,
		resolved: ResolvedSchema) -> list[tuple[str, str]]:
	"""`(side, struct)` for each of a relation's two views whose struct takes
	an argument (0050).

	What rungs 3, 5 and 6 ask before emitting anything for a relation. They
	are not handed a message to acquire a view of -- the caller holds the
	views, or in C the raw `situ_view_t` -- so unlike `edit` and `frame`
	there is no call of theirs a value could arrive on without a second
	design deciding whose argument is whose.
	"""
	found = []
	for param in relation.params:
		struct = resolved.find_struct(param.type_name)
		if struct is not None and takes_arguments(struct):
			found.append((param.name, param.type_name))
	return found


#: Why rung 3 and rung 5 decline: they never acquire a view at all.
HANDED_VIEWS = ("this rung is handed views rather than the bytes to acquire "
                "them from, so there is no call of its own the caller's "
                "argument could arrive on")

#: Why a rung-6 driver declines. It DOES acquire one -- of every datagram it
#: receives -- which is the opposite half of the same gap, and a message that
#: said "handed views" here would be describing another rung's problem.
ACQUIRES_DATAGRAMS = ("this driver acquires a view of every datagram it "
                      "receives with nothing to tell it the argument that "
                      "view is built under")


def argued_refusal(relation: ast.Relation, resolved: ResolvedSchema,
		because: str = HANDED_VIEWS) -> str:
	"""Why this relation gets nothing at rungs 3, 5 and 6, in the sentence
	shape those rungs already print beside a key that does not fit.

	`because` is the rung's own half, because the two rungs decline for
	opposite reasons and one sentence for both would be wrong somewhere: a
	predicate never acquires a view and a driver acquires one per datagram.
	Empty where the relation takes no argued side, so a caller may use this
	as the test as well as the wording.
	"""
	sides = argued_sides(relation, resolved)
	if not sides:
		return ""
	names = ", ".join(f"`{side}: {struct}`" for side, struct in sides)
	verb  = "takes" if len(sides) == 1 else "take"
	return (f"{names} {verb} a `parameter`, and {because} (decision 0050). "
	        f"The view rung takes it: build the view with the argument and "
	        f"compare, match or drive by hand")
