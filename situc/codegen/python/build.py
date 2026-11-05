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

from collections.abc import Mapping

from situc import ast, traverse
from situc.codegen import plain_build
from situc.codegen.c.build import (ARMTAG, BITS, ENDED, LITERAL, NESTED, PAD,
                                   PEEKED, REPEAT, RUN, SCALAR, SIZE, SPAN, TAG, VARINT,
                                   ZERO, Part, Shape)
from situc.codegen.plain_build import plain
from situc.codegen.python.emit import py_name
from situc.layout import BITS_PER_BYTE
from situc.resolve import ResolvedSchema, ResolvedStruct
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
	return [f"\tif len(situ_out) - situ_at < {size}:",
	        f'\t\traise BoundsError("{path}: {{}} byte(s) needed, {{}} left"',
	        f"\t\t\t.format({size}, len(situ_out) - situ_at))"]


def _bit_store(part: Part, value: str) -> list[str]:
	"""Set a field inside a group `_bits` zeroed; the cursor stays put.

	Through a view over the group, so the assembly is the runtime's and
	not a second copy of it.
	"""
	held = part.placement
	assert held.scalar is not None
	msb = traverse.bit_extractor(held.scalar, held) == "msb"
	return [f"\tView(Message(memoryview(situ_out)), situ_at, len(situ_out) - situ_at)"
	        f"._set_bits({part.bit_at}, {held.size_bits}, {value}, msb={msb})"]


def _varint(part: Part, parts: list[Part]) -> list[str]:
	"""A number as wide as its own value.

	The declaration bounds the value, not the bytes, and the runtime's `put`
	writes the minimal encoding, which satisfies a `minimal` declaration by
	construction. A size field states what it measures, as any other does.
	"""
	assert part.declared is not None
	most = (1 << part.value) - 1
	big  = part.declared.encoding is ast.VarintEncoding.BE128
	tail = (f", {part.declared.max_bytes}, {part.declared.terminal_bits}"
	        if big else "")
	path = part.placement.path
	call = "varint_be_put" if big else "varint_put"
	if part.sizes is not None:
		run   = next(one.local for one in parts
		             if one.placement is part.sizes)
		held  = f"len({py_name(run)})"
		value = held
	elif part.minimal:
		held  = f"{py_name(part.local)} & 0xFFFFFFFFFFFFFFFF"
		value = f"zigzag_encode({py_name(part.local)})"
	else:
		held  = f"{py_name(part.local)} & 0xFFFFFFFFFFFFFFFF"
		value = held
	return [
		*([f"\tif {held} > {most}:",
		   f'\t\traise ConstraintError("{path}: wider than it may be")']
		  if part.value < 64 else []),
		f"\tsitu_used = {call}(situ_out, situ_at, {value}{tail})",
		"\tif situ_used == 0:",
		f'\t\traise BoundsError("{path}: no room")',
		"\tsitu_at += situ_used",
	]


def _nested(part: Part) -> list[str]:
	"""One whole inner struct the caller built.

	Its extent is its own to state, so the writer asks rather than trusting
	the length it was given: three bytes too many shift every member after
	it. And where its first byte is a peeked discriminant it has to say
	which arm this is, or the bytes are a valid message of another arm.
	"""
	local = py_name(part.local)
	path  = part.placement.path
	inner = f"situ_schema.{py_name(part.placement.type_name or '')}"
	refuse = f'\t\traise ConstraintError("{path}: not one whole {inner}")'
	return [
		*([] if not part.others and part.value <= 0 else [
			f"\tif not {local}:", refuse,
			(f"\tif {local}[0] != {part.value}:" if part.value >= 0
			 else f"\tif {local}[0] in {tuple(part.others)!r}:"),
			refuse]),
		"\ttry:",
		f"\t\tsitu_need = {inner}.required({local})",
		"\texcept TruncatedError:",
		"\t\tsitu_need = -1",
		f"\tif situ_need != len({local}):", refuse,
		*_room(f"len({local})", path),
		f"\tsitu_out[situ_at:situ_at + len({local})] = {local}",
		f"\tsitu_at += len({local})",
	]


