"""Decode into an owned C struct, for callers that cannot hold a view.

Situ's usual output is a view: a base, a limit, and accessors that are
arithmetic against the buffer. That is the right shape for a parser that
reads a frame and answers questions about it while the bytes are still
there, and it is the wrong shape for a codebase whose callers hold decoded
values on the stack and outlive the buffer entirely:

    fzp_group_show_resp_t v;                  /* on the caller's stack */
    if (fzp_ctrl_decode_group_show_resp(payload, len, &v) != FZP_OK)
            return 1;
    /* payload is gone from here on; v is the data */

A project reporting 225 such call sites evaluated situ and said no, and was
right to: swapping a codec is one change and swapping how every caller reads
is another. What it asked for instead was this -- a fixed-size C type per
struct and a `decode(buf, len, out)` returning an error code, which is what
it already has 225 of, written by hand.

WHAT THIS EMITS, AND WHY IT IS A SEPARATE FILE. A `<name>_owned.h` and
`<name>_owned.c` beside the ordinary output rather than inside it. The two
models have different costs and a caller should have to choose: the owned
struct copies every field out of the buffer, so it cannot alias, cannot
mutate in place, and cannot be zero-copy. Emitting both into one header
would make the expensive one reachable by autocomplete.

WHAT IT REFUSES, AND WHY THAT IS MOST OF THE DESIGN. Only a struct whose
layout is a fixed size gets an owned form. A variable-length member has no
honest fixed-size C field: the choices are a pointer, which reintroduces the
lifetime the caller was trying to escape, or an array of the worst case,
which is a silent decision about memory nobody asked for. Refusing says so
rather than picking (section 17.0). The same goes for a variant, a region,
or a TLV run, each of which is a length the data decides.
"""

from __future__ import annotations

from collections.abc import Mapping

from situc import ast
from situc.types import ScalarKind
from situc.codegen.c.names import bare_name, c_name, ident, macro
from situc.layout import BITS_PER_BYTE, Placement
from situc.resolve import ResolvedSchema, ResolvedStruct
from situc.traverse import own_entries

WORD_WIDTHS = (8, 16, 32, 64)


def storage_width(bits: int) -> int:
	for width in WORD_WIDTHS:
		if bits <= width:
			return width
	return 64


def owned_structs(resolved: ResolvedSchema) -> list[ResolvedStruct]:
	"""The structs an owned form can be emitted for.

	Fixed size and nothing else, for the reason in the module docstring. A
	register file is excluded as well: its members are bus transactions
	rather than bytes in a buffer, so there is no buffer to decode from.
	"""
	return [struct for struct in resolved.structs.values()
	        if struct.layout.is_fixed_size
	        and struct.layout.register is None
	        and struct.layout.size_bytes > 0
	        and all(_can_own(entry.placement, resolved)
	                for entry in own_entries(struct))]


def _can_own(placement: Placement, resolved: ResolvedSchema) -> bool:
	"""Whether one member has an honest fixed-size C field."""
	if placement.kind in ("variant", "region", "tlv", "opaque", "coded",
	                      "sealed", "authenticated"):
		return False
	if placement.delimiter is not None or placement.repeat_while is not None:
		return False
	if placement.sized_by is not None:
		return False

	# `preserve` and `unknown` say the bits are somebody else's to keep. An
	# owned struct drops them by construction -- it holds fields, not the
	# buffer -- so the round-trip this mode promises cannot be made.
	if placement.kind == "reserved" \
			and _reserved_policy(placement.attrs) in ("preserve", "unknown"):
		return False

	# A nested struct is owned only if it is itself ownable, which is the
	# same question one level down and is why this is not a flat check.
	if placement.scalar is None:
		inner = resolved.structs.get(placement.type_name or "")
		return inner is not None and inner.layout.is_fixed_size

	return True


