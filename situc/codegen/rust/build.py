"""Append-only building in Rust: the write half of rung 2 (26.858, 26.860).

`plain_build` says which structs and C's plan what each part is. The
sequence is C's: bounds-check what is left, store at the cursor, advance,
and take a view over exactly what was written and `validate()` it, because
constraints are the view's to state and two checks of one schema is how
they come to disagree.

**A member is taken as its integer, not its enum.** A Rust enum cannot hold
a value nothing names, and `default = pass` says a message may carry one.

**A fixed run is `&[u8; N]`**, so the compiler holds the length C's header
can only ask for; a measured run is a slice, whose `len()` is its size.

**The backing is `&mut [u8]`**, so the compiler already says what C's
header asks for: no view of the buffer exists while it is being filled.
"""

from __future__ import annotations

from situc import __version__, ast, traverse
from situc.codegen import plain_build
from situc.codegen.c.build import (ARMTAG, BITS, CHOSEN, ENDED, LITERAL, NESTED,
                                   PAD, PEEKED, REPEAT, RUN, SCALAR, SIZE, SPAN,
                                   TAG, VARINT, WIDE, ZERO, ArmOf, Part, Shape)
from situc.codegen.plain_build import plain
from situc.codegen.c.build import peeked_byte
from situc.codegen.varint import varint_put_tail, varint_stem
from situc.codegen.rust.edit import _scalar
from situc.codegen.rust.emit import _ident, _pascal, _writer
from situc.layout import BITS_PER_BYTE
from situc.resolve import ResolvedSchema, ResolvedStruct

__all__ = ["generate", "plain", "declined", "Shape"]


def declined(resolved: ResolvedSchema,
		shape: Shape) -> list[tuple[str, str]]:
	"""What this builder does not build, and why, for stderr."""
	return plain_build.declined(resolved, shape, "Rust")


def _room(size: str) -> list[str]:
	return [f"\tif situ_out.len() - situ_at < {size} {{",
	        "\t\treturn Err(Error::Bounds);", "\t}"]


def _bit_store(part: Part, value: str) -> list[str]:
	"""Set a field inside a group `_bits` zeroed; the cursor stays put."""
	held = part.placement
	assert held.scalar is not None
	msb = traverse.bit_extractor(held.scalar, held) == "msb"
	return [f"\tsitu_rt::write_bits(&mut situ_out[situ_at..], {part.bit_at},"
	        f" {held.size_bits}, {str(msb).lower()}, {value} as u64);"]


def _digits(part: Part, value: str) -> list[str]:
	"""A number written as text: `digits` wide, or as few as it needs.

	C's `_value`. The fixed form pads with zeros, and the minimal form is
	that with the width counted first, then the byte that ends it. A
	negative value's digits are its magnitude's, and the sign is a byte more.
	"""
	assert part.placement.scalar is not None
	signed = part.placement.scalar.signed
	call   = "situ_rt::format_int" if signed else "situ_rt::format_uint"
	widest = "i64" if signed else "u64"
	held   = f"({value} as {widest})"
	refuse = ["\t\treturn Err(Error::Constraint);", "\t}"]
	if not part.minimal:
		return [*_room(str(part.digits)),
		        f"\tif !{call}(&mut situ_out[situ_at..situ_at + {part.digits}],"
		        f" {part.radix}, {held}) {{", *refuse,
		        f"\tsitu_at += {part.digits};"]
	left = f"{held}.unsigned_abs()" if signed else held
	return [
		"\t{",
		"\t\tlet mut situ_digits = 1usize;",
		f"\t\tlet mut situ_left: u64 = {left};",
		f"\t\twhile situ_left >= {part.radix} {{",
		f"\t\t\tsitu_left /= {part.radix};",
		"\t\t\tsitu_digits += 1;",
		"\t\t}",
		*([f"\t\tif {held} < 0 {{", "\t\t\tsitu_digits += 1;", "\t\t}"]
		  if signed else []),
		"\t\tif situ_out.len() - situ_at < situ_digits + 1 {",
		"\t\t\treturn Err(Error::Bounds);",
		"\t\t}",
		f"\t\tif !{call}(&mut situ_out[situ_at..situ_at + situ_digits],"
		f" {part.radix}, {held}) {{",
		"\t\t\treturn Err(Error::Constraint);",
		"\t\t}",
		"\t\tsitu_at += situ_digits;",
		f"\t\tsitu_out[situ_at] = {part.delimiter[0]:#04x};",
		"\t\tsitu_at += 1;",
		"\t}",
	]


