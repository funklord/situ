"""Append-only building in Python: the write half of rung 2 (26.855).

C has had this since 26.610 and no other backend had any of it, so
`--layer edit` emitted `--layer view`'s output in three languages and said
nothing about it until 26.854. This is the first increment of the port and
it is deliberately one CLASS of struct rather than one feature of the
builder: every member a whole-byte fixed scalar, stated in `plain_build`
for all three languages that have one. Every other struct is named on stderr, which
is C's own stance -- a caller who asked for a builder and found their
struct missing would conclude the generator was broken.

**C's shape, not a Python reinvention of it.** The sequence is the one
`c/build.py` emits and `26.610` settled: bounds-check the remaining
capacity, write the member's bytes at the cursor, advance, and when every
member is down take a view over exactly what was written and `validate()`
it. That last step is the reason this is small -- "constraints are the
view's to state and this reuses them rather than restating them: two
checks of one schema is how they come to disagree."

**Why not call the setters instead**, which would reuse the accessor's own
arithmetic: a `reserved` member has no setter, a field inside an
authenticated region has no plain one, and a driving field's setter
invalidates the view it was written through. The builder would then need
the same decline logic the accessors have, by a second route -- and the
two backends' builders would stop being comparable, which is what the
four-way differential is for.
"""

from __future__ import annotations

from situc import ast, traverse
from situc.codegen import plain_build
from situc.codegen.c.build import (LITERAL, RUN, SCALAR, SIZE, SPAN, ZERO, Part,
                                   Shape)
from situc.codegen.plain_build import plain
from situc.codegen.python.emit import py_name
from situc.layout import BITS_PER_BYTE
from situc.resolve import ResolvedSchema
from situc import __version__

#: `Shape` is C's and is re-exported rather than rebuilt: the CLI
#: needs one to ask `refusals`, and a second construction here would
#: be a second set of inputs to that decision.
__all__ = ["generate", "plain", "declined", "Shape"]


def declined(resolved: ResolvedSchema,
		shape: Shape) -> list[tuple[str, str]]:
	"""What this builder does not build, and why, for stderr."""
	return plain_build.declined(resolved, shape, "Python")


def _room(size: str, path: str) -> list[str]:
	return [f"\tif len(out) - at < {size}:",
	        f'\t\traise BoundsError("{path}: {{}} byte(s) needed, {{}} left"',
	        f"\t\t\t.format({size}, len(out) - at))"]


def _store(part: Part, value: str) -> list[str]:
	"""One scalar's bounds check, write and advance -- C's three steps.

	**The byte order comes from the emitter's own helper**, not from a test
	here. Written as `is not LITTLE -> big` first, which is the exact
	derivation `_byte_order`'s docstring records as having been wrong twice
	already -- and it read netlink's little-endian header back as
	353637138 where 303240213 went in. One question, one predicate.

	**And the value is not CALLED.** `int({local})` was defensive and was
	the one line that could not survive a member named `int`, which
	`edges.keywords` has: the parameter shadows the builtin, so `int(int)`
	calls an integer. The annotation says what the argument is; calling it
	is the fault `test_a_member_named_for_a_builtin_is_read_and_not_called`
	is about, met from the writing side.
	"""
	from situc.codegen.python.emit import _byte_order

	placement = part.placement
	assert placement.scalar is not None
	width  = placement.scalar.bits // BITS_PER_BYTE
	order  = _byte_order(placement.endian)
	return [
		*_room(str(width), placement.path),
		f"\tout[at:at + {width}] = ({value}).to_bytes("
		f"{width}, {order}, signed={placement.scalar.signed})",
		f"\tat += {width}",
	]


def _size(part: Part, run: str) -> list[str]:
	"""A size field, from the length of the run it measures."""
	assert part.placement.scalar is not None
	path  = part.placement.path
	limit = (1 << part.placement.scalar.bits) - 1
	refuse = (f'\t\traise ConstraintError("{path}: the run is longer than '
	          'this can say")')
	if part.solved is None:
		return [f"\tif len({run}) > {limit}:", refuse,
		        *_store(part, f"len({run})")]
	scale, offset = part.solved.scale, part.solved.offset
	shift = f" + {-offset}" if offset < 0 else f" - {offset}" if offset else ""
	return [
		f"\tsolved = len({run}){shift}",
		"\tif solved < 0"
		+ (f" or solved % {scale} != 0" if scale != 1 else "") + ":",
		refuse,
		*([f"\tsolved //= {scale}"] if scale != 1 else []),
		f"\tif solved > {limit}:", refuse,
		*_store(part, "solved"),
	]


