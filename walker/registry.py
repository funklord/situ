"""A registry's cells, walked from an image (0070, step 4).

The generated C's four functions over a registry, read from the image's
`registry` rows instead: `find` a key, `check` a cell's bytes, read its
`value`, and say which of two values is `safer`. Each cell is walked as a
message of its type, through the same `validate` every other walk uses, so
what this refuses is what a compiled reader of the cell refuses -- and the
rows are declarative, so a program that wrote them without the compiler is
read the same way.

Verdicts are the backends' codes: 0 OK, 1 BOUNDS, 2 CONSTRAINT, and None
where this image cannot say.
"""

from __future__ import annotations

from walker import report
from walker.image import NONE, Image, RegistryEntry, _string_at
from walker.walk import Refused, _evaluate, _value_of, acquire

OK, BOUNDS, CONSTRAINT = 0, 1, 2
SAFE_LOWER, SAFE_HIGHER = 1, 2


def key_of(image: Image, entry: RegistryEntry) -> str:
	return _string_at(image.strings, entry.key)


def find(image: Image, key: str, registry: str | None = None
		) -> RegistryEntry | None:
	"""The entry `key` is: exact, or a family's instance -- the family's
	key, then one word of `a-z0-9`."""
	for entry in image.registry:
		if registry is not None \
				and _string_at(image.strings, entry.registry) != registry:
			continue
		held = key_of(image, entry)
		if not entry.family:
			if key == held:
				return entry
			continue
		word = key[len(held):]
		if key.startswith(held) and word \
				and all("a" <= c <= "z" or "0" <= c <= "9" for c in word):
			return entry
	return None


def _cell(image: Image, entry: RegistryEntry, data: bytes
		) -> tuple[int | None, int | None]:
	"""(verdict, value): the cell walked as a message of its type."""
	struct = image.structs[entry.cell]
	if struct.size_bits != NONE and len(data) * 8 != struct.size_bits:
		return BOUNDS, None
	try:
		view = acquire(image, data, entry.cell)
	except Refused:
		return BOUNDS, None
	verdict = report._validate(image, view, entry.cell)
	if verdict is None or verdict != OK:
		return verdict, None
	if entry.value_kind == 1:
		held = image.lets[entry.value]
		try:
			value = _evaluate(view, held.code)
		except Refused:
			return None, None
		value &= (1 << held.bits) - 1
		if held.signed and value >> (held.bits - 1):
			value -= 1 << held.bits
		return OK, value
	if entry.value_kind == 2:
		try:
			return OK, _value_of(view, entry.value)
		except Refused:
			return None, None
	return OK, None


def check(image: Image, entry: RegistryEntry, data: bytes) -> int | None:
	"""Whether `data` is a cell of `entry`: its bytes' bounds, its type's
	`validate`, then its value's bounds."""
	if len(data) < entry.min_size \
			or (entry.max_size != NONE and len(data) > entry.max_size):
		return CONSTRAINT
	verdict, value = _cell(image, entry, data)
	if verdict != OK or value is None:
		return verdict
	if (entry.bounded & 1 and value < entry.min) \
			or (entry.bounded & 2 and value > entry.max):
		return CONSTRAINT
	return OK


def value(image: Image, entry: RegistryEntry, data: bytes) -> int | None:
	"""What a cell is worth, or None where it is not a cell, has no value,
	or this image cannot say."""
	if check(image, entry, data) != OK:
		return None
	return _cell(image, entry, data)[1]


def safer(image: Image, entry: RegistryEntry, a: bytes, b: bytes) -> bool:
	"""Whether `b` is strictly safer than `a` by the entry's direction."""
	if entry.safe not in (SAFE_LOWER, SAFE_HIGHER):
		return False
	first, second = value(image, entry, a), value(image, entry, b)
	if first is None or second is None:
		return False
	return second < first if entry.safe == SAFE_LOWER else second > first


def words(image: Image, entry: RegistryEntry, policy: str) -> list[str]:
	"""The words of `policy` the entry names, in the order written."""
	return [_string_at(image.strings, word) for held, word in entry.words
	        if _string_at(image.strings, held) == policy]
