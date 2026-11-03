"""Code generation backends.

C is primary (project.md section 20.1). Rust is phase 11 and must not be
started before the C backend has proven the lattice.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from situc import ast, traverse
from situc.diagnostics import not_yet_implemented
from situc.layout import BITS_PER_BYTE, Placement
from situc.resolve import ResolvedSchema, ResolvedStruct
from situc.traverse import own_members, reserved_constraint

if TYPE_CHECKING:		# a cycle at run time, a name at check time
	# `c.vectors` imports this module, so `Case` is a forward reference
	# here and the two predicates beside it are imported inside the
	# functions that need them.
	from situc.codegen.c.vectors import Case


def refuse_parameters(schema: ast.Schema) -> None:
	"""Stop a build that would silently read a parameter off the buffer.

	One function for every generator that has not learned to pass an
	argument, for `traverse.parameters`' own reason: a copy of this refusal
	per generator is a copy to reword separately.

	It covered the four backends and the packer too until 2026-09-17, and
	those six now take their arguments (26.393 to 26.397). What is left are
	the generators that emit a SECOND artifact over a schema and build calls
	of their own: each would have to thread an argument through them, and
	none is on a `situc build` path, so what a parameter costs there is
	`situc gen-checks` and its siblings declining rather than a broken
	build.

	**Four call it**, and the list is kept here because this is where
	somebody checks whether a generator is covered: the C checks, fuzz and
	tamper harnesses, and the golden-vector suite. **It said seven and
	meant three** -- naming the differ and the three `derived` emitters,
	which decline by their own means or state that they read no struct or
	member and so need none -- while `c/vectors.py`, which genuinely needed
	it, was in neither the list nor the code and emitted C nobody could
	compile (26.707). A list that over-claims is worse than none in the one
	function that exists to be the single copy of this refusal.

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


# ---------------------------------------------------------------------------
# What a golden vector says about one field, and whether a backend can say it
#
# Language-independent, and here for the reason `refuse_parameters` is above:
# the vector FILE is one file read by four emitters, so "can this expectation
# be compared" must have one answer. Putting it in `python/vectors.py` -- the
# first emitter to need it -- would have made the Rust one import the Python
# one, which is how a change to one backend breaks another.


#: An expectation's value, where it is an integer the three new targets can
#: splice. The trailing `[uUlL]*` is a C integer suffix and is stripped
#: rather than refused: `0xDEADBEEFu` is in `test/schema/header.vectors` and
#: is the same number without it. Allowing it moved the comparable share of
#: the corpus from 140 expectations to 142 -- small, and also two
#: expectations that would otherwise have gone silently uncompared.
#:
#: The leading sign is `example/ntp`'s `precision = -23`, an `i8`, and it
#: was the LAST expectation in the corpus that nothing compared -- one of
#: 197, found by asking which one remained rather than by assuming the tail
#: was empty. All four languages read `-23`.
#: The bytes a conformance check writes to ask what a field's bytes MEAN.
#:
#: Distinct, so any permutation of the field's bytes is a different value;
#: top bit set in the first, so a getter that forgot to sign-extend returns
#: something else.
#:
#: Shared because two backends now write it, and a probe pattern that
#: differed between them would make the same question two questions. The C
#: backend defined it first and this is that definition, moved.
PROBE_BYTES = (0x81, 0x23, 0x45, 0x67, 0x89, 0xAB, 0xCD, 0xEF)


INT_LITERAL = re.compile(r"(-?0[xX][0-9a-fA-F]+|-?\d+)[uUlL]*\Z")