def _store(part: Part, value: str) -> list[str]:
	"""One scalar's bounds check, store and advance -- C's three steps."""
	assert part.placement.scalar is not None
	if part.role == BITS:
		return _bit_store(part, value)
	if part.radix:
		return _digits(part, value)
	width = part.placement.scalar.bits // BITS_PER_BYTE
	return [
		*_room(str(width)),
		f"\tsitu_rt::{_writer(part.placement.endian)}(situ_out, situ_at,"
		f" {width}, {value} as u64);",
		f"\tsitu_at += {width};",
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
			*([f"\tif {length} as u64 > {limit} {{",
			   "\t\treturn Err(Error::Constraint);", "\t}"]
			  if width < 64 else []),
			*_store(part, f"{length}"),
		]
	scale, offset = part.solved.scale, part.solved.offset
	shift = f" + {-offset}" if offset < 0 else f" - {offset}" if offset else ""
	return [
		"\t{",
		f"\t\tlet situ_solved = {length} as i64{shift};",
		"\t\tif situ_solved < 0"
		+ (f" || situ_solved % {scale} != 0" if scale != 1 else "") + " {",
		"\t\t\treturn Err(Error::Constraint);",
		"\t\t}",
		*([f"\t\tlet situ_solved = situ_solved / {scale};"]
		  if scale != 1 else []),
		*([f"\t\tif situ_solved > {limit} {{",
		   "\t\t\treturn Err(Error::Constraint);",
		   "\t\t}"] if limit < (1 << 63) - 1 else []),
		*(f"\t{line}" for line in _store(part, "situ_solved")),
		"\t}",
	]