def _write(part: Part, parts: list[Part]) -> list[str]:
	"""One part, as `c/build.py` writes it."""
	path = part.placement.path
	if part.role == SCALAR:
		return _store(part, py_name(part.local))
	if part.role == SIZE:
		run = next(one.local for one in parts
		           if one.placement is part.sizes)
		return _size(part, py_name(run))
	if part.role == ZERO:
		ones = traverse.reserved_policy(part.placement.attrs) == "must_be_one"
		return [*_room(str(part.bytes), path),
		        f"\tout[at:at + {part.bytes}] = "
		        f"b\"{'\\xff' if ones else '\\x00'}\" * {part.bytes}",
		        f"\tat += {part.bytes}"]
	if part.role == LITERAL:
		return [*_room(str(part.bytes), path),
		        f"\tout[at:at + {part.bytes}] = {bytes(part.literal)!r}",
		        f"\tat += {part.bytes}"]
	local = py_name(part.local)
	if part.role == RUN:
		return [f"\tif len({local}) != {part.bytes}:",
		        f'\t\traise BoundsError("{path}: {part.bytes} byte(s) are '
		        'wanted")',
		        *_room(str(part.bytes), path),
		        f"\tout[at:at + {part.bytes}] = {local}",
		        f"\tat += {part.bytes}"]
	assert part.role == SPAN, part.role
	return [*_room(f"len({local})", path),
	        f"\tout[at:at + len({local})] = {local}",
	        f"\tat += len({local})"]


def _parameters(parts: list[Part]) -> list[str]:
	found: list[str] = []
	for part in parts:
		if part.role == SCALAR:
			found.append(f"{py_name(part.local)}: int")
		elif part.role in (RUN, SPAN):
			found.append(f"{py_name(part.local)}: bytes")
	return found


def generate(schema: ast.Schema, resolved: ResolvedSchema,
		basename: str) -> dict[str, str]:
	"""`<basename>_build.py`, or nothing where no struct qualifies.

	A rung adds files rather than changing them (0032), which is why this
	is its own module and not more of `<basename>.py`.
	"""
	shape = Shape("", "situ", resolved.structs,
	              {decl.name: decl for decl in schema.varints()})
	structs = plain(resolved, shape)
	if not structs:
		return {}

	lines = [
		f'"""Generated by situc {__version__} from {basename}.situ'
		" -- do not edit.",
		"",
		"Append-only building: the write half of rung 2. Each member goes",
		"down in schema order into backing you supply, nothing moves once",
		"written, and no view into the buffer exists while it is being",
		"filled -- which is why this needs rung 2's permission and nothing",
		"above it.",
		"",
		"A layout this cannot write forward has no builder here, and",
		"`situc build` names it. Nothing here allocates: the caller's",
		"`bytearray` is the message.",
		'"""',
		"",
		"from __future__ import annotations",
		"",
		f"from {basename} import *  # noqa: F403",
		# Named rather than left to the star import: `NATIVE_BIG` is not in
		# the base module's `__all__`, and a `native` member's write reads
		# it -- netlink's header is `native` and the generated module raised
		# `NameError` on its first line until this said so.
		#
		# ...and only WHERE one is native, because the base module defines
		# it only then: importing it unconditionally turned one NameError
		# into fifteen ImportErrors, which is the correction being wider
		# than the fault it corrected.
		f"from {basename} import Message"
		+ (", NATIVE_BIG" if any(
			one.placement.endian is ast.Endian.NATIVE
			for struct in structs
			for one in plain_build.parts(struct, shape) or ()
			if one.role in (SCALAR, SIZE)) else ""),
		"from situ_runtime import BoundsError, ConstraintError",
		"",
	]

	for struct in structs:
		held  = py_name(struct.name)
		parts = plain_build.parts(struct, shape)
		assert parts is not None
		lines.extend([
			"",
			f"def build_{held}(out: bytearray"
			+ "".join(f", {one}" for one in _parameters(parts)) + ") -> int:",
			f'\t"""Build a {struct.name} into `out` and return its length.',
			"",
			"\tOne member at a time in schema order, then the result is",
			"\tvalidated through the ordinary view before it is reported:",
			"\tconstraints are the view's to state and this reuses them",
			"\trather than restating them, two checks of one schema being",
			"\thow they come to disagree.",
			"",
			"\tRaises BoundsError where `out` is short of what the members",
			"\tneed, ConstraintError where a run is longer than its size",
			'\tfield can say, and whatever `validate` raises for the result."""',
			"\tat = 0",
		])
		for part in parts:
			lines.extend(_write(part, parts))
		lines.extend([
			"",
			"\t# The view over exactly what was written, not over `out`: a",
			"\t# longer buffer would hide a struct that came out short.",
			f"\tview = {held}.at(Message(memoryview(out)[:at]), 0"
			+ ("" if struct.layout.is_fixed_size else ", at") + ")",
			"\tview.validate()",
			"\treturn at",
		])

	return {f"{basename}_build.py": "\n".join(lines).rstrip("\n") + "\n"}