def refusals(resolved: ResolvedSchema) -> list[tuple[str, str]]:
	"""Every struct that gets no owned form, and the reason.

	Reported rather than silently skipped: a caller who asked for this mode
	and received a header missing the struct they wanted would conclude the
	generator was broken, which is worse than being told the layout does not
	admit one.
	"""
	found = []
	for name, struct in resolved.structs.items():
		if struct.layout.register is not None:
			continue
		if struct.layout.is_fixed_size and struct.layout.size_bytes == 0:
			found.append((name, "it occupies no bytes"))
			continue
		if not struct.layout.is_fixed_size:
			found.append((name, "its size is decided by the data, so an owned "
			                    "struct would need a pointer or a worst-case "
			                    "array; neither is this generator's to choose"))
			continue

		blocked = [entry.placement for entry in own_entries(struct)
		           if not _can_own(entry.placement, resolved)]
		if blocked:
			found.append((name, f"`{blocked[0].name}` has no fixed-size C "
			                    f"field ({blocked[0].kind})"))
	return found


def _ctype(placement: Placement, prefix: str, enums: Mapping[str, object]) -> str:
	"""The C type of one owned field."""
	if placement.type_name in enums:
		return f"{ident(prefix, placement.type_name or '')}_t"

	scalar = placement.scalar
	assert scalar is not None
	if scalar.kind is ScalarKind.FLOAT:
		return {16: "uint16_t", 32: "float", 64: "double"}[scalar.bits]

	width = storage_width(scalar.bits)
	return f"int{width}_t" if scalar.signed else f"uint{width}_t"


def _read(placement: Placement, prefix: str) -> str:
	"""The expression that lifts one scalar out of `data`."""
	scalar = placement.scalar
	assert scalar is not None and placement.offset_bits is not None

	offset = placement.offset_bits // BITS_PER_BYTE
	width  = storage_width(scalar.bits)

	if placement.offset_bits % BITS_PER_BYTE or scalar.bits not in WORD_WIDTHS:
		# Bit-packed: the runtime's extractor takes a bit offset and a width,
		# and the bit order is the schema's rather than the host's.
		order = "msb" if placement.bit_order is ast.BitOrder.MSB_FIRST else "lsb"
		return (f"(uint{width}_t)situ_bits_get_{order}(data, "
		        f"{placement.offset_bits}u, {scalar.bits}u)")

	if scalar.bits == 8:
		return f"data[{offset}u]"

	end = "le" if placement.endian is ast.Endian.LITTLE else "be"
	return f"situ_get_{end}{scalar.bits}(data + {offset}u)"


def _write(placement: Placement, prefix: str, value: str) -> list[str]:
	"""The statements that put one scalar back into `data`."""
	scalar = placement.scalar
	assert scalar is not None and placement.offset_bits is not None

	offset = placement.offset_bits // BITS_PER_BYTE

	if placement.offset_bits % BITS_PER_BYTE or scalar.bits not in WORD_WIDTHS:
		order = "msb" if placement.bit_order is ast.BitOrder.MSB_FIRST else "lsb"
		return [f"\tsitu_bits_set_{order}(data, {placement.offset_bits}u, "
		        f"{scalar.bits}u, (uint64_t){value});"]

	if scalar.bits == 8:
		return [f"\tdata[{offset}u] = (uint8_t){value};"]

	# Cast to the writer's own unsigned parameter. A signed field is
	# two's complement on the wire and the runtime takes the unsigned
	# width, so the conversion is exact -- but it is a conversion, and
	# `-Wsign-conversion` is right to want it written down. Four of the
	# twenty-seven examples have a signed field, and all four failed to
	# compile until this cast existed.
	end   = "le" if placement.endian is ast.Endian.LITTLE else "be"
	width = storage_width(scalar.bits)
	return [f"\tsitu_put_{end}{scalar.bits}(data + {offset}u, "
	        f"(uint{width}_t){value});"]


def _fields(struct: ResolvedStruct, prefix: str,
		enums: Mapping[str, object], resolved: ResolvedSchema) -> list[str]:
	"""The members of the owned struct, in declaration order.

	`reserved` is not among them, deliberately. Reserved bytes are a
	constraint rather than a value (section 8.8): the decoder checks them and
	the encoder writes them, and a field the caller can set would invite
	setting them to something else.
	"""
	lines = []
	for entry in own_entries(struct):
		placement = entry.placement
		if placement.kind == "reserved":
			continue

		name = bare_name(placement.name)
		if placement.scalar is None:
			inner = resolved.structs.get(placement.type_name or "")
			assert inner is not None
			lines.append(f"\t{ident(prefix, inner.name)}_t {name};")
		elif placement.radix is not None:
			# A text number is `array_count` *digits* holding one value, not
			# an array of values. Declaring it an array made the decoder read
			# four bytes per digit and run off the end of the struct -- 24
			# bytes past a cpio header, and the same misreading the walk had
			# (26.84, 26.85).
			lines.append(f"\t{_ctype(placement, prefix, enums)} {name};")
		elif placement.array_count is not None:
			held = _ctype(placement, prefix, enums)
			lines.append(f"\t{held} {name}[{placement.array_count}u];")
		else:
			lines.append(f"\t{_ctype(placement, prefix, enums)} {name};")
	return lines


