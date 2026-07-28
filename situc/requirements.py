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
class Demand:
	"""One axis a predicate reads, and the value it insists on."""

	axis: Axis
	required: Value
	# Most demands are lower bounds: `in_place` is satisfied by anything at
	# least as strong as InPlaceSlack. A few name a value exactly, because they
	# ask for a *weak* property -- `immutable` is not satisfied by a field that
	# happens to be freely writable, and `verify_gated` is not satisfied by a
	# field that was never behind a gate.
	exact: bool = False


@dataclass(frozen=True)
class Predicate:
	name: str
	axis: Axis
	required: Value
	summary: str
	takes_argument: bool = False
	exact: bool = False
	# Further axes that must hold, beyond the primary one. A predicate is a
	# question, and some questions are about two axes at once: section 14.2
	# turns on `in_place` asking both "can this be written where it sits" and
	# "does writing it leave a tag stale", because the answers differ and the
	# difference is the whole design pressure.
	extra: tuple[Demand, ...] = ()

	@property
	def demands(self) -> tuple[Demand, ...]:
		return (Demand(self.axis, self.required, self.exact),) + self.extra


PREDICATES: dict[str, Predicate] = {
	"absolute_static": Predicate(
		"absolute_static", Axis.OFFSET, Value("AbsoluteStatic"),
		"the offset is known at compile time from the message base"),
	"static": Predicate(
		"static", Axis.OFFSET, Value("FrameStatic"),
		"the offset is known at compile time, from the message or frame base"),
	"frame_static": Predicate(
		"frame_static", Axis.OFFSET, Value("FrameStatic"),
		"the offset is fixed relative to a frame base resolved once at parse time"),
	"max_size": Predicate(
		"max_size", Axis.SIZE, Value("Bounded"),
		"the worst-case extent is known, so a static buffer can hold it",
		takes_argument=True),
	"in_place": Predicate(
		"in_place", Axis.MUTATE, Value("InPlaceSlack"),
		"the field can be written without moving anything else, and writing it "
		"leaves no tag stale",
		extra = (Demand(Axis.AUTH, Value("Uncovered"), exact=True),)),
	"in_place_dirty": Predicate(
		"in_place_dirty", Axis.MUTATE, Value("InPlaceSlack"),
		"the field can be written without moving anything else, accepting that "
		"a tag covering it must then be recomputed"),
	"no_tag_invalidation": Predicate(
		"no_tag_invalidation", Axis.AUTH, Value("Uncovered"),
		"writing the field invalidates no authentication tag", exact=True),
	"verify_gated": Predicate(
		"verify_gated", Axis.STAGE, Value("VerifyGated"),
		"no view into these bytes can be obtained before the tag verifies",
		exact=True),
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
	# `repr` is one of the thirteen axes and had no way to ask about it, which
	# left the one question a caller most often has -- can I point at these
	# bytes and read them as they are? -- unaskable. Byte order, bit packing,
	# fixed point and BCD all answer no, for four different reasons.
	"memory_identical": Predicate(
		"memory_identical", Axis.REPR, Value("MemoryIdentical"),
		"the bytes are the value: no swap, shift, scale or decode stands "
		"between them", exact=True),
	"uncovered": Predicate(
		"uncovered", Axis.AUTH, Value("Uncovered"),
		"no authentication tag covers these bytes"),
	"aligned": Predicate(
		"aligned", Axis.ALIGN, Value("Aligned"),
		"the field starts on the requested byte boundary", takes_argument=True),
	# Section 11.1 insists these two be distinguished. A byte-order-marked
	# format is not canonical -- two byte sequences encode the same value -- but
	# a writer is still deterministic, because it always emits host order plus
	# the matching marker. The consequence, stated as a rule: verify over
	# received bytes, never over re-encoded bytes.
	"deterministic_writer": Predicate(
		"deterministic_writer", Axis.CANONICAL, Value("CanonicalGiven"),
		"a writer always emits the same bytes for the same value"),
}

#: Predicates the language names and this build cannot decide, with the reason.
#:
#: These were once keyed by the phase that would implement them. Every one of
#: those phases has since landed without the predicate arriving with it, so the
#: phase number had become a promise the schedule no longer backed -- a user
#: reading "needs phase 7" would wait for something that already happened. What
#: is true of each of them is a reason, so that is what is recorded.
DEFERRED_PREDICATES = {
	"deterministic":
		"it asks about a codec's property signature rather than a field's "
		"capability vector, and nothing connects a field to the codec above it",
	"no_alloc":
		"generated code never allocates (invariant 4), so this always holds "
		"and the predicate would be a lint rather than a requirement",
	"bounded_stack":
		"it needs a stack-depth model of the generated code, which the "
		"compiler does not build",
	"no_realloc":
		"it depends on a runtime value, so it is a SITU_CHECKED check rather "
		"than a compile-time discharge, and that machinery is not wired",
}