def _walk(structs: Mapping[str, ResolvedStruct], part: Part,
		parts: list[Part]) -> list[str]:
	"""Step through a run of inner structs the caller built.

	C's `_walk`, and its reasons: the element's own `required` says where
	one ends, a zero-byte element would never advance the loop, and a
	delimiter breaks the run only at an element boundary, where the reader
	would stop. An element the buffer ended rather than `required` measured
	is taken on approval, and a failure there means the bytes are not a
	whole number of elements.
	"""
	local  = py_name(part.local)
	path   = part.placement.path
	held   = part.placement.type_name or ""
	inner  = f"situ_schema.{py_name(held)}"
	framed = not structs[held].layout.is_fixed_size
	count  = plain_build.counts_elements(part, parts)
	refuse = f'raise ConstraintError("{path}: not whole elements")'
	return [
		*(["\tsitu_count = 0"] if count else []),
		"\tsitu_scan = 0",
		f"\tsitu_elements = Message(bytearray({local}))",
		f"\twhile situ_scan < len({local}):",
		*([f"\t\tif {local}[situ_scan:situ_scan + {len(part.delimiter)}]"
		   f" == {part.delimiter!r}:",
		   f"\t\t\t{refuse}"] if part.delimiter else []),
		"\t\tsitu_tail = False",
		"\t\ttry:",
		f"\t\t\tsitu_need = {inner}.required({local}[situ_scan:])",
		"\t\texcept TruncatedError:",
		f"\t\t\tsitu_need = len({local}) - situ_scan",
		"\t\t\tsitu_tail = True",
		"\t\tif situ_need == 0:",
		f"\t\t\t{refuse}",
		"\t\ttry:",
		f"\t\t\tsitu_element = {inner}.at(situ_elements, situ_scan"
		+ (f", len({local}) - situ_scan" if framed else "") + ")",
		"\t\texcept SituError as situ_refused:",
		f"\t\t\t{refuse} from situ_refused",
		"\t\ttry:",
		"\t\t\tsitu_element.validate()",
		"\t\texcept SituError:",
		"\t\t\tif situ_tail:",
		f"\t\t\t\t{refuse} from None",
		"\t\t\traise",
		"\t\tsitu_scan += situ_need",
		*(["\t\tsitu_count += 1"] if count else []),
	]


def _needle(part: Part) -> list[str]:
	"""The delimiter that ends a run, and the cursor past it."""
	wide = len(part.delimiter)
	return [*_room(f"{wide}", part.placement.path),
	        f"\tsitu_out[situ_at:situ_at + {wide}] = {part.delimiter!r}",
	        f"\tsitu_at += {wide}"]


def _ended(part: Part) -> list[str]:
	"""Bytes, then the delimiter that ends them.

	C's two forms. With no escape a run holding the delimiter would read
	back short, and is refused. With one the caller hands over content, and
	exactly two kinds of byte carry the escape: the delimiter's first byte
	where it begins one, and the escape itself.
	"""
	local  = py_name(part.local)
	path   = part.placement.path
	wide   = len(part.delimiter)
	needle = repr(part.delimiter)
	escape = part.placement.delimiter_escape
	if escape is None:
		return [f"\tif {needle} in {local}:",
		        f'\t\traise ConstraintError("{path}: holds its delimiter")',
		        *_room(f"len({local})", path),
		        f"\tsitu_out[situ_at:situ_at + len({local})] = {local}",
		        f"\tsitu_at += len({local})",
		        *_needle(part)]
	return [f"\tfor situ_scan in range(len({local})):",
	        f"\t\tif {local}[situ_scan] == {escape} or "
	        f"{local}[situ_scan:situ_scan + {wide}] == {needle}:",
	        *(f"\t\t{line}" for line in _room("1", path)),
	        f"\t\t\tsitu_out[situ_at] = {escape}",
	        "\t\t\tsitu_at += 1",
	        *(f"\t{line}" for line in _room("1", path)),
	        f"\t\tsitu_out[situ_at] = {local}[situ_scan]",
	        "\t\tsitu_at += 1",
	        *_needle(part)]