def _wide(part: Part) -> list[str]:
	"""Elements wider than a byte: the caller passes values, and the byte
	order is the writer's to apply."""
	local = _ident(part.local)
	each  = part.bit_at
	return [
		*([f"\tif {local}.len() != {part.bytes} {{",
		   "\t\treturn Err(Error::Constraint);", "\t}"]
		  if part.bytes else []),
		f"\tif {local}.len() > (situ_out.len() - situ_at) / {each} {{",
		"\t\treturn Err(Error::Bounds);", "\t}",
		f"\tfor situ_value in {local} {{",
		f"\t\tsitu_rt::{_writer(part.placement.endian)}(situ_out, situ_at,"
		f" {each}, *situ_value as u64);",
		f"\t\tsitu_at += {each};",
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
	call = f"situ_rt::{varint_stem(part.declared)}_put"
	if part.stated is not None:
		held  = f"{part.stated}u64"
		value = held
	elif part.sizes is not None:
		run   = next(one.local for one in parts
		             if one.placement is part.sizes)
		held  = f"{_ident(run)}.len() as u64"
		value = held
	elif part.minimal:
		held  = f"{_ident(part.local)} as u64"
		value = f"situ_rt::zigzag_encode({_ident(part.local)})"
	else:
		held  = f"{_ident(part.local)} as u64"
		value = held
	return [
		*(["\tif " + " || ".join(f"{_ident(part.local)} == {v}"
		                         for v in part.others)
		   + " {", "\t\treturn Err(Error::Constraint);", "\t}"]
		  if part.others else []),
		*([f"\tif {held} > {most} {{",
		   "\t\treturn Err(Error::Constraint);", "\t}"]
		  if part.value < 64 else []),
		f"\tlet situ_used = {call}(&mut situ_out[situ_at..], {value}{tail});",
		"\tif situ_used == 0 {",
		"\t\treturn Err(Error::Bounds);",
		"\t}",
		"\tsitu_at += situ_used;",
	]


def _nested(part: Part) -> list[str]:
	"""One whole inner struct the caller built.

	Its extent is its own to state, so the writer asks rather than trusting
	the length it was given: three bytes too many shift every member after
	it. And where its first byte is a peeked discriminant it has to say
	which arm this is, or the bytes are a valid message of another arm.
	"""
	local = _ident(part.local)
	inner = _pascal(part.placement.type_name or "")
	refuse = ["\t\treturn Err(Error::Constraint);", "\t}"]
	return [
		*([] if not part.others and part.value <= 0 else [
			f"\tif {local}.is_empty() {{", *refuse,
			(f"\tif {peeked_byte(local, part)} != {part.value} {{" if part.value >= 0
			 else "\tif "
			      + " || ".join(f"{peeked_byte(local, part)} == {v}" for v in part.others)
			      + " {"),
			*refuse]),
		# An arm that ends the message: its bytes are one whole value
		# however long, if they validate as one (26.973).
		*([f"\tif {inner}::new({local}).and_then(|held| held.validate())"
		   ".is_err() {",
		   *refuse] if part.tail else [
		f"\tmatch {inner}::required({local}) {{",
		f"\t\tsitu_rt::Framing::Complete(n) if n == {local}.len() => {{}}",
		"\t\t_ => return Err(Error::Constraint),",
		"\t}"]),
		*_room(f"{local}.len()"),
		f"\tsitu_out[situ_at..situ_at + {local}.len()]"
		f".copy_from_slice({local});",
		f"\tsitu_at += {local}.len();",
	]


def _walk(part: Part, parts: list[Part]) -> list[str]:
	"""Step through a run of inner structs the caller built.

	C's `_walk`, and its reasons: the element's own `required` says where
	one ends, a zero-byte element would never advance the loop, and a
	delimiter breaks the run only at an element boundary, where the reader
	would stop. An element the buffer ended rather than `required` measured
	is taken on approval, and a failure there means the bytes are not a
	whole number of elements.
	"""
	local  = _ident(part.local)
	inner  = _pascal(part.placement.type_name or "")
	count  = plain_build.counts_elements(part, parts)
	needle = ", ".join(f"{byte:#04x}" for byte in part.delimiter)
	return [
		*(["\tlet mut situ_count = 0usize;"] if count else []),
		"\t{",
		"\t\tlet mut situ_scan = 0usize;",
		f"\t\twhile situ_scan < {local}.len() {{",
		*([f"\t\t\tif {local}[situ_scan..].starts_with(&[{needle}]) {{",
		   "\t\t\t\treturn Err(Error::Constraint);",
		   "\t\t\t}"] if part.delimiter else []),
		f"\t\t\tlet (situ_need, situ_tail) = match {inner}::required("
		f"&{local}[situ_scan..]) {{",
		"\t\t\t\tsitu_rt::Framing::Complete(n) => (n, false),",
		f"\t\t\t\t_ => ({local}.len() - situ_scan, true),",
		"\t\t\t};",
		"\t\t\tif situ_need == 0 {",
		"\t\t\t\treturn Err(Error::Constraint);",
		"\t\t\t}",
		f"\t\t\tlet situ_element = {inner}::new(&{local}[situ_scan..])"
		".map_err(|_| Error::Constraint)?;",
		"\t\t\tif let Err(situ_verdict) = situ_element.validate() {",
		"\t\t\t\treturn Err(if situ_tail { Error::Constraint } "
		"else { situ_verdict });",
		"\t\t\t}",
		"\t\t\tsitu_scan += situ_need;",
		*(["\t\t\tsitu_count += 1;"] if count else []),
		"\t\t}",
		"\t}",
	]


def _needle(delimiter: bytes) -> list[str]:
	"""The delimiter that ends a run, and the cursor past it."""
	wide   = len(delimiter)
	spelt  = ", ".join(f"{byte:#04x}" for byte in delimiter)
	return [*_room(f"{wide}"),
	        f"\tsitu_out[situ_at..situ_at + {wide}]"
	        f".copy_from_slice(&[{spelt}]);",
	        f"\tsitu_at += {wide};"]


def _ended(part: Part) -> list[str]:
	"""Bytes, then the delimiter that ends them.

	C's two forms. With no escape a run holding the delimiter would read
	back short, and is refused. With one the caller hands over content, and
	exactly two kinds of byte carry the escape: the delimiter's first byte
	where it begins one, and the escape itself.
	"""
	local  = _ident(part.local)
	spelt  = ", ".join(f"{byte:#04x}" for byte in part.delimiter)
	escape = part.placement.delimiter_escape
	if escape is None:
		return [f"\tif {local}.windows({len(part.delimiter)})"
		        f".any(|situ_w| situ_w == [{spelt}]) {{",
		        "\t\treturn Err(Error::Constraint);", "\t}",
		        *_room(f"{local}.len()"),
		        f"\tsitu_out[situ_at..situ_at + {local}.len()]"
		        f".copy_from_slice({local});",
		        f"\tsitu_at += {local}.len();",
		        *_needle(part.delimiter)]
	return [f"\tfor situ_scan in 0..{local}.len() {{",
	        f"\t\tif {local}[situ_scan] == {escape:#04x}"
	        f" || {local}[situ_scan..].starts_with(&[{spelt}]) {{",
	        "\t\t\tif situ_out.len() - situ_at < 1 {",
	        "\t\t\t\treturn Err(Error::Bounds);",
	        "\t\t\t}",
	        f"\t\t\tsitu_out[situ_at] = {escape:#04x};",
	        "\t\t\tsitu_at += 1;",
	        "\t\t}",
	        "\t\tif situ_out.len() - situ_at < 1 {",
	        "\t\t\treturn Err(Error::Bounds);",
	        "\t\t}",
	        f"\t\tsitu_out[situ_at] = {local}[situ_scan];",
	        "\t\tsitu_at += 1;",
	        "\t}",
	        *_needle(part.delimiter)]


def _repeat(part: Part, parts: list[Part]) -> list[str]:
	local  = _ident(part.local)
	return [
		*([] if plain_build.walked_at_size(part, parts)
		  else _walk(part, parts)),
		*([f"\tif situ_count != {part.digits} {{",
		   "\t\treturn Err(Error::Constraint);", "\t}"]
		  if part.digits > 0 else []),
		*_room(f"{local}.len()"),
		f"\tsitu_out[situ_at..situ_at + {local}.len()]"
		f".copy_from_slice({local});",
		f"\tsitu_at += {local}.len();",
		*(_needle(part.delimiter) if part.delimiter else []),
	]


def _bits(part: Part, parts: list[Part]) -> list[str]:
	"""One field of a byte several share.

	The group is zeroed when it opens, which is what writes `must_be_zero`,
	and the bytes are spent when it closes. A value wider than its field is
	refused rather than masked.
	"""
	held  = part.placement
	limit = (1 << (held.size_bits or 0)) - 1
	lines = [*_room(str(part.bytes)),
	         f"\tsitu_out[situ_at..situ_at + {part.bytes}].fill(0);"] \
		if part.opens else []
	if part.sizes is not None:
		run = next(one.local for one in parts
		           if one.placement is part.sizes)
		lines += _size(part, f"{_ident(run)}.len()")
	elif part.chooser is not None:
		lines += [*([f"\tif {_tag(part)} > {limit} {{",
		             "\t\treturn Err(Error::Constraint);", "\t}"]
		            if any(one.tag for one in part.chooser.arms) else []),
		          *_bit_store(part, _tag(part))]
	elif part.stated is not None:
		lines += _bit_store(part, str(part.stated))
	elif plain_build.takes_value(part):
		local = _ident(part.local)
		lines += [*([f"\tif {' || '.join(f'{local} as u64 == {v}' for v in part.others)} {{",
		             "\t\treturn Err(Error::Constraint);", "\t}"]
		            if part.others else []),
		          f"\tif {local} as u64 > {limit} {{",
		          "\t\treturn Err(Error::Constraint);", "\t}",
		          *_bit_store(part, local)]
	elif plain_build.stated_ones(part):
		lines += _bit_store(part, str(limit))
	if part.closes:
		lines.append(f"\tsitu_at += {part.bytes};")
	return lines


def _tag(part: Part) -> str:
	"""What a discriminant holds, from the arm the caller handed over."""
	assert part.chooser is not None
	return f"{_ident(part.chooser.local)}.situ_tag()"


def _arm_type(struct: ResolvedStruct, part: Part) -> str:
	name = traverse.local_name(struct, part.placement)
	return f"{_pascal(struct.name)}{_pascal(name)}Arm"


def arm_variant(name: str) -> str:
	"""An arm's case in its enum: `self` capitalises into a keyword."""
	spelt = _pascal(name)
	return f"{spelt}_" if spelt == "Self" else spelt


def _arm_fields(struct: ResolvedStruct, arm: ArmOf) -> list[str]:
	"""The types an arm's value holds, its discriminant first."""
	held = [one for one in (arm.tag, arm.part) if one is not None]
	return [one.split(": ", 1)[1] for one in _parameters(struct, held)]


def _borrows(struct: ResolvedStruct, part: Part) -> bool:
	return any("&" in kind for arm in part.arms
	           for kind in _arm_fields(struct, arm))


def _elided(struct: ResolvedStruct, part: Part) -> str:
	return "<'_>" if _borrows(struct, part) else ""


def _arm_values(struct: ResolvedStruct, part: Part) -> list[str]:
	"""The enum a variant's arm travels in.

	Which case it is says what the discriminant holds, so a value cannot
	say one arm and carry another's body.
	"""
	held     = _arm_type(struct, part)
	name     = traverse.local_name(struct, part.placement)
	lifetime = "<'a>" if _borrows(struct, part) else ""
	lines = [
		"",
		f"/// Which arm of `{name}` a {struct.name} carries.",
		"#[derive(Clone, Copy)]",
		f"pub enum {held}{lifetime} {{",
	]
	for arm in part.arms:
		fields = ", ".join(kind.replace("&", "&'a ")
		                   for kind in _arm_fields(struct, arm))
		lines.append(f"\t{arm_variant(arm.name)}" + (f"({fields})," if fields
		                                         else ","))
	lines.extend([
		"}",
		"",
		f"impl {held}{_elided(struct, part)} {{",
		"\tfn situ_tag(&self) -> u64 {",
		"\t\tmatch self {",
	])
	for arm in part.arms:
		case = f"Self::{arm_variant(arm.name)}"
		if arm.tag is not None:
			lines.append(f"\t\t\t{case}(situ_given, ..) => *situ_given as u64,")
			continue
		shape = "(..)" if arm.part is not None else ""
		lines.append(f"\t\t\t{case}{shape} => {arm.value or 0}i64 as u64,")
	lines.extend(["\t\t}", "\t}", "}"])
	return lines


def _chosen(struct: ResolvedStruct, part: Part, parts: list[Part]
		) -> list[str]:
	"""A variant's slot: whichever arm the caller's value holds.

	The arm's part is written as it is for a builder of that arm alone, the
	pattern binding what it reads to the name it expects.
	"""
	held  = _arm_type(struct, part)
	lines = [*([f"\tif {_ident(part.local)}.situ_tag() != "
	            f"{_ident(part.agrees.local)}.situ_tag() {{",
	            "\t\treturn Err(Error::Constraint);", "\t}"]
	           if part.agrees is not None else []),
	         f"\tmatch {_ident(part.local)} {{"]
	for arm in part.arms:
		bound = [*(["situ_given"] if arm.tag else []),
		         *([_ident(arm.part.local)] if arm.part else [])]
		lines.append(f"\t\t{held}::{arm_variant(arm.name)}"
		             + (f"({', '.join(bound)})" if bound else "") + " => {")
		if arm.tag is not None:
			claimed = " || ".join(f"situ_given as u64 == {value}"
			                      for value in arm.tag.others)
			lines.extend([f"\t\t\tif {claimed} {{",
			              "\t\t\t\treturn Err(Error::Constraint);",
			              "\t\t\t}"])
		for one in (arm.lead, arm.part):
			if one is not None:
				lines.extend(f"\t\t{line}" if line else ""
				             for line in _write(struct, one, parts))
		lines.append("\t\t}")
	lines.append("\t}")
	return lines


def _write(struct: ResolvedStruct, part: Part, parts: list[Part]
		) -> list[str]:
	"""One part, as `c/build.py` writes it."""
	if part.role == BITS:
		return _bits(part, parts)
	if part.role == NESTED:
		return _nested(part)
	if part.role == REPEAT:
		return _repeat(part, parts)
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
		        *(f"\t{line}" for line in [f"\tlet situ_pad = ({align} - situ_at % {align}) % {align};",
		                                     *_room("situ_pad"),
		                                     "\tsitu_out[situ_at..situ_at + situ_pad].fill(0);",
		                                     "\tsitu_at += situ_pad;"]),
		        "\t}"]
	if part.role == TAG:
		# Its value covers bytes not written yet, so its place is kept and
		# it is filled once the message is whole.
		return [*_room(f"{part.bytes}"),
		        f"\tlet situ_tag_{part.local} = situ_at;",
		        f"\tsitu_out[situ_at..situ_at + {part.bytes}].fill(0);",
		        f"\tsitu_at += {part.bytes};"]
	if part.role == ARMTAG:
		return _store(part, _tag(part) if part.chooser else str(part.value))
	if part.role == CHOSEN:
		return _chosen(struct, part, parts)
	if part.role == PEEKED:
		return []		# the bytes it reads belong to what follows
	if part.role == SCALAR:
		# A value a named arm claims would make the message read as that
		# arm, so the default builder refuses it.
		local = _ident(part.local)
		return [*([f"\tif {' || '.join(f'{local} as u64 == {v}' for v in part.others)} {{",
		           "\t\treturn Err(Error::Constraint);", "\t}"]
		          if part.others else []),
		        *_store(part, local)]
	if part.role == SIZE:
		measured = next(one for one in parts if one.placement is part.sizes)
		if part.inner:
			return [*_walk(measured, parts), *_size(part, "situ_count")]
		return _size(part, f"{_ident(measured.local)}.len()")
	if part.role == ZERO:
		ones = traverse.reserved_policy(part.placement.attrs) == "must_be_one"
		return [*_room(str(part.bytes)),
		        f"\tsitu_out[situ_at..situ_at + {part.bytes}]"
		        f".fill({'0xFF' if ones else '0'});",
		        f"\tsitu_at += {part.bytes};"]
	if part.role == LITERAL:
		spelt = ", ".join(f"0x{byte:02X}" for byte in part.literal)
		return [*_room(str(part.bytes)),
		        f"\tsitu_out[situ_at..situ_at + {part.bytes}]"
		        f".copy_from_slice(&[{spelt}]);",
		        f"\tsitu_at += {part.bytes};"]
	local = _ident(part.local)
	if part.role == RUN:
		return [*_room(str(part.bytes)),
		        f"\tsitu_out[situ_at..situ_at + {part.bytes}]"
		        f".copy_from_slice({local});",
		        f"\tsitu_at += {part.bytes};"]
	assert part.role == SPAN, part.role
	return [*_room(f"{local}.len()"),
	        f"\tsitu_out[situ_at..situ_at + {local}.len()]"
	        f".copy_from_slice({local});",
	        f"\tsitu_at += {local}.len();"]