def _decode_digits(placement: Placement, name: str, prefix: str,
		enums: Mapping[str, object]) -> list[str]:
	"""Parse a text number into the one value it holds (section 8.6.2).

	The same call the view accessor makes, and for the same reason: the
	scalar type gives the value's *domain* rather than its width in the
	buffer, which for a text number depends on the number.

	A spelling this cannot give back is refused rather than accepted. The
	owned form stores a value, fixed width makes the leading zeros
	mandatory, and the only freedom left is case -- so a lower-case hex
	digit is a second encoding of one number, and `encode` would hand back
	the other one. Refusing keeps `decode` then `encode` byte-exact, which
	is what the round-trip test asserts and what `canonical` already claims
	of these fields.
	"""
	assert placement.offset_bits is not None and placement.array_count
	assert placement.radix is not None
	radix  = placement.radix
	offset = placement.offset_bits // BITS_PER_BYTE
	digits = placement.array_count
	held   = _ctype(placement, prefix, enums)
	most   = placement.radix_max

	lines = ["\t{", "\t\tuint64_t held;", ""]
	if radix > 10:
		lines.extend([
			f"\t\tif (situ_digits_canonical(data + {offset}u, {digits}u) == 0) {{",
			"\t\t\treturn SITU_ERR_CONSTRAINT;",
			"\t\t}",
		])
	lines.extend([
		f"\t\tif (situ_parse_uint(data + {offset}u, {digits}u, "
		f"{radix}u, {most}u, &held) != 0) {{",
		"\t\t\treturn SITU_ERR_CONSTRAINT;",
		"\t\t}",
		f"\t\tout->{name} = ({held})held;",
		"\t}",
	])
	return lines


def _element(placement: Placement, index: str) -> str:
	"""One element of a scalar run, read out of `data`."""
	scalar = placement.scalar
	assert scalar is not None and placement.offset_bits is not None

	stride = scalar.bits // BITS_PER_BYTE
	base   = placement.offset_bits // BITS_PER_BYTE
	at     = f"{base}u + {index} * {stride}u" if stride > 1 else f"{base}u + {index}"

	if scalar.bits == 8:
		return f"data[{at}]"
	end = "le" if placement.endian is ast.Endian.LITTLE else "be"
	return f"situ_get_{end}{scalar.bits}(data + {at})"


def _decode_body(struct: ResolvedStruct, prefix: str,
		enums: Mapping[str, object], resolved: ResolvedSchema) -> list[str]:
	lines = []
	for entry in own_entries(struct):
		placement = entry.placement
		name      = bare_name(placement.name)

		if placement.kind == "reserved":
			continue

		if placement.scalar is None:
			inner  = resolved.structs.get(placement.type_name or "")
			assert inner is not None and placement.offset_bits is not None
			offset = placement.offset_bits // BITS_PER_BYTE
			lines.append(f"\t{{")
			lines.append(f"\t\tconst situ_err_t err = "
			             f"{ident(prefix, inner.name, 'decode')}("
			             f"data + {offset}u, "
			             f"{macro(prefix, inner.name, 'SIZE_FIXED')}, "
			             f"&out->{name});")
			lines.append("")
			lines.append("\t\tif (err != SITU_OK) {")
			lines.append("\t\t\treturn err;")
			lines.append("\t\t}")
			lines.append("\t}")
			continue

		if placement.radix is not None:
			lines.extend(_decode_digits(placement, name, prefix, enums))
			continue

		if placement.array_count is not None:
			held = _ctype(placement, prefix, enums)
			lines.append(f"\tfor (uint32_t i = 0; i < {placement.array_count}u;"
			             f" ++i) {{")
			lines.append(f"\t\tout->{name}[i] = ({held})({_element(placement, 'i')});")
			lines.append("\t}")
			continue

		held = _ctype(placement, prefix, enums)
		lines.append(f"\tout->{name} = ({held})({_read(placement, prefix)});")

	return lines