def _repeat(structs: Mapping[str, ResolvedStruct], part: Part,
		parts: list[Part]) -> list[str]:
	local = py_name(part.local)
	path  = part.placement.path
	return [
		*([] if plain_build.walked_at_size(part, parts)
		  else _walk(structs, part, parts)),
		*([f"\tif situ_count != {part.digits}:",
		   f'\t\traise ConstraintError("{path}: not {part.digits} elements")']
		  if part.digits > 0 else []),
		*_room(f"len({local})", path),
		f"\tsitu_out[situ_at:situ_at + len({local})] = {local}",
		f"\tsitu_at += len({local})",
		*(_needle(part) if part.delimiter else []),
	]


def _bits(part: Part, parts: list[Part]) -> list[str]:
	"""One field of a byte several share.

	The group is zeroed when it opens, which is what writes `must_be_zero`,
	and the bytes are spent when it closes. A value wider than its field is
	refused rather than masked. Compared as C compares it, an unsigned
	64-bit number, so a negative value is refused as it is there.
	"""
	held  = part.placement
	path  = held.path
	limit = (1 << (held.size_bits or 0)) - 1
	lines = [*_room(str(part.bytes), path),
	         f"\tsitu_out[situ_at:situ_at + {part.bytes}] = bytes({part.bytes})"] \
		if part.opens else []
	if part.sizes is not None:
		run = next(one.local for one in parts
		           if one.placement is part.sizes)
		lines += _size(part, f"len({py_name(run)})")
	elif part.stated is not None:
		lines += _bit_store(part, str(part.stated))
	elif plain_build.takes_value(part):
		local = py_name(part.local)
		lines += [*([f"\tif {local} in {tuple(part.others)!r}:",
		             f'\t\traise ConstraintError("{path}: reads as something else")']
		            if part.others else []),
		          f"\tif {local} & 0xFFFFFFFFFFFFFFFF > {limit}:",
		          f'\t\traise ConstraintError("{path}: wider than its field")',
		          *_bit_store(part, local)]
	elif plain_build.stated_ones(part):
		lines += _bit_store(part, str(limit))
	if part.closes:
		lines.append(f"\tsitu_at += {part.bytes}")
	return lines


def _digits(part: Part, value: str) -> list[str]:
	"""A number written as text: `digits` wide, or as few as it needs.

	C's `_value`. The fixed form pads with zeros, and the minimal form is
	that with the width counted first, then the byte that ends it. A
	negative value's digits are its magnitude's, and the sign is a byte more.
	"""
	assert part.placement.scalar is not None
	path   = part.placement.path
	call   = "format_int" if part.placement.scalar.signed else "format_uint"
	refuse = f'\t\traise ConstraintError("{path}: too wide for its digits")'
	if not part.minimal:
		return [*_room(str(part.digits), path),
		        f"\tif not {call}(situ_out, situ_at, {part.digits}, "
		        f"{part.radix}, {value}):", refuse,
		        f"\tsitu_at += {part.digits}"]
	return [
		"\tsitu_digits = 1",
		f"\tsitu_left = abs({value})",
		f"\twhile situ_left >= {part.radix}:",
		f"\t\tsitu_left //= {part.radix}",
		"\t\tsitu_digits += 1",
		*([f"\tif {value} < 0:", "\t\tsitu_digits += 1"]
		  if part.placement.scalar.signed else []),
		*_room("situ_digits + 1", path),
		f"\tif not {call}(situ_out, situ_at, situ_digits, "
		f"{part.radix}, {value}):", refuse,
		"\tsitu_at += situ_digits",
		f"\tsitu_out[situ_at] = {part.delimiter[0]}",
		"\tsitu_at += 1",
	]


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
	if part.role == BITS:
		return _bit_store(part, value)
	if part.radix:
		return _digits(part, value)
	width  = placement.scalar.bits // BITS_PER_BYTE
	order  = _byte_order(placement.endian)
	return [
		*_room(str(width), placement.path),
		f"\tsitu_out[situ_at:situ_at + {width}] = ({value}).to_bytes("
		f"{width}, {order}, signed={placement.scalar.signed})",
		f"\tsitu_at += {width}",
	]


