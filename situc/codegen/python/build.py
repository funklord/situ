"""Append-only building in Python: the write half of rung 2 (26.855).

C has had this since 26.610 and no other backend had any of it, so
`--layer edit` emitted `--layer view`'s output in three languages and said
nothing about it until 26.854. This is the first increment of the port and
it is deliberately one CLASS of struct rather than one feature of the
builder: every member a whole-byte fixed scalar, which is 48 of the 157
structs C's builder serves. Every other struct is named on stderr, which
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

from situc import ast
from situc.codegen.c.build import Shape, refusals
from situc.codegen.python.emit import py_name
from situc.layout import BITS_PER_BYTE, Placement
from situc.resolve import ResolvedSchema, ResolvedStruct
from situc.traverse import is_own_member, local_name
from situc import __version__

#: `Shape` is C's and is re-exported rather than rebuilt: the CLI
#: needs one to ask `refusals`, and a second construction here would
#: be a second set of inputs to that decision.
__all__ = ["generate", "plain", "declined", "Shape"]


def _is_plain(struct: ResolvedStruct) -> bool:
	"""Every member a whole-byte fixed scalar at a static offset.

	The class this increment serves. It excludes a bit-packed field for a
	reason worth naming rather than deferring: the packed run is written
	zeroed and then set in place, which is a second write to a byte this
	builder has already advanced past, and append-only is the property the
	rung was given permission for.
	"""
	seen = False
	for entry in struct.entries:
		placement = entry.placement
		if not is_own_member(struct, placement):
			continue
		seen = True
		scalar = placement.scalar
		if scalar is None or placement.kind != "field":
			return False
		if placement.array_count is not None or placement.sized_by is not None:
			return False
		if placement.radix is not None or placement.varint is not None:
			return False
		if placement.delimiters or scalar.is_bit_packed:
			return False
		if placement.offset_bits is None or not placement.size_bits:
			return False		# a zero-width member has nothing to write

		if placement.offset_bits % BITS_PER_BYTE \
				or placement.size_bits % BITS_PER_BYTE:
			return False
		if placement.parameter or placement.since is not None:
			return False
	return seen


def plain(resolved: ResolvedSchema, shape: Shape) -> list[ResolvedStruct]:
	"""The structs this increment builds, in declaration order.

	Intersected with C's own set rather than computed alone: a struct C
	refuses is one 26.610 decided a builder cannot serve, and a second
	opinion here would be a second rule to keep in step.
	"""
	refused = {name for name, _why in refusals(resolved, shape)}
	return [struct for struct in resolved.structs.values()
	        if struct.name not in refused and _is_plain(struct)]


def declined(resolved: ResolvedSchema,
		shape: Shape) -> list[tuple[str, str]]:
	"""What this increment does not build, and why, for stderr.

	C's refusals pass through unchanged -- they are the same decisions --
	and a struct C serves that this does not is named with the class it
	falls outside, so the increment's own boundary is readable rather than
	inferred from an absence.
	"""
	out = list(refusals(resolved, shape))
	refused = {name for name, _why in out}
	for struct in resolved.structs.values():
		if struct.name in refused or _is_plain(struct):
			continue
		out.append((struct.name,
		            "Python's builder takes a struct whose every member is "
		            "a whole-byte fixed scalar so far; this one is not "
		            "(26.855)"))
	return out


def _members(struct: ResolvedStruct) -> list[Placement]:
	return [entry.placement for entry in struct.entries
	        if is_own_member(struct, entry.placement)]


def _write(placement: Placement, local: str) -> list[str]:
	"""One member's bounds check, write and advance -- C's three steps.

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

	width  = (placement.size_bits or 0) // BITS_PER_BYTE
	order  = _byte_order(placement.endian)
	signed = bool(placement.scalar and placement.scalar.signed)
	return [
		f"\tif len(out) - at < {width}:",
		f'\t\traise BoundsError("{placement.path}: {width} byte(s) needed,"',
		'\t\t\tf" {len(out) - at} left")',
		f"\tout[at:at + {width}] = {local}.to_bytes("
		f"{width}, {order}, signed={signed})",
		f"\tat += {width}",
	]


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
		f"from {basename} import Message, BoundsError"
		+ (", NATIVE_BIG" if any(
			one.endian is ast.Endian.NATIVE
			for struct in structs for one in _members(struct)) else ""),
		"",
	]

	for struct in structs:
		members = _members(struct)
		names   = [py_name(local_name(struct, one)) for one in members]
		held    = py_name(struct.name)
		lines.extend([
			"",
			f"def build_{held}(out: bytearray, "
			+ ", ".join(f"{one}: int" for one in names) + ") -> int:",
			f'\t"""Build a {struct.name} into `out` and return its length.',
			"",
			"\tOne member at a time in schema order, then the result is",
			"\tvalidated through the ordinary view before it is reported:",
			"\tconstraints are the view's to state and this reuses them",
			"\trather than restating them, two checks of one schema being",
			"\thow they come to disagree.",
			"",
			"\tRaises BoundsError where `out` is short of what the members",
			'\tneed, and whatever `validate` raises for a result the schema',
			'\trefuses."""',
			"\tat = 0",
		])
		for placement, local in zip(members, names):
			lines.extend(_write(placement, local))
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
