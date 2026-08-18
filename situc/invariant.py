"""What an invariant's right-hand side means, once, for every backend.

Section 16.1 says an invariant may use `size`, `count`, `offset` and
arithmetic over them, and that an expression the backend cannot evaluate gets
no recompute and says so. That sentence is the language, not a property of any
target: C, C++, Python and Rust must agree on which expressions are evaluable,
because a schema that derives a field in one of them and refuses in another is
a schema that means two things.

So the *walk* lives here and the *leaves* do not. A backend supplies `Terms`
-- how it spells a literal, an operator and the size of a member -- and gets
the same answer about which expressions are admissible as everybody else. The
alternative was four copies of a recursive descent over the same six node
types, which is the shape `traverse.py` already exists to prevent.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from situc import ast

# Under TYPE_CHECKING only. `layout` calls `paths_in` while solving, and an
# ordinary import here would close the cycle layout -> invariant -> layout.
if TYPE_CHECKING:
	from situc.layout import Placement
	from situc.resolve import ResolvedStruct

#: Arithmetic an invariant may use.
#:
#: No comparison, no bit twiddling, no calls beyond `BUILTINS`. Not because
#: they are hard to emit, but because an invariant states that a field *equals*
#: something, and the moment the right side can be a predicate the construct
#: has quietly become a second `require` with a worse syntax.
OPERATORS = frozenset("+-*/")

#: What an invariant may ask of a member. Each is a fact the layout solver
#: already knows, which is what makes them safe to derive from.
BUILTINS = frozenset({"offset", "size", "count"})


class Terms(Protocol):
	"""The leaves of an invariant, in whatever language the backend emits.

	Returning `None` from any of these means "this backend cannot evaluate
	that", and the whole expression collapses to `None`. That is a refusal, not
	a failure: the field keeps its `mutate = Immutable`, so the invariant
	cannot be broken -- only left unsatisfiable, which the generated code says
	out loud.
	"""

	def literal(self, value: int) -> str:
		"""A constant, spelled for this language."""

	def binary(self, op: str, left: str, right: str) -> str:
		"""Two evaluated operands, joined."""

	def offset(self, struct: "ResolvedStruct", placement: "Placement") -> str | None:
		"""Where the member starts, in bytes from the view's base."""

	def size(self, struct: "ResolvedStruct", placement: "Placement") -> str | None:
		"""How many bytes the member occupies."""

	def count(self, struct: "ResolvedStruct", placement: "Placement") -> str | None:
		"""How many elements it holds."""


class BoundTerms(Terms, Protocol):
	"""A `Terms` that can also read what a member *holds*."""

	def value(self, struct: "ResolvedStruct",
			placement: "Placement") -> str | None:
		"""The member's value, read from the view being validated."""

	def bound_literal(self, value: int) -> str:
		"""A constant in a bound, which is not the same context as a constant
		in an invariant. `literal` spells one for the type the *invariant*
		assigns to -- `1u`, `1usize` -- and a bound is compared against a
		widened signed value, where those do not type-check."""


def bound(struct: "ResolvedStruct", expr: ast.Expr,
		terms: BoundTerms) -> str | None:
	"""A constraint's bound, in the backend's language, or None.

	A separate entry point from `expression` rather than a flag on it, because
	the two are asking different questions and the difference is worth keeping
	where a reader can see it. An invariant says what a field *equals*, and
	may read a layout fact -- offset, size, count -- which the solver knows
	without looking at any message. A `[max]` is checked against a message
	that is in front of you, so a sibling's *value* is available to it and is
	not available to an invariant. Sharing one function would have made
	invariants able to read values as a side effect of fixing bounds.
	"""
	if isinstance(expr, ast.IntLiteral):
		return terms.bound_literal(expr.value)

	if isinstance(expr, (ast.NameRef, ast.Access)):
		# Not `member()`: it strips everything before the first dot, which is
		# right for `size(s.chunks)` and drops the whole name from a bare
		# `chunks`. A bound is written beside the member it constrains, so the
		# sibling is usually named without a qualifier.
		paths = paths_in(expr)
		if len(paths) != 1:
			return None
		field     = paths[0].partition(".")[2] or paths[0]
		placement = next((entry.placement for entry in struct.entries
		                  if entry.placement.path == f"{struct.name}.{field}"),
		                 None)
		if placement is None or placement.scalar is None:
			return None
		return terms.value(struct, placement)

	if isinstance(expr, ast.Binary):
		if expr.op not in OPERATORS:
			return None
		left  = bound(struct, expr.left, terms)
		right = bound(struct, expr.right, terms)
		if left is None or right is None:
			return None
		return terms.binary(expr.op, left, right)

	return expression(struct, expr, terms)


