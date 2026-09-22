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


def sized_shown(placement: Placement) -> str:
	"""How a reader should see what sizes this member.

	`placement.sized_by` holds a PATH and holds nothing for a member sized
	by arithmetic -- `size_expr` holds that -- so three backends printed
	`sized by `None`` into shipped source for every such member. The Lua
	dissector met the same bug and was fixed; `dissector.py` has carried the
	working spelling ever since and the other three never received it.

	Order matters and is the dissector's: the reader's form first, the
	compiler's parenthesised form second, the bare path last. A member with
	none of the three is sized by nothing a comment can name, and `?` says
	that rather than naming a Python object.
	"""
	return (placement.size_shown or placement.size_expr
	        or placement.sized_by or "?")


def arm_label(arm: object) -> str:
	"""What a variant arm selects, as a reader should see it.

	`arm.source or arm.value` reads like the whole answer and is not: a
	`default:` arm has NEITHER, so three backends printed the Python object
	into shipped source --

	    cpp/hpp:  present when the discriminant selects `None`.
	    c/h:      present when the discriminant selects `default`.

	-- from the same member of `example/json/json.situ`, which is committed.
	C was right because it tests `arm.value is None` on its way to building
	the guard and has the word to hand; the other three had only the
	expression, and an `or` chain cannot distinguish "no source spelling"
	from "no value either".

	Here rather than four times: what an arm selects is one fact about the
	schema, and 0017 asks a second backend to re-spell rather than
	re-derive. A `0` value is why this is not an `or` chain either.
	"""
	source = getattr(arm, "source", None)
	if source is not None:
		return str(source)
	value = getattr(arm, "value", None)
	return "default" if value is None else str(value)


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
