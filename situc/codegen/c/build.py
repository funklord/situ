# Append-only message building: the write half of rung 2 (26.610).
#
# `--owned` already encodes: `_encode` lays a filled struct back into a
# buffer. It is restricted to a struct whose every member has a fixed-size C
# field, so what it cannot write is exactly what 0032 row 2 names -- "build
# or resize a message whose extent is not fixed". hull asked for that half
# and found nothing emitting it in any backend (suggestion/hull.md, 11).
#
# The restriction here is append-only, and it is a property of the LAYOUT
# rather than a flag: `traverse.append_only_refusals` reads it out of the
# schema, because 0032's closing invariant is that no rung invents a fact
# the schema did not state. A caller cannot ask for an append-only build of
# a format that is not one.
#
# Two writers of one member is how a pair comes to be self-consistent and
# wrong together, which `owned.py` paid for in BCD. So this file emits no
# representation conversion at all: it writes through the runtime's own
# `situ_put_*`, and REFUSES every member whose value differs from its bits
# by more than byte order -- BCD, a radix, a sub-byte scalar, a varint. The
# conversion then has exactly one implementation per direction, and it is
# not this one.
#
# And the bytes are checked by the view rather than by a second opinion:
# every build ends by validating what it wrote through the generated
# `_validate`, for the reason `owned.py` states -- two checks of one schema
# is how they come to disagree.

from __future__ import annotations

from collections.abc import Mapping

from situc import ast, traverse
from situc.codegen.c.names import bare_name, ident, macro
from situc.codegen.c.owned import _ctype, storage_width
from situc.layout import BITS_PER_BYTE, Placement
from situc.resolve import ResolvedSchema, ResolvedStruct
from situc.types import ScalarKind
from situc import __version__

WORD_WIDTHS = (8, 16, 32, 64)

#: The writer's own locals. Prefixed because a member name is a schema's to
#: choose and these are not: `test/schema/image.situ` has a member called
#: `cap`, which redefined the capacity parameter and was then unused, so one
#: collision produced two compile errors. A member that collides even with
#: these is refused by name rather than mangled -- a parameter list is an
#: interface, and renaming somebody's member in it is the kind of invention
#: 0032 asks a rung not to make.
OUT, CAP, WROTE = "situ_out", "situ_cap", "situ_wrote"
AT, MSG, VIEW, ERR = "situ_at", "situ_msg", "situ_view", "situ_err"
FIXTURES = frozenset({OUT, CAP, WROTE, AT, MSG, VIEW, ERR, "situ_fixed"})

# What a member is to the writer. `literal` and `size` take no parameter:
# both are facts the schema states, and asking the caller for either would
# invite a message that disagrees with its own schema.
LITERAL = "literal"	# pinned bytes, written from the schema
SIZE    = "size"	# the length of a run that follows it
SCALAR  = "scalar"	# one whole-byte binary scalar, from the caller
RUN     = "run"		# a byte run of fixed length, from the caller
SPAN    = "span"	# a byte run whose length the caller supplies
ZERO    = "zero"	# reserved bytes, which no caller names


class Part:
	"""One member, and how the writer reaches it."""

	def __init__(self, role: str, placement: Placement, local: str,
			bytes_: int = 0, literal: bytes = b"",
			sizes: Placement | None = None) -> None:
		self.role      = role
		self.placement = placement
		self.local     = local
		self.bytes     = bytes_
		self.literal   = literal
		self.sizes     = sizes		# the run this member's value measures


def _local(struct: ResolvedStruct, placement: Placement) -> str:
	"""The parameter name for a member.

	A parameter list is the third place a member name is emitted on its own,
	after the owned struct's field and every C++ accessor -- so it is
	`bare_name`'s third caller rather than a new rule, and it mangles for
	0025's reason: the schema keeps its name and the emitter moves. The
	first draft used the path verbatim and `<reserved0>` reached four
	signatures; the second used `c_name` and a member called `short` reached
	one.

	Mangling costs a caller nothing here, which is worth saying because it
	is not true everywhere: C has no named arguments, so a parameter name is
	documentation rather than call syntax. Refusing the struct would cost
	them the builder.
	"""
	return bare_name(traverse.local_name(struct, placement))


def _whole_bytes(placement: Placement) -> int | None:
	"""How many whole bytes this member occupies, or None if it is not
	byte-aligned or not of a fixed size."""
	if placement.size_bits is None or placement.size_bits <= 0:
		return None
	if placement.size_bits % BITS_PER_BYTE:
		return None
	return placement.size_bits // BITS_PER_BYTE


