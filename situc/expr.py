"""Evaluation of compile-time constant expressions.

The expression language must stay total and decidable (project.md section 10):
no function calls beyond a fixed builtin set, no recursion, no iteration, no
forward references, no floating point. Everything here therefore terminates,
and an expression that cannot be folded is an error rather than a deferral.

Interval arithmetic over field references arrives in phase 5. Phase 2 needs
only the single-point case, which is what constants, enum members and
`size()`/`offset()` of a solved struct all are.
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

	def with_layout(self, resolver: Callable[[str, str], int | None]) -> Env:
		return Env(self.consts, self.enums, resolver)


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


def _unary(expr: ast.Unary, env: Env) -> int:
	operand = evaluate(expr.operand, env)
	if expr.op == "-":
		return -operand
	if expr.op == "~":
		return ~operand
	if expr.op == "!":
		return 0 if operand else 1
	raise error(f"unknown unary operator `{expr.op}`", expr.span)


def _binary(expr: ast.Binary, env: Env) -> int:
	left  = evaluate(expr.left, env)
	right = evaluate(expr.right, env)

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

	# Integer division truncates toward zero, matching C rather than Python,
	# because these expressions describe layouts a C backend will reproduce.
	operations: dict[str, Callable[[int, int], int]] = {
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

	operation = operations.get(expr.op)
	if operation is None:
		raise error(f"unknown operator `{expr.op}`", expr.span)
	return operation(left, right)


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