def _size(part: Part, length: str) -> list[str]:
	"""A size field, from the length of the run it measures."""
	assert part.placement.scalar is not None
	path  = part.placement.path
	# A text number holds what its scalar holds, whatever its digits' width.
	limit = (1 << (part.placement.scalar.bits if part.radix
	               else part.placement.size_bits or 0)) - 1
	refuse = (f'\t\traise ConstraintError("{path}: the run is longer than '
	          'this can say")')
	if part.solved is None:
		return [f"\tif {length} > {limit}:", refuse,
		        *_store(part, f"{length}")]
	scale, offset = part.solved.scale, part.solved.offset
	shift = f" + {-offset}" if offset < 0 else f" - {offset}" if offset else ""
	return [
		f"\tsolved = {length}{shift}",
		"\tif solved < 0"
		+ (f" or solved % {scale} != 0" if scale != 1 else "") + ":",
		refuse,
		*([f"\tsolved //= {scale}"] if scale != 1 else []),
		f"\tif solved > {limit}:", refuse,
		*_store(part, "solved"),
	]


def _write(structs: Mapping[str, ResolvedStruct], part: Part,
		parts: list[Part]) -> list[str]:
	"""One part, as `c/build.py` writes it."""
	path = part.placement.path
	if part.role == BITS:
		return _bits(part, parts)
	if part.role == NESTED:
		return _nested(part)
	if part.role == REPEAT:
		return _repeat(structs, part, parts)
	if part.role == ENDED:
		return _ended(part)
	if part.role == VARINT:
		return _varint(part, parts)
	if part.role == PAD:
		# To the next boundary, from this struct's own base, which is where
		# the cursor counts from.
		align = traverse.pad_alignment(part.placement)
		return [f"\tsitu_pad = -situ_at % {align}",
		        *_room("situ_pad", part.placement.path),
		        "\tsitu_out[situ_at:situ_at + situ_pad] = bytes(situ_pad)",
		        "\tsitu_at += situ_pad"]
	if part.role == TAG:
		# Its value covers bytes not written yet, so its place is kept and
		# it is filled once the message is whole.
		held = py_name(part.local)
		return [*_room(f"{part.bytes}", part.placement.path),
		        f"\tsitu_tag_{held} = situ_at",
		        f"\tsitu_out[situ_at:situ_at + {part.bytes}] = "
		        f"bytes({part.bytes})",
		        f"\tsitu_at += {part.bytes}"]
	if part.role == ARMTAG:
		return _store(part, str(part.value))
	if part.role == PEEKED:
		return []		# the bytes it reads belong to what follows
	if part.role == SCALAR:
		# A value a named arm claims would make the message read as that
		# arm, so the default builder refuses it.
		local = py_name(part.local)
		return [*([f"\tif {local} in {tuple(part.others)!r}:",
		           f'\t\traise ConstraintError("{path}: reads as something else")']
		          if part.others else []),
		        *_store(part, local)]
	if part.role == SIZE:
		measured = next(one for one in parts if one.placement is part.sizes)
		if part.inner:
			return [*_walk(structs, measured, parts), *_size(part, "situ_count")]
		return _size(part, f"len({py_name(measured.local)})")
	if part.role == ZERO:
		ones = traverse.reserved_policy(part.placement.attrs) == "must_be_one"
		return [*_room(str(part.bytes), path),
		        f"\tsitu_out[situ_at:situ_at + {part.bytes}] = "
		        f"b\"{'\\xff' if ones else '\\x00'}\" * {part.bytes}",
		        f"\tsitu_at += {part.bytes}"]
	if part.role == LITERAL:
		return [*_room(str(part.bytes), path),
		        f"\tsitu_out[situ_at:situ_at + {part.bytes}] = {bytes(part.literal)!r}",
		        f"\tsitu_at += {part.bytes}"]
	local = py_name(part.local)
	if part.role == RUN:
		return [f"\tif len({local}) != {part.bytes}:",
		        f'\t\traise BoundsError("{path}: {part.bytes} byte(s) are '
		        'wanted")',
		        *_room(str(part.bytes), path),
		        f"\tsitu_out[situ_at:situ_at + {part.bytes}] = {local}",
		        f"\tsitu_at += {part.bytes}"]
	assert part.role == SPAN, part.role
	return [*_room(f"len({local})", path),
	        f"\tsitu_out[situ_at:situ_at + len({local})] = {local}",
	        f"\tsitu_at += len({local})"]


