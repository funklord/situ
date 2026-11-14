"""Append-only building in C++: the write half of rung 2 (26.858, 26.860).

`plain_build` says which structs and C's plan what each part is. The
sequence is C's: bounds-check what is left, store at the cursor, advance,
and take a view over exactly what was written and `validate()` it, because
constraints are the view's to state and two checks of one schema is how
they come to disagree.

**A member is taken as its integer, not its enum.** An `enum class` value
has to be one the type can name, and `default = pass` says a message may
carry one that nothing names; Python's builder takes an int for the same
reason.

**In `situ_build`, not `situ`**, so a schema's struct cannot collide with
its own builder: a struct called `build_x` beside one called `x` would.
"""

from __future__ import annotations

from collections.abc import Mapping

from situc import __version__, ast, traverse
from situc.codegen import plain_build
from situc.codegen.c.build import (ARG, WRITABLE_CODES, FRAMED, MARKER, ARMTAG, BITS, CHOSEN, ENDED, LITERAL, NESTED,
                                   PAD, PEEKED, REPEAT, RUN, SCALAR, SIZE, SPAN,
                                   TAG, VARINT, WIDE, ZERO, ArmOf, Part, Shape)
from situc.codegen.c.names import bare_name, c_name
from situc.codegen.cpp.edit import _scalar
from situc.codegen.cpp.emit import _order_suffix
from situc.codegen.cpp.names import class_name
from situc.codegen.plain_build import plain
from situc.codegen.c.build import peeked_byte
from situc.codegen.varint import varint_put_tail, varint_stem
from situc.layout import BITS_PER_BYTE
from situc.resolve import ResolvedSchema, ResolvedStruct

__all__ = ["generate", "plain", "declined", "Shape"]

BOUNDS     = "\t\treturn ::situ::rt::err::bounds;"
CONSTRAINT = "\t\treturn ::situ::rt::err::constraint;"


def declined(resolved: ResolvedSchema,
		shape: Shape) -> list[tuple[str, str]]:
	"""What this builder does not build, and why, for stderr."""
	return plain_build.declined(resolved, shape, "C++")


def _room(size: str) -> list[str]:
	return [f"\tif (situ_cap - situ_at < {size}) {{", BOUNDS, "\t}"]


def _bit_store(part: Part, value: str) -> list[str]:
	"""Set a field inside a group `_write` zeroed; the cursor stays put."""
	held = part.placement
	assert held.scalar is not None
	order = traverse.bit_extractor(held.scalar, held)
	return [f"\tsitu_bits_set_{order}(situ_out + situ_at, {part.bit_at}u,"
	        f" {held.size_bits}u, static_cast<std::uint64_t>({value}));"]


def _digits(part: Part, value: str) -> list[str]:
	"""A number written as text: `digits` wide, or as few as it needs.

	C's `_value`. The fixed form pads with zeros, and the minimal form is
	that with the width counted first, then the byte that ends it. A
	negative value's digits are its magnitude's, and the sign is a byte more.
	"""
	assert part.placement.scalar is not None
	signed = part.placement.scalar.signed
	call   = "situ_format_int" if signed else "situ_format_uint"
	widest = "std::int64_t" if signed else "std::uint64_t"
	if not part.minimal:
		return [*_room(f"{part.digits}u"),
		        f"\tif ({call}(situ_out + situ_at, {part.digits}u, "
		        f"{part.radix}u,",
		        f"\t\t\tstatic_cast<{widest}>({value})) != 0) {{",
		        CONSTRAINT, "\t}",
		        f"\tsitu_at += {part.digits}u;"]
	held = f"static_cast<{widest}>({value})"
	left = (f"{held} < 0 ? static_cast<std::uint64_t>(-({held} + 1)) + 1u"
	        f" : static_cast<std::uint64_t>({held})" if signed else held)
	return [
		"\t{",
		"\t\tstd::uint32_t situ_digits = 1;",
		f"\t\tstd::uint64_t situ_left = {left};",
		"",
		f"\t\twhile (situ_left >= {part.radix}u) {{",
		f"\t\t\tsitu_left /= {part.radix}u;",
		"\t\t\tsitu_digits++;",
		"\t\t}",
		*([f"\t\tif ({held} < 0) {{", "\t\t\tsitu_digits++;", "\t\t}"]
		  if signed else []),
		"\t\tif (situ_cap - situ_at < situ_digits + 1u) {",
		"\t\t\treturn ::situ::rt::err::bounds;",
		"\t\t}",
		f"\t\tif ({call}(situ_out + situ_at, situ_digits, {part.radix}u, "
		f"{held}) != 0) {{",
		"\t\t\treturn ::situ::rt::err::constraint;",
		"\t\t}",
		"\t\tsitu_at += situ_digits;",
		f"\t\tsitu_out[situ_at] = 0x{part.delimiter[0]:02X}u;",
		"\t\tsitu_at += 1u;",
		"\t}",
	]