def enum_constants(schema: ast.Schema, prefix: str = "situ") -> dict[str, int]:
	"""Every enum member's C macro name against its value.

	A vectors file may give an expectation as the C constant the C backend
	emits -- `SITU_LEAP_INDICATOR_NONE` -- which is 18 of the corpus's 197
	expectations and was the largest class no target could compare. The
	other three languages spell the member differently, so splicing the name
	is not an option and resolving it to its NUMBER is, because the schema
	knows the number.

	Built by asking `macro` for the name rather than by taking the constant
	apart: that function is what the C backend emits with, so this inverts
	the actual rule. Guessing at `SITU_` plus an underscore split would
	break on an enum or member whose own name contains one, which is most of
	them -- `leap_indicator`, `last_minute_61`.
	"""
	from situc.codegen.c.names import macro

	found: dict[str, int] = {}
	for decl in schema.enums():
		for member in decl.members:
			value = getattr(member.value, "value", None)
			if isinstance(value, int):
				found[macro(prefix, decl.name, member.name)] = value
	return found


def comparable(resolved: ResolvedSchema, case: "Case",
		path: str, value: str,
		constants: dict[str, int] | None = None,
		nested: bool = False, text: bool = False) -> str | None:
	"""The literal to compare this expectation against, or None.

	Measured over the corpus: of 197 expectations, **142 are a plain scalar
	against an integer**. The rest divide into four classes for four
	different reasons -- 22 name a field inside a nested struct, 18 give a C
	enum constant such as `SITU_LEAP_INDICATOR_NONE`, 11 are a byte run, and
	4 are a text number whose getter is fallible.

	Each of those four is its own piece of work, and what makes declining
	them safe is that the generated file says how many and why: a suite that
	compares 72 per cent of its input and does not say so reads as coverage
	of all of it.
	"""
	from situc.codegen.c.vectors import _byte_run, _nested_of

	# `nested` is the backend saying it can REACH a nested field, which is
	# not the same as the field being comparable. Python chains properties
	# -- `view.head.magic` -- so it passes True; Rust's accessor returns a
	# `Result` and C++'s takes an out-parameter, so each needs a block and
	# neither has one yet. The predicates below work on a dotted path
	# unchanged, `_byte_run` and `radix` both asking the leaf.
	if not nested and _nested_of(resolved, case.struct, path) is not None:
		return None
	if _byte_run(resolved, case.struct, path) is not None:
		return None

	# A text number's getter is fallible, and what that costs differs by
	# backend: C takes an out-parameter and returns a code, so calling it as
	# a plain getter does not compile; Python makes it the one property that
	# can RAISE, which for a golden vector's valid digits is simply the
	# number. So `text` is a backend saying it can read one, the same shape
	# as `nested` above.
	found = resolved.find(f"{case.struct}.{path}")
	if not text and found is not None and found.placement.radix is not None:
		return None

	match = INT_LITERAL.match(value)
	if match:
		return match.group(1)

	# A C enum constant, resolved to the number the schema gives it. Not
	# spliced by name: the other three languages spell the member
	# differently, and the number is the thing they agree on.
	if constants is not None and value in constants:
		return str(constants[value])
	return None


def byte_run(resolved: ResolvedSchema, case: "Case",
		path: str, value: str, nested: bool = False) -> bytes | None:
	"""The bytes this expectation names, where it names a byte run.

	11 of the corpus's 197 expectations are this shape, and they are the
	class that made `example/arp` -- twenty of its twenty-eight bytes
	addresses -- unable to state almost anything about itself before the C
	emitter grew the form (26.35).

	`_byte_run` for the count and `_hex` for the value, both the C emitter's:
	the count comes from the layout and the spelling is the vector file's,
	and both questions already have one answer. `_hex` is why
	`00:1A:2B:3C:4D:5E` and `C0 A8 01 0A` are the same kind of thing here.
	"""
	from situc.codegen.c.vectors import _byte_run, _hex, _nested_of

	# Nested first, exactly as the C emitter orders it. A byte run inside a
	# nested struct reads through that struct's own view, so the dotted path
	# is not a method name: asking the byte-run question first emitted
	# `view.header_magic()` for `cpio_entry.header.magic` and nothing
	# defines it. Caught by a count that did not match -- 13 comparisons
	# where the byte-run class measured 11 -- rather than by the compiler.
	if not nested and _nested_of(resolved, case.struct, path) is not None:
		return None

	count = _byte_run(resolved, case.struct, path)
	if count is None:
		return None

	try:
		raw = _hex(value)
	except ValueError:
		return None
	return raw if len(raw) == count else None


