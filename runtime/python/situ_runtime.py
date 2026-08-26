"""The Python runtime for generated situ accessors (section 26.17).

Python reaches people who would otherwise not describe their format at all,
which is the argument for the backend. It also enforces the least of the
lattice of any target situ has, and section 20.1 asks that a backend say so
rather than let a reader assume the C guarantees came along.

What holds here:

  * **Zero copy.** A view is a `memoryview` over the caller's buffer. Reading a
    field slices it; nothing is duplicated, and a write through a view is
    visible to whoever owns the bytes.
  * **Bounds.** Checked once at acquisition, as in C, and the slice enforces it
    thereafter.
  * **Invalidation (section 12.3).** A view carries the generation its message
    had when it was taken, and every access checks it. This is the one place
    Python is *stronger* than release-build C, where the check compiles out.
  * **Constraints.** `validate()` raises rather than returning a code, because
    that is what a Python caller will actually handle.

What does not:

  * **`atomic`.** Python has no notion of a single-instruction access. The GIL
    is not a statement about bus transactions, and `AtomicWord` in the map
    means nothing here.
  * **`repr`.** A `ValueConverted` field costs a shift or a swap, and a
    property makes that look like an attribute lookup. The map is where the
    cost is recorded; the syntax cannot show it.
  * **The stage gate (section 14.3).** The gate below refuses to construct
    without a verification token, which is a real run-time check. It is not the
    C++ guarantee: nothing stops `object.__new__`, and Python has no access
    control to lean on. A determined caller can reach a sealed interior
    unverified, and no Python backend can prevent that.

Standard library only, matching `situc` itself: this vendors into a build with
nothing installed.
"""

from __future__ import annotations

#: What the readers accept. Generated code passes a `memoryview` over the
#: message and never a `bytes`, so annotating these `bytes` was wrong from
#: the first one -- it went unnoticed because `memoryview` was not generic
#: until recently and mypy could not tell the two apart.
Buffer = bytes | bytearray | memoryview

import enum
import sys
from typing import Final, TypeVar

#: The enum a generated getter hands back, or the integer where the
#: value is not one the schema names (8.7).
EnumT = TypeVar("EnumT", bound=enum.Enum)

#: Whether *this* machine is big-endian, for `endian native` (8.3).
#:
#: Read here rather than decided by the compiler: situc runs on the machine
#: building the code and not on the machine running it, and a byte order
#: baked in by the generator is right only by coincidence on a cross build
#: (invariant 8). It is the reason this constant exists at all rather than
#: the emitter writing `big=True` or `big=False`.
#:
#: `endian native` read every field big-endian in this backend and in Rust,
#: silently, on every little-endian host -- no note, no diagnostic, the wrong
#: number. Nothing noticed because no schema in the repository used it.
NATIVE_BIG: Final = sys.byteorder == "big"


class SituError(Exception):
	"""Base for everything raised here, so a caller can catch one thing."""


class BoundsError(SituError):
	"""A view would not fit the buffer, or an index is past the end."""


class ConstraintError(SituError):
	"""A field holds a value the schema does not admit."""


class TruncatedError(SituError):
	"""The bytes so far are a valid prefix and more are needed.

	Not an error in the way the others are: a stream reader raises this on
	every partial read, which is why it is separate from `BoundsError` -- that
	one means a read went outside the buffer, a bug or an attack. Conflating
	them makes a receiver treat normal progress as hostile.

	`needed` is a lower bound on the total length, so a caller can size the
	next read instead of guessing.
	"""

	def __init__(self, message: str, needed: int) -> None:
		super().__init__(message)
		self.needed = needed


class StaleViewError(SituError):
	"""The message moved under this view (section 12.3).

	Raised rather than returned: a stale view is a bug in the caller, not a
	condition to branch on.
	"""


class VersionError(SituError):
	"""A member was reached in a message whose version does not carry it.

	Its own class rather than a `ConstraintError`, because the remedy is
	different in kind: a constraint failure means the message is malformed,
	and this means the message is fine and older than the field. A caller
	handling the two the same way would reject a peer it is supposed to
	interoperate with (section 19.4).
	"""


class StageError(SituError):
	"""A region's stage gate has not been passed (section 12.1).

	Distinct from `TagError`, which had been carrying both: a tag that fails
	to verify is a hostile or corrupt message, and reaching a sealed interior
	without opening the gate is a bug in the caller. A receiver that cannot
	tell them apart logs the second as an attack and the first as a typo.

	The C runtime has named the two separately since it was written; this one
	had six of the seven failure classes.
	"""


