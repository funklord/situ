"""Code generation backends.

C is primary (project.md section 20.1). Rust is phase 11 and must not be
started before the C backend has proven the lattice.
"""

from __future__ import annotations

from situc import ast, traverse
from situc.diagnostics import not_yet_implemented


def refuse_parameters(schema: ast.Schema) -> None:
	"""Stop a build that would silently read a parameter off the buffer.

	One function for every generator that has not learned to pass an
	argument, for `traverse.parameters`' own reason: a copy of this refusal
	per generator is a copy to reword separately.

	It covered the four backends and the packer too until 2026-09-17, and
	those six now take their arguments (26.393 to 26.397). What is left are
	the generators that emit a SECOND artifact over a schema -- the differ,
	the C checks, fuzz and tamper harnesses, and the three `derived`
	emitters. Each builds calls of its own and would have to thread an
	argument through them; none is on a `situc build` path, so what a
	parameter costs there is `situc gen-checks` and its siblings declining,
	not a broken build.

	A whole-schema refusal rather than a note beside the parameter, because
	a note leaves every expression that READS it still emitting that read:
	`_over_fields` reads a member where it sits, and a parameter sits at the
	offset of the member after it. A size expression naming one compiles
	cleanly and measures the wrong byte, which is the shape 26.32 rates
	worst.
	"""
	held = traverse.parameters(schema)
	if not held:
		return

	struct, member = held[0]
	raise not_yet_implemented(
		f"`parameter {member.name}` in `{struct}`", member.span, 12,
		[
			"a parameter is an argument the caller supplies, and this "
			"generator does not pass one -- so a read of it would come "
			"off the buffer at its offset, which is where the member "
			"after it begins (decision 0050)",
			"the four `situc build` backends DO take their arguments, so "
			"a view for this schema is available; what declines here is "
			"the second artifact this command emits, which builds calls "
			"of its own",
		])
