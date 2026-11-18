"""`[chars = "..."]`: the bytes a run may hold (26.1099).

A character class is a set of byte values, written as a regular
expression's bracket is without the brackets: single characters and
`a-z` ranges, a `-` literal where it opens or ends the class, and the
string literal's own escapes (`\\x2D`, `\\"`) for anything else. There is no
negation: a class says what is allowed, which is what a reader checks and
what a contract promises, and "everything but" is a different promise
every time the domain grows.

`[encoding = ascii]` is the nearest existing constraint and is wider: an
ADDRESS host is letters, digits and `.-:`, an HTTP token is RFC 9110's
`tchar`, a DNS label is letters, digits and `-`, and a base64 or hex body
is its alphabet. Each is a run of bytes whose every byte is in a set, and
nothing said so.

Bytes, not characters. Every reader checks byte by byte against the same
256-bit table this module computes, so the C, C++, Rust and Python
backends, both walkers and the dissector cannot disagree about a member
of the class -- the table is the definition, and the string is how a
person writes it.
"""

from __future__ import annotations

from situc.diagnostics import Span, error

#: One bit per byte value, low bit first: byte `b` is bit `b % 8` of the
#: table's byte `b // 8`. The image carries exactly this.
TABLE_BYTES = 32


def parse(text: str, span: Span) -> frozenset[int]:
	"""The byte values `text` names, or a refusal at `span`."""
	if not text:
		raise error("`[chars]` names no byte", span,
		            label = "an empty class admits nothing",
		            notes = ["a run that may hold no byte is a run of length "
		                     "zero; say that with its length"])
	values = [ord(one) for one in text]
	wide = next((one for one in values if one > 0xFF), None)
	if wide is not None:
		raise error(f"`[chars]` names U+{wide:04X}, which is not a byte",
		            span, label = "a class is a set of byte values",
		            notes = ["a character outside one byte is several bytes "
		                     "in any encoding, and a class checks each byte "
		                     "on its own; write the bytes, or constrain the "
		                     "encoding"])
	held: set[int] = set()
	at = 0
	while at < len(values):
		first = values[at]
		# `a-z`: a range, unless the `-` is the class's last byte.
		if at + 2 < len(values) and values[at + 1] == ord("-"):
			last = values[at + 2]
			if last < first:
				raise error(
					f"`[chars]` range `{chr(first)}-{chr(last)}` runs "
					"backwards", span,
					label = f"0x{first:02X} is above 0x{last:02X}",
					notes = ["write the lower byte first"])
			held.update(range(first, last + 1))
			at += 3
			continue
		held.add(first)
		at += 1
	return frozenset(held)


def _shown(value: int) -> str:
	"""One byte as the class spells it: itself where printable and not one of
	the three bytes the syntax gives a meaning, `\\xNN` otherwise."""
	if 0x21 <= value <= 0x7E and chr(value) not in '-"\\':
		return chr(value)
	return f"\\x{value:02X}"


def canonical(held: frozenset[int]) -> str:
	"""`held` written one way: ascending, a run of three or more as a range.

	So two spellings of one class -- `"a-cx"` and `"xabc"` -- render the
	same in a wire signature, and a comparison of two contracts is a
	comparison of sets rather than of how somebody typed them.
	"""
	out: list[str] = []
	ordered = sorted(held)
	at = 0
	while at < len(ordered):
		end = at
		while end + 1 < len(ordered) and ordered[end + 1] == ordered[end] + 1:
			end += 1
		if end - at >= 2:
			out.append(f"{_shown(ordered[at])}-{_shown(ordered[end])}")
		else:
			out.extend(_shown(one) for one in ordered[at:end + 1])
		at = end + 1
	return "".join(out)


def table(held: frozenset[int]) -> bytes:
	"""The 256-bit membership table every reader checks against."""
	bits = bytearray(TABLE_BYTES)
	for value in held:
		bits[value >> 3] |= 1 << (value & 7)
	return bytes(bits)


def of(attrs: tuple[object, ...]) -> frozenset[int] | None:
	"""The class a member's attributes declare, or None. Parsed again rather
	than cached: the attribute is a string literal, and `parse` has already
	refused anything this would raise on by the time a backend asks."""
	attr = next((one for one in attrs
	             if getattr(one, "name", None) == "chars"), None)
	if attr is None:
		return None
	value = getattr(attr, "value", None)
	return parse(getattr(value, "value", ""), attr.span)	# type: ignore[attr-defined]