def bound_widening(struct: "ResolvedStruct", against: "Placement",
		expr: ast.Expr) -> Exception | None:
	"""Refuse a bound whose comparison has no correct spelling, or None.

	The same rule `relation._widen` applies, and for the same reason: every
	unsigned width below 64 fits in a signed 64-bit value unchanged, and a
	64-bit unsigned does not. The generated comparison casts both sides to a
	signed 64-bit type, so a `u64` above `INT64_MAX` arrives negative and the
	test silently answers the opposite of the truth -- `used = 2**63` against
	`cap = 1` validated as OK, which is a message accepted that should have
	been refused.

	Relations refused this from the start. Bounds widened without carrying the
	rule across, and a caveat written in a message is not a guard in the
	compiler.
	"""
	from situc.diagnostics import error

	names = {entry.placement.path.partition(".")[2]: entry.placement
	         for entry in struct.entries}
	operands = [against]
	for path in paths_in(expr):
		found = names.get(path.partition(".")[2] or path)
		if found is not None:
			operands.append(found)

	wide = [one for one in operands
	        if one.scalar is not None
	        and not one.scalar.signed and one.scalar.bits >= 64]
	if not wide:
		return None

	return error(
		f"this bound compares a 64-bit unsigned value, which has no correct "
		f"spelling as a comparison",
		expr.span,
		label = "cannot be widened",
		notes = ["the check widens both sides to a signed 64-bit type, and no "
		         "64-bit type holds both ranges -- a value above the signed "
		         "maximum would arrive negative and the test would answer the "
		         "opposite of the truth",
		         f"`{wide[0].path}` is the operand that does not fit",
		         "a relation refuses the same comparison for the same reason"],
	)


def bound_refusal(struct: "ResolvedStruct", expr: ast.Expr,
		original: Exception) -> Exception:
	"""A better reason than the constant folder's, where there is one.

	Once a bound may name a sibling, "not a compile-time constant" stops being
	the whole story: the likeliest cause of an unresolvable name is a typo in
	a sibling's, and that note sends the author looking for a `const` they
	never meant to write. Only replaces the message where the expression is a
	bare name, which is the case it can speak to.
	"""
	from situc.diagnostics import error

	# The name is usually inside an expression rather than the whole of it:
	# `chunks - 1` is the shape a bound takes, so looking only at a bare name
	# would speak to almost none of them.
	known = {entry.placement.path.partition(".")[2] for entry in struct.entries}
	unresolved = [one for one in paths_in(expr)
	              if (one.partition(".")[2] or one) not in known]
	if len(unresolved) != 1:
		return original

	paths = unresolved

	named = [entry.placement.path.partition(".")[2] for entry in struct.entries
	         if entry.placement.scalar is not None
	         and entry.placement.array_count is None]
	return error(
		f"`{paths[0]}` is neither a constant nor a member of `{struct.name}`",
		expr.span,
		label = "not found",
		notes = ["a bound may be a `const`, an enum member, or the name of a "
		         "scalar member of this struct, read from the message being "
		         "validated",
		         f"this struct has: {', '.join(named) or 'no scalar members'}"],
	)


def paths_in(expr: ast.Expr) -> list[str]:
	"""Every dotted field path the expression mentions, in order.

	A dotted path is not one node. `s.hdr` parses as `Access(NameRef('s'),
	'hdr')`, and code that expected a `NameRef` carrying a dot found nothing
	and concluded the expression named no fields at all.
	"""
	if isinstance(expr, ast.Access):
		base = paths_in(expr.base)
		return [f"{base[0]}.{expr.name}"] if base else [expr.name]
	if isinstance(expr, ast.NameRef):
		return [expr.name]
	if isinstance(expr, ast.Call):
		return [path for arg in expr.args for path in paths_in(arg)]
	if isinstance(expr, ast.Binary):
		return paths_in(expr.left) + paths_in(expr.right)
	if isinstance(expr, ast.Unary):
		return paths_in(expr.operand)
	return []


def member(struct: "ResolvedStruct", expr: ast.Expr) -> "Placement | None":
	"""The one member an argument names, or None if it does not name exactly one.

	Exactly one on purpose. `size(a + b)` is not a member's size, and guessing
	which half was meant would put a number in generated code that nobody
	wrote.
	"""
	paths = paths_in(expr)
	if len(paths) != 1:
		return None

	field = paths[0].partition(".")[2]
	return next((entry.placement for entry in struct.entries
	             if entry.placement.path == f"{struct.name}.{field}"), None)


def expression(struct: "ResolvedStruct", expr: ast.Expr,
		terms: Terms) -> str | None:
	"""The right-hand side, in the backend's language, or None if it cannot be.

	Every backend gets the same answer about *which* expressions are evaluable,
	and its own answer about how to spell the ones that are.
	"""
	if isinstance(expr, ast.IntLiteral):
		return terms.literal(expr.value)

	if isinstance(expr, ast.Binary):
		if expr.op not in OPERATORS:
			return None
		left  = expression(struct, expr.left, terms)
		right = expression(struct, expr.right, terms)
		if left is None or right is None:
			return None
		return terms.binary(expr.op, left, right)

	if isinstance(expr, ast.Call):
		if expr.name not in BUILTINS or len(expr.args) != 1:
			return None
		placement = member(struct, expr.args[0])
		if placement is None:
			return None
		if expr.name == "offset":
			return terms.offset(struct, placement)
		if expr.name == "size":
			return terms.size(struct, placement)
		return terms.count(struct, placement)

	return None


def derived(schema: ast.Schema, struct: "ResolvedStruct") -> list[ast.Invariant]:
	"""The invariants this struct maintains, in declaration order."""
	return [decl for decl in schema.invariants()
	        if decl.derived.partition(".")[0] == struct.name]