@dataclass
class Outcome:
	"""One requirement, discharged or deferred."""

	requirement: ast.Requirement
	satisfied: bool
	detail: str
	deferred: str | None		= None
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
	reason = pending_reason(requirement.expr)
	if reason is not None:
		return Outcome(requirement, satisfied=False, deferred=reason,
		               detail=reason)

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

	entry  = resolved.find(path)
	struct = None

	if entry is not None:
		vector = entry.vector
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

	# The first demand that fails is the one reported. Predicates are ordered
	# with the primary axis first, so a field that fails on both reads the way
	# its name does; and where only the secondary one fails -- a writable field
	# under a tag -- the diagnostic is about the tag, which is the thing the
	# author has to decide about.
	failed  = None
	primary = _required_value(predicate, call)

	for demand in predicate.demands:
		required = primary if demand.axis is predicate.axis else demand.required
		actual   = vector.get(demand.axis)

		if predicate.name == "max_size":
			satisfied, actual = _within_max_size(actual, required)
		elif demand.exact:
			satisfied = actual.base == required.base
		else:
			satisfied = is_at_least(demand.axis, actual, required)

		if not satisfied:
			failed = (demand, actual, required)
			break

	if failed is None:
		return Outcome(requirement, satisfied=True,
		               detail=f"{predicate.axis.value}({path}) is "
		                      f"{vector.get(predicate.axis).render()}")

	demand, actual, required = failed

	if entry is not None:
		blame = entry.blame(demand.axis)
	else:
		# A struct's vector is the meet of its members', so the blame for a
		# struct-level failure lives in whichever members caused it.
		assert struct is not None
		blame = _member_blame(struct.entries, demand.axis, required)

	detail  = (f"{demand.axis.value}({path}) is {actual.render()}, "
	           f"required {required.render()}")
	outcome = Outcome(requirement, satisfied=False, detail=detail)
	outcome.blame = [f"{w.rule.name}: {w.effect.because}" for w in blame]
	outcome.diagnostic = _capability_failure(
		requirement, call, predicate, path, demand.axis, actual, required,
		resolved, blame)

	return outcome


def _within_max_size(actual: Value, required: Value) -> tuple[bool, Value]:
	"""`max_size(X) <= N`: the worst case must fit, and must be known.

	An Unbounded size fails whatever N is, which is the point: a region with no
	upper bound cannot be statically allocated at any size.
	"""
	limit = int(required.params[0])

	if actual.base == "Unbounded":
		return False, actual
	if actual.base == "Fixed":
		return int(actual.params[0]) <= limit, actual

	worst = int(actual.params[-1])
	return worst <= limit, actual


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
		predicate: Predicate, path: str, axis: Axis, actual: Value,
		required: Value, resolved: ResolvedSchema,
		blame: list[Weakening]) -> Diagnostic:
	"""The section 17 report: what failed, why, how far it spread, what to do."""
	notes = [
		f"{axis.value}({path}) is {actual.render()}, "
		f"required {required.render()}",
		f"`{predicate.name}` asks that {predicate.summary}",
	]

	labels = []

	for weakening in blame:
		notes.append(f"caused by: {weakening.rule.construct} -- {weakening.effect.because}")
		labels.append(Label(weakening.span, f"{weakening.rule.name} applies here"))

	if not blame:
		notes.append("caused by: the declaration itself; nothing upstream weakened it")

	radius = _blast_radius(resolved, axis, required, path)
	if radius:
		listed = ", ".join(radius[:4]) + (", ..." if len(radius) > 4 else "")
		notes.append(f"{len(radius)} other field(s) share this weakness: {listed}")

	for weakening in blame:
		if weakening.rule.remedy:
			notes.append(f"remedy: {weakening.rule.remedy}")

	return Diagnostic(
		severity = _severity(requirement),
		message  = "requirement not satisfied",
		primary  = Label(call.span, f"{axis.value} is {actual.render()}"),
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


def pending_reason(expr: ast.Expr) -> str | None:
	"""Why this expression cannot be decided, or None if it can be."""
	reasons = _reasons(expr)
	return reasons[0] if reasons else None


def _reasons(expr: ast.Expr) -> list[str]:
	found: list[str] = []

	if isinstance(expr, ast.Call):
		reason = DEFERRED_PREDICATES.get(expr.name)
		if reason is not None:
			found.append(reason)
		for arg in expr.args:
			found.extend(_reasons(arg))
	elif isinstance(expr, ast.Binary):
		found.extend(_reasons(expr.left))
		found.extend(_reasons(expr.right))
	elif isinstance(expr, ast.Unary):
		found.extend(_reasons(expr.operand))

	return found


def warnings(outcomes: list[Outcome]) -> list[Diagnostic]:
	"""Failed `assert`s, rendered as warnings."""
	return [
		outcome.diagnostic
		for outcome in outcomes
		if outcome.deferred is None and not outcome.satisfied and outcome.diagnostic
	]


def deferrals(outcomes: list[Outcome]) -> list[Diagnostic]:
	"""Requirements this build could not decide, grouped by why."""
	from situc.unparse import expr_to_source

	by_reason: dict[str, list[Outcome]] = {}
	for outcome in outcomes:
		if outcome.deferred is not None:
			by_reason.setdefault(outcome.deferred, []).append(outcome)

	rendered = []
	for reason in sorted(by_reason):
		group  = by_reason[reason]
		listed = ", ".join(
			f"`{expr_to_source(outcome.requirement.expr)}`" for outcome in group)
		rendered.append(Diagnostic(
			severity = Severity.NOTE,
			message  = f"{len(group)} requirement{'s' if len(group) != 1 else ''} "
			           f"not checked by this build",
			primary  = Label(group[0].requirement.span, "first of them here"),
			notes    = [listed, reason],
		))

	return rendered
