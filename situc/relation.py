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


Binding = SubView | Read


@dataclass
class Constraint:
	"""One `must`: what to bind, in order, and the test over the bindings."""

	bindings: list[Binding] = field(default_factory=list)
	locals_for: dict[str, str] = field(default_factory=dict)
	expr: ast.Expr | None = None
	#: Whether every operand must be widened as signed. Meaningless where a
	#: language has no fixed-width integers, and harmless to ignore there.
	signed: bool = False


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

		constraint.signed = _widen(operands, where)
		_check(must.expr, constraint.locals_for, where)
		plans.append(constraint)

	return plans


def _check(expr: ast.Expr, locals_for: dict[str, str], where: str) -> None:
	"""Refuse anything a relation may not hold, before any backend sees it."""
	if isinstance(expr, ast.IntLiteral):
		return
	if isinstance(expr, (ast.Access, ast.NameRef)):
		return
	if isinstance(expr, ast.Binary):
		if expr.op not in OPERATORS:
			raise Refused(f"{where} uses `{expr.op}`, which a relation may not")
		_check(expr.left, locals_for, where)
		_check(expr.right, locals_for, where)
		return
	if isinstance(expr, ast.Unary):
		if expr.op not in UNARY:
			raise Refused(f"{where} uses unary `{expr.op}`, which a relation "
			              f"may not")
		_check(expr.operand, locals_for, where)
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


C_LIKE = Spelling()
PYTHON = Spelling(logical_and="and", logical_or="or", logical_not="not ")


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
		      "||": spelling.logical_or}.get(expr.op, expr.op)
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