class TagError(SituError):
	"""A sealed region was opened without a verified tag."""


class Message:
	"""A buffer, and the generation that invalidates views of it."""

	__slots__ = ("buffer", "generation", "dirty")

	def __init__(self, buffer: bytearray | memoryview) -> None:
		self.buffer: Final = memoryview(buffer)
		self.generation = 0
		self.dirty      = 0

	def mark_dirty(self, bit: int) -> None:
		"""A covered write happened; what it invalidates is now stale.

		A tag or an invariant -- one word for both, because a message is
		either ready to send or it is not, and section 11.1 calls the axis
		"which obligation covers these bytes" for the same reason.
		"""
		self.dirty |= bit

	def clear_dirty(self, bit: int) -> None:
		self.dirty &= ~bit

	def transmittable(self) -> None:
		"""Raise unless every obligation is discharged (sections 14.2, 16.1)."""
		if self.dirty:
			raise TagError(
				f"a covered write left obligation bits {self.dirty:#x} stale; "
				f"recompute them before sending")

	def touch(self) -> None:
		"""Something moved. Every view taken before now is stale."""
		self.generation += 1


class View:
	"""A resolved base and extent over a message.

	Subclassed by every generated struct. The generation check on each access
	is what makes section 12.3 real here; it costs an integer compare, which is
	not a cost worth avoiding in Python.
	"""

	__slots__ = ("_msg", "_at", "_len", "_gen")

	SIZE_BYTES: int = 0
	SIZE_MIN: int   = 0

	def __init__(self, msg: Message, at: int, length: int) -> None:
		self._msg = msg
		self._at  = at
		self._len = length
		self._gen = msg.generation

	@property
	def bytes(self) -> memoryview:
		"""The bytes this view covers. Zero copy, and writable."""
		self._check()
		return self._msg.buffer[self._at:self._at + self._len]

	@property
	def _span(self) -> memoryview:
		"""The same bytes, under a name a schema member cannot take.

		`bytes` is an ordinary field name, and a member called that emits a
		property which *overrides* the one above -- so generated code reading
		`self.bytes` would get the member's few bytes rather than the view's.
		Nothing generated from a schema starts with an underscore, so this
		spelling cannot be captured. Generated code uses it; a caller reads
		`bytes` and gets what they asked for either way (26.80).
		"""
		self._check()
		return self._msg.buffer[self._at:self._at + self._len]

	def _check(self) -> None:
		if self._gen != self._msg.generation:
			raise StaleViewError(
				f"this view was taken at generation {self._gen}; the message "
				f"is at {self._msg.generation}. Something written since then "
				f"moved the bytes it points at.")

	def _read(self, offset: int, width: int, *, signed: bool,
			big: bool) -> int:
		self._check()
		start = self._at + offset
		return int.from_bytes(self._msg.buffer[start:start + width],
		                      "big" if big else "little", signed=signed)

	def _write(self, offset: int, width: int, value: int, *, signed: bool,
			big: bool) -> None:
		self._check()
		start = self._at + offset
		self._msg.buffer[start:start + width] = value.to_bytes(
			width, "big" if big else "little", signed=signed)

	def _bits(self, offset_bits: int, width: int, *, msb: bool,
			signed: bool) -> int:
		"""A bit-packed field, read through the bytes it lives in."""
		self._check()
		first = offset_bits // 8
		last  = (offset_bits + width - 1) // 8
		span  = (last - first + 1) * 8
		raw   = int.from_bytes(
			self._msg.buffer[self._at + first:self._at + last + 1], "big")

		skip  = offset_bits - first * 8
		shift = span - skip - width if msb else skip
		value = (raw >> shift) & ((1 << width) - 1)

		if signed and value >> (width - 1):
			value -= 1 << width
		return value

	def _set_bits(self, offset_bits: int, width: int, value: int, *,
			msb: bool) -> None:
		self._check()
		first = offset_bits // 8
		last  = (offset_bits + width - 1) // 8
		span  = (last - first + 1) * 8
		raw   = int.from_bytes(
			self._msg.buffer[self._at + first:self._at + last + 1], "big")

		skip  = offset_bits - first * 8
		shift = span - skip - width if msb else skip
		mask  = ((1 << width) - 1) << shift
		raw   = (raw & ~mask) | ((value & ((1 << width) - 1)) << shift)

		self._msg.buffer[self._at + first:self._at + last + 1] = \
			raw.to_bytes(last - first + 1, "big")


