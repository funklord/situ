"""What a relation means, once, for every backend (26.95, decision 0030).

A relation is a pure predicate over two views. Resolving one -- walking
`response.hdr.msg` down to the getter that reads it, deciding whether the
comparison has a correct spelling at all -- is the *language*, not a property
of any target. So the walk lives here and the spelling does not, which is the
shape `invariant.py` already uses and for the same reason: four copies of a
recursive descent over six node types is what `traverse.py` exists to prevent.

**The refusals are shared, deliberately.** Python's integers are arbitrary
precision and would happily compare a `u64` against an `i8`; C, C++ and Rust
cannot, because no 64-bit type holds both ranges. Letting Python accept what
the other three refuse would make a schema mean one thing in one backend and
another elsewhere -- the exact failure the four-way agreement tests exist to
catch. A relation is therefore refused everywhere or nowhere, and the reason
is decided here.

What a backend supplies is how to spell a sub-view acquisition, a read and a
comparison. What it never decides is whether the relation is expressible.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from situc import ast
from situc.layout import Placement
from situc.resolve import ResolvedSchema, ResolvedStruct
from situc.traverse import is_own_member, local_name
from situc.types import ScalarKind

#: Operators a relation body may use.
#:
#: A closed set on purpose. An operator that reached four backends unchecked
#: because they happen to spell it alike is a silent difference waiting for
#: the first language that does not.
OPERATORS = frozenset({
	"==", "!=", "<", "<=", ">", ">=",
	"+", "-", "*", "/", "%",
	"&", "|", "^", "<<", ">>",
	"&&", "||",
})

UNARY = frozenset({"-", "~", "!"})


class Refused(Exception):
	"""This relation cannot be emitted, and the message says why.

	Raised rather than returned because a refusal surfaces from anywhere in
	the walk and every caller wants the same thing: drop this one relation,
	keep the rest, and say so.
	"""


@dataclass(frozen=True)
class SubView:
	"""Acquire `struct.member` out of `source`, binding `target`.

	`into` is the struct the sub-view *is*, as against `struct`, which is the
	one whose accessor reaches it. C never needs the distinction -- every view
	there is a `situ_view_t` -- and C++ declares the local's type outright, so
	conflating the two produced a header that named the parent class and did
	not compile.
	"""

	struct: str
	member: str
	into: str
	source: str
	target: str
	path: str


@dataclass(frozen=True)
class Read:
	"""Read the scalar `struct.member` out of `source`, binding `target`."""

	struct: str
	member: str
	source: str
	target: str
	path: str


@dataclass(frozen=True)
class ReadBytes:
	"""Bind the bytes of the fixed-size array `struct.member` in `source`.

	A separate binding from `Read` because it is a different operation, not a
	relaxed one: there is no scalar to widen, no signedness to reconcile, and
	the thing bound is a span rather than a value.
	"""

	struct: str
	member: str
	source: str
	target: str
	path: str
	#: Bytes, known here because only fixed-size arrays reach this.
	length: int


Binding = SubView | Read | ReadBytes


@dataclass(frozen=True)
class BytesEqual:
	"""`==` or `!=` between two equal-length fixed-size arrays.

	Kept off the expression tree on purpose. Every other constraint is an
	expression over scalars that each backend spells with its own operators;
	this one is "do these bytes equal those bytes", which C spells with
	`memcmp`, Rust and Python with `==` on a slice, and no backend spells by
	widening anything. Putting it in the expression would make four backends
	each decide what an array operand means, which is what this module exists
	to prevent.
	"""

	left: str
	right: str
	length: int
	negated: bool


@dataclass
class Constraint:
	"""One `must`: what to bind, in order, and the test over the bindings."""

	bindings: list[Binding] = field(default_factory=list)
	locals_for: dict[str, str] = field(default_factory=dict)
	expr: ast.Expr | None = None
	#: Whether every operand must be widened as signed. Meaningless where a
	#: language has no fixed-width integers, and harmless to ignore there.
	signed: bool = False
	#: Set instead of `expr` where the constraint compares two arrays. A
	#: backend that finds this emits its own byte comparison and ignores the
	#: expression, which is still carried for the comment at the call site.
	bytes_equal: BytesEqual | None = None


def paths_in(expr: ast.Expr) -> list[str]:
	"""Every dotted path the expression names, in order, with duplicates.

	`invariant.paths_in` answers a similar question and is not reused: it
	folds a `Call`'s arguments into the caller's list, which is right for an
	invariant -- where `size(x)` names `x` -- and wrong here, where a call is
	refused outright and its arguments must not be promoted into paths that
	look reachable.
	"""
	if isinstance(expr, ast.Access):
		base = paths_in(expr.base)
		return [f"{base[0]}.{expr.name}"] if base else [expr.name]
	if isinstance(expr, ast.NameRef):
		return [expr.name]
	if isinstance(expr, ast.Binary):
		return paths_in(expr.left) + paths_in(expr.right)
	if isinstance(expr, ast.Unary):
		return paths_in(expr.operand)
	return []


def _member(struct: ResolvedStruct, name: str) -> Placement | None:
	for entry in struct.entries:
		placement = entry.placement
		if (is_own_member(struct, placement)
				and local_name(struct, placement) == name):
			return placement
	return None


def _leaf_placement(path: str, params: dict[str, ast.RelationParam],
		resolved: ResolvedSchema) -> Placement:
	"""The placement a path ends at, without binding anything.

	The walk in `plan` binds as it goes, which is what a backend needs and
	the wrong shape for deciding whether the comparison is expressible at
	all: that answer has to be known for *both* sides before either is
	bound, or a refusal arrives half-way through a constraint.
	"""
	components = path.split(".")
	param      = params[components[0]]
	struct     = resolved.structs.get(param.type_name)
	if struct is None:
		raise Refused(f"`{param.type_name}` has no resolved layout")

	placement: Placement | None = None
	for position, component in enumerate(components[1:]):
		placement = _member(struct, component)
		if placement is None:
			raise Refused(f"`{struct.name}.{component}` has no placement")
		if position == len(components) - 2:
			break
		nested = resolved.structs.get(placement.type_name or "")
		if nested is None:
			raise Refused(f"`{path}` reaches through `{component}`, "
			              f"which is not a struct")
		struct = nested

	if placement is None:
		raise Refused(f"`{path}` names no member")
	return placement


def _array_bytes(path: str, placement: Placement) -> int:
	"""How many bytes the array occupies, or a refusal naming why not."""
	scalar = placement.scalar
	if scalar is None:
		raise Refused(f"`{path}` names `{placement.kind}`, which is not an "
		              f"array of scalars")
	if scalar.kind is ScalarKind.FLOAT:
		raise Refused(f"`{path}` is an array of floating point, and an exact "
		              f"comparison of one is rarely what a wire contract means")
	if scalar.bits % 8 != 0:
		raise Refused(f"`{path}` has {scalar.bits}-bit elements, which do not "
		              f"land on byte boundaries to compare")
	assert placement.array_count is not None
	return placement.array_count * (scalar.bits // 8)


def _array_equality(expr: ast.Expr, params: dict[str, ast.RelationParam],
		resolved: ResolvedSchema, where: str) -> tuple[str, str, int, bool] | None:
	"""`a.x == b.y` between two equal-length fixed arrays, or None.

	None means "not this shape", and the ordinary scalar path handles it --
	including one side being an array, which `_leaf` refuses with the reason
	it always did.

	The refusals here are the narrow ones the shape needs. Differing lengths
	stay refused because a schema comparing a 16-byte field against a 32-byte
	one is almost certainly a mistake, and silently comparing the shorter
	prefix would be the dangerous reading. Ordering is refused because a key
	has no order: `<` over two identifiers is a question nobody asked.
	"""
	if not isinstance(expr, ast.Binary) or expr.op not in ("==", "!=", "<",
	                                                       "<=", ">", ">="):
		return None

	paths = [paths_in(side) for side in (expr.left, expr.right)]
	if any(len(side) != 1 for side in paths):
		return None

	left, right = paths[0][0], paths[1][0]
	places = [_leaf_placement(one, params, resolved) for one in (left, right)]
	if all(place.array_count is None for place in places):
		return None
	if any(place.array_count is None for place in places):
		# One side an array and the other not. `_leaf` already says what is
		# wrong with that, and says it about the side that is wrong.
		return None

	if expr.op not in ("==", "!="):
		raise Refused(f"{where} orders two arrays with `{expr.op}`, and an "
		              f"identifier has no order -- equality is the only "
		              f"comparison a key answers")

	widths = [_array_bytes(one, place) for one, place in zip((left, right), places)]
	if widths[0] != widths[1]:
		raise Refused(f"{where} compares {widths[0]} bytes against "
		              f"{widths[1]}; a comparison that held only over the "
		              f"shorter one would answer a question the schema did "
		              f"not ask")

	kinds = [place.scalar.kind for place in places if place.scalar is not None]
	if len(set(kinds)) != 1:
		raise Refused(f"{where} compares arrays of different element types")

	return left, right, widths[0], expr.op == "!="


def _leaf(path: str, placement: Placement) -> tuple[bool, int]:
	"""Signedness and width of the scalar a path ends at, or a refusal."""
	scalar = placement.scalar
	if scalar is None:
		raise Refused(f"`{path}` names `{placement.kind}`, which has no single "
		              f"value to compare")
	if placement.array_count is not None:
		raise Refused(f"`{path}` is an array, and a relation compares one "
		              f"value against another")
	if scalar.kind is ScalarKind.FLOAT:
		raise Refused(f"`{path}` is floating point, and an exact comparison of "
		              f"one is rarely what a wire contract means")
	return scalar.signed, scalar.bits


def _widen(operands: list[tuple[bool, int]], where: str) -> bool:
	"""Whether the constraint's operands widen as signed, or a refusal.

	Signed wins wherever it can, because every unsigned width below 64 fits in
	a signed 64-bit value without changing it. What it cannot cover is a
	64-bit unsigned alongside anything signed: no 64-bit type holds both
	ranges, so the comparison has no correct spelling in C, C++ or Rust and is
	refused in all four -- Python included, so that a schema does not mean one
	thing there and another everywhere else.
	"""
	if not any(signed for signed, _ in operands):
		return False
	if any(not signed and bits >= 64 for signed, bits in operands):
		raise Refused(f"{where} compares a 64-bit unsigned value against a "
		              f"signed one, and no 64-bit type holds both ranges")
	return True


def plan(relation: ast.Relation, resolved: ResolvedSchema) -> list[Constraint]:
	"""Every constraint resolved to bindings, or a refusal naming the reason.

	The local names are `_0`, `_1` and so on with the path in a comment at the
	call site: a name derived from the path could collide with a parameter the
	schema author chose, and a number cannot.
	"""
	params = {param.name: param for param in relation.params}
	plans: list[Constraint] = []
	index = 0

	for number, must in enumerate(relation.body, start=1):
		where      = f"constraint {number} of `{relation.name}`"
		constraint = Constraint(expr=must.expr)
		operands: list[tuple[bool, int]] = []
		arrays     = _array_equality(must.expr, params, resolved, where)

		for path in dict.fromkeys(paths_in(must.expr)):
			components = path.split(".")
			param      = params[components[0]]
			struct     = resolved.structs.get(param.type_name)
			if struct is None:
				raise Refused(f"`{param.type_name}` has no resolved layout")

			source = param.name
			walked = param.name

			for position, component in enumerate(components[1:]):
				walked = f"{walked}.{component}"
				placement = _member(struct, component)
				if placement is None:
					# wellformed proved the name exists on the declaration; a
					# placement absent here means the solver dropped it,
					# which is a different fact and gets a different sentence.
					raise Refused(f"`{struct.name}.{component}` has no placement")

				target = f"_{index}"
				index += 1
				last   = position == len(components) - 2

				if last:
					if arrays is not None:
						constraint.bindings.append(
							ReadBytes(struct.name, component, source, target,
							          path, _array_bytes(path, placement)))
						constraint.locals_for[path] = target
						break
					operands.append(_leaf(path, placement))
					constraint.bindings.append(
						Read(struct.name, component, source, target, path))
					constraint.locals_for[path] = target
					break

				nested = resolved.structs.get(placement.type_name or "")
				if nested is None:
					raise Refused(f"`{path}` reaches through `{component}`, "
					              f"which is not a struct")
				constraint.bindings.append(
					SubView(struct.name, component, nested.name, source,
					        target, walked))
				source = target
				struct = nested

		if arrays is not None:
			left, right, length, negated = arrays
			constraint.bytes_equal = BytesEqual(
				constraint.locals_for[left], constraint.locals_for[right],
				length, negated)
		else:
			constraint.signed = _widen(operands, where)
			_check(must.expr, constraint.locals_for, where, constraint.signed)

		plans.append(constraint)

	return plans


def _check(expr: ast.Expr, locals_for: dict[str, str], where: str,
		signed: bool = False) -> None:
	"""Refuse anything a relation may not hold, before any backend sees it."""
	if isinstance(expr, ast.IntLiteral):
		return
	if isinstance(expr, (ast.Access, ast.NameRef)):
		return
	if isinstance(expr, ast.Binary):
		if expr.op not in OPERATORS:
			raise Refused(f"{where} uses `{expr.op}`, which a relation may not")
		if signed and expr.op in ("/", "%"):
			# Spelling `/` as `//` in Python is right for two non-negative
			# operands and wrong for the rest: C, C++ and Rust truncate
			# toward zero and Python floors, so `-7 / 2` is -3 there and -4
			# here, and `-7 % 3` is -1 there and 2 here. Measured, not
			# reasoned.
			#
			# 26.208 refused the same pair in a field expression for the same
			# reason. Carrying it across is what `bound_widening` says should
			# have happened for bounds and did not: a rule that exists in one
			# path and not the next is a caveat, not a guard.
			raise Refused(
				f"{where} divides a signed value, and the backends do not "
				f"agree what that means -- C, C++ and Rust truncate toward "
				f"zero where Python floors, so `-7 / 2` is -3 in three of "
				f"them and -4 in the fourth")
		_check(expr.left, locals_for, where, signed)
		_check(expr.right, locals_for, where, signed)
		return
	if isinstance(expr, ast.Unary):
		if expr.op not in UNARY:
			raise Refused(f"{where} uses unary `{expr.op}`, which a relation "
			              f"may not")
		_check(expr.operand, locals_for, where, signed)
		return
	if isinstance(expr, ast.Call):
		raise Refused(f"{where} calls `{expr.name}`; a relation compares values "
		              f"a getter returns, and asks the layout nothing")
	raise Refused(f"{where} holds an expression a relation cannot emit")


#: How a language spells the three operators whose text is not universal.
#: Everything else in `OPERATORS` is identical in C, C++, Rust and Python.
@dataclass(frozen=True)
class Spelling:
	logical_and: str = "&&"
	logical_or: str = "||"
	logical_not: str = "!"
	#: How this language spells integer division. Python's `/` is float
	#: division and every other backend's is integer, so a relation reading
	#: `b.hdr.tweak / a.hdr.index` answered `1` in C, C++ and Rust and
	#: `1.5 == 1` -- false -- in Python. The same message, the same relation,
	#: opposite verdicts, which is the failure this module's header says the
	#: shared refusals exist to prevent (26.214).
	#:
	#: The main Python emitter has spelled this `//` since 8.6.2 and names the
	#: hazard in its own docstring. Relations never asked it. `bound_widening`
	#: describes the identical shape one construct over: a rule that exists,
	#: and a path that did not carry it across.
	divide: str = "/"


C_LIKE = Spelling()
PYTHON = Spelling(logical_and="and", logical_or="or", logical_not="not ",
                  divide="//")


def render(expr: ast.Expr, locals_for: dict[str, str],
		spelling: Spelling = C_LIKE) -> str:
	"""The constraint as source, with each path replaced by its local.

	`_check` has already refused everything this cannot render, so a node
	reaching the fallthrough is a compiler bug rather than a schema error.
	"""
	if isinstance(expr, ast.IntLiteral):
		return str(expr.value)

	if isinstance(expr, (ast.Access, ast.NameRef)):
		return locals_for[paths_in(expr)[0]]

	if isinstance(expr, ast.Binary):
		op = {"&&": spelling.logical_and,
		      "||": spelling.logical_or,
		      "/":  spelling.divide}.get(expr.op, expr.op)
		left  = render(expr.left, locals_for, spelling)
		right = render(expr.right, locals_for, spelling)
		return f"({left} {op} {right})"

	if isinstance(expr, ast.Unary):
		op = spelling.logical_not if expr.op == "!" else expr.op
		return f"({op}{render(expr.operand, locals_for, spelling)})"

	raise AssertionError(f"unrendered node {type(expr).__name__}: `_check` "
	                     f"should have refused it")


def refusals(schema: ast.Schema, resolved: ResolvedSchema) -> list[tuple[str, str]]:
	"""Every relation that gets no predicate, and why.

	The same list in every backend, which is the point: reported rather than
	silently skipped, because a caller who asked for the rung and found their
	predicate missing would conclude the generator was broken.
	"""
	found = []
	for relation in schema.relations():
		try:
			plan(relation, resolved)
		except Refused as why:
			found.append((relation.name, str(why)))
	return found


def plans(schema: ast.Schema,
		resolved: ResolvedSchema) -> list[tuple[ast.Relation, list[Constraint]]]:
	"""Every relation that can be emitted, planned. Refusals are dropped."""
	ready = []
	for relation in schema.relations():
		try:
			ready.append((relation, plan(relation, resolved)))
		except Refused:
			continue
	return ready


def conversation_key(relation: ast.Relation) -> list[tuple[str, str]]:
	"""The paths a relation says must be equal, as (request, response) pairs.

	**The equality constraints are the conversation key**, and that is the
	whole reason a relation buys more than the comparison it spells. A
	dissector needs to know what to hash a conversation on and a fuzz harness
	needs to know which bytes to copy from one message into the other; both
	are this list, and neither needs a second declaration in the schema.

	Only a top-level `==` between the two parameters counts. `b.index <
	a.chunks` is a rule about a pair and not a thing that identifies one, and
	`b.x == b.y` names one message -- which `wellformed` already refuses, but
	the filter here is what makes this function's answer true rather than
	true-by-luck.
	"""
	first, second = (param.name for param in relation.params)
	pairs: list[tuple[str, str]] = []

	for must in relation.body:
		expr = must.expr
		if not isinstance(expr, ast.Binary) or expr.op != "==":
			continue
		left  = paths_in(expr.left)
		right = paths_in(expr.right)
		if len(left) != 1 or len(right) != 1:
			continue

		roots = (left[0].split(".")[0], right[0].split(".")[0])
		if roots == (first, second):
			pairs.append((left[0], right[0]))
		elif roots == (second, first):
			pairs.append((right[0], left[0]))

	return pairs


#: Bits a *packed* key may occupy: one `uint64_t`, today's spelling, kept
#: byte-identical for every relation that fits it (0042). Wider keys and keys
#: with byte-string parts are exact bytes instead -- never a digest, because a
#: collision is a silently wrong pairing in the pairing layer.
KEY_BITS = 64

#: The ceiling on an exact-bytes key. A named number rather than a principle:
#: it covers every identifier 14.8 names -- TLS session ids at 32, Noise and
#: WireGuard static keys at 32, QUIC connection ids at 20 -- and it exists so
#: a schema cannot draft a kilobyte of payload into the slot table. A protocol
#: that outgrows it moves it the way 0042 moved KEY_BITS: by its own record.
KEY_MAX_BYTES = 32


def keykey_width(resolved: ResolvedSchema, bind: Read) -> int:
	struct = resolved.structs[bind.struct]
	for entry in struct.entries:
		placement = entry.placement
		if placement.path == f"{bind.struct}.{bind.member}":
			return placement.size_bits or 0
	return 0


#: A key part ends in a `Read` for a scalar or a `ReadBytes` for an exact
#: byte string (0042); the `SubView`s before it are the walk that gets there.
#: A backend asks `isinstance` of the last step, which it already did for the
#: first two kinds.
KeyStep = SubView | Read | ReadBytes
Side = list[tuple[list[KeyStep], int]]


@dataclass(frozen=True)
class KeyLayout:
	"""How a relation's conversation key is represented (0042).

	`packed` is today's spelling -- every part a scalar, 64 bits or fewer,
	shift-packed into one word -- and a relation that fit yesterday keeps
	byte-identical generated code. Anything else is the exact bytes: parts
	in declaration order, a scalar part as ceil(width/8) bytes big-endian, a
	bytes part as its own bytes. One layout, four spellings; the sequence of
	bytes is the language's so the backends cannot disagree about it.
	"""

	packed: bool
	total_bytes: int
	request: Side
	response: Side


def key_width(resolved: ResolvedSchema, bind: Read) -> int:
	"""How many bits the field a read lands on occupies."""
	struct = resolved.structs[bind.struct]
	for entry in struct.entries:
		placement = entry.placement
		if placement.path == f"{bind.struct}.{bind.member}":
			return placement.size_bits or 0
	return 0


def key_layout(relation: ast.Relation,
		resolved: ResolvedSchema) -> KeyLayout:
	"""The reads that build each side's key, in declaration order.

	Taken from the plan rather than resolved again here, so the accessors a
	key is read through are the same ones the predicate compares.
	"""
	first, second = (param.name for param in relation.params)
	pairs = conversation_key(relation)
	if not pairs:
		raise Refused("it states no equality, so nothing identifies an exchange")

	request: Side = []
	response: Side = []
	total = 0
	any_bytes = False

	for constraint, must in zip(plan(relation, resolved), relation.body):
		if constraint.bytes_equal is not None:
			if constraint.bytes_equal.negated:
				# `!=` over byte strings is a constraint, not an identity:
				# nothing about "these differ" says which exchange this is.
				continue

			byte_reads = [bind for bind in constraint.bindings
			              if isinstance(bind, ReadBytes)]
			if len(byte_reads) != 2:
				continue

			for bind in byte_reads:
				chain: list[KeyStep] = [
					step for step in constraint.bindings
					if isinstance(step, (SubView, Read, ReadBytes))
					and (step.path == bind.path
					     or bind.path.startswith(step.path + "."))]
				side: tuple[list[KeyStep], int] = (chain, bind.length * 8)
				(request if bind.path.split(".")[0] == first
				 else response).append(side)

			total += byte_reads[0].length * 8
			any_bytes = True
			continue

		expr = must.expr
		if not isinstance(expr, ast.Binary) or expr.op != "==":
			continue

		reads = [bind for bind in constraint.bindings if isinstance(bind, Read)]
		if len(reads) != 2:
			continue

		for read in reads:
			chain = [step for step in constraint.bindings
			         if isinstance(step, (SubView, Read))
			         and (step.path == read.path
			              or read.path.startswith(step.path + "."))]
			side = (chain, key_width(resolved, read))
			(request if read.path.split(".")[0] == first else response).append(side)

		total += key_width(resolved, reads[0])

	total_bytes = (total + 7) // 8
	if total_bytes > KEY_MAX_BYTES:
		raise Refused(f"its key is {total_bytes} bytes and the ceiling is "
		              f"{KEY_MAX_BYTES}; hashing it down would make two "
		              f"exchanges that collided indistinguishable, so a key "
		              f"this wide moves the ceiling by its own record (0042)")
	if not request or len(request) != len(response):
		raise Refused("its key does not read one field from each message")

	return KeyLayout(packed=not any_bytes and total <= KEY_BITS,
	                 total_bytes=total_bytes,
	                 request=request, response=response)