def _parameters(struct: ResolvedStruct, parts: list[Part]) -> list[str]:
	found: list[str] = []
	for part in parts:
		name = _ident(part.local)
		if part.role == CHOSEN:
			found.append(f"{name}: {_arm_type(struct, part)}"
			             f"{_elided(struct, part)}")
		elif part.role == VARINT and plain_build.takes_value(part):
			found.append(f"{name}: {'i64' if part.minimal else 'u64'}")
		elif plain_build.takes_value(part):
			found.append(f"{name}: {_scalar(part.placement)}")
		elif part.role == RUN:
			found.append(f"{name}: &[u8; {part.bytes}]")
		elif part.role == TAG and part.opens:
			# The pseudo-header its codec sums, which no member holds.
			found.append(f"{part.local}_prefix: &[u8]")
		elif part.role == WIDE:
			found.append(f"{name}: &[{_scalar(part.placement)}]")
		elif part.role in plain_build.MEASURED:
			found.append(f"{name}: &[u8]")
	return found


def _filled(struct: ResolvedStruct, held: str,
		parts: list[Part]) -> list[str]:
	"""Each tag, computed over what was written, stored, and checked: the
	check is what proves the store. A view at a time, because storing
	needs the buffer the view borrows."""
	found: list[str] = []
	for part in parts:
		if part.role != TAG:
			continue
		name = traverse.local_name(struct, part.placement)
		bits = part.bytes * BITS_PER_BYTE
		at   = f"situ_tag_{part.local}"
		over = f"{part.local}_prefix" if part.opens else ""
		found.extend([
			f"\tlet situ_tag = {held}::new(&situ_out[..situ_at])?"
			f".{name}_compute({over})?;",
			f"\tsitu_out[{at}..{at} + {part.bytes}].copy_from_slice("
			f"&(situ_tag as u{bits}).to_{plain_build.tag_order(part)}_bytes());",
			f"\t{held}::new(&situ_out[..situ_at])?.{name}_check({over})?;"])
	return found


