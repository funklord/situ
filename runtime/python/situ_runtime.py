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

from typing import Final


class SituError(Exception):
	"""Base for everything raised here, so a caller can catch one thing."""


class BoundsError(SituError):
	"""A view would not fit the buffer, or an index is past the end."""


class ConstraintError(SituError):
	"""A field holds a value the schema does not admit."""


class StaleViewError(SituError):
	"""The message moved under this view (section 12.3).

	Raised rather than returned: a stale view is a bug in the caller, not a
	condition to branch on.
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
		"""A covered write happened; the tag it invalidates is now stale."""
		self.dirty |= bit

	def clear_dirty(self, bit: int) -> None:
		self.dirty &= ~bit

	def transmittable(self) -> None:
		"""Raise unless every tag is current (section 14.2)."""
		if self.dirty:
			raise TagError(
				f"a covered write left tag bits {self.dirty:#x} stale; "
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
	return cls(msg, at, length)		# type: ignore[call-arg,no-any-return]


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
			raise TagError(
				"a sealed region's interior is reachable only through its "
				"verified open; see section 14.3")
		self._view = view


def open_gate(gate_class: type, view: View, verified: bool) -> Gate:
	"""Hand out a gate, and only once the tag has verified."""
	if not verified:
		raise TagError("the tag has not verified, so the sealed interior "
		               "stays closed (section 14.3)")
	return gate_class(view, Gate._KEY)	# type: ignore[call-arg,no-any-return]


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


def as_enum(cls: type, raw: int) -> object:
	"""The enum member, or the raw value when it is not one.

	A getter is not where a caller should discover a malformed field -- that is
	`validate`'s job, and section 8.7's `default = error` is a rule about
	parsing rather than about reading. Raising here would also make a getter
	fail on data a `default = pass` schema explicitly admits.
	"""
	try:
		return cls(raw)		# type: ignore[call-arg]
	except ValueError:
		return raw


def known_enum(cls: type, raw: int) -> bool:
	"""Whether `raw` names a member. What `default = error` asks on parse."""
	try:
		cls(raw)		# type: ignore[call-arg]
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


def nul_len(data: memoryview | bytes, capacity: int) -> int:
	"""Content length of a nul-terminated field, bounded by its capacity."""
	raw = bytes(data)[:capacity]
	end = raw.find(0)
	return capacity if end < 0 else end


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