def acquire(cls: type, msg: Message, at: int, length: int) -> View:
	"""The one bounds check. Everything after it trusts the extent."""
	if at < 0 or length < 0 or at + length > len(msg.buffer):
		raise BoundsError(
			f"{cls.__name__} needs {length} bytes at offset {at}; the message "
			f"is {len(msg.buffer)} bytes")
	return cls(msg, at, length)		# type: ignore[no-any-return]


class Gate:
	"""A sealed region's interior (section 14.3).

	Constructing one requires the token below, which only a verified open
	produces. That is a real run-time refusal and it is *not* the C++
	guarantee, where the type has no public constructor and forging one does
	not compile. Python has no access control to lean on: `object.__new__` will
	make one of these regardless, and nothing here can stop it.

	Stated plainly because a reader who has seen the C++ backend would
	otherwise assume the same strength carried over.
	"""

	__slots__ = ("_view",)

	class _Token:
		"""Unexported, unconstructible by accident, and the only key."""

	_KEY: Final = _Token()

	def __init__(self, view: View, token: _Token) -> None:
		if token is not Gate._KEY:
			raise StageError(
				"a sealed region's interior is reachable only through its "
				"verified open; see section 14.3")
		self._view = view


def open_gate(gate_class: type, view: View, verified: bool) -> Gate:
	"""Hand out a gate, and only once the tag has verified."""
	if not verified:
		raise TagError("the tag has not verified, so the sealed interior "
		               "stays closed (section 14.3)")
	return gate_class(view, Gate._KEY)	# type: ignore[no-any-return]


class Register:
	"""A memory-mapped register, over whatever the caller can address.

	Python cannot promise `volatile`: there is no way to tell the interpreter
	that a read has a side effect, and no bus transaction to order. So this is
	*not* a driver, and the module says so where a reader will look.

	What it is good for is the thing people actually reach for Python to do --
	bring-up scripts and test rigs, over an `mmap` of `/dev/mem`, a debug probe,
	or a simulator. The composition below is exact and the same arithmetic the
	C++ backend does; only the transport is the caller's.

	`read_word` and `write_word` are supplied by the caller because situ does
	not know what is on the other end.
	"""

	__slots__ = ("read_word", "write_word", "address")

	def __init__(self, read_word: object, write_word: object,
			address: int = 0) -> None:
		self.read_word  = read_word
		self.write_word = write_word
		self.address    = address


def compose(raw: int, value: int, shift: int, mask: int) -> int:
	"""Place `value` into `raw` at `shift`, clearing only its own bits.

	Reserved bits keep whatever the read gave them, which is what `[preserve]`
	asks for.
	"""
	return (raw & ~(mask << shift)) | ((value & mask) << shift)


def as_enum(cls: type[EnumT], raw: int) -> EnumT | int:
	"""The enum member, or the raw value when it is not one.

	A getter is not where a caller should discover a malformed field -- that is
	`validate`'s job, and section 8.7's `default = error` is a rule about
	parsing rather than about reading. Raising here would also make a getter
	fail on data a `default = pass` schema explicitly admits.

	Generic in the enum, because the generated getter is annotated
	`-> ether_type | int` and this returned `object`. Every enum field in every
	generated module was a type error to a caller who ran mypy over it, which
	is the only thing an annotation is for.
	"""
	try:
		return cls(raw)
	except ValueError:
		return raw


def known_enum(cls: type[enum.Enum], raw: int) -> bool:
	"""Whether `raw` names a member. What `default = error` asks on parse."""
	try:
		cls(raw)
	except ValueError:
		return False
	return True


def utf8_valid(data: memoryview | bytes) -> bool:
	"""Strict, as RFC 3629 requires: an overlong form or a surrogate half is a
	second spelling of a character that already has one."""
	try:
		bytes(data).decode("utf-8")
	except UnicodeDecodeError:
		return False
	return True


def ascii_valid(data: memoryview | bytes) -> bool:
	return all(byte <= 0x7F for byte in bytes(data))


def utf16le_valid(data: memoryview | bytes) -> bool:
	"""UTF-16LE, as strict as the utf8 check beside it (decision 0044): an odd
	byte count or a lone surrogate decodes to no character, and Python's strict
	decoder refuses exactly that set."""
	try:
		bytes(data).decode("utf-16-le")
	except UnicodeDecodeError:
		return False
	return True


def utf16be_valid(data: memoryview | bytes) -> bool:
	"""UTF-16BE; see `utf16le_valid`. The order is the encoding's, not the
	field's, which is why the two are separate names."""
	try:
		bytes(data).decode("utf-16-be")
	except UnicodeDecodeError:
		return False
	return True