def declined_reason(resolved: ResolvedSchema, case: "Case",
		path: str, value: str,
		constants: dict[str, int] | None = None,
		nested: bool = False, text: bool = False) -> str:
	"""Why one expectation is not compared, in the words a reader needs.

	Four reasons rather than one count, because "37 not compared" tells a
	reader nothing about what to do and "22 are nested" tells them where the
	next piece of work is.
	"""
	from situc.codegen.c.vectors import _byte_run, _nested_of

	if not nested and _nested_of(resolved, case.struct, path) is not None:
		return "a field inside a nested struct, read through its own view"
	if _byte_run(resolved, case.struct, path) is not None:
		if byte_run(resolved, case, path, value) is None:
			return ("a byte run whose value is not that many bytes of hex, "
			        "so there is nothing to compare it against")
		return "a byte run, which compares as bytes rather than as a number"

	found = resolved.find(f"{case.struct}.{path}")
	if not text and found is not None and found.placement.radix is not None:
		return "a text number, whose getter is fallible"
	if constants is not None:
		return ("a value that is neither an integer literal nor an enum "
		        "constant this schema declares")
	return "a value that is not an integer literal, such as a C enum constant"


def tally(resolved: ResolvedSchema, cases: list["Case"],
		constants: dict[str, int] | None = None, nested: bool = False,
		text: bool = False) -> tuple[int, dict[str, int]]:
	"""How many expectations a backend compares, and why it declines the rest."""
	compared = 0
	declined: dict[str, int] = {}

	for case in cases:
		for path, value in case.expectations:
			if (comparable(resolved, case, path, value, constants,
			               nested, text) is not None
			    or byte_run(resolved, case, path, value, nested) is not None):
				compared += 1
			else:
				why = declined_reason(resolved, case, path, value, constants,
				                      nested, text)
				declined[why] = declined.get(why, 0) + 1

	return compared, declined


# -- what a constraint check must write, computed from the map -----------
#
# These six answer a question about VALUES and BYTE POSITIONS rather than
# about source text, so none of them is a backend's business. They were
# `--target c`'s, and they move here for `PROBE_BYTES`' reason: the second
# backend to need them would otherwise carry a second copy, and two copies
# of "what must this field hold for the struct to validate" is two answers
# to one question.
#
# What they are FOR is the baseline a constraint check needs -- a buffer
# satisfying every constraint before anything is broken -- without which a
# check is vacuous, a zeroed buffer already breaking `must_be_one` so the
# check would pass against a validator that did nothing.

#: The nine kinds of conformance check, named once.
#:
#: Each backend's generated file says which it emits and what each one
#: declines per member, and until this was shared the two lists had already
#: drifted: `--target cpp` called the sixth "`reserved` is enforced" and
#: `--target python` called it "`reserved` must be zero". One kind under
#: two names, which is the shape four of this day's fixes were about -- a
#: layout naming an arm's member `s.body.arm` where the placement is
#: `s.body.<reserved0>`, a tag's fixed-width flag read in one reader and
#: not the other, a struct's minimum read by one walker and not its pair.
#: A list is the cheapest instance of it and the easiest to prevent.
CHECK_KINDS = (
	"refuses a short buffer",
	"a field occupies its claimed bytes",
	"a field round-trips in place",
	"a field decodes a known encoding",
	"a nested member starts where the map says",
	"`reserved` must be zero",
	"`must_eq` is enforced",
	"an enum rejects a value no member names",
	"a framing reader reassembles across every boundary",
)