def _scalar_refusal(placement: Placement) -> str | None:
	"""Why this scalar's value is not its bytes in the runtime's own order.

	Each of these has a conversion somewhere else in the tree, and a second
	copy here is the fault `owned.py` records against its own BCD: the two
	were self-consistent and wrong together, so a round trip returned
	SITU_OK about a date the bytes did not spell.
	"""
	scalar = placement.scalar
	assert scalar is not None
	if scalar.kind is ScalarKind.BCD or scalar.is_bcd:
		return "is packed BCD, whose value is not its bytes"
	if scalar.kind is ScalarKind.FLOAT:
		return "is floating point"
	if scalar.kind in (ScalarKind.SFIXED, ScalarKind.UFIXED):
		return "is fixed point, whose value is scaled"
	if placement.radix is not None:
		return f"is written in base {placement.radix}, and the runtime has " \
		       "no radix writer"
	if placement.scaled:
		return "is scaled, so its value is not its bits"
	if placement.varint:
		return "is a varint, whose width depends on its value"
	if scalar.bits % BITS_PER_BYTE or scalar.bits not in WORD_WIDTHS:
		return f"is {scalar.bits} bits wide, so it shares a byte with its " \
		       "neighbours and cannot be appended on its own"
	if placement.offset_bits is not None \
			and placement.offset_bits % BITS_PER_BYTE:
		return "does not start on a byte boundary"
	return None


def _member_refusal(placement: Placement) -> str | None:
	"""Why this member is beyond the first writer, by name.

	Everything here is a real construct that a later increment may reach.
	Refusing by name is the point: 0032 asks a rung to be absent rather
	than defaulted where the schema states something it cannot hold to.
	"""
	if placement.kind == "checksum" or placement.tag_covers:
		return "is a tag or checksum, which this writer does not compute"
	if placement.kind == "variant":
		return "is a variant, so which arm to write is the caller's choice " \
		       "and not yet expressible"
	if placement.kind not in ("field", "reserved", "preamble"):
		return f"is a {placement.kind}"
	if placement.codec is not None:
		return "passes through a codec"
	if placement.sealed_by or placement.sealed_nonce or placement.sealed_key:
		return "is sealed, and sealing is rung 3's"
	if placement.index_table is not None:
		return "is an index table"
	if placement.delimiters:
		return "ends at a delimiter the writer would have to prove its " \
		       "bytes do not contain"
	if placement.repeat_while is not None:
		return "repeats while a condition holds"
	if placement.pad_to is not None:
		return "pads to an alignment"
	if placement.pad_bounds is not None:
		return "is a length-hiding pad, so how long to make it is a " \
		       "decision rather than a fact the schema states"
	if placement.parameter:
		return "comes from a struct argument"
	if placement.remaining_cap:
		return "takes whatever is left, so its length is the frame's"
	if placement.size_expr is not None:
		return f"is sized by the expression `{placement.size_expr}`, which " \
		       "this writer cannot invert"
	return None


