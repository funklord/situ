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