def _held_to_the_reader(struct: ResolvedStruct,
		parts: list[Part]) -> list[str]:
	"""A `while` run ends where a predicate stops holding, which is the
	reader's to evaluate, so its count is asked rather than reproduced."""
	found: list[str] = []
	for part in parts:
		if part.role == REPEAT and part.value == 1:
			name = traverse.local_name(struct, part.placement)
			found.extend([f"\tif situ_view.{name}_count() != situ_count {{",
			              "\t\treturn Err(Error::Constraint);", "\t}"])
	return found


def generate(schema: ast.Schema, resolved: ResolvedSchema,
		basename: str) -> dict[str, str]:
	"""`<basename>_build.rs`, or nothing where no struct qualifies.

	A rung adds files rather than changing them (0032).
	"""
	shape = plain_build.shape(schema, resolved)
	built = plain_build.units(resolved, shape)
	if not built:
		return {}

	lines = [
		f"// Generated by situc {__version__} from {basename}.situ"
		" -- do not edit.",
		"//",
		"// Append-only building: the write half of rung 2. Each member goes",
		"// down in schema order into backing you supply, nothing moves once",
		"// written, and the `&mut` is the compiler saying no view of the",
		"// buffer exists while it is being filled.",
		"//",
		"// A size field is computed from the run it measures rather than",
		"// supplied, so a message cannot disagree with its own schema. A",
		"// layout this cannot write forward has no builder here, and",
		"// `situc build` names it. Nothing allocates; `situ_rt` is no_std.",
		"",
		f"use crate::{basename}::*;",
		"use crate::situ_rt::{self, Error, Result};",
	]

	for unit in built:
		struct = unit.struct
		held   = _pascal(struct.name)
		parts  = list(unit.parts)
		name   = _ident(struct.name).removeprefix("r#")
		if unit.suffix:
			name = f"{name}_{_ident(unit.suffix).removeprefix('r#')}"
		for part in parts:
			if part.role == CHOSEN:
				lines.extend(_arm_values(struct, part))
		lines.extend([
			"",
			f"/// Build a {struct.name} into `situ_out` and validate the result.",
			"///",
			"/// Returns its length. `Err(Bounds)` where the slice is short of",
			"/// what the members need, `Err(Constraint)` where a run is longer",
			"/// than its size field can say, and whatever `validate` answers",
			"/// for a result the schema refuses.",
			f"pub fn build_{name}(",
			"\tsitu_out: &mut [u8],",
			*(f"\t{one}," for one in _parameters(struct, parts)),
			") -> Result<usize> {",
			"\tlet mut situ_at = 0usize;",
			"",
		])
		for part in parts:
			lines.extend([*_write(struct, part, parts), ""])
		lines.extend([
			"\t// Over exactly what was written, not over the backing: a longer",
			"\t// slice would hide a struct that came out short.",
			*_filled(struct, held, parts),
			f"\tlet situ_view = {held}::new(&situ_out[..situ_at])?;",
			*_held_to_the_reader(struct, parts),
			"\tsitu_view.validate()?;",
			"\tOk(situ_at)",
			"}",
		])

	return {f"{basename}_build.rs": "\n".join(lines) + "\n"}
