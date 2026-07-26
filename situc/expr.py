"""Evaluation of compile-time constant expressions.

The expression language must stay total and decidable (project.md section 10):
no function calls beyond a fixed builtin set, no recursion, no iteration, no
forward references, no floating point. Everything here therefore terminates,
and an expression that cannot be folded is an error rather than a deferral.

Every expression carries a symbolic interval derived from the declared bounds of
its inputs. That interval is what the solver uses to compute worst-case sizes
and to decide whether an offset is statically known. An expression whose
interval is a single point is a compile-time constant **even when it textually
references a field** -- `x[hdr.n]` where `hdr.n [must_eq = 4]` is a fixed array,
not a dynamic one.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from situc import ast
from situc.diagnostics import SituError, error

# Builtins whose arguments are values rather than paths.
VALUE_BUILTINS = {"min": 2, "max": 2, "align_up": 2}

# Builtins taking a path to a declaration and answering from the layout.
LAYOUT_BUILTINS = {"size": 1, "offset": 1, "count": 1}


UNBOUNDED = None


@dataclass(frozen=True)
class Interval:
	"""The range of values an expression can take.

	`hi` of None means unbounded above, which is what an unconstrained field
	reference through an unbounded construct produces. `lo` is always known:
	nothing here produces a value with no lower bound.
	"""

	lo: int
	hi: int | None = UNBOUNDED

	@staticmethod
	def point(value: int) -> Interval:
		return Interval(value, value)

	@property
	def is_point(self) -> bool:
		return self.hi is not None and self.lo == self.hi

	@property
	def is_bounded(self) -> bool:
		return self.hi is not None

	def value(self) -> int:
		assert self.is_point, "not a single-point interval"
		return self.lo

	def render(self) -> str:
		if self.is_point:
			return str(self.lo)
		return f"[{self.lo}, {'inf' if self.hi is None else self.hi}]"


def scalar_interval(bits: int, signed: bool) -> Interval:
	"""What a field of this width can hold before any constraint narrows it."""
	if signed:
		return Interval(-(1 << (bits - 1)), (1 << (bits - 1)) - 1)
	return Interval(0, (1 << bits) - 1)


def _combine(a: Interval, b: Interval, op: Callable[[int, int], int]) -> Interval:
	"""Interval arithmetic by corner enumeration.

	Sound for the monotone operators this language has: the extremes of a
	monotone binary function over two ranges lie at the corners.

	An unbounded operand makes the result unbounded, and an operation that
	cannot be evaluated at a corner -- a division by a range spanning zero --
	widens to unbounded rather than guessing. Both are the conservative
	direction: they cost a capability rather than granting one.
	"""
	if a.hi is None or b.hi is None:
		return Interval(0, UNBOUNDED)

	corners = []
	for left in (a.lo, a.hi):
		for right in (b.lo, b.hi):
			try:
				corners.append(op(left, right))
			except (ValueError, ZeroDivisionError):
				return Interval(0, UNBOUNDED)

	return Interval(min(corners), max(corners))


def interval_of(expr: ast.Expr, env: Env) -> Interval:
	"""The range an expression can take, given what is known about its inputs.

	This is the decision procedure behind "static or dynamic". A size expression
	whose interval is a single point places a fixed array however it is written,
	so `x[hdr.n]` with `hdr.n [must_eq = 4]` is exactly as static as `x[4]`.
	"""
	if isinstance(expr, ast.IntLiteral):
		return Interval.point(expr.value)

	if isinstance(expr, ast.NameRef):
		constant = env.consts.get(expr.name)
		if constant is not None:
			return Interval.point(constant)

		known = env.fields.get(expr.name)
		if known is not None:
			return known

		return _not_resolvable(expr, expr.name, env)

	if isinstance(expr, ast.Access):
		path = path_text(expr)
		if path is not None:
			known = env.fields.get(path)
			if known is not None:
				return known

		# An enum member is still a constant here.
		try:
			return Interval.point(_access(expr, env))
		except SituError:
			return _not_resolvable(expr, path or "that path", env)

	if isinstance(expr, ast.Unary):
		operand = interval_of(expr.operand, env)
		if expr.op == "-":
			if operand.hi is None:
				return Interval(0, UNBOUNDED)
			return Interval(-operand.hi, -operand.lo)
		if operand.is_point:
			return Interval.point(UNARY_OPS[expr.op](operand.value()))
		return Interval(0, UNBOUNDED)

	if isinstance(expr, ast.Binary):
		left      = interval_of(expr.left, env)
		right     = interval_of(expr.right, env)
		operation = BINARY_OPS.get(expr.op)

		if operation is None:
			raise error(f"unknown operator `{expr.op}`", expr.span)

		# Points are folded through the same operator table rather than through
		# `evaluate`, which does not know about field references. A field with
		# a single-point interval is a constant here, and that is the whole
		# reason intervals exist.
		if left.is_point and right.is_point:
			_guard(expr, right.value())
			return Interval.point(operation(left.value(), right.value()))

		if expr.op in ("/", "%") and right.lo <= 0 <= (right.hi if right.hi is not None else 1):
			raise error(
				"divisor may be zero",
				expr.right.span,
				label = f"range is {right.render()}",
				notes = ["constrain the field with `[min = 1]` so the solver can "
				         "rule zero out"],
			)

		return _combine(left, right, operation)

	if isinstance(expr, ast.Call):
		return _call_interval(expr, env)

	if isinstance(expr, ast.Remaining):
		# Resolved by the solver against the enclosing frame, not here.
		return Interval(0, UNBOUNDED)

	raise error("expression is not usable as a size or bound", expr.span)


def _not_resolvable(expr: ast.Expr, name: str, env: Env) -> Interval:
	notes = ["a size may refer to a `const`, an enum member, or a field declared "
	         "earlier in the same struct"]
	if env.fields:
		listed = ", ".join(sorted(env.fields)[:6])
		notes.append(f"fields in scope here: {listed}")
	else:
		notes.append("no fields are in scope at this point")

	raise error(
		f"`{name}` is not in scope here",
		expr.span,
		label = "cannot be resolved",
		notes = notes,
	)


def _call_interval(expr: ast.Call, env: Env) -> Interval:
	if expr.name in VALUE_BUILTINS:
		_check_arity(expr, VALUE_BUILTINS[expr.name])
		args = [interval_of(arg, env) for arg in expr.args]
		if all(arg.is_point for arg in args):
			return Interval.point(_call(expr, env))

		if expr.name == "min":
			hi = None if any(a.hi is None for a in args) else min(
				a.hi for a in args if a.hi is not None)
			return Interval(min(a.lo for a in args), hi)
		if expr.name == "max":
			hi = None if any(a.hi is None for a in args) else max(
				a.hi for a in args if a.hi is not None)
			return Interval(max(a.lo for a in args), hi)
		return Interval(0, UNBOUNDED)

	# size(), offset() and count() answer from a solved layout, so they are
	# always single points when they answer at all.
	return Interval.point(_call(expr, env))


@dataclass
class Env:
	"""What names an expression may refer to.

	`layout` is supplied by the solver once it can answer `size(X)`; while the
	layout is still being computed it is absent, which is what stops a struct's
	own size from being referenced during its own layout.
	"""

	consts: dict[str, int]			= field(default_factory=dict)
	enums: dict[str, dict[str, int]]	= field(default_factory=dict)
	layout: Callable[[str, str], int | None] | None = None
	# Fields whose interval is known, keyed by the path an expression would use
	# to name them. Populated by the solver as it walks a struct, so a size
	# expression can only ever see fields declared before it -- which is the
	# no-forward-reference rule of section 10, enforced by construction.
	fields: dict[str, Interval]		= field(default_factory=dict)

	def with_layout(self, resolver: Callable[[str, str], int | None]) -> Env:
		return Env(self.consts, self.enums, resolver, self.fields)

	def with_fields(self, fields: dict[str, Interval]) -> Env:
		return Env(self.consts, self.enums, self.layout, fields)


def evaluate(expr: ast.Expr, env: Env) -> int:
	"""Fold an expression to a single integer, or raise."""
	if isinstance(expr, ast.IntLiteral):
		return expr.value

	if isinstance(expr, ast.NameRef):
		return _name(expr, env)

	if isinstance(expr, ast.Access):
		return _access(expr, env)

	if isinstance(expr, ast.Unary):
		return _unary(expr, env)

	if isinstance(expr, ast.Binary):
		return _binary(expr, env)

	if isinstance(expr, ast.Call):
		return _call(expr, env)

	if isinstance(expr, ast.Remaining):
		raise error(
			"`remaining` is not a compile-time constant",
			expr.span,
			label = "depends on the enclosing frame's extent",
			notes = ["frame-relative sizing arrives in phase 5 (project.md section 26.5)"],
		)

	if isinstance(expr, ast.StringLiteral):
		raise error("a string is not an integer expression", expr.span)

	raise error("expression is not a compile-time constant", expr.span)


def _name(expr: ast.NameRef, env: Env) -> int:
	value = env.consts.get(expr.name)
	if value is not None:
		return value

	notes = ["only `const` values and enum members are compile-time constants"]
	if expr.name in env.enums:
		notes = [f"`{expr.name}` is an enum; write `{expr.name}.<member>` to name a value"]

	raise error(
		f"`{expr.name}` is not a compile-time constant",
		expr.span,
		label = "not a constant",
		notes = notes,
	)


def _access(expr: ast.Access, env: Env) -> int:
	base = expr.base
	if isinstance(base, ast.NameRef):
		members = env.enums.get(base.name)
		if members is not None:
			value = members.get(expr.name)
			if value is None:
				known = ", ".join(sorted(members)) or "none"
				raise error(
					f"enum `{base.name}` has no member `{expr.name}`",
					expr.span,
					label = "unknown enum member",
					notes = [f"members: {known}"],
				)
			return value

	raise error(
		"field references are not compile-time constants",
		expr.span,
		label = "not a constant",
		notes = ["reading a value out of a parsed message arrives in phase 5"],
	)


UNARY_OPS: dict[str, Callable[[int], int]] = {
	"-": lambda a: -a,
	"~": lambda a: ~a,
	"!": lambda a: 0 if a else 1,
}


def _unary(expr: ast.Unary, env: Env) -> int:
	operation = UNARY_OPS.get(expr.op)
	if operation is None:
		raise error(f"unknown unary operator `{expr.op}`", expr.span)
	return operation(evaluate(expr.operand, env))


def _guard(expr: ast.Binary, right: int) -> None:
	"""Operand checks that apply however the operands were obtained."""
	if expr.op in ("/", "%") and right == 0:
		raise error(
			"division by zero",
			expr.span,
			label = "the right operand evaluates to zero",
		)

	if expr.op in ("<<", ">>"):
		if right < 0:
			raise error("negative shift count", expr.right.span)
		if right > 64:
			raise error(
				f"shift count {right} exceeds the widest scalar",
				expr.right.span,
				label = "at most 64",
			)


def _binary(expr: ast.Binary, env: Env) -> int:
	left  = evaluate(expr.left, env)
	right = evaluate(expr.right, env)

	_guard(expr, right)

	operation = BINARY_OPS.get(expr.op)
	if operation is None:
		raise error(f"unknown operator `{expr.op}`", expr.span)
	return operation(left, right)


# Integer division truncates toward zero, matching C rather than Python, because
# these expressions describe layouts a C backend will reproduce.
BINARY_OPS: dict[str, Callable[[int, int], int]] = {
	"+":  lambda a, b: a + b,
	"-":  lambda a, b: a - b,
	"*":  lambda a, b: a * b,
	"/":  lambda a, b: abs(a) // abs(b) * (1 if (a < 0) == (b < 0) else -1),
	"%":  lambda a, b: a - (abs(a) // abs(b) * (1 if (a < 0) == (b < 0) else -1)) * b,
	"&":  lambda a, b: a & b,
	"|":  lambda a, b: a | b,
	"^":  lambda a, b: a ^ b,
	"<<": lambda a, b: a << b,
	">>": lambda a, b: a >> b,
	"==": lambda a, b: int(a == b),
	"!=": lambda a, b: int(a != b),
	"<":  lambda a, b: int(a < b),
	">":  lambda a, b: int(a > b),
	"<=": lambda a, b: int(a <= b),
	">=": lambda a, b: int(a >= b),
	"&&": lambda a, b: int(bool(a) and bool(b)),
	"||": lambda a, b: int(bool(a) or bool(b)),
}


def _call(expr: ast.Call, env: Env) -> int:
	if expr.name in VALUE_BUILTINS:
		_check_arity(expr, VALUE_BUILTINS[expr.name])
		args = [evaluate(arg, env) for arg in expr.args]
		if expr.name == "min":
			return min(args)
		if expr.name == "max":
			return max(args)
		return _align_up(expr, args[0], args[1])

	if expr.name in LAYOUT_BUILTINS:
		_check_arity(expr, LAYOUT_BUILTINS[expr.name])
		return _layout_call(expr, env)

	raise error(
		f"unknown function `{expr.name}`",
		expr.span,
		label = "not a builtin",
		notes = ["the expression language has no user-defined functions "
		         "(project.md section 10)",
		         "builtins: " + ", ".join(sorted(VALUE_BUILTINS | LAYOUT_BUILTINS))],
	)


def _align_up(expr: ast.Call, value: int, alignment: int) -> int:
	if alignment <= 0:
		raise error(
			"alignment must be positive",
			expr.args[1].span,
			label = f"evaluates to {alignment}",
		)
	return ((value + alignment - 1) // alignment) * alignment


def _layout_call(expr: ast.Call, env: Env) -> int:
	path = path_text(expr.args[0])
	if path is None:
		raise error(
			f"`{expr.name}` takes a declaration path",
			expr.args[0].span,
			label = "expected a name such as `Header` or `Header.seq`",
		)

	if env.layout is None:
		raise error(
			f"`{expr.name}` cannot be used here",
			expr.span,
			label = "the layout is not resolved yet",
			notes = ["a struct's own layout cannot depend on its size"],
		)

	value = env.layout(expr.name, path)
	if value is None:
		raise error(
			f"unknown path `{path}`",
			expr.args[0].span,
			label = "not a declared struct or field",
		)
	return value


def _check_arity(expr: ast.Call, expected: int) -> None:
	if len(expr.args) != expected:
		word = "argument" if expected == 1 else "arguments"
		raise error(
			f"`{expr.name}` takes {expected} {word}, found {len(expr.args)}",
			expr.span,
			label = f"expected {expected} {word}",
		)


def path_text(expr: ast.Expr) -> str | None:
	"""Render a dotted path expression, or None if it is not one.

	`Message.recs[].value` renders with the empty index, because that is how the
	capability predicates of section 16 name an element.
	"""
	if isinstance(expr, ast.NameRef):
		return expr.name

	if isinstance(expr, ast.Access):
		base = path_text(expr.base)
		return None if base is None else f"{base}.{expr.name}"

	if isinstance(expr, ast.Index):
		base = path_text(expr.base)
		if base is None:
			return None
		if expr.index is None:
			return f"{base}[]"
		if isinstance(expr.index, ast.IntLiteral):
			return f"{base}[{expr.index.text}]"
		return None

	return None


def build_env(schema: ast.Schema) -> Env:
	"""Fold every `const` and enum member in a schema.

	Resolution walks declarations in source order, so a declaration may refer to
	an earlier one but never a later one. That is the same no-forward-reference
	rule section 10 applies to fields, and it keeps this a single pass.

	Order matters across kinds as well as within them: an enum member may be
	defined in terms of a constant and a constant in terms of an enum member, so
	the two cannot be resolved in separate passes.
	"""
	env = Env()

	for decl in schema.decls:
		if isinstance(decl, ast.EnumDecl):
			members: dict[str, int] = {}
			for member in decl.members:
				members[member.name] = evaluate(member.value, env)
			env.enums[decl.name] = members
		elif isinstance(decl, ast.ConstDecl):
			env.consts[decl.name] = _const_value(decl, env)

	return env


def _const_value(decl: ast.ConstDecl, env: Env) -> int:
	try:
		return evaluate(decl.value, env)
	except SituError as exc:
		exc.diagnostic.notes.append(
			f"while evaluating the constant `{decl.name}`")
		raise