def passes_unknown(schema: ast.Schema, name: str | None) -> bool:
	"""Whether this enum admits values no member names (section 8.7).

	`default = pass` is the escape hatch the section offers deliberately:
	IANA keeps assigning numbers, and a receiver refusing tomorrow's would
	refuse tomorrow's traffic. A check that demanded a rejection regardless
	of the policy failed a schema for using it.

	Shared for the reason the six below are: the second backend to ask
	would otherwise carry a second copy of one question.
	"""
	for decl in schema.enums():
		if decl.name == name:
			return decl.effective_default is ast.EnumDefault.PASS
	return False


def placed_bytes(offset_bits: int, bits: int, raw: int, endian: ast.Endian | None,
		bit_order: ast.BitOrder | None, packed: bool) -> dict[int, int] | None:
	"""Where `raw` lands in a zeroed buffer, from the map rather than the accessor.

	This is the whole point of the check: the expected bytes are derived here,
	from the offset and the declared order, by code that shares nothing with the
	emitter. An accessor and a check that agree because they were generated from
	the same expression would agree just as readily about a wrong answer.

	`None` when the encoding is not a fixed pattern -- `native` order depends on
	the machine that eventually runs the code, which is not knowable here.
	"""
	if endian is ast.Endian.NATIVE:
		return None

	if not packed:
		if offset_bits % BITS_PER_BYTE != 0 or bits % BITS_PER_BYTE != 0:
			return None
		first  = offset_bits // BITS_PER_BYTE
		count  = bits // BITS_PER_BYTE
		chunks = [(raw >> (BITS_PER_BYTE * i)) & 0xFF for i in range(count)]
		if endian is not ast.Endian.LITTLE:
			chunks.reverse()	# big endian: most significant byte lowest
		return {first + i: chunk for i, chunk in enumerate(chunks)}

	# Bit-packed: assemble the whole bytes the field touches, then place the
	# value within them the way the declared bit order numbers bits.
	first = offset_bits // BITS_PER_BYTE
	last  = (offset_bits + bits - 1) // BITS_PER_BYTE
	span  = (last - first + 1) * BITS_PER_BYTE
	skip  = offset_bits - first * BITS_PER_BYTE

	if bit_order is ast.BitOrder.LSB_FIRST:
		acc   = raw << skip
		order = range(first, last + 1)			# earlier byte, lower bits
	else:
		acc   = raw << (span - skip - bits)
		order = range(last, first - 1, -1)		# earlier byte, higher bits

	placed = {}
	for at in order:
		placed[at] = acc & 0xFF
		acc >>= BITS_PER_BYTE
	return placed


def required_run(placement: Placement, every: bool = False):	# type: ignore[no-untyped-def]
	"""The bytes a pinned byte RUN must hold, or None where it pins none.

	Three spellings reach one fact, and each of them is an array:
	`[must_eq = "WOZ2"]` on a `u8[4]`, a `preamble` (0052), and an enum
	whose members are byte runs. `required_pattern` next door answers for
	a scalar and cannot answer for these -- a run has no single integer to
	fold to, which is why the two are separate functions rather than one
	with a branch.

	The FIRST of several, where the schema admits several: `signature` is
	`BM` or `MZ` and either validates, so a baseline needs one that does
	rather than a rule about which.
	"""
	found: tuple[bytes, ...] = ()
	if placement.pinned_runs:
		found = placement.pinned_runs
	else:
		for attr in placement.attrs:
			if attr.name != "must_eq":
				continue
			held = getattr(attr.value, "value", None)
			if isinstance(held, str):
				found = (held.encode("latin-1"),)
			break

	if every:
		return found
	return found[0] if found else None


