"""Code generation backends.

C is primary (project.md section 20.1). Rust is phase 11 and must not be
started before the C backend has proven the lattice.
"""

from __future__ import annotations

from situc import ast, traverse
from situc.diagnostics import not_yet_implemented


def refuse_parameters(schema: ast.Schema) -> None:
	"""Stop a build that would silently read a parameter off the buffer.

	One function for all four backends and the packer, for
	`traverse.parameters`' own reason: five copies of this refusal is five
	things to reword separately. It goes when a view learns to carry an
	argument, which is what 0050's status line is waiting on.

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
			"a parameter is an argument the caller supplies, and no view "
			"carries one yet -- so an accessor for it would read the "
			"buffer at its offset, which is where the member after it "
			"begins (decision 0050)",
			"the construct parses, and `situc doc`, `situc dump` and the "
			"layout all describe it; what is missing is the view "
			"constructor, the walker's `acquire` and the dissector's "
			"preference",
		])