def plan(struct: ResolvedStruct) -> tuple[list[Part], str | None]:
	"""What the writer does for each member, or the first reason it cannot.

	The order is the schema's, which is what makes the pass forward-only.
	"""
	# A register is a bus transaction rather than bytes in a buffer and gets
	# an entirely different API (15.1), so there is no view to validate
	# through and nothing to append into. A struct that is not a whole
	# number of bytes has no accessors at all. Both are read from the layout
	# where `emit.py` reads them, rather than from the target directive: one
	# witness, and it is the one that decided what was emitted.
	if struct.layout.register is not None:
		return [], "is a register, which is a bus transaction rather than " \
		           "bytes in a buffer"
	if not struct.layout.is_byte_sized:
		return [], f"is {struct.layout.size_bits} bits, not a whole number " \
		           "of bytes, so no accessors exist to read it back"

	refusals = traverse.append_only_refusals(struct)
	if refusals:
		return [], refusals[0]

	members = traverse.own_members(struct)
	if not members:
		return [], "has no members to write"

	# A size field is the writer's to compute, so it must be known before
	# the loop reaches it -- and `sized_by` names it from the run, which is
	# the member that comes later.
	by_name = {held.name: held for held in members}
	measures: dict[str, Placement] = {}
	for held in members:
		if not held.sized_by or held.sized_by not in by_name:
			# `sized_by` also carries `remaining`, which names no member:
			# the run takes the rest of the frame, and since the builder
			# decides the frame, the length the caller gives IS the rest.
			continue
		if held.sized_by in measures:
			# Two runs measured by one field would make the writer compute
			# one value for two lengths, and the last one to be planned
			# would win silently.
			return [], f"`{held.sized_by}` is the stated size of both " \
			           f"`{measures[held.sized_by].name}` and " \
			           f"`{held.name}`, so one value cannot say both"
		measures[held.sized_by] = held

	parts: list[Part] = []
	for held in members:
		local = _local(struct, held)
		runs  = traverse.pinned_runs(held)
		size  = _whole_bytes(held)

		if runs and len(runs) == 1 and size is not None \
				and len(runs[0]) == size:
			parts.append(Part(LITERAL, held, local, size, runs[0]))
			continue

		if held.kind in ("reserved", "preamble"):
			# Reserved bytes are a constraint rather than a value (8.8) and
			# have no accessor, so there is nothing for a caller to pass.
			# Zero is not a choice either: the content policy is
			# `must_be_zero` or nothing, so zero is the one value that is
			# required where it is stated and refused nowhere.
			if size is None:
				return [], f"`{traverse.local_name(struct, held)}` is " \
				           "reserved and has no stated length"
			parts.append(Part(ZERO, held, local, size))
			continue

		why = _member_refusal(held)
		if why:
			return [], f"`{traverse.local_name(struct, held)}` {why}"

		if held.name in measures:
			if held.scalar is None:
				return [], f"`{local}` sizes " \
				           f"`{measures[held.name].name}` and is not a scalar"
			why = _scalar_refusal(held)
			if why:
				return [], f"`{traverse.local_name(struct, held)}` {why}"
			parts.append(Part(SIZE, held, local,
			                  held.scalar.bits // BITS_PER_BYTE,
			                  sizes=measures[held.name]))
			continue

		if held.scalar is not None and held.array_count is None \
				and not traverse.data_sized(held):
			why = _scalar_refusal(held)
			if why:
				return [], f"`{traverse.local_name(struct, held)}` {why}"
			parts.append(Part(SCALAR, held, local,
			                  held.scalar.bits // BITS_PER_BYTE))
			continue

		# A byte run: fixed where the schema counted it, caller-measured
		# where another member holds the count. Anything wider than a byte
		# per element would need the element's byte order applied per
		# element, which is a loop this increment does not write.
		element = held.element_bits or 0
		if element != BITS_PER_BYTE:
			spelt = (f"a run of {element}-bit elements" if element
			         else f"a run of `{held.type_name}`")
			return [], f"`{traverse.local_name(struct, held)}` is {spelt}, " \
			           "not of bytes"
		if held.array_count is not None:
			parts.append(Part(RUN, held, local, held.array_count))
			continue
		if held.sized_by:
			parts.append(Part(SPAN, held, local))
			continue
		return [], f"`{traverse.local_name(struct, held)}` has no stated " \
		           "length, so nothing says when the writer should stop"

	# One more pass for the same reason, over names that are legal C and
	# still cannot stand where they are: the writer's own locals, and each
	# other -- a `value` span emits a `value_len`, which a member of that
	# name would then redefine. `image.situ` has a member called `cap`, and
	# one collision produced two errors, a redefinition and an unused
	# parameter. Declaration order decides, so the earlier member keeps its
	# spelling.
	taken = set(FIXTURES)
	for part in parts:
		local = part.local
		while local in taken or f"{local}_len" in taken:
			local += "_"
		part.local = local
		taken.add(local)
		if part.role == SPAN:
			taken.add(f"{local}_len")

	if not any(part.role in (SCALAR, RUN, SPAN) for part in parts):
		# Every byte is a fact the schema states, so there is nothing for a
		# caller to supply and `_encode` of a constant is not a builder.
		return [], "is entirely literal, so there is nothing to build"
	return parts, None


def buildable(resolved: ResolvedSchema) -> list[ResolvedStruct]:
	return [struct for struct in resolved.structs.values()
	        if plan(struct)[0]]


def refusals(resolved: ResolvedSchema) -> list[tuple[str, str]]:
	"""Every struct with no builder, and why -- by name, on stderr."""
	found = []
	for name, struct in resolved.structs.items():
		parts, why = plan(struct)
		if not parts and why:
			found.append((name, why))
	return found


def _signature(struct: ResolvedStruct, parts: list[Part], prefix: str,
		enums: Mapping[str, object]) -> list[str]:
	name = ident(prefix, struct.name, "build")
	args = [f"uint8_t *{OUT}", f"uint32_t {CAP}"]
	for part in parts:
		if part.role == SCALAR:
			args.append(f"{_ctype(part.placement, prefix, enums)} "
			            f"{part.local}")
		elif part.role == RUN:
			args.append(f"const uint8_t *{part.local}")
		elif part.role == SPAN:
			args.append(f"const uint8_t *{part.local}")
			args.append(f"uint32_t {part.local}_len")
	args.append(f"uint32_t *{WROTE}")

	lines = [f"static inline situ_err_t {name}("]
	for i, arg in enumerate(args):
		lines.append(f"\t\t{arg}" + ("," if i + 1 < len(args) else ")"))
	return lines


def _store(part: Part, value: str) -> list[str]:
	"""Put one whole-byte scalar at the cursor."""
	scalar = part.placement.scalar
	assert scalar is not None
	width = storage_width(scalar.bits)
	if scalar.bits == BITS_PER_BYTE:
		return [f"\t{OUT}[{AT}] = (uint8_t){value};"]
	end = "le" if part.placement.endian is ast.Endian.LITTLE else "be"
	return [f"\tsitu_put_{end}{scalar.bits}({OUT} + {AT},",
	        f"\t              (uint{width}_t){value});"]


def _room(size: str) -> list[str]:
	"""The capacity check before a write of `size` bytes.

	`{CAP} - {AT}` rather than `{AT} + size > {CAP}`: the cursor never
	passes the capacity, so the subtraction cannot wrap, while the addition
	can on a length the caller supplied.
	"""
	return [f"\tif ({CAP} - {AT} < {size}) {{",
	        "\t\treturn SITU_ERR_BOUNDS;",
	        "\t}"]


def _one(struct: ResolvedStruct, prefix: str,
		enums: Mapping[str, object]) -> list[str]:
	parts, _ = plan(struct)
	lines = [
		f"/** Build a {struct.name} into `{OUT}`, one member at a time in",
		" *  schema order, and validate the result before reporting it.",
		" *",
		f" * `*{WROTE}` is set only on success. A size field is computed",
		" * from the run it measures rather than supplied, so a message",
		" * cannot disagree with its own schema.",
		" *",
		f" * SITU_OK             built; the first `*{WROTE}` bytes are a",
		f" *                     complete {struct.name}",
		f" * SITU_ERR_BOUNDS     `{CAP}` is short of what the members need",
		" * SITU_ERR_CONSTRAINT a run is longer than its size field can say,",
		" *                     or the result does not satisfy the schema",
		" */",
		*_signature(struct, parts, prefix, enums),
		"{",
		f"\tsitu_msg_t  {MSG};",
		f"\tsitu_view_t {VIEW};",
		f"\tsitu_err_t  {ERR};",
		f"\tuint32_t    {AT} = 0;",
		"",
		f"\tif ({OUT} == NULL || {WROTE} == NULL) {{",
		"\t\treturn SITU_ERR_BOUNDS;",
		"\t}",
		"",
	]

	for part in parts:
		held  = part.placement
		local = traverse.local_name(struct, held)
		if part.role == SIZE:
			assert part.sizes is not None and held.scalar is not None
			run   = _local(struct, part.sizes)
			limit = (1 << held.scalar.bits) - 1
			lines += [
				f"\t/* {local}: the length of `{run}`, which the schema "
				"states. */",
				f"\tif ({run}_len > {limit}u) {{",
				"\t\treturn SITU_ERR_CONSTRAINT;",
				"\t}",
				*_room(f"{part.bytes}u"),
				*_store(part, f"{run}_len"),
				f"\t{AT} += {part.bytes}u;",
				"",
			]
		elif part.role == SCALAR:
			lines += [
				*_room(f"{part.bytes}u"),
				*_store(part, part.local),
				f"\t{AT} += {part.bytes}u;",
				"",
			]
		elif part.role == ZERO:
			lines += [
				f"\t/* {local}: {part.bytes} reserved byte(s). Zero is what",
				"\t * a `must_be_zero` policy requires and what no other",
				"\t * policy refuses. */",
				*_room(f"{part.bytes}u"),
				f"\tmemset({OUT} + {AT}, 0, {part.bytes}u);",
				f"\t{AT} += {part.bytes}u;",
				"",
			]
		elif part.role == LITERAL:
			spelt = ", ".join(f"0x{byte:02X}u" for byte in part.literal)
			lines += [
				f"\t/* {local}: {part.bytes} byte(s) the schema spells out. */",
				*_room(f"{part.bytes}u"),
				"\t{",
				f"\t\tstatic const uint8_t situ_fixed[{part.bytes}] = "
				f"{{ {spelt} }};",
				"",
				f"\t\tmemcpy({OUT} + {AT}, situ_fixed, {part.bytes}u);",
				"\t}",
				f"\t{AT} += {part.bytes}u;",
				"",
			]
		elif part.role == RUN:
			lines += [
				f"\tif ({part.local} == NULL) {{",
				"\t\treturn SITU_ERR_BOUNDS;",
				"\t}",
				*_room(f"{part.bytes}u"),
				f"\tmemcpy({OUT} + {AT}, {part.local}, {part.bytes}u);",
				f"\t{AT} += {part.bytes}u;",
				"",
			]
		else:
			lines += [
				*_room(f"{part.local}_len"),
				# A zero-length run is a legal message, and `memcpy` with a
				# null source is undefined even for zero bytes.
				f"\tif ({part.local}_len != 0u) {{",
				f"\t\tif ({part.local} == NULL) {{",
				"\t\t\treturn SITU_ERR_BOUNDS;",
				"\t\t}",
				f"\t\tmemcpy({OUT} + {AT}, {part.local}, {part.local}_len);",
				"\t}",
				f"\t{AT} += {part.local}_len;",
				"",
			]

	lines += [
		"\t/* Constraints are the view's to state and this reuses them",
		"\t * rather than restating them: two checks of one schema is how",
		"\t * they come to disagree. */",
		f"\tsitu_msg_init(&{MSG}, {OUT}, {AT});",
		# A frame's view takes the length the caller has and a fixed
		# struct's does not -- `emit.py` keys that on `is_fixed_size` and so
		# does this. The first draft did not, and it was a compile error
		# rather than a quiet one.
		f"\tif ({ident(prefix, struct.name, 'view')}(&{MSG}, 0, "
		+ ("" if struct.layout.is_fixed_size else f"{AT}, ")
		+ f"&{VIEW}) != SITU_OK) {{",
		"\t\treturn SITU_ERR_BOUNDS;",
		"\t}",
		f"\t{ERR} = {ident(prefix, struct.name, 'validate')}({VIEW});",
		f"\tif ({ERR} != SITU_OK) {{",
		f"\t\treturn {ERR};",
		"\t}",
		"",
		f"\t*{WROTE} = {AT};",
		"\treturn SITU_OK;",
		"}",
		"",
	]
	return lines


def generate(schema: ast.Schema, resolved: ResolvedSchema, basename: str,
		prefix: str = "situ") -> dict[str, str]:
	"""The build header, or nothing where no layout qualifies."""
	structs = buildable(resolved)
	if not structs:
		return {}

	guard = macro(prefix, basename, "BUILD_H")
	lines = [
		f"/* Generated by situc {__version__} from {basename}.situ -- do not edit.",
		" *",
		" * Append-only building: the write half of rung 2. Each member goes",
		" * down in schema order into backing you supply, nothing moves once",
		" * written, and no view into the buffer exists while it is being",
		" * filled -- which is why this needs rung 2's permission and",
		" * nothing above it.",
		" *",
		" * A layout that cannot be written forward has no builder here, and",
		" * `situc build` names it. Nothing here allocates. A separate",
		" * header -- a rung adds files rather than changing them (0032).",
		" */",
		"",
		f"#ifndef {guard}",
		f"#define {guard}",
		"",
		"#include <string.h>",
		"",
		"#include \"situ.h\"",
		f"#include \"{basename}.h\"",
		"",
		"#ifdef __cplusplus",
		"extern \"C\" {",
		"#endif",
		"",
	]

	enums = dict(resolved.layout.env.enums)
	for struct in structs:
		lines.extend(_one(struct, prefix, enums))

	lines += ["#ifdef __cplusplus", "}", "#endif", "",
	          f"#endif /* {guard} */"]

	return {f"{basename}_build.h": "\n".join(lines) + "\n"}