def _store(part: Part, value: str) -> list[str]:
	"""One scalar's bounds check, store and advance -- C's three steps."""
	assert part.placement.scalar is not None
	if part.role == BITS:
		return _bit_store(part, value)
	if part.radix:
		return _digits(part, value)
	bits  = part.placement.scalar.bits
	width = bits // BITS_PER_BYTE
	if part.placement.marker and bits in (16, 32, 64):
		# In a marker's scope, whose value chose the order (26.1007).
		flag = f"situ_little_{part.placement.marker}"
		return [*_room(f"{width}u"),
		        f"\tif ({flag}) {{",
		        f"\t\tsitu_put_le{bits}(situ_out + situ_at, "
		        f"static_cast<std::uint{bits}_t>({value}));",
		        "\t} else {",
		        f"\t\tsitu_put_be{bits}(situ_out + situ_at, "
		        f"static_cast<std::uint{bits}_t>({value}));",
		        "\t}",
		        f"\tsitu_at += {width}u;"]
	if bits not in (8, 16, 32, 64):
		# C's `_store_bytes`: a byte at a time, refused where too wide.
		end = _order_suffix(part.placement.endian)
		return [*_room(f"{width}u"),
		        f"\tif (static_cast<std::uint64_t>({value}) > {(1 << bits) - 1:#x}u) {{",
		        "\t\treturn ::situ::rt::err::constraint;", "\t}",
		        *(f"\tsitu_out[situ_at + {i}u] = static_cast<std::uint8_t>("
		          f"static_cast<std::uint64_t>({value}) >> "
		          f"{8 * (width - 1 - i) if end == 'be' else 8 * i}u);"
		          for i in range(width)),
		        f"\tsitu_at += {width}u;"]
	return [
		*_room(f"{width}u"),
		(f"\tsitu_out[situ_at] = static_cast<std::uint8_t>({value});"
		 if bits == BITS_PER_BYTE else
		 f"\tsitu_put_{_order_suffix(part.placement.endian)}{bits}("
		 f"situ_out + situ_at, static_cast<std::uint{bits}_t>({value}));"),
		f"\tsitu_at += {width}u;",
	]


def _size(part: Part, length: str) -> list[str]:
	"""A size field, from the length of the run it measures."""
	assert part.placement.scalar is not None
	# A text number holds what its scalar holds, whatever its digits' width.
	width = (part.placement.scalar.bits if part.radix
	         else part.placement.size_bits or 0)
	limit = (1 << width) - 1
	if part.solved is None:
		return [
			*([f"\tif ({length} > {limit}u) {{", CONSTRAINT, "\t}"]
			  if width < 32 else []),
			*_store(part, f"{length}"),
		]
	scale, offset = part.solved.scale, part.solved.offset
	shift = f" + {-offset}" if offset < 0 else f" - {offset}" if offset else ""
	return [
		"\t{",
		f"\t\tstd::int64_t situ_solved = static_cast<std::int64_t>({length})"
		f"{shift};",
		"",
		"\t\tif (situ_solved < 0"
		+ (f" || situ_solved % {scale} != 0" if scale != 1 else "") + ") {",
		"\t\t\treturn ::situ::rt::err::constraint;",
		"\t\t}",
		*([f"\t\tsitu_solved /= {scale};"] if scale != 1 else []),
		*([f"\t\tif (situ_solved > {limit}) {{",
		   "\t\t\treturn ::situ::rt::err::constraint;",
		   "\t\t}"] if limit < (1 << 63) - 1 else []),
		*(f"\t{line}" for line in _store(part, "situ_solved")),
		"\t}",
	]


def _length(run: str, parts: list[Part]) -> str:
	"""What a size field reads: a count for values, bytes for the rest."""
	wide = any(one.role == WIDE and one.local == run for one in parts)
	return f"{run}_count" if wide else f"{run}_len"


def _wide(part: Part) -> list[str]:
	"""Elements wider than a byte: the caller passes values, and the byte
	order is the writer's to apply. The count is held to the room first,
	since `count * each` on a caller's count can overflow."""
	assert part.placement.scalar is not None
	local = part.local
	each  = part.bit_at
	bits  = part.placement.scalar.bits
	count = f"{part.bytes}u" if part.bytes else f"{local}_count"
	end   = _order_suffix(part.placement.endian)
	# C's: a caller's count of zero is an empty run -- RTP's CSRC list,
	# nearly always -- and the loop reads nothing then (26.1016).
	null = (f"\tif ({local} == nullptr) {{" if part.bytes else
	        f"\tif ({count} != 0u && {local} == nullptr) {{")
	return [
		null, BOUNDS, "\t}",
		f"\tif ({count} > (situ_cap - situ_at) / {each}u) {{", BOUNDS, "\t}",
		f"\tfor (std::uint32_t situ_scan = 0; situ_scan < {count}; "
		"situ_scan++) {",
		f"\t\tsitu_put_{end}{bits}(situ_out + situ_at,",
		f"\t\t\t\tstatic_cast<std::uint{bits}_t>({local}[situ_scan]));",
		f"\t\tsitu_at += {each}u;",
		"\t}",
	]