def _reserved_policy(attrs: tuple[ast.Attr, ...]) -> str:
	"""Reserved behaviour, defaulting to must_be_zero (section 8.8)."""
	for attr in attrs:
		if attr.name in ("must_be_zero", "must_be_one", "preserve", "unknown"):
			return attr.name
	return "must_be_zero"


def _reserved_write(placement: Placement) -> list[str]:
	"""Put a reserved field back, whatever width it is.

	`preserve` and `unknown` are not written: both say the bits belong to
	somebody else, so an owned struct has nothing to put there and the
	round-trip cannot be claimed for them. They are refused at the top of
	this module rather than silently mangled here.
	"""
	assert placement.offset_bits is not None and placement.size_bits

	policy = _reserved_policy(placement.attrs)
	value  = "0u" if policy == "must_be_zero" else \
	         f"{(1 << placement.size_bits) - 1}u"

	if placement.offset_bits % BITS_PER_BYTE \
			or placement.size_bits % BITS_PER_BYTE:
		order = "msb" if placement.bit_order is ast.BitOrder.MSB_FIRST else "lsb"
		return [f"\tsitu_bits_set_{order}(data, {placement.offset_bits}u, "
		        f"{placement.size_bits}u, (uint64_t){value});"]

	offset = placement.offset_bits // BITS_PER_BYTE
	width  = placement.size_bits // BITS_PER_BYTE
	fill   = "0" if policy == "must_be_zero" else "0xFF"
	return [f"\tmemset(data + {offset}u, {fill}, {width}u);"]


def _encode_body(struct: ResolvedStruct, prefix: str,
		enums: Mapping[str, object], resolved: ResolvedSchema) -> list[str]:
	lines = []
	for entry in own_entries(struct):
		placement = entry.placement
		name      = bare_name(placement.name)
		assert placement.offset_bits is not None
		offset = placement.offset_bits // BITS_PER_BYTE

		if placement.kind == "reserved":
			# Written rather than left alone: the caller's buffer is not
			# known to be zeroed, and a reserved field is a value the format
			# fixes rather than one nobody looks at.
			#
			# Including the sub-byte ones, which is where this first went
			# wrong. `reserved u3 [must_be_zero]` is three bits with no whole
			# byte to memset, so an earlier version skipped it, left the
			# caller's buffer showing through, and broke the round-trip on
			# every schema with a bit-packed reserved field. The round-trip
			# test found it on the eighth random draw.
			lines.extend(_reserved_write(placement))
			continue

		if placement.scalar is None:
			inner = resolved.structs.get(placement.type_name or "")
			assert inner is not None
			lines.append("\t{")
			lines.append(f"\t\tconst situ_err_t err = "
			             f"{ident(prefix, inner.name, 'encode')}(&in->{name}, "
			             f"data + {offset}u, "
			             f"{macro(prefix, inner.name, 'SIZE_FIXED')});")
			lines.append("")
			lines.append("\t\tif (err != SITU_OK) {")
			lines.append("\t\t\treturn err;")
			lines.append("\t\t}")
			lines.append("\t}")
			continue

		if placement.radix is not None:
			offset = placement.offset_bits // BITS_PER_BYTE
			lines.extend([
				f"\tif (situ_format_uint(data + {offset}u, "
				f"{placement.array_count}u, {placement.radix}u, "
				f"(uint64_t)in->{name}) != 0) {{",
				"\t\treturn SITU_ERR_CONSTRAINT;",
				"\t}",
			])
			continue

		if placement.array_count is not None:
			scalar = placement.scalar
			stride = scalar.bits // BITS_PER_BYTE
			lines.append(f"\tfor (uint32_t i = 0; i < {placement.array_count}u;"
			             f" ++i) {{")
			if scalar.bits == 8:
				step = f"{offset}u + i"
				lines.append(f"\t\tdata[{step}] = (uint8_t)in->{name}[i];")
			else:
				end   = "le" if placement.endian is ast.Endian.LITTLE else "be"
				step  = f"{offset}u + i * {stride}u"
				width = storage_width(scalar.bits)
				lines.append(f"\t\tsitu_put_{end}{scalar.bits}(data + {step}, "
				             f"(uint{width}_t)in->{name}[i]);")
			lines.append("\t}")
			continue

		lines.extend(_write(placement, prefix, f"in->{name}"))

	return lines