def nul_len(data: memoryview | bytes, capacity: int) -> int:
	"""Content length of a nul-terminated field, bounded by its capacity."""
	raw = bytes(data)[:capacity]
	end = raw.find(0)
	return capacity if end < 0 else end


#: A byte position no byte occupies, for a format with no quote or escape.
NO_BYTE: Final = -1


#: The bound every leaf of a size expression is held to (14.2b).
#:
#: Python's integers do not overflow, so nothing here needs the bound for its
#: own sake -- it is here so this backend agrees with the three that do. A
#: varint a lying message set to 1.6e19 has to produce the same offset in all
#: four, and the only way to get that is to bound the leaves identically.
LEAF_MAX = 0x7FFFFFFF


def leaf(value: int) -> int:
	"""One leaf of a size expression, held to `LEAF_MAX`."""
	return max(-LEAF_MAX, min(value, LEAF_MAX))


def nonneg(value: int) -> int:
	"""A computed length: negative reads as zero, and a result past `u32`
	saturates rather than truncating (14.2b)."""
	if value <= 0:
		return 0
	return 0xFFFFFFFF if value > 0xFFFFFFFF else value


def advance(at: int, by: int, limit: int) -> int:
	"""Advance an offset by a length the message chose, and stop at the end.

	A member placed after a variable-length region has an offset that is a sum
	of lengths an attacker fills in: `example/packet` with `hdr.length =
	0xffff` puts its tag 65581 bytes into a 62-byte message. Python cannot read
	out of bounds -- a `memoryview` slice past the end is short rather than
	unsafe -- so what this buys here is not safety but *agreement*: the four
	backends have to answer the same question with the same number, and C's
	answer is this one (26.27).
	"""
	room = limit - at if limit > at else 0

	return at + (by if by < room else room)


def align_up(at: int, n: int, limit: int) -> int:
	"""`pad_to(n)` (decision 0043): the next multiple of n, clamped to the
	view the way `advance` is -- the aligned offset may sit past a short
	message, and `validate` reports that rather than a short slice hiding it."""
	pad = (n - at % n) % n
	return advance(at, pad, limit)


def scan(data: memoryview | bytes, limit: int, delim: bytes,
		quote: int = NO_BYTE, escape: int = NO_BYTE) -> int:
	"""Where a delimited member's content stops (section 8.6.1).

	The offset of the first occurrence of `delim` within `limit` bytes, or
	`limit` where it is not there. The caller distinguishes the two: a member
	whose delimiter is absent is truncated rather than empty, and a getter is
	not the place to decide what to do about that.

	`quote` toggles -- inside a quoted run the delimiter is content -- and
	`escape` applies to the byte after it, including a quote byte and
	including itself. A quoted run left open finds no delimiter, which is the
	same answer as one that is not there and the right one, since the content
	has not been terminated.
	"""
	raw = bytes(data)[:limit]

	if not delim or len(delim) > limit:
		return limit

	# `bytes.find` is C and this loop is not, so the common case takes the
	# fast path. The relaxed one cannot: whether a match counts depends on
	# state built up by walking every byte before it.
	if quote == NO_BYTE and escape == NO_BYTE:
		found = raw.find(delim)
		return limit if found < 0 else found

	quoted = False
	i      = 0
	while i + len(delim) <= limit:
		byte = raw[i]
		if escape != NO_BYTE and byte == escape:
			i += 2
			continue
		if quote != NO_BYTE and byte == quote:
			quoted = not quoted
			i += 1
			continue
		if not quoted and raw[i:i + len(delim)] == delim:
			return i
		i += 1
	return limit


def parse_uint(data: memoryview | bytes, radix: int, limit: int) -> int | None:
	"""A number written as digits, or None where it is not one.

	`None` rather than an exception, because every caller has something to do
	with it: a getter raises, and the offset arithmetic that cannot fail reads
	zero. Refused for the reasons a protocol cares about -- an empty run,
	because no digits is not the number zero; a byte that is not a digit in
	this base, including a trailing space; and a value outside the declared
	type.
	"""
	raw = bytes(data)
	if not raw:
		return None

	try:
		value = int(raw.decode("ascii"), radix)
	except ValueError:
		return None

	# `int` accepts a sign, whitespace and an `0x` prefix, none of which are
	# digits. Checking the bytes rather than trusting the parse keeps this the
	# same language the C runtime accepts, which is the point of having one
	# answer per schema rather than one per backend.
	if not all(chr(byte) in DIGITS[:radix] for byte in raw.lower()):
		return None

	return value if 0 <= value <= limit else None


DIGITS: Final = "0123456789abcdef"