def _varint(part: Part, parts: list[Part]) -> list[str]:
	"""A number as wide as its own value.

	The declaration bounds the value, not the bytes, and the runtime's `put`
	writes the minimal encoding, which satisfies a `minimal` declaration by
	construction. A size field states what it measures, as any other does.
	"""
	assert part.declared is not None
	most = (1 << part.value) - 1
	tail = varint_put_tail(part.declared)
	call = f"situ_{varint_stem(part.declared)}_put"
	if part.stated is not None:
		held  = f"static_cast<std::uint64_t>({part.stated}u)"
		value = held
	elif part.sizes is not None:
		run   = next(one.local for one in parts
		             if one.placement is part.sizes)
		held  = _length(run, parts)
		value = f"static_cast<std::uint64_t>({held})"
	elif part.minimal:
		held  = f"static_cast<std::uint64_t>({part.local})"
		value = f"situ_zigzag_encode({part.local})"
	else:
		held  = f"static_cast<std::uint64_t>({part.local})"
		value = held
	return [
		*(["\tif ("
		   + " || ".join(f"{part.local} == {v}u" for v in part.others)
		   + ") {", CONSTRAINT, "\t}"] if part.others else []),
		# Not where `held` is a run's 32-bit length and the varint holds at
		# least that: matroska's 56-bit size made `data_len > ...u` a test
		# that cannot fire, and `-Wtype-limits` refuses it.
		*([f"\tif ({held} > {most}u) {{", CONSTRAINT, "\t}"]
		  if part.value < (32 if part.sizes is not None
		                   and part.stated is None else 64) else []),
		"\t{",
		f"\t\tconst std::uint32_t situ_used = {call}(situ_out + situ_at,",
		f"\t\t\t\tsitu_cap - situ_at, {value}{tail});",
		"",
		"\t\tif (situ_used == 0u) {",
		"\t\t\treturn ::situ::rt::err::bounds;",
		"\t\t}",
		"\t\tsitu_at += situ_used;",
		"\t}",
	]