def _parameters(parts: list[Part]) -> list[str]:
	found: list[str] = []
	for part in parts:
		if plain_build.takes_value(part):
			found.append(f"{py_name(part.local)}: int")
		elif part.role == RUN or part.role in plain_build.MEASURED:
			found.append(f"{py_name(part.local)}: bytes")
		elif part.role == TAG and part.opens:
			# The pseudo-header its codec sums, which no member holds.
			found.append(f"{py_name(part.local)}_prefix: bytes")
	return found


def _filled(struct: ResolvedStruct, parts: list[Part]) -> list[str]:
	"""Each tag, computed over what was written, stored, and checked: the
	check is what proves the store."""
	found: list[str] = []
	for part in parts:
		if part.role != TAG:
			continue
		name  = py_name(traverse.local_name(struct, part.placement))
		order = "little" if plain_build.tag_order(part) == "le" else "big"
		at    = f"situ_tag_{py_name(part.local)}"
		over  = f"{py_name(part.local)}_prefix" if part.opens else ""
		found.extend([
			f"\tsitu_out[{at}:{at} + {part.bytes}] = "
			f"situ_view.{name}_compute({over}).to_bytes({part.bytes}, "
			f"\"{order}\")",
			f"\tsitu_view.{name}_check({over})"])
	return found


def _held_to_the_reader(struct: ResolvedStruct,
		parts: list[Part]) -> list[str]:
	"""A `while` run ends where a predicate stops holding, which is the
	reader's to evaluate, so its count is asked rather than reproduced."""
	found: list[str] = []
	for part in parts:
		if part.role == REPEAT and part.value == 1:
			name = py_name(traverse.local_name(struct, part.placement))
			found.extend([
				f"\tif situ_view.{name}_count != situ_count:",
				f'\t\traise ConstraintError("{part.placement.path}: '
				'the reader counts otherwise")'])
	return found


def generate(schema: ast.Schema, resolved: ResolvedSchema,
		basename: str) -> dict[str, str]:
	"""`<basename>_build.py`, or nothing where no struct qualifies.

	A rung adds files rather than changing them (0032), which is why this
	is its own module and not more of `<basename>.py`.
	"""
	shape = plain_build.shape(schema, resolved)
	built = plain_build.units(resolved, shape)
	if not built:
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
		# By the module, for a nested member's struct: `header header;`
		# makes the parameter shadow the class of the same name.
		f"import {basename} as situ_schema",
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
			for unit in built for one in unit.parts
			if one.role in (ARMTAG, SCALAR, SIZE)) else ""),
		"from situ_runtime import (BoundsError, ConstraintError, SituError,",
		"                          TruncatedError, View, format_int,",
		"                          format_uint, varint_be_put, varint_put,",
		"                          zigzag_encode)",
		"",
	]

	for unit in built:
		struct = unit.struct
		held   = py_name(struct.name)
		parts  = list(unit.parts)
		name   = f"{held}_{py_name(unit.suffix)}" if unit.suffix else held
		lines.extend([
			"",
			f"def build_{name}(situ_out: bytearray"
			+ "".join(f", {one}" for one in _parameters(parts)) + ") -> int:",
			f'\t"""Build a {struct.name} into `situ_out` and return its length.',
			"",
			"\tOne member at a time in schema order, then the result is",
			"\tvalidated through the ordinary view before it is reported:",
			"\tconstraints are the view's to state and this reuses them",
			"\trather than restating them, two checks of one schema being",
			"\thow they come to disagree.",
			"",
			"\tRaises BoundsError where `situ_out` is short of what the members",
			"\tsitu_need, ConstraintError where a run is longer than its size",
			'\tfield can say, and whatever `validate` raises for the result."""',
			"\tsitu_at = 0",
		])
		for part in parts:
			lines.extend(_write(resolved.structs, part, parts))
		lines.extend([
			"",
			"\t# The view over exactly what was written, not over `situ_out`: a",
			"\t# longer buffer would hide a struct that came out short.",
			f"\tsitu_view = {held}.at(Message(memoryview(situ_out)[:situ_at]), 0"
			+ ("" if struct.layout.is_fixed_size else ", situ_at") + ")",
			*_filled(struct, parts),
			*_held_to_the_reader(struct, parts),
			"\tsitu_view.validate()",
			"\treturn situ_at",
		])

	return {f"{basename}_build.py": "\n".join(lines).rstrip("\n") + "\n"}
