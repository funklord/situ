"""The scalar type table (project.md section 8.1).

Deliberately extensible: fixed-point (`q16_16`) and BCD are named in section 8.1
as deferred, and land by adding kinds here rather than by touching the parser.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

MAX_WIDTH = 64


class ScalarKind(Enum):
	UINT	= "unsigned integer"
	SINT	= "signed integer"
	FLOAT	= "floating point"
	BIT	= "bit"
	SFIXED	= "signed fixed point"
	UFIXED	= "unsigned fixed point"
	BCD	= "packed binary-coded decimal"


@dataclass(frozen=True)
class ScalarType:
	"""A scalar with a known width in bits."""

	name: str
	kind: ScalarKind
	bits: int

	#: Fractional bits, for fixed point. Zero for everything else, which makes
	#: `bits - frac_bits` the integer part for any type without a special case.
	frac_bits: int = 0

	#: Decimal digits, for BCD. Four bits each, so `digits * 4 == bits`.
	digits: int = 0

	@property
	def signed(self) -> bool:
		return self.kind in (ScalarKind.SINT, ScalarKind.SFIXED)

	@property
	def is_fixed_point(self) -> bool:
		return self.kind in (ScalarKind.SFIXED, ScalarKind.UFIXED)

	@property
	def is_bcd(self) -> bool:
		return self.kind is ScalarKind.BCD

	@property
	def int_bits(self) -> int:
		"""The integer part of a fixed-point type, sign bit included."""
		return self.bits - self.frac_bits

	@property
	def scale(self) -> int:
		"""What the stored integer is divided by to get the value it means."""
		return 1 << self.frac_bits

	@property
	def decimal_max(self) -> int:
		"""The largest value a BCD field can hold.

		All nines, *unless the field is narrower than its digits*. Decision
		0027 lets a register put a control bit above packed decimal --
		`bcd2 [bits = 7]` is seven bits of it -- and then the top digit has
		three bits rather than four, so it stops at seven and the field stops
		at 79.

		It answered 99 for that field: a `_MAX` macro naming a value the
		field cannot hold, and a setter that writes 0x99 into seven bits and
		reads back 19. The digits are what the type is named for; the bits
		are what there are.
		"""
		if self.digits < 1:
			return 0

		low      = int(10 ** (self.digits - 1))
		top_bits = self.bits - BITS_PER_DIGIT * (self.digits - 1)
		top      = min(9, (1 << top_bits) - 1) if top_bits > 0 else 0
		return top * low + low - 1

	@property
	def is_bit_packed(self) -> bool:
		"""Whether this type packs with its neighbours rather than starting on
		a byte boundary.

		Section 8.1 states the rule twice and the two statements disagree about
		widths like u48. Resolved in doc/decision/0005-integer-widths.md in
		favour of the operational form: a width that is a whole number of bytes
		is a byte-aligned scalar, everything else packs.
		"""
		return self.bits % 8 != 0

	@property
	def crosses_byte_boundary(self) -> bool:
		"""Whether a bit-packed field of this width must straddle a byte.

		True for any packed width above 8, at any starting bit offset. Section
		8.2 makes straddling an error without `[allow_straddle]`, because it
		silently forces a multi-byte read-modify-write.
		"""
		return self.is_bit_packed and self.bits > 8


# Types with a name rather than a width suffix. `bool` is u1 with a value
# constraint and `byte` is u8 for which endianness is irrelevant; both are
# section 8.1 aliases, so they resolve to the underlying scalar here and the
# distinction is carried by the field, not the type.
NAMED_SCALARS = {
	"bit":   ScalarType("bit",   ScalarKind.BIT,   1),
	"bool":  ScalarType("bool",  ScalarKind.UINT,  1),
	"byte":  ScalarType("byte",  ScalarKind.UINT,  8),
	"f16":   ScalarType("f16",   ScalarKind.FLOAT, 16),
	"f32":   ScalarType("f32",   ScalarKind.FLOAT, 32),
	"f64":   ScalarType("f64",   ScalarKind.FLOAT, 64),
}

WIDTH_SUFFIX = re.compile(r"\A([ui])([0-9]+)\Z")

# Fixed point, in the Q notation section 8.1 names: `q16_16` is a signed value
# with 16 integer bits (the sign among them) and 16 fractional, and `uq16_16`
# is the unsigned one. The width is the sum, so the existing bit-packing rule
# applies to it unchanged.
FIXED_SUFFIX = re.compile(r"\A(u?)q([0-9]+)_([0-9]+)\Z")

# Packed BCD: `bcd8` is eight decimal digits, a nibble each. Real hardware
# counts digits rather than bits -- an RTC holds `bcd2` for seconds -- so that
# is what the name says, and the width follows.
BCD_SUFFIX = re.compile(r"\Abcd([0-9]+)\Z")

BITS_PER_DIGIT = 4


class WidthError(Exception):
	"""A `uN`/`iN` name whose width is out of range or malformed."""


def lookup(name: str) -> ScalarType | None:
	"""Resolve a scalar type name, or None if it is not a scalar at all.

	Raises WidthError for a name that is unmistakably a width form but invalid,
	so the parser can say "u65 is too wide" rather than "unknown type u65".
	"""
	named = NAMED_SCALARS.get(name)
	if named is not None:
		return named

	fixed = _fixed_point(name)
	if fixed is not None:
		return fixed

	bcd = _bcd(name)
	if bcd is not None:
		return bcd

	match = WIDTH_SUFFIX.match(name)
	if match is None:
		return None

	prefix, digits = match.group(1), match.group(2)

	if len(digits) > 1 and digits.startswith("0"):
		raise WidthError(f"`{name}` has a leading zero in its width")

	width = int(digits)
	if width < 1 or width > MAX_WIDTH:
		raise WidthError(f"`{name}` is out of range; widths run from 1 to {MAX_WIDTH}")

	kind = ScalarKind.UINT if prefix == "u" else ScalarKind.SINT
	return ScalarType(name, kind, width)


def _digits_ok(name: str, digits: str, what: str) -> int:
	if len(digits) > 1 and digits.startswith("0"):
		raise WidthError(f"`{name}` has a leading zero in its {what}")
	return int(digits)


def _fixed_point(name: str) -> ScalarType | None:
	"""`q<int>_<frac>`, or `uq<int>_<frac>` for the unsigned form."""
	match = FIXED_SUFFIX.match(name)
	if match is None:
		return None

	unsigned, integer, fraction = match.groups()
	int_bits  = _digits_ok(name, integer, "integer width")
	frac_bits = _digits_ok(name, fraction, "fractional width")
	width     = int_bits + frac_bits

	if int_bits < 1:
		raise WidthError(f"`{name}` has no integer bits; a fixed-point type "
		                 f"needs at least one, and a signed one spends it on "
		                 f"the sign")
	if frac_bits < 1:
		raise WidthError(f"`{name}` has no fractional bits, so it is the "
		                 f"integer type {'u' if unsigned else 'i'}{int_bits}")
	if width > MAX_WIDTH:
		raise WidthError(f"`{name}` is {width} bits; widths run from 1 to "
		                 f"{MAX_WIDTH}")

	kind = ScalarKind.UFIXED if unsigned else ScalarKind.SFIXED
	return ScalarType(name, kind, width, frac_bits=frac_bits)


def _bcd(name: str) -> ScalarType | None:
	"""`bcd<digits>`: packed decimal, one nibble to a digit."""
	match = BCD_SUFFIX.match(name)
	if match is None:
		return None

	digits = _digits_ok(name, match.group(1), "digit count")
	width  = digits * BITS_PER_DIGIT

	if digits < 1:
		raise WidthError(f"`{name}` has no digits")
	if width > MAX_WIDTH:
		raise WidthError(f"`{name}` is {digits} digits, which is {width} bits; "
		                 f"widths run from 1 to {MAX_WIDTH}")

	return ScalarType(name, ScalarKind.BCD, width, digits=digits)


def is_scalar_name(name: str) -> bool:
	try:
		return lookup(name) is not None
	except WidthError:
		return True


#: The attributes that narrow a field's numeric range. `Solver.constrain`
#: reads exactly these and skips everything else in the loop, and
#: `check_attribute_values` refuses each one written without a value --
#: `[max]` alone narrows nothing, so accepting it is the schema making a
#: claim the generated code does not carry (14.5).
#:
#: One list rather than two: the guard that refuses a text bound and the
#: guard that refuses a missing one are about the same three names, and
#: two copies of that set would drift (invariant 13).
NUMERIC_BOUNDS = frozenset({"must_eq", "max", "min"})


def pinned_shown(expected: bytes) -> str:
	"""A pinned run rendered for a comment, on one line and in ASCII.

	`bytes.decode(..., "backslashreplace")` is not this: it escapes bytes
	that are not ASCII and leaves the ones that are, so a CRLF preamble came
	through as a real carriage return and line feed. Embedded in a `//`
	comment that ends the comment and makes the rest of the line code --
	which is exactly what it did, in Rust, in four backends at once.

	The bytes are the point of this construct, so the rendering has to be
	reversible by eye and can never be a newline.
	"""
	out = []
	for byte in expected:
		if byte == 0x5C:			# a backslash escapes itself
			out.append("\\\\")
		elif 0x20 <= byte < 0x7F and byte != 0x22:
			out.append(chr(byte))
		else:
			out.append(f"\\x{byte:02x}")
	return "".join(out)
