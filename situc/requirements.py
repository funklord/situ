"""Discharge of `require` and `assert` declarations.

Two keywords, deliberately distinguished (project.md section 16): `require` is a
build-time gate whose failure is an error, `assert` is the same check reported
as a warning. A schema states its budget in requirements so that a later edit
cannot quietly regress it.

Every failure carries a blame chain. Section 26 invariant 3 is categorical: a
diagnostic without one is a bug. A missing capability with no explanation is
hostile, and the explanation is the product.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from situc import ast
from situc.capability import Axis, Value, is_at_least
from situc.diagnostics import Diagnostic, Label, Severity, SituError, error
from situc.expr import Env, evaluate, path_text
from situc.propagate import Resolved, Weakening
from situc.resolve import ResolvedSchema

# Predicates decidable from the static subset, each naming the axis it reads and
# the value it demands. Data rather than branches, for the same reason the
# propagation table is.
@dataclass(frozen=True)
class Predicate:
	name: str
	axis: Axis
	required: Value
	summary: str
	takes_argument: bool = False
	# Most predicates are lower bounds: `in_place` is satisfied by anything at
	# least as strong as InPlaceSlack. A few name a value exactly, because they
	# ask for a *weak* property -- `immutable` is not satisfied by a field that
	# happens to be freely writable.
	exact: bool = False


PREDICATES: dict[str, Predicate] = {
	"absolute_static": Predicate(
		"absolute_static", Axis.OFFSET, Value("AbsoluteStatic"),
		"the offset is known at compile time from the message base"),
	"static": Predicate(
		"static", Axis.OFFSET, Value("FrameStatic"),
		"the offset is known at compile time, from the message or frame base"),
	"in_place": Predicate(
		"in_place", Axis.MUTATE, Value("InPlaceSlack"),
		"the field can be written without moving anything else"),
	"immutable": Predicate(
		"immutable", Axis.MUTATE, Value("Immutable"),
		"the field cannot be written at all", exact=True),
	"stable_address": Predicate(
		"stable_address", Axis.ADDRESS, Value("Stable"),
		"a pointer to the field stays valid"),
	"random_access": Predicate(
		"random_access", Axis.ACCESS, Value("Random"),
		"element N can be reached without walking the ones before it"),
	"canonical": Predicate(
		"canonical", Axis.CANONICAL, Value("Canonical"),
		"exactly one byte sequence encodes a given value"),
	"atomic": Predicate(
		"atomic", Axis.ATOMIC, Value("AtomicWord"),
		"the access fits in a single instruction"),
	"uncovered": Predicate(
		"uncovered", Axis.AUTH, Value("Uncovered"),
		"no authentication tag covers these bytes"),
	"aligned": Predicate(
		"aligned", Axis.ALIGN, Value("Aligned"),
		"the field starts on the requested byte boundary", takes_argument=True),
}

# Predicates whose axis exists but which need a construct from a later phase.
DEFERRED_PREDICATES = {
	"frame_static":		5,
	"in_place_dirty":	8,
	"deterministic":	7,
	"deterministic_writer":	4,
	"no_tag_invalidation":	8,
	"verify_gated":		8,
	"no_alloc":		4,
	"bounded_stack":	4,
	"max_size":		5,
	"no_realloc":		5,
}


@dataclass
class Outcome:
	"""One requirement, discharged or deferred."""

	requirement: ast.Requirement
	satisfied: bool
	detail: str
	deferred: int | None		= None
	blame: list[str]		= field(default_factory=list)
	diagnostic: Diagnostic | None	= None

	@property
	def is_error(self) -> bool:
		return (self.deferred is None
		        and not self.satisfied
		        and self.requirement.kind is ast.RequirementKind.REQUIRE)


def discharge(schema: ast.Schema, resolved: ResolvedSchema) -> list[Outcome]:
	env      = resolved.layout.env.with_layout(resolved.layout.lookup)
	outcomes = []

	for requirement in schema.requirements():
		outcome = _discharge_one(requirement, resolved, env)
		outcomes.append(outcome)
		if outcome.is_error:
			assert outcome.diagnostic is not None
			raise SituError(outcome.diagnostic)

	return outcomes


def _discharge_one(requirement: ast.Requirement, resolved: ResolvedSchema,
		env: Env) -> Outcome:
	phase = pending_phase(requirement.expr)
	if phase is not None:
		return Outcome(requirement, satisfied=False, deferred=phase,
		               detail=f"needs a construct from phase {phase}")

	predicate_call = _capability_call(requirement.expr)
	if predicate_call is not None:
		return _discharge_capability(requirement, predicate_call, resolved)

	return _discharge_arithmetic(requirement, env)


def _capability_call(expr: ast.Expr) -> ast.Call | None:
	if isinstance(expr, ast.Call) and expr.name in PREDICATES:
		return expr
	return None


def _discharge_arithmetic(requirement: ast.Requirement, env: Env) -> Outcome:
	value  = evaluate(requirement.expr, env)
	detail = _arithmetic_detail(requirement.expr, env)
	outcome = Outcome(requirement, satisfied=bool(value), detail=detail)

	if not outcome.satisfied:
		outcome.diagnostic = Diagnostic(
			severity = _severity(requirement),
			message  = "requirement not satisfied",
			primary  = Label(requirement.span, detail),
			notes    = ["a `require` is a build-time gate; use `assert` to record "
			            "intent without failing the build (section 16)"],
		)

	return outcome


def _discharge_capability(requirement: ast.Requirement, call: ast.Call,
		resolved: ResolvedSchema) -> Outcome:
	predicate = PREDICATES[call.name]
	path      = path_text(call.args[0]) if call.args else None

	if path is None:
		raise error(
			f"`{call.name}` takes a field or struct path",
			call.span,
			label = "expected a name such as `Header` or `Header.seq`",
		)

	required = _required_value(predicate, call)
	entry    = resolved.find(path)

	if entry is not None:
		vector = entry.vector
		blame  = entry.blame(predicate.axis)
	else:
		struct = resolved.find_struct(path)
		if struct is None:
			raise error(
				f"unknown path `{path}`",
				call.args[0].span,
				label = "not a declared struct or field",
				notes = [f"known paths: {_nearby(resolved, path)}"],
			)
		vector = struct.vector
		# A struct's vector is the meet of its members', so the blame for a
		# struct-level failure lives in whichever members caused it.
		blame = _member_blame(struct.entries, predicate.axis, required)

	actual = vector.get(predicate.axis)

	if predicate.exact:
		satisfied = actual.base == required.base
	else:
		satisfied = is_at_least(predicate.axis, actual, required)

	detail  = (f"{predicate.axis.value}({path}) is {actual.render()}, "
	           f"required {required.render()}")
	outcome = Outcome(requirement, satisfied=satisfied, detail=detail)

	if not satisfied:
		outcome.blame = [f"{w.rule.name}: {w.effect.because}" for w in blame]
		outcome.diagnostic = _capability_failure(
			requirement, call, predicate, path, actual, required, resolved, blame)

	return outcome


def _member_blame(entries: list[Resolved], axis: Axis, required: Value) -> list[Weakening]:
	"""Weakenings inside a struct that account for its own weakened axis."""
	found: list[Weakening] = []
	for entry in entries:
		if is_at_least(axis, entry.vector.get(axis), required):
			continue
		found.extend(entry.blame(axis))
	return found


def _required_value(predicate: Predicate, call: ast.Call) -> Value:
	if not predicate.takes_argument:
		return predicate.required

	if len(call.args) != 2:
		raise error(
			f"`{predicate.name}` takes a path and a value",
			call.span,
			label = f"expected `{predicate.name}(X, n)`",
		)

	argument = call.args[1]
	if not isinstance(argument, ast.IntLiteral):
		raise error(
			f"`{predicate.name}` needs a literal second argument",
			argument.span,
			label = "expected an integer",
		)

	return Value(predicate.required.base, (str(argument.value),))


def _capability_failure(requirement: ast.Requirement, call: ast.Call,
		predicate: Predicate, path: str, actual: Value, required: Value,
		resolved: ResolvedSchema, blame: list[Weakening]) -> Diagnostic:
	"""The section 17 report: what failed, why, how far it spread, what to do."""
	notes = [
		f"{predicate.axis.value}({path}) is {actual.render()}, "
		f"required {required.render()}",
		f"`{predicate.name}` asks that {predicate.summary}",
	]

	labels = []

	for weakening in blame:
		notes.append(f"caused by: {weakening.rule.construct} -- {weakening.effect.because}")
		labels.append(Label(weakening.span, f"{weakening.rule.name} applies here"))

	if not blame:
		notes.append("caused by: the declaration itself; nothing upstream weakened it")

	radius = _blast_radius(resolved, predicate.axis, required, path)
	if radius:
		listed = ", ".join(radius[:4]) + (", ..." if len(radius) > 4 else "")
		notes.append(f"{len(radius)} other field(s) share this weakness: {listed}")

	for weakening in blame:
		if weakening.rule.remedy:
			notes.append(f"remedy: {weakening.rule.remedy}")

	return Diagnostic(
		severity = _severity(requirement),
		message  = "requirement not satisfied",
		primary  = Label(call.span, f"{predicate.axis.value} is {actual.render()}"),
		labels   = labels,
		notes    = notes,
	)


def _blast_radius(resolved: ResolvedSchema, axis: Axis, required: Value,
		exclude: str) -> list[str]:
	"""Other fields failing the same requirement.

	Section 17 asks for this: a weakening that cost one field a capability has
	usually cost several, and knowing how many is what tells an author whether
	to reorder the schema or live with it.
	"""
	return [
		entry.placement.path
		for struct in resolved.structs.values()
		for entry in struct.entries
		if entry.placement.path != exclude
		and not is_at_least(axis, entry.vector.get(axis), required)
	]


def _severity(requirement: ast.Requirement) -> Severity:
	return (Severity.ERROR if requirement.kind is ast.RequirementKind.REQUIRE
	        else Severity.WARNING)


def _nearby(resolved: ResolvedSchema, path: str) -> str:
	head   = path.partition(".")[0]
	known  = [p for p in resolved.paths() if p.startswith(head + ".")]
	listed = known[:6] or sorted(resolved.structs)[:6]
	return ", ".join(listed) if listed else "none"


def _arithmetic_detail(expr: ast.Expr, env: Env) -> str:
	if isinstance(expr, ast.Binary) and expr.op in ("==", "!=", "<", ">", "<=", ">="):
		from situc.unparse import expr_to_source
		left  = evaluate(expr.left, env)
		right = evaluate(expr.right, env)
		return f"{expr_to_source(expr.left)} is {left}, {expr.op} {right} required"

	return "evaluates to false"


def pending_phase(expr: ast.Expr) -> int | None:
	"""The earliest phase that could decide this expression, or None if now."""
	phases = _phases(expr)
	return max(phases) if phases else None


def _phases(expr: ast.Expr) -> list[int]:
	found: list[int] = []

	if isinstance(expr, ast.Call):
		phase = DEFERRED_PREDICATES.get(expr.name)
		if phase is not None:
			found.append(phase)
		for arg in expr.args:
			found.extend(_phases(arg))
	elif isinstance(expr, ast.Binary):
		found.extend(_phases(expr.left))
		found.extend(_phases(expr.right))
	elif isinstance(expr, ast.Unary):
		found.extend(_phases(expr.operand))

	return found


def warnings(outcomes: list[Outcome]) -> list[Diagnostic]:
	"""Failed `assert`s, rendered as warnings."""
	return [
		outcome.diagnostic
		for outcome in outcomes
		if outcome.deferred is None and not outcome.satisfied and outcome.diagnostic
	]


def deferrals(outcomes: list[Outcome]) -> list[Diagnostic]:
	"""Requirements this build could not decide, grouped by phase."""
	from situc.unparse import expr_to_source

	by_phase: dict[int, list[Outcome]] = {}
	for outcome in outcomes:
		if outcome.deferred is not None:
			by_phase.setdefault(outcome.deferred, []).append(outcome)

	rendered = []
	for phase in sorted(by_phase):
		group  = by_phase[phase]
		listed = ", ".join(
			f"`{expr_to_source(outcome.requirement.expr)}`" for outcome in group)
		rendered.append(Diagnostic(
			severity = Severity.NOTE,
			message  = f"{len(group)} requirement{'s' if len(group) != 1 else ''} "
			           f"not checked by this build; needs phase {phase}",
			primary  = Label(group[0].requirement.span, "first of them here"),
			notes    = [listed],
		))

	return rendered