def _nested(part: Part, inner: str) -> list[str]:
	"""One whole inner struct the caller built.

	Its extent is its own to state, so the writer asks rather than trusting
	the length it was given: three bytes too many shift every member after
	it. And where its first byte is a peeked discriminant it has to say
	which arm this is, or the bytes are a valid message of another arm.
	"""
	local = part.local
	if part.equals:
		# C's: an arm whose extent is its argument is as long as the field
		# the argument is; its content is validated with the message
		# (26.1012, 26.1020).
		return [f"\tif (static_cast<std::uint64_t>({local}_len)"
		        f" != static_cast<std::uint64_t>({part.equals})) {{",
		        "\t\treturn ::situ::rt::err::constraint;", "\t}",
		        f"\tif ({local}_len != 0u && {local} == nullptr) {{", BOUNDS,
		        "\t}",
		        *_room(f"{local}_len"),
		        f"\tif ({local}_len != 0u) {{",
		        f"\t\tstd::memcpy(situ_out + situ_at, {local}, {local}_len);",
		        "\t}",
		        f"\tsitu_at += {local}_len;"]
	return [
		*([] if not part.others and part.value <= 0 else [
			f"\tif ({local}_len == 0u) {{", CONSTRAINT, "\t}",
			(f"\tif ({peeked_byte(local, part)} != {part.value}u) {{" if part.value >= 0
			 else "\tif ("
			      + " || ".join(f"{peeked_byte(local, part)} == {v}u" for v in part.others)
			      + ") {"),
			CONSTRAINT, "\t}"]),
		f"\tif ({local}_len != 0u && {local} == nullptr) {{", BOUNDS, "\t}",
		# An arm that ends the message: its bytes are one whole value
		# however long, if they validate as one (26.973).
		*(["\t{",
		   "\t\tconst situ_view_t situ_raw = {const_cast<std::uint8_t *>"
		   f"({local}), {local}_len, 0u, nullptr}};",
		   f"\t\tif ({local}_len < ::situ::{inner}::size_min"
		   f" || ::situ::{inner}(situ_raw).validate()"
		   " != ::situ::rt::err::ok) {",
		   "\t\t\treturn ::situ::rt::err::constraint;",
		   "\t\t}",
		   "\t}"] if part.tail else [
		"\t{",
		"\t\tstd::uint32_t situ_need = 0;",
		"",
		f"\t\tif (::situ::{inner}::required({local}, {local}_len, situ_need)"
		" != ::situ::rt::err::ok",
		f"\t\t    || situ_need != {local}_len) {{",
		"\t\t\treturn ::situ::rt::err::constraint;",
		"\t\t}",
		"\t}"]),
		*_room(f"{local}_len"),
		f"\tif ({local}_len != 0u) {{",
		f"\t\tstd::memcpy(situ_out + situ_at, {local}, {local}_len);",
		"\t}",
		f"\tsitu_at += {local}_len;",
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
	local  = part.local
	held   = structs[part.placement.type_name or ""]
	inner  = f"::situ::{class_name(held)}"
	length = "" if held.layout.is_fixed_size else f" {local}_len - situ_scan,"
	# An argued element's arguments, before the out-parameter as the
	# reader takes them (26.1018).
	given  = "".join(f" {value}," for value in part.args)
	count  = plain_build.counts_elements(part, parts)
	wide   = len(part.delimiter)
	spelt  = ", ".join(f"0x{byte:02X}u" for byte in part.delimiter)
	return [
		f"\tif ({local}_len != 0u && {local} == nullptr) {{", BOUNDS, "\t}",
		*(["\tsitu_count = 0;"] if count else []),
		"\t{",
		"\t\tstd::uint32_t situ_scan = 0;",
		"\t\t::situ::rt::message situ_elements("
		f"const_cast<std::uint8_t *>({local}), {local}_len);",
		"",
		f"\t\twhile (situ_scan < {local}_len) {{",
		"\t\t\tstd::uint32_t situ_need = 0;",
		"\t\t\tbool situ_tail = false;",
		f"\t\t\t{inner} situ_element;",
		"",
		*([f"\t\t\tstatic constexpr std::uint8_t situ_end[{wide}] = "
		   f"{{ {spelt} }};",
		   "",
		   f"\t\t\tif (situ_scan + {wide}u <= {local}_len",
		   f"\t\t\t    && std::memcmp({local} + situ_scan, situ_end, "
		   f"{wide}u) == 0) {{",
		   "\t\t\t\treturn ::situ::rt::err::constraint;",
		   "\t\t\t}"] if wide else []),
		f"\t\t\tif ({inner}::required({local} + situ_scan,",
		f"\t\t\t                     {local}_len - situ_scan,{given} "
		"situ_need)",
		"\t\t\t    != ::situ::rt::err::ok) {",
		f"\t\t\t\tsitu_need = {local}_len - situ_scan;",
		"\t\t\t\tsitu_tail = true;",
		"\t\t\t}",
		"\t\t\tif (situ_need == 0u) {",
		"\t\t\t\treturn ::situ::rt::err::constraint;",
		"\t\t\t}",
		f"\t\t\tif ({inner}::at(situ_elements, situ_scan,{length}{given}"
		" situ_element)",
		"\t\t\t    != ::situ::rt::err::ok) {",
		"\t\t\t\treturn ::situ::rt::err::constraint;",
		"\t\t\t}",
		"\t\t\tconst ::situ::rt::err situ_verdict = situ_element.validate();",
		"",
		"\t\t\tif (situ_verdict != ::situ::rt::err::ok) {",
		"\t\t\t\treturn situ_tail ? ::situ::rt::err::constraint : situ_verdict;",
		"\t\t\t}",
		"\t\t\tsitu_scan += situ_need;",
		*(["\t\t\tsitu_count++;"] if count else []),
		"\t\t}",
		"\t}",
	]


def _needle(delimiter: bytes) -> list[str]:
	"""The delimiter that ends a run, and the cursor past it."""
	wide  = len(delimiter)
	spelt = ", ".join(f"0x{byte:02X}u" for byte in delimiter)
	return [*_room(f"{wide}u"),
	        "\t{",
	        f"\t\tstatic constexpr std::uint8_t situ_end[{wide}] = {{ {spelt} }};",
	        "",
	        f"\t\tstd::memcpy(situ_out + situ_at, situ_end, {wide}u);",
	        "\t}",
	        f"\tsitu_at += {wide}u;"]


def _encoded(part: Part) -> list[str]:
	"""A coded region from its content, through the C codec: C's `_encoded`."""
	from situc.kernels import STUFFING_BOUNDS

	worst, per, over = STUFFING_BOUNDS[part.code]
	own  = WRITABLE_CODES[part.code]
	tail = 0 if own else len(part.delimiter)
	local = part.local
	return [f"\tif ({local}_len != 0u && {local} == nullptr) {{", BOUNDS, "\t}",
	        f"\tif (static_cast<std::uint64_t>(situ_cap - situ_at)"
	        f" < (static_cast<std::uint64_t>({local}_len) * {worst}u"
	        f" + {per - 1}u) / {per}u + {over + tail}u) {{", BOUNDS, "\t}",
	        f"\tsitu_at += situ_{part.placement.codec}_encode({local},"
	        f" {local}_len, situ_out + situ_at);",
	        *([] if own else _needle(part.delimiter))]


def _ended(part: Part) -> list[str]:
	"""Bytes, then the delimiter that ends them.

	C's two forms. With no escape a run holding the delimiter would read
	back short, and is refused. With one the caller hands over content, and
	exactly two kinds of byte carry the escape: the delimiter's first byte
	where it begins one, and the escape itself.
	"""
	if part.code:
		return _encoded(part)
	if part.trim:
		# C's: content its reader would trim is refused (26.1014).
		edge = " || ".join(f"{{0}} == 0x{one:02X}u" for one in part.trim)
		trim = [f"\tif ({part.local}_len != 0u && {part.local} != nullptr && (",
		        f"\t    {edge.format(part.local + '[0]')} ||",
		        f"\t    {edge.format(part.local + '[' + part.local + '_len - 1u]')})) {{",
		        "\t\treturn ::situ::rt::err::constraint;", "\t}"]
		saved, part.trim = part.trim, ()
		try:
			return [*trim, *_ended(part)]
		finally:
			part.trim = saved
	local  = part.local
	wide   = len(part.delimiter)
	spelt  = ", ".join(f"0x{byte:02X}u" for byte in part.delimiter)
	escape = part.placement.delimiter_escape
	hit    = (f"situ_scan + {wide}u <= {local}_len\n"
	          f"\t\t\t    && std::memcmp({local} + situ_scan, situ_end, "
	          f"{wide}u) == 0")
	start  = [f"\tif ({local}_len != 0u && {local} == nullptr) {{", BOUNDS, "\t}",
	          "\t{",
	          f"\t\tstatic constexpr std::uint8_t situ_end[{wide}] = {{ {spelt} }};",
	          "",
	          f"\t\tfor (std::uint32_t situ_scan = 0; situ_scan < {local}_len;"
	          " situ_scan++) {"]
	if escape is None:
		return [*start,
		        f"\t\t\tif ({hit}) {{",
		        "\t\t\t\treturn ::situ::rt::err::constraint;",
		        "\t\t\t}",
		        "\t\t}",
		        "\t}",
		        *_room(f"{local}_len"),
		        f"\tif ({local}_len != 0u) {{",
		        f"\t\tstd::memcpy(situ_out + situ_at, {local}, {local}_len);",
		        "\t}",
		        f"\tsitu_at += {local}_len;",
		        *_needle(part.delimiter)]
	return [*start,
	        f"\t\t\tif ({local}[situ_scan] == 0x{escape:02X}u",
	        f"\t\t\t    || ({hit})) {{",
	        "\t\t\t\tif (situ_cap - situ_at < 1u) {",
	        "\t\t\t\t\treturn ::situ::rt::err::bounds;",
	        "\t\t\t\t}",
	        f"\t\t\t\tsitu_out[situ_at++] = 0x{escape:02X}u;",
	        "\t\t\t}",
	        "\t\t\tif (situ_cap - situ_at < 1u) {",
	        "\t\t\t\treturn ::situ::rt::err::bounds;",
	        "\t\t\t}",
	        f"\t\t\tsitu_out[situ_at++] = {local}[situ_scan];",
	        "\t\t}",
	        "\t}",
	        *_needle(part.delimiter)]


def _repeat(structs: Mapping[str, ResolvedStruct], part: Part,
		parts: list[Part]) -> list[str]:
	local = part.local
	return [
		*([] if plain_build.walked_at_size(part, parts)
		  else _walk(structs, part, parts)),
		*([f"\tif (situ_count != {part.digits}u) {{", CONSTRAINT, "\t}"]
		  if part.digits > 0 else []),
		*_room(f"{local}_len"),
		f"\tif ({local}_len != 0u) {{",
		f"\t\tstd::memcpy(situ_out + situ_at, {local}, {local}_len);",
		"\t}",
		f"\tsitu_at += {local}_len;",
		*(_needle(part.delimiter) if part.delimiter else []),
	]


def _bits(part: Part, parts: list[Part]) -> list[str]:
	"""One field of a byte several share.

	The group is zeroed when it opens, which is what writes `must_be_zero`,
	and the bytes are spent when it closes. A value wider than its field is
	refused rather than masked: `situ_bits_set_*` would write a valid
	message saying something else.
	"""
	held  = part.placement
	limit = (1 << (held.size_bits or 0)) - 1
	lines = [*_room(f"{part.bytes}u"),
	         f"\tstd::memset(situ_out + situ_at, 0, {part.bytes}u);"] \
		if part.opens else []
	if part.sizes is not None:
		run = next(one.local for one in parts
		           if one.placement is part.sizes)
		lines += _size(part, _length(run, parts))
	elif part.chooser is not None:
		tag = f"{part.chooser.local}.situ_tag"
		lines += [*([f"\tif ({tag} > {limit}u) {{", CONSTRAINT, "\t}"]
		            if any(one.tag for one in part.chooser.arms) else []),
		          *_bit_store(part, tag)]
	elif part.stated is not None:
		lines += _bit_store(part, f"{part.stated}u")
	elif plain_build.takes_value(part):
		lines += [*([f"\tif ({' || '.join(f'{part.local} == {v}u' for v in part.others)}) {{",
		             CONSTRAINT, "\t}"] if part.others else []),
		          f"\tif (static_cast<std::uint64_t>({part.local}) > {limit}u) {{",
		          CONSTRAINT, "\t}", *_bit_store(part, part.local)]
	elif plain_build.stated_ones(part):
		lines += _bit_store(part, f"{limit}u")
	if part.closes:
		lines.append(f"\tsitu_at += {part.bytes}u;")
	return lines


def _arm_type(struct: ResolvedStruct, part: Part) -> str:
	name = c_name(traverse.local_name(struct, part.placement))
	return f"{class_name(struct)}_{name}_arm"


def _arm_fields(struct: ResolvedStruct, arm: ArmOf) -> list[str]:
	"""What a caller gives this arm, as C++ declarations."""
	return _parameters(struct, [one for one in (arm.tag, arm.part)
	                            if one is not None])


def _field_name(declaration: str) -> str:
	return declaration.rsplit(" ", 1)[-1].lstrip("*")


def _arm_values(struct: ResolvedStruct, part: Part) -> list[str]:
	"""The type a variant's arm travels in, and one maker per arm.

	Each maker fixes which arm this is and what the discriminant holds, so
	a value cannot say one arm and carry another's body.
	"""
	held   = _arm_type(struct, part)
	name   = traverse.local_name(struct, part.placement)
	fields = [(at, _arm_fields(struct, arm))
	          for at, arm in enumerate(part.arms)]
	lines  = [
		"",
		f"/** Which arm of `{name}` a {struct.name} carries. */",
		f"struct {held} {{",
		"\tstd::uint32_t situ_arm;",
		"\tstd::uint64_t situ_tag;",
		*(["\tunion {",
		   *(line for at, given in fields if given for line in (
			   "\t\tstruct {",
			   *(f"\t\t\t{one};" for one in given),
			   f"\t\t}} arm_{at};")),
		   "\t} situ_as;"] if any(given for _, given in fields) else []),
	]
	for (at, given), arm in zip(fields, part.arms):
		tag = (f"static_cast<std::uint64_t>({arm.tag.local})"
		       if arm.tag is not None
		       else f"static_cast<std::uint64_t>({arm.value or 0}ll)")
		lines.extend([
			"",
			f"\tstatic {held} {bare_name(arm.name)}("
			+ ", ".join(given) + ") noexcept",
			"\t{",
			f"\t\t{held} situ_choice{{}};",
			"",
			f"\t\tsitu_choice.situ_arm = {at}u;",
			f"\t\tsitu_choice.situ_tag = {tag};",
			*(f"\t\tsitu_choice.situ_as.arm_{at}.{_field_name(one)} = "
			  f"{_field_name(one)};" for one in given),
			"\t\treturn situ_choice;",
			"\t}",
		])
	lines.append("};")
	return lines


def _chosen(struct: ResolvedStruct, structs: Mapping[str, ResolvedStruct],
		part: Part, parts: list[Part]) -> list[str]:
	"""A variant's slot: whichever arm the caller's value holds.

	The arm's part is written as it is for a builder of that arm alone,
	with what it reads bound to the names it expects.
	"""
	lines = [*([f"\tif ({part.local}.situ_tag != {part.agrees.local}.situ_tag) {{",
	            CONSTRAINT, "\t}"] if part.agrees is not None else []),
	         f"\tswitch ({part.local}.situ_arm) {{"]
	for at, arm in enumerate(part.arms):
		bound = _parameters(struct, [arm.part]) if arm.part else []
		body  = [f"\t{one} = {part.local}.situ_as.arm_{at}."
		         f"{_field_name(one)};" for one in bound]
		if arm.tag is not None:
			claimed = " || ".join(f"{part.local}.situ_tag == {value}u"
			                      for value in arm.tag.others)
			body += [f"\tif ({claimed}) {{", CONSTRAINT, "\t}"]
		if arm.lead is not None:
			body += _write(struct, structs, arm.lead, parts)
		if arm.part is not None:
			body += _write(struct, structs, arm.part, parts)
		lines.extend([f"\tcase {at}u: {{",
		              *(f"\t{line}" if line else "" for line in body),
		              "\t\tbreak;", "\t}"])
	lines.extend(["\tdefault:", CONSTRAINT, "\t}"])
	return lines


def _write(struct: ResolvedStruct, structs: Mapping[str, ResolvedStruct],
		part: Part, parts: list[Part]) -> list[str]:
	"""One part, as `c/build.py` writes it."""
	if part.role == MARKER:
		# C's: one of the schema's literals, written as the table spells
		# it, whose value says the order of its scope (26.1007).
		bits = part.bytes * BITS_PER_BYTE
		allowed = " || ".join(f"{part.local} == {v:#x}u"
		                      for v in (*part.little, *part.big))
		little = " || ".join(f"{part.local} == {v:#x}u" for v in part.little)
		return [f"\tif (!({allowed})) {{",
		        "\t\treturn ::situ::rt::err::constraint;", "\t}",
		        *_room(f"{part.bytes}u"),
		        (f"\tsitu_out[situ_at] = static_cast<std::uint8_t>({part.local});"
		         if bits == 8 else
		         f"\tsitu_put_be{bits}(situ_out + situ_at, "
		         f"static_cast<std::uint{bits}_t>({part.local}));"),
		        f"\tsitu_at += {part.bytes}u;",
		        f"\tconst bool situ_little_{part.placement.name} = ({little});"]
	if part.role == ARG:
		# C's: no byte, and where it is a byte order the fields in its
		# scope are stored in the order it says (26.1018).
		name = traverse.argument_name(part.placement)
		return [f"\tconst bool situ_little_{part.placement.name} = ("
		        + " || ".join(f"{name} == {v:#x}u" for v in part.little)
		        + ");"] if part.little else []
	if part.role == FRAMED:
		# C's: zeros to where the struct's own framing ends it (26.1004).
		return ["\t{",
		        "\t\tstd::uint32_t situ_end = 0u;",
		        f"\t\tconst ::situ::rt::err situ_framed = ::situ::"
		        f"{class_name(struct)}::required(situ_out, situ_at, situ_end);",
		        "\t\tif ((situ_framed != ::situ::rt::err::ok",
		        "\t\t     && situ_framed != ::situ::rt::err::truncated)"
		        " || situ_end < situ_at) {",
		        "\t\t\treturn ::situ::rt::err::constraint;", "\t\t}",
		        "\t\tif (situ_cap - situ_at < situ_end - situ_at) {",
		        "\t\t\treturn ::situ::rt::err::bounds;", "\t\t}",
		        "\t\tstd::memset(situ_out + situ_at, 0, situ_end - situ_at);",
		        "\t\tsitu_at = situ_end;",
		        "\t}"]
	if part.role == CHOSEN:
		return _chosen(struct, structs, part, parts)
	if part.role == BITS:
		return _bits(part, parts)
	if part.role == NESTED:
		return _nested(part, class_name(structs[part.placement.type_name or ""]))
	if part.role == REPEAT:
		return _repeat(structs, part, parts)
	if part.role == ENDED:
		return _ended(part)
	if part.role == VARINT:
		return _varint(part, parts)
	if part.role == WIDE:
		return _wide(part)
	if part.role == PAD:
		# To the next boundary, from this struct's own base, which is where
		# the cursor counts from.
		align = traverse.pad_alignment(part.placement)
		return ["\t{",
		        *(f"\t{line}" for line in [f"\tconst std::uint32_t situ_pad = (({align}u - situ_at % {align}u) % {align}u);",
		                                     *_room("situ_pad"),
		                                     f"\tstd::memset(situ_out + situ_at, 0, situ_pad);",
		                                     "\tsitu_at += situ_pad;"]),
		        "\t}"]
	if part.role == TAG:
		# Its value covers bytes not written yet, so its place is kept and
		# it is filled once the message is whole.
		return [*_room(f"{part.bytes}u"),
		        f"\tconst std::uint32_t situ_tag_{part.local} = situ_at;",
		        f"\tstd::memset(situ_out + situ_at, 0, {part.bytes}u);",
		        f"\tsitu_at += {part.bytes}u;"]
	if part.role == ARMTAG:
		return _store(part, f"{part.chooser.local}.situ_tag" if part.chooser
		              else f"{part.value}u")
	if part.role == PEEKED:
		return []		# the bytes it reads belong to what follows
	if part.role == SCALAR:
		# A value a named arm claims would make the message read as that
		# arm, so the default builder refuses it.
		return [*([f"\tif ({' || '.join(f'{part.local} == {v}u' for v in part.others)}) {{",
		           CONSTRAINT, "\t}"] if part.others else []),
		        *_store(part, part.local)]
	if part.role == SIZE:
		measured = next(one for one in parts if one.placement is part.sizes)
		if part.inner:
			return [*_walk(structs, measured, parts),
			        *_size(part, "situ_count")]
		return _size(part, _length(measured.local, parts))
	if part.role == ZERO:
		ones = traverse.reserved_policy(part.placement.attrs) == "must_be_one"
		return [*_room(f"{part.bytes}u"),
		        f"\tstd::memset(situ_out + situ_at, {'0xFF' if ones else '0'},"
		        f" {part.bytes}u);",
		        f"\tsitu_at += {part.bytes}u;"]
	if part.role == LITERAL:
		spelt = ", ".join(f"0x{byte:02X}u" for byte in part.literal)
		return [*_room(f"{part.bytes}u"),
		        "\t{",
		        f"\t\tstatic constexpr std::uint8_t situ_fixed[{part.bytes}] ="
		        f" {{ {spelt} }};",
		        "",
		        f"\t\tstd::memcpy(situ_out + situ_at, situ_fixed, {part.bytes}u);",
		        "\t}",
		        f"\tsitu_at += {part.bytes}u;"]
	if part.role == RUN:
		return [f"\tif ({part.local} == nullptr) {{", BOUNDS, "\t}",
		        *_room(f"{part.bytes}u"),
		        f"\tstd::memcpy(situ_out + situ_at, {part.local}, {part.bytes}u);",
		        f"\tsitu_at += {part.bytes}u;"]
	assert part.role == SPAN, part.role
	return [*([f"\tif ({part.local}_len != {part.twin}_len) {{",
	           "\t\treturn ::situ::rt::err::constraint;", "\t}"]
	          if part.twin else []),
	        *_room(f"{part.local}_len"),
	        f"\tif ({part.local}_len != 0u) {{",
	        f"\t\tif ({part.local} == nullptr) {{",
	        "\t\t\treturn ::situ::rt::err::bounds;",
	        "\t\t}",
	        f"\t\tstd::memcpy(situ_out + situ_at, {part.local}, {part.local}_len);",
	        "\t}",
	        f"\tsitu_at += {part.local}_len;"]


def _parameters(struct: ResolvedStruct, parts: list[Part]) -> list[str]:
	found: list[str] = []
	for part in parts:
		if part.role == CHOSEN:
			found.append(f"{_arm_type(struct, part)} {part.local}")
		elif part.role == ARG:
			found.append(f"{_scalar(part.placement)} "
			             f"{traverse.argument_name(part.placement)}")
		elif part.role == VARINT and plain_build.takes_value(part):
			kind = "std::int64_t" if part.minimal else "std::uint64_t"
			found.append(f"{kind} {part.local}")
		elif plain_build.takes_value(part):
			found.append(f"{_scalar(part.placement)} {part.local}")
		elif part.role == RUN:
			found.append(f"const std::uint8_t *{part.local}")
		elif part.role == WIDE:
			found.append(f"const {_scalar(part.placement)} *{part.local}")
			if not part.bytes:
				found.append(f"std::uint32_t {part.local}_count")
		elif part.role == TAG and part.opens:
			# The pseudo-header its codec sums, which no member holds.
			found.extend([f"const std::uint8_t *{part.local}_prefix",
			              f"std::uint32_t {part.local}_prefix_len"])
		elif part.role in plain_build.MEASURED:
			found.extend([f"const std::uint8_t *{part.local}",
			              f"std::uint32_t {part.local}_len"])
	return found


def _counted(parts: list[Part]) -> bool:
	return any(one.role == REPEAT and plain_build.counts_elements(one, parts)
	           for one in parts)


def _filled(struct: ResolvedStruct, parts: list[Part]) -> list[str]:
	"""Each tag, computed over what was written, stored, and checked: the
	check is what proves the store."""
	found: list[str] = []
	for part in parts:
		if part.role != TAG:
			continue
		name = traverse.local_name(struct, part.placement)
		bits = part.bytes * BITS_PER_BYTE
		over = (f"{part.local}_prefix, {part.local}_prefix_len"
		        if part.opens else "")
		found.extend([
			*([f"\tif ({part.local}_prefix == nullptr) {{", BOUNDS, "\t}"]
			  if part.opens else []),
			"\tif (situ_e == ::situ::rt::err::ok) {",
			"\t\tstd::uint32_t situ_tag = 0;",
			"",
			f"\t\tsitu_e = situ_view.{name}_compute("
			+ (f"{over}, " if over else "") + "situ_tag);",
			"\t\tif (situ_e == ::situ::rt::err::ok) {",
			*([f"\t\t\tsitu_out[situ_tag_{part.local}] = "
			   "static_cast<std::uint8_t>(situ_tag);"] if bits == 8 else
			  [f"\t\t\tsitu_put_{plain_build.tag_order(part)}{bits}("
			   f"situ_out + situ_tag_{part.local},",
			   f"\t\t\t              static_cast<std::uint{bits}_t>"
			   "(situ_tag));"]),
			f"\t\t\tsitu_e = situ_view.{name}_check({over});",
			"\t\t}",
			"\t}"])
	return found


def _held_to_the_reader(struct: ResolvedStruct,
		parts: list[Part]) -> list[str]:
	"""A `while` run ends where a predicate stops holding, which is the
	reader's to evaluate, so its count is asked rather than reproduced."""
	found: list[str] = []
	for part in parts:
		if part.role == REPEAT and part.value == 1:
			name = traverse.local_name(struct, part.placement)
			found.extend([
				"\tif (situ_e == ::situ::rt::err::ok",
				f"\t    && situ_view.{name}_count() != situ_count) {{",
				"\t\tsitu_e = ::situ::rt::err::constraint;",
				"\t}"])
		elif part.verify:
			# C's: as long as the reader says, once whole (26.1016).
			name = traverse.local_name(struct, part.placement)
			found.extend([
				"\tif (situ_e == ::situ::rt::err::ok",
				f"\t    && situ_view.{name}().size() != {part.local}_len) {{",
				"\t\tsitu_e = ::situ::rt::err::constraint;",
				"\t}"])
	return found


def generate(schema: ast.Schema, resolved: ResolvedSchema,
		basename: str) -> dict[str, str]:
	"""`<basename>_build.hpp`, or nothing where no struct qualifies.

	A rung adds files rather than changing them (0032).
	"""
	shape = plain_build.shape(schema, resolved)
	built = plain_build.units(resolved, shape)
	if not built:
		return {}

	guard = f"SITU_{c_name(basename).upper()}_BUILD_HPP"
	lines = [
		f"/* Generated by situc {__version__} from {basename}.situ"
		" -- do not edit.",
		" *",
		" * Append-only building: the write half of rung 2. Each member goes",
		" * down in schema order into backing you supply, nothing moves once",
		" * written, and no view into the buffer exists while it is being",
		" * filled -- which is why this needs rung 2's permission and nothing",
		" * above it.",
		" *",
		" * A size field is computed from the run it measures rather than",
		" * supplied, so a message cannot disagree with its own schema. A",
		" * layout this cannot write forward has no builder here, and",
		" * `situc build` names it. Nothing here allocates.",
		" */",
		"",
		f"#ifndef {guard}",
		f"#define {guard}",
		"",
		"#include <cstdint>",
		"#include <cstring>",
		"",
		f"#include \"{basename}.hpp\"",
		"",
		"namespace situ_build {",
	]

	for unit in built:
		struct = unit.struct
		held   = class_name(struct)
		parts  = list(unit.parts)
		name   = f"{held}_{c_name(unit.suffix)}" if unit.suffix else held
		length = "" if struct.layout.is_fixed_size else " situ_at,"
		for part in parts:
			if part.role == CHOSEN:
				lines.extend(_arm_values(struct, part))
		lines.extend([
			"",
			f"/** Build a {struct.name} into `situ_out` and validate the result.",
			" *",
			" * `situ_wrote` is set only on success. `bounds` where `situ_cap`",
			" * is short of what the members need, `constraint` where a run",
			" * is longer than its size field can say, and whatever `validate`",
			" * answers for a result the schema refuses.",
			" */",
			f"[[nodiscard]] inline ::situ::rt::err {name}(",
			"\t\tstd::uint8_t *situ_out,",
			"\t\tstd::uint32_t situ_cap,",
			*(f"\t\t{one}," for one in _parameters(struct, parts)),
			"\t\tstd::uint32_t &situ_wrote) noexcept",
			"{",
			"\tstd::uint32_t situ_at = 0;",
			*(["\tstd::uint32_t situ_count = 0;"]
			  if _counted(parts) else []),
			"",
			"\tif (situ_out == nullptr) {",
			BOUNDS,
			"\t}",
			"",
		])
		for part in parts:
			lines.extend([*_write(struct, resolved.structs, part, parts), ""])
		lines.extend([
			"\t/* Over exactly what was written, not over the backing: a longer",
			"\t * buffer would hide a struct that came out short. */",
			"\t::situ::rt::message situ_msg(situ_out, situ_at);",
			f"\t::situ::{held} situ_view;",
			f"\t::situ::rt::err situ_e = ::situ::{held}::at(situ_msg, 0,{length}"
			+ "".join(f" {traverse.argument_name(one.placement)},"
			          for one in parts if one.role == ARG)
			+ " situ_view);",
			"",
			*_filled(struct, parts),
			*_held_to_the_reader(struct, parts),
			"\tif (situ_e == ::situ::rt::err::ok) {",
			"\t\tsitu_e = situ_view.validate();",
			# C's: a stated length past a checked span's bytes is met as
			# truncation, which a whole message cannot be (26.1016).
			*(["\t\tif (situ_e == ::situ::rt::err::bounds) {",
			   "\t\t\tsitu_e = ::situ::rt::err::constraint;",
			   "\t\t}"] if any(one.verify for one in parts) else []),
			"\t}",
			"\tif (situ_e == ::situ::rt::err::ok) {",
			"\t\tsitu_wrote = situ_at;",
			"\t}",
			"\treturn situ_e;",
			"}",
		])

	lines.extend(["", "}  /* namespace situ_build */", "",
	              f"#endif /* {guard} */"])
	return {f"{basename}_build.hpp": "\n".join(lines) + "\n"}
