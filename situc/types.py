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


@dataclass(frozen=True)
class ScalarType:
	"""A scalar with a known width in bits."""

	name: str
	kind: ScalarKind
	bits: int

	@property
	def signed(self) -> bool:
		return self.kind is ScalarKind.SINT

	@property
	def is_bit_packed(self) -> bool:
		"""Whether this type packs with its neighbours rather than starting on
		a byte boundary.

		Section 8.1 states the rule twice and the two statements disagree about
		widths like u48. Resolved in docs/decisions/0005-integer-widths.md in
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


def is_scalar_name(name: str) -> bool:
	try:
		return lookup(name) is not None
	except WidthError:
		return True