def required_pattern(resolved: ResolvedSchema, struct: ResolvedStruct,
		placement: Placement) -> int | None:
	"""The value this member must hold for the struct to validate, if any."""
	policy = reserved_policy(placement)
	if policy == "must_be_one":
		return (1 << placement.size_bits) - 1
	if policy == "must_be_zero":
		return 0

	# `must_eq` pins the value; `min` is the smallest that satisfies it, and
	# satisfies any `max` alongside it too. A field with only `max` is happy at
	# zero, which is where the buffer starts.
	from situc.expr import evaluate

	for name in ("must_eq", "min"):
		for attr in placement.attrs:
			if attr.name != name or attr.value is None:
				continue
			try:
				return int(evaluate(attr.value, resolved.layout.env))
			except Exception:	# noqa: BLE001 - not foldable, so not satisfiable
				return None

	# An enum with `default = error` admits its members and nothing else, so a
	# baseline that leaves one at zero is not valid unless zero is a member.
	values = resolved.layout.env.enums.get(placement.type_name or "")
	if values:
		return None if 0 in values.values() else min(values.values())
	return None


def selected_arms(resolved: ResolvedSchema, struct: ResolvedStruct,
		chosen: str | None) -> set[str]:
	"""Which arms the message a baseline builds actually carries.

	One per variant, and never more: `chosen` where the check being built
	selects an arm of that variant itself, and otherwise whichever arm the
	discriminant's own baseline value happens to name. The second half is
	the part that is easy to forget -- a check about a member *outside*
	every variant still leaves the discriminant somewhere, and the arm it
	lands on has constraints of its own that the baseline has to satisfy or
	`validate` refuses the buffer the check calls valid.
	"""
	held: set[str] = set()

	for variant in own_members(struct):
		if variant.kind != "variant" or variant.discriminant is None:
			continue

		arms = {arm.value: arm.member for arm in variant.arm_cases
		        if arm.value is not None and arm.member is not None}
		if chosen is not None and chosen in arms.values():
			held.add(chosen)
			continue

		disc = next((entry.placement for entry in struct.entries
		             if entry.placement.name == variant.discriminant), None)
		if disc is None:
			continue

		landed = arms.get(required_pattern(resolved, struct, disc) or 0)
		if landed is not None:
			held.add(landed)

	return held


def digit_pattern(resolved: ResolvedSchema, struct: ResolvedStruct,
		placement: Placement) -> bytes | None:
	"""The ASCII a text number must hold for the struct to validate.

	`None` for anything that is not a fixed-width text number. For one that
	is, the smallest value the field admits, spelled in its own radix and
	zero-padded to its own width -- which is what `[minimal]` would demand
	anyway and what every real header writes.

	Separate from `required_pattern` because that answers with a NUMBER,
	and a text number's constraint is on its bytes: 70701 as an integer put
	into six bytes is not `"070701"`, and the baseline has to write what a
	reader will parse.
	"""
	if placement.radix is None or placement.offset_bits is None:
		return None
	if placement.offset_bits % BITS_PER_BYTE or placement.size_bits % BITS_PER_BYTE:
		return None
	if placement.delimiters:
		return None		# its width is the scan's, not the schema's

	width = placement.array_count or placement.size_bits // BITS_PER_BYTE
	if width <= 0:
		return None

	least = required_pattern(resolved, struct, placement) or 0
	# The digits in the field's own base: `X` for hex, which the runtime
	# writes upper, and `o` for octal (26.756).
	spelled = format(least, f"0{width}"
	                 + {10: "d", 16: "X", 8: "o"}[placement.radix or 10])
	if len(spelled) != width:
		return None		# the smallest admitted value does not fit its field

	return spelled.encode("ascii")


def reserved_policy(placement: Placement) -> str | None:
	"""`must_be_zero` (the default) or `must_be_one`, for a reserved field.

	The policy itself is `traverse.reserved_constraint`, which three other
	backends also read; what stays here is the question this builder asks
	of a NON-reserved member, which is not a constraint either (26.658).
	"""
	if placement.kind != "reserved":
		return None
	return reserved_constraint(placement.attrs)
