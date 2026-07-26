"""Discharge of `require` and `assert` declarations.

Phase 2 handles only what a static layout can answer: predicates over `size()`,
`offset()` and `count()`, and the comparisons built from them. The capability
predicates of section 16 -- `absolute_static`, `in_place`, `canonical` and the
rest -- need the lattice, and are deferred to phase 3 with a diagnostic saying
so rather than silently passing.

Silently passing would be the worst possible behaviour here. A schema states its
budget in requirements precisely so a later edit cannot regress it; a
requirement that quietly does nothing is worse than no requirement at all.
"""

from __future__ import annotations

from dataclasses import dataclass

from situc import ast
from situc.diagnostics import Diagnostic, Label, Severity, SituError, error
from situc.expr import Env, evaluate
from situc.layout import SchemaLayout

# Capability predicates and the phase that will discharge each. Listed so an
# unimplemented one names its phase instead of being mistaken for a builtin.
CAPABILITY_PREDICATES = {
	"absolute_static":	3,
	"frame_static":		5,
	"static":		3,
	"in_place":		3,
	"in_place_dirty":	8,
	"immutable":		3,
	"random_access":	6,
	"stable_address":	3,
	"canonical":		3,
	"deterministic":	7,
	"deterministic_writer":	4,
	"aligned":		3,
	"atomic":		3,
	"no_tag_invalidation":	8,
	"verify_gated":		8,
	"uncovered":		8,
	"no_alloc":		4,
	"bounded_stack":	4,
	"max_size":		5,
	"no_realloc":		5,
}


@dataclass
class Outcome:
	"""One requirement, discharged or deferred.

	`deferred` names the phase that will be able to decide this requirement, and
	is None when the requirement was decided here. Section 16 forbids *silently*
	downgrading a check; carrying the distinction and reporting it is what makes
	the deferral not silent.
	"""

	requirement: ast.Requirement
	satisfied: bool
	detail: str
	deferred: int | None = None

	@property
	def is_error(self) -> bool:
		return (self.deferred is None
		        and not self.satisfied
		        and self.requirement.kind is ast.RequirementKind.REQUIRE)


def discharge(schema: ast.Schema, layout: SchemaLayout) -> list[Outcome]:
	"""Evaluate every requirement, raising on the first failed `require`.

	An `assert` that fails is a warning and does not stop the build, which is
	the whole distinction between the two keywords (section 16).

	A requirement resting on a capability predicate is deferred rather than
	refused. Refusing would make the capability map -- the deliverable of this
	phase -- unobtainable for any schema that states a budget, which is every
	schema worth writing. Deferral is reported on every run, so nothing about it
	is silent.
	"""
	env      = layout.env.with_layout(layout.lookup)
	outcomes = []

	for requirement in schema.requirements():
		outcome = _discharge_one(requirement, env)
		outcomes.append(outcome)
		if outcome.is_error:
			raise _failure(outcome)

	return outcomes


def _discharge_one(requirement: ast.Requirement, env: Env) -> Outcome:
	phase = pending_phase(requirement.expr)
	if phase is not None:
		return Outcome(requirement, satisfied=False, deferred=phase,
		               detail=f"needs the capability lattice; phase {phase}")

	value  = evaluate(requirement.expr, env)
	detail = _detail(requirement.expr, env)
	return Outcome(requirement, satisfied=bool(value), detail=detail)


def pending_phase(expr: ast.Expr) -> int | None:
	"""The earliest phase that could decide this expression, or None if now.

	A requirement mentioning several predicates reports the latest of them,
	because that is when the whole requirement becomes checkable.
	"""
	phases = _phases(expr)
	return max(phases) if phases else None


def _phases(expr: ast.Expr) -> list[int]:
	found: list[int] = []

	if isinstance(expr, ast.Call):
		phase = CAPABILITY_PREDICATES.get(expr.name)
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


def _detail(expr: ast.Expr, env: Env) -> str:
	"""Restate a failed comparison with its operands evaluated.

	"size(Header) == 10" is not a diagnostic; "size(Header) is 9, required 10"
	is. The blame chain that turns this into the full section 17 report arrives
	in phase 3.
	"""
	if isinstance(expr, ast.Binary) and expr.op in ("==", "!=", "<", ">", "<=", ">="):
		from situc.unparse import expr_to_source
		left  = evaluate(expr.left, env)
		right = evaluate(expr.right, env)
		return f"{expr_to_source(expr.left)} is {left}, {expr.op} {right} required"

	return "evaluates to false"


def _failure(outcome: Outcome) -> SituError:
	requirement = outcome.requirement
	return SituError(Diagnostic(
		severity = Severity.ERROR,
		message  = "requirement not satisfied",
		primary  = Label(requirement.span, outcome.detail),
		notes    = [
			"a `require` is a build-time gate; use `assert` to record intent "
			"without failing the build (project.md section 16)",
		],
	))


def warnings(outcomes: list[Outcome]) -> list[Diagnostic]:
	"""Failed `assert`s, rendered as warnings."""
	return [
		Diagnostic(
			severity = Severity.WARNING,
			message  = "assertion not satisfied",
			primary  = Label(outcome.requirement.span, outcome.detail),
		)
		for outcome in outcomes
		if outcome.deferred is None and not outcome.satisfied
	]


def deferrals(outcomes: list[Outcome]) -> list[Diagnostic]:
	"""Requirements this build could not decide, grouped by phase.

	Reported every run. A requirement is a schema's budget, and one that quietly
	does nothing is worse than no requirement at all.
	"""
	from situc.unparse import expr_to_source

	by_phase: dict[int, list[Outcome]] = {}
	for outcome in outcomes:
		if outcome.deferred is not None:
			by_phase.setdefault(outcome.deferred, []).append(outcome)

	rendered = []
	for phase in sorted(by_phase):
		group = by_phase[phase]
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
