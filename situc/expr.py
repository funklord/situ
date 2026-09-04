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
from situc.types import literal_bytes
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
	reference through an unbounded construct produces.

	`lo_known` is False where the rules below could not derive a lower bound
	at all, and then `lo` is a placeholder rather than a fact. It used to be
	neither: every widening returned `Interval(0, None)`, and the zero was
	read as "this cannot be negative" by the one check that asks. So
	`x[align_up(n, 4) - n - 10]`, which is negative for every `n`, was
	accepted -- the widening had *granted* the property the check was looking
	for. A conservative approximation that happens to satisfy the predicate
	you are about to test is not conservative.
	"""

	lo: int
	hi: int | None = UNBOUNDED
	lo_known: bool = True

	@staticmethod
	def point(value: int) -> Interval:
		return Interval(value, value)

	@staticmethod
	def unknown() -> Interval:
		"""Nothing derivable, and saying so. Both ends, deliberately: an
		interval with an unknown lower bound and a known upper one has no
		consumer here, and pretending `lo <= hi` for it invites the same
		confusion the placeholder zero caused."""
		return Interval(0, UNBOUNDED, lo_known = False)

	@property
	def is_point(self) -> bool:
		return self.hi is not None and self.lo == self.hi and self.lo_known

	@property
	def is_bounded(self) -> bool:
		return self.hi is not None

	def value(self) -> int:
		assert self.is_point, "not a single-point interval"
		return self.lo

	def render(self) -> str:
		if self.is_point:
			return str(self.lo)
		low = str(self.lo) if self.lo_known else "unknown"
		return f"[{low}, {'inf' if self.hi is None else self.hi}]"


#: The width every operand of a layout expression is computed at. The
#: generated C widens with `situ_leaf_u64` before it does anything else, so a
#: shift amount at or above this is undefined there whatever the field's own
#: width is (0049).
SHIFT_WIDTH = 64


def scalar_interval(bits: int, signed: bool) -> Interval:
	"""What a field of this width can hold before any constraint narrows it."""
	if signed:
		return Interval(-(1 << (bits - 1)), (1 << (bits - 1)) - 1)
	return Interval(0, (1 << bits) - 1)


def _span(lo: int | None, hi: int | None) -> Interval:
	"""An interval from bounds either of which may be unknown."""
	if lo is None:
		return Interval.unknown()
	return Interval(lo, hi)


def _nonneg(x: Interval) -> bool:
	return x.lo_known and x.lo >= 0


def _mask(value: int) -> int:
	"""The largest integer with no more bits set than `value` has."""
	return (1 << value.bit_length()) - 1


def _corners(a: Interval, b: Interval, op: Callable[[int, int], int]) -> Interval:
	"""Interval arithmetic by corner enumeration.

	Sound only where `op` is monotone in each argument separately -- then the
	extremes over a box lie at its corners. **That is a property of the
	operator, and this language has operators that do not have it.** `%`, `&`,
	`|` and `^` were all put through this function, and `(4 - n % 4) % 4` came
	out `[0, 1]` for an expression whose range is `0..3`: the corners `1 % 4`
	and `4 % 4` are 1 and 0, and the values in between are not. The map then
	*understated* a member's size, which is the direction that grants a
	capability rather than costing one.

	Each of those four has its own rule below. This one is for the operators
	whose docstring claim was true.
	"""
	if a.hi is None or b.hi is None or not (a.lo_known and b.lo_known):
		return Interval.unknown()

	corners = []
	for left in (a.lo, a.hi):
		for right in (b.lo, b.hi):
			try:
				corners.append(op(left, right))
			except (ValueError, ZeroDivisionError):
				return Interval.unknown()

	return Interval(min(corners), max(corners))


def _add_interval(a: Interval, b: Interval) -> Interval:
	lo = a.lo + b.lo if a.lo_known and b.lo_known else None
	hi = None if a.hi is None or b.hi is None else a.hi + b.hi
	return _span(lo, hi)


def _sub_interval(a: Interval, b: Interval) -> Interval:
	"""Where the placeholder zero did its damage: `x - y` with `y` unbounded
	above has no lower bound at all, and returning zero for it said the
	subtraction could not go negative."""
	lo = a.lo - b.hi if a.lo_known and b.hi is not None else None
	hi = None if a.hi is None or not b.lo_known else a.hi - b.lo
	return _span(lo, hi)


def _mul_interval(a: Interval, b: Interval) -> Interval:
	if _nonneg(a) and _nonneg(b):
		hi = None if a.hi is None or b.hi is None else a.hi * b.hi
		return Interval(a.lo * b.lo, hi)
	return _corners(a, b, BINARY_OPS["*"])


def _div_interval(a: Interval, b: Interval) -> Interval:
	"""The divisor cannot span zero -- `interval_of` refuses that first."""
	if _nonneg(a) and b.lo_known and b.lo > 0:
		lo = 0 if b.hi is None else a.lo // b.hi
		hi = None if a.hi is None else a.hi // b.lo
		return Interval(lo, hi)
	return _corners(a, b, BINARY_OPS["/"])


def _mod_interval(a: Interval, b: Interval) -> Interval:
	"""`a % b` is not monotone in `a`, which is what corner enumeration
	assumed. For a non-negative dividend and a positive divisor the range is
	`0 .. min(a.hi, b.hi - 1)`, and where the dividend cannot reach the
	divisor the modulo is the identity."""
	if not (_nonneg(a) and b.lo_known and b.lo > 0):
		return Interval.unknown()
	if a.hi is not None and a.hi < b.lo:
		return a

	limits = [limit for limit in (a.hi, None if b.hi is None else b.hi - 1)
	          if limit is not None]
	return Interval(0, min(limits) if limits else UNBOUNDED)


def _and_interval(a: Interval, b: Interval) -> Interval:
	"""No bit survives that both operands do not have, so neither operand's
	maximum is exceeded. Not monotone: `5 & 3` is 1 and `4 & 3` is 0."""
	if not (_nonneg(a) and _nonneg(b)):
		return Interval.unknown()
	limits = [limit for limit in (a.hi, b.hi) if limit is not None]
	return Interval(0, min(limits) if limits else UNBOUNDED)


def _or_interval(a: Interval, b: Interval) -> Interval:
	"""At least each operand, and no wider than the widest of them."""
	if not (_nonneg(a) and _nonneg(b)):
		return Interval.unknown()
	lo = max(a.lo, b.lo)
	if a.hi is None or b.hi is None:
		return Interval(lo, UNBOUNDED)
	return Interval(lo, _mask(max(a.hi, b.hi)))


def _xor_interval(a: Interval, b: Interval) -> Interval:
	if not (_nonneg(a) and _nonneg(b)):
		return Interval.unknown()
	if a.hi is None or b.hi is None:
		return Interval(0, UNBOUNDED)
	return Interval(0, _mask(max(a.hi, b.hi)))


def _shl_interval(a: Interval, b: Interval) -> Interval:
	if _nonneg(a) and _nonneg(b):
		hi = None if a.hi is None or b.hi is None else a.hi << b.hi
		return Interval(a.lo << b.lo, hi)
	return _corners(a, b, BINARY_OPS["<<"])


def _shr_interval(a: Interval, b: Interval) -> Interval:
	if _nonneg(a) and _nonneg(b):
		lo = 0 if b.hi is None else a.lo >> b.hi
		hi = None if a.hi is None else a.hi >> b.lo
		return Interval(lo, hi)
	return _corners(a, b, BINARY_OPS[">>"])


def _predicate_interval(a: Interval, b: Interval) -> Interval:
	"""A comparison answers 0 or 1 whatever its operands are."""
	del a, b
	return Interval(0, 1)


#: One rule per operator, because soundness is a property of the operator.
#: Section 8's sizes and section 11's `size` axis are read off these, so a
#: rule that is merely plausible shows up as a capability the map does not
#: have or a bound the generated code does not hold to.
INTERVAL_RULES: dict[str, Callable[[Interval, Interval], Interval]] = {
	"+":  _add_interval,
	"-":  _sub_interval,
	"*":  _mul_interval,
	"/":  _div_interval,
	"%":  _mod_interval,
	"&":  _and_interval,
	"|":  _or_interval,
	"^":  _xor_interval,
	"<<": _shl_interval,
	">>": _shr_interval,
	"==": _predicate_interval,
	"!=": _predicate_interval,
	"<":  _predicate_interval,
	">":  _predicate_interval,
	"<=": _predicate_interval,
	">=": _predicate_interval,
	"&&": _predicate_interval,
	"||": _predicate_interval,
}


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
			if operand.hi is None or not operand.lo_known:
				return Interval.unknown()
			return Interval(-operand.hi, -operand.lo)
		if operand.is_point:
			return Interval.point(UNARY_OPS[expr.op](operand.value()))
		if expr.op == "!":
			return Interval(0, 1)
		return Interval.unknown()

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

		padding = _padding_interval(expr, env)
		if padding is not None:
			return padding

		if expr.op in ("/", "%") and (not right.lo_known
				or right.lo <= 0 <= (right.hi if right.hi is not None else 1)):
			raise error(
				"divisor may be zero",
				expr.right.span,
				label = f"range is {right.render()}",
				notes = ["constrain the field with `[min = 1]` so the solver can "
				         "rule zero out"],
			)

		# And the dividend, for a reason that is about the backends rather
		# than about arithmetic. `BINARY_OPS` below defines `/` and `%` as C
		# does -- truncating toward zero -- "because these expressions
		# describe layouts a C backend will reproduce". C, C++ and Rust
		# reproduce it. Python's `//` and `%` *floor*, and Lua's do too, and
		# the two answers differ exactly when the operands have opposite
		# signs: `(n - 10) / 3 + 5` at n = 5 is 4 in C and 3 in Python, which
		# is a one-byte difference in an array length.
		#
		# Refused rather than papered over, because the expression reaches a
		# host compiler as text and making two of the four spell a call
		# instead of an operator needs the tree this layout has already
		# turned into source. Where the solver can prove the dividend
		# non-negative the four agree, which is every committed schema.
		if expr.op in ("/", "%") and (not left.lo_known or left.lo < 0):
			raise error(
				f"the left operand of `{expr.op}` may be negative",
				expr.left.span,
				label = f"range is {left.render()}",
				notes = ["`/` and `%` truncate toward zero here, as in C -- "
				         "and Python and Lua floor, so the backends would "
				         "disagree about a negative dividend",
				         "give it a lower bound: `[min = N]` on the fields it "
				         "reads, or reorder so the subtraction cannot go "
				         "below zero"],
			)

		# And the shift amount, for the reason 0049 gives. The generated C
		# widens every operand of a layout expression to 64 bits before
		# shifting -- `situ_leaf_u64(...) >> 2` -- so the amount has to land
		# in [0, 64). At or above the width it is undefined in C, a
		# deny-by-default `arithmetic_overflow` in Rust, a panic in a debug
		# build, and an ordinary answer in Python: four descriptions, three
		# behaviours, none of them the schema's.
		#
		# `[max]` is what makes an amount provable, and the interval reaching
		# here has already been narrowed by it -- `layout.constrain` folds
		# `[max]`, `[min]` and `[must_eq]` into a field's range while
		# solving -- so the remedy the note names is one the solver can act
		# on.
		if expr.op in ("<<", ">>") and (
				not right.lo_known or right.lo < 0
				or right.hi is None or right.hi >= SHIFT_WIDTH):
			raise error(
				f"the shift amount of `{expr.op}` is not provably below "
				f"{SHIFT_WIDTH}",
				expr.right.span,
				label = f"range is {right.render()}",
				notes = [f"a shift by {SHIFT_WIDTH} or more is undefined in C, "
				         "refused outright by rustc, and an ordinary answer in "
				         "Python, so the backends would not agree",
				         "bound it with `[max = 63]` on the field it reads, or "
				         "write a literal amount"],
			)

		rule = INTERVAL_RULES.get(expr.op)
		if rule is None:
			return Interval.unknown()
		return rule(left, right)

	if isinstance(expr, ast.Call):
		return _call_interval(expr, env)

	if isinstance(expr, ast.Remaining):
		# Resolved by the solver against the enclosing frame, not here.
		return Interval(0, UNBOUNDED)

	raise error("expression is not usable as a size or bound", expr.span)


def _padding_interval(expr: ast.Binary, env: Env) -> Interval | None:
	"""`align_up(x, k) - x`: how many bytes of padding follow `x`.

	Interval arithmetic has no memory of which ranges came from the same
	value, so it reads this as an unrelated `[k, x.hi + k] - [x.lo, x.hi]` and
	answers "may be negative" for an expression that is between 0 and `k - 1`
	always. That refusal falls on the one thing anybody writes `align_up` for:
	the kernel spells it `NLA_ALIGN(len) - len` and calls it `nla_padlen`
	(include/net/netlink.h), and a builtin whose only real use the solver
	rejects is a builtin that does not work.

	Recognised rather than inferred, and narrowly: the same *expression* on
	both sides, and a constant alignment. The answer is exact rather than
	conservative, which is why it is worth the special case -- correlation is
	not something a wider rule would recover.

	The two sides are compared structurally rather than by path. A cpio
	header pads its name to a four-byte boundary counted from the start of
	the entry, which is `align_up(110 + namesize, 4) - (110 + namesize)`:
	the same value twice, and neither occurrence is a bare field.
	"""
	if expr.op != "-" or not isinstance(expr.left, ast.Call):
		return None
	call = expr.left
	if call.name != "align_up" or len(call.args) != 2:
		return None

	if not same_expression(call.args[0], expr.right):
		return None

	alignment = interval_of(call.args[1], env)
	if not alignment.is_point or alignment.value() <= 0:
		return None
	return Interval(0, alignment.value() - 1)


def same_expression(left: ast.Expr, right: ast.Expr) -> bool:
	"""Whether two expressions are the same one, structurally.

	Not an evaluation: two expressions that happen to be equal for every
	input are not the same expression, and nothing here needs them to be.
	What this answers is "is this the same value written twice", which is the
	question correlation turns on.
	"""
	if type(left) is not type(right):
		return False

	if isinstance(left, ast.IntLiteral) and isinstance(right, ast.IntLiteral):
		return left.value == right.value
	if isinstance(left, ast.NameRef) and isinstance(right, ast.NameRef):
		return left.name == right.name
	if isinstance(left, ast.Access) and isinstance(right, ast.Access):
		return (left.name == right.name
		        and same_expression(left.base, right.base))
	if isinstance(left, ast.Unary) and isinstance(right, ast.Unary):
		return (left.op == right.op
		        and same_expression(left.operand, right.operand))
	if isinstance(left, ast.Binary) and isinstance(right, ast.Binary):
		return (left.op == right.op
		        and same_expression(left.left, right.left)
		        and same_expression(left.right, right.right))
	if isinstance(left, ast.Call) and isinstance(right, ast.Call):
		return (left.name == right.name
		        and len(left.args) == len(right.args)
		        and all(same_expression(one, two)
		                for one, two in zip(left.args, right.args)))
	return False


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

		if not all(arg.lo_known for arg in args):
			return Interval.unknown()

		if expr.name == "min":
			hi = None if any(a.hi is None for a in args) else min(
				a.hi for a in args if a.hi is not None)
			return Interval(min(a.lo for a in args), hi)
		if expr.name == "max":
			hi = None if any(a.hi is None for a in args) else max(
				a.hi for a in args if a.hi is not None)
			return Interval(max(a.lo for a in args), hi)

		# `align_up` had no rule and widened to "nothing known", which is how
		# a padding member -- the one construct that needs it -- came out
		# `size=Unbounded` for a range of three bytes. It is monotone
		# non-decreasing in its first argument, so where the alignment is a
		# constant the bounds are just the rounded ends.
		value, alignment = args
		if alignment.is_point and alignment.value() > 0 and _nonneg(value):
			unit = alignment.value()
			hi = None if value.hi is None else _round_up(value.hi, unit)
			return Interval(_round_up(value.lo, unit), hi)
		return Interval.unknown()

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
	#: `enum format : u8[2] { bmp = "BM" }` -- arms that are spans rather
	#: than numbers (0052). Kept apart from `enums` so that nothing can get
	#: an integer out of one: there is no correct integer to get.
	byte_enums: dict[str, dict[str, bytes]]	= field(default_factory=dict)
	layout: Callable[[str, str], int | None] | None = None
	# Fields whose interval is known, keyed by the path an expression would use
	# to name them. Populated by the solver as it walks a struct, so a size
	# expression can only ever see fields declared before it -- which is the
	# no-forward-reference rule of section 10, enforced by construction.
	fields: dict[str, Interval]		= field(default_factory=dict)
	#: Why a `lookup` came back empty, for the diagnostic. Optional because
	#: only the solver has it and every other caller wants the value alone.
	explain: Callable[[str, str], tuple[str, str]] | None = None

	def with_layout(self, resolver: Callable[[str, str], int | None],
			explain: Callable[[str, str], tuple[str, str]] | None = None) -> Env:
		# Keyword rather than positional: a field added to `Env` shifted
		# these silently, and a dataclass with six of them is a shape
		# nobody should have to count.
		return Env(consts = self.consts, enums = self.enums,
		           byte_enums = self.byte_enums, layout = resolver,
		           fields = self.fields, explain = explain)

	def with_fields(self, fields: dict[str, Interval]) -> Env:
		return Env(consts = self.consts, enums = self.enums,
		           byte_enums = self.byte_enums, layout = self.layout,
		           fields = fields, explain = self.explain)


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


def _round_up(value: int, alignment: int) -> int:
	"""The arithmetic `align_up` names, with no diagnostics attached.

	Shared with the interval rule and with what the backends emit, so the
	three cannot drift: a generated `align_up` that rounds differently from
	the one that folded a constant is a schema whose meaning depends on
	whether the alignment was written as a literal.
	"""
	return ((value + alignment - 1) // alignment) * alignment


def _align_up(expr: ast.Call, value: int, alignment: int) -> int:
	if alignment <= 0:
		raise error(
			"alignment must be positive",
			expr.args[1].span,
			label = f"evaluates to {alignment}",
		)
	return _round_up(value, alignment)


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
		# Four reasons, one of which was reported for all of them. See
		# `SchemaLayout.explain`.
		message, label = (
			env.explain(expr.name, path) if env.explain is not None
			else (f"unknown path `{path}`", "not a declared struct or field"))
		raise error(message, expr.args[0].span, label = label)
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
			# A byte-run enum's arms are literals rather than numbers, and
			# there is deliberately no integer to fall back on: `"BM"` as a
			# `u16` is 0x424D or 0x4D42 depending on endianness, and a
			# signature has no byte order (0052). They live in `byte_enums`
			# so that nothing reading `env.enums` can get a number out of
			# one by accident.
			if decl.width is not None:
				env.byte_enums[decl.name] = {
					member.name: _enum_bytes(member, decl.width)
					for member in decl.members}
				continue
			members: dict[str, int] = {}
			for member in decl.members:
				members[member.name] = evaluate(member.value, env)
			env.enums[decl.name] = members
		elif isinstance(decl, ast.ConstDecl):
			env.consts[decl.name] = _const_value(decl, env)

	return env


def _enum_bytes(member: ast.EnumMember, width: int) -> bytes:
	"""One arm of a byte-run enum, checked against the declared width."""
	if not isinstance(member.value, ast.StringLiteral):
		raise error(
			f"`{member.name}` is not a byte string",
			member.value.span,
			label = "expected a literal here",
			notes = ["a byte-run enum names spans of bytes, so every arm is "
			         "written out as one"])

	# `wellformed.check_byte_enums` has already refused a non-byte literal
	# and a width mismatch; this is the fold, and it repeats both checks
	# rather than assuming, because `build_env` is reachable from tests that
	# do not run the front end.
	held = literal_bytes(member.value.value)
	if held is None:
		raise error(
			f"`{member.name}` is not writable as bytes",
			member.value.span,
			label = "not bytes",
			notes = ["a byte-run enum names spans of bytes, so an arm is "
			         "written with `\\xNN` escapes where it is not printable"])

	if len(held) != width:
		raise error(
			f"`{member.name}` is {len(held)} byte(s) of a {width}-byte enum",
			member.value.span,
			label = f"{len(held)} byte(s) here",
			notes = [f"every arm of this enum is {width} bytes, because that "
			         f"is how wide one value of it is",
			         "an enum whose arms differ in length is a grammar, not "
			         "a value"])
	return held


def _const_value(decl: ast.ConstDecl, env: Env) -> int:
	try:
		return evaluate(decl.value, env)
	except SituError as exc:
		exc.diagnostic.notes.append(
			f"while evaluating the constant `{decl.name}`")
		raise