def generate(schema: ast.Schema, resolved: ResolvedSchema, basename: str,
		prefix: str = "situ") -> dict[str, str]:
	"""The owned header and source, or an empty mapping if nothing qualifies."""
	enums   = dict(resolved.layout.env.enums)
	structs = owned_structs(resolved)
	if not structs:
		return {}

	guard = macro(prefix, basename, "OWNED_H")
	head  = [
		f"/* Generated by situc from {basename}.situ -- do not edit.",
		" *",
		" * Owned decode: a fixed-size C struct per layout, and a decode that",
		" * copies every field out of the buffer into it. For callers that hold",
		" * the decoded value after the bytes are gone.",
		" *",
		" * This is the expensive model and it is here because it was asked for.",
		" * Every field is copied and byte-swapped on decode, so nothing is",
		" * zero-copy, nothing aliases the buffer, and mutating the struct",
		" * changes no bytes until it is encoded again. Where a caller can hold a",
		" * view instead, the ordinary header costs less.",
		" */",
		"",
		f"#ifndef {guard}",
		f"#define {guard}",
		"",
		"#include <stdint.h>",
		"#include <string.h>",
		"",
		'#include "situ.h"',
		f'#include "{basename}.h"',
		"",
	]

	for struct in structs:
		name  = ident(prefix, struct.name)
		lines = _fields(struct, prefix, enums, resolved)
		head.extend([
			f"/* {struct.name}: {struct.layout.size_bytes} bytes on the wire. */",
			"typedef struct {",
			*(lines or ["\tchar situ_empty;"]),
			f"}} {name}_t;",
			"",
			f"/** Copy every field of a {struct.name} out of `data`.",
			" *",
			" * SITU_OK             decoded; `out` is complete and independent",
			" * SITU_ERR_BOUNDS     `len` is short of the fixed size",
			" * SITU_ERR_CONSTRAINT the bytes are the right length and say",
			" *                     something the schema forbids",
			" */",
			f"situ_err_t {name}_decode(const uint8_t *data, uint32_t len,",
			f"                         {name}_t *out);",
			"",
			f"/** Lay a {struct.name} back out into `data`. */",
			f"situ_err_t {name}_encode(const {name}_t *in, uint8_t *data,",
			f"                         uint32_t len);",
			"",
		])

	head.extend([f"#endif /* {guard} */", ""])

	body = [
		f"/* Generated by situc from {basename}.situ -- do not edit. */",
		"",
		f'#include "{basename}_owned.h"',
		"",
	]

	for struct in structs:
		name = ident(prefix, struct.name)
		size = macro(prefix, struct.name, "SIZE_FIXED")
		body.extend([
			f"situ_err_t {name}_decode(const uint8_t *data, uint32_t len,",
			f"                         {name}_t *out)",
			"{",
			f"\tif (len < {size}) {{",
			"\t\treturn SITU_ERR_BOUNDS;",
			"\t}",
			"",
			*_decode_body(struct, prefix, enums, resolved),
			"",
			"\t/* Constraints are the view's to state and this reuses them",
			"\t * rather than restating them: two checks of one schema is how",
			"\t * they come to disagree. */",
			"\t{",
			"\t\tsitu_msg_t  msg;",
			"\t\tsitu_view_t view;",
			"",
			"\t\tsitu_msg_init(&msg, (uint8_t *)(uintptr_t)data, len);",
			f"\t\tif ({ident(prefix, struct.name, 'view')}(&msg, 0, &view)"
			" != SITU_OK) {",
			"\t\t\treturn SITU_ERR_BOUNDS;",
			"\t\t}",
			f"\t\treturn {ident(prefix, struct.name, 'validate')}(view);",
			"\t}",
			"}",
			"",
			f"situ_err_t {name}_encode(const {name}_t *in, uint8_t *data,",
			f"                         uint32_t len)",
			"{",
			f"\tif (len < {size}) {{",
			"\t\treturn SITU_ERR_BOUNDS;",
			"\t}",
			"",
			*_encode_body(struct, prefix, enums, resolved),
			"",
			"\treturn SITU_OK;",
			"}",
			"",
		])

	return {f"{basename}_owned.h": "\n".join(head),
	        f"{basename}_owned.c": "\n".join(body)}