#: What `[trim]` removes. Space and horizontal tab, and nothing else -- not
#: `str.strip`, which also takes CR, LF, VT and FF, three of which are
#: delimiters in the protocols this is for. This is HTTP's OWS.
OWS: Final = b" \t"


def trim(data: memoryview | bytes) -> bytes:
	"""The value with the optional whitespace at either end removed."""
	return bytes(data).strip(OWS)


def ascii_ci_eq(a: memoryview | bytes, b: memoryview | bytes) -> bool:
	"""ASCII case folding, and only ASCII.

	`str.lower` is Unicode: it maps `KELVIN SIGN` to `k` and the Turkish
	dotless forms in ways a protocol token never means. Folding exactly A-Z
	is what the formats specify.
	"""
	fold = bytes.maketrans(b"ABCDEFGHIJKLMNOPQRSTUVWXYZ",
	                       b"abcdefghijklmnopqrstuvwxyz")
	return bytes(a).translate(fold) == bytes(b).translate(fold)


def digits_minimal(data: memoryview | bytes, radix: int) -> bool:
	"""Whether digits are the one spelling of their value (section 8.6.2).

	A leading zero is another spelling of the same number, and above base ten
	so is a change of case. `[minimal]` is what asks for this; without it the
	field is NonCanonical and the map says so, which is the honest default --
	most formats do permit `007`.
	"""
	raw = bytes(data)
	if not raw:
		return False
	if len(raw) > 1 and raw[0:1] == b"0":
		return False
	return radix <= 10 or not any(b"A"[0] <= byte <= b"F"[0] for byte in raw)


def bcd_decode(packed: int, digits: int) -> int:
	value = 0
	for i in range(digits - 1, -1, -1):
		value = value * 10 + ((packed >> (4 * i)) & 0xF)
	return value


def bcd_encode(value: int, digits: int) -> int:
	packed = 0
	for i in range(digits):
		packed |= (value % 10) << (4 * i)
		value //= 10
	return packed


def bcd_valid(packed: int, digits: int) -> bool:
	return all(((packed >> (4 * i)) & 0xF) <= 9 for i in range(digits))


def varint_get(data: Buffer, at: int, max_bytes: int) -> tuple[int, int] | None:
	"""Decode one varint at `at`: the value, and the bytes it occupied.

	None where the buffer ends mid-value or the value needs more than
	`max_bytes`. A primitive, not a format -- what a `tlv` region does with the
	number is its own grammar's business (section 9.5), which the generated
	walk carries rather than this file.
	"""
	acc   = 0
	shift = 0

	for i in range(max_bytes):
		if at + i >= len(data):
			return None
		byte = data[at + i]
		if shift < 64:
			acc |= (byte & 0x7F) << shift
		shift += 7
		if not byte & 0x80:
			return acc, i + 1

	return None


def varint_len(value: int) -> int:
	"""The number of bytes `value` needs, encoded minimally.

	What a `minimal` varint type is held to: a longer encoding of the same
	value is a second encoding, and a schema declaring `minimal` does not admit
	one.
	"""
	n = 1
	while value >= 0x80:
		value >>= 7
		n += 1
	return n


def zigzag_decode(raw: int) -> int:
	"""ZigZag, as protobuf's sint32 and sint64 use it: a small magnitude stays
	short whether it is positive or negative."""
	return (raw >> 1) ^ -(raw & 1)


def zigzag_encode(value: int) -> int:
	return (value << 1) ^ (value >> 63) if value < 0 else value << 1


def varint_be_get(data: Buffer, at: int, max_bytes: int,
		terminal_bits: int) -> tuple[int, int] | None:
	"""Decode one big-endian base-128 varint: the high group first.

	ASN.1's identifier octets, MIDI's delta times and SQLite's record varints
	are all this. `terminal_bits` is what the last permitted byte carries;
	where that is eight there is no spare bit for a continuation flag, so the
	byte is read whole and ends the value whatever its high bit says.
	"""
	acc = 0

	for i in range(max_bytes):
		if at + i >= len(data):
			return None
		byte = data[at + i]

		if terminal_bits == 8 and i + 1 == max_bytes:
			return (acc << 8) | byte, i + 1

		acc = (acc << 7) | (byte & 0x7F)
		if not byte & 0x80:
			return acc, i + 1

	return None


def varint_be_len(value: int, max_bytes: int, terminal_bits: int) -> int:
	"""The bytes `value` needs under `varint_be_get`'s rules."""
	n = 1
	while value >= 0x80:
		value >>= 7
		n += 1
	if terminal_bits == 8 and n > max_bytes:
		n = max_bytes
	return n
