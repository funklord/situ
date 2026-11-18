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

import dataclasses
import struct as _struct

from walker import report
from walker.image import (MAGIC, NONE, Image, RegistryEntry, _string_at,
                          load)
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


# -- written at runtime (0070, step 5) ----------------------------------------

#: `std/image.situ`'s tags and the format these rows are written in.
_FORMAT_VERSION = 10
_STRINGS, _REGISTRY, _REGISTRY_WORDS = 4, 32, 33
_ANCILLARY = 1 << 16
_ENTRY = "<IIIBBBBIqqIIIIII"


def cell_value_of(image: Image, shape: int) -> tuple[int, int]:
	"""What a cell of struct `shape` is worth, read from the image alone:
	its `let value` (1, the let's row), or its one member where that is a
	text number (2, the placement); (0, NONE) where neither. A binary
	member is not offered: from the image alone a scalar and a byte run
	of its width are not told apart, and a guess is not a value."""
	for row, held in enumerate(image.lets):
		if held.owner == shape \
				and _string_at(image.strings, held.name) == "value":
			return 1, row
	struct = image.structs[shape]
	if struct.placement_count == 1:
		index = struct.first_placement
		if image.placements[index].radix:
			return 2, index
	return 0, NONE


def build_rows(base: Image, registry: str, cells: list[dict[str, object]]
		) -> bytes:
	"""A registry written without the compiler: an image of three sections --
	strings, `image_registry_entry` and `image_registry_word` rows -- over
	the cell types `base` carries, which `attach` reads beside it.

	Each cell is a dict: `key`, `type` (a struct name `base` names), and
	any of `min`, `max`, `min_size`, `max_size`, `default`, `unit`,
	`summary`, `safe` (`lower` or `higher`), `window` and `words` (policy
	name to a list of words). The rows are what `situc pack` writes for
	the same registry, so a walker cannot tell the two apart."""
	pool = bytearray()
	at: dict[str, int] = {}

	def intern(text: str | None) -> int:
		if text is None:
			return NONE
		if text not in at:
			at[text] = len(pool)
			pool.extend(text.encode("ascii") + b"\0")
		return at[text]

	names = {name: index for index, name in enumerate(base.struct_names)}
	entries, words = bytearray(), bytearray()
	for row, cell in enumerate(cells):
		key = str(cell["key"])
		shape = names.get(str(cell["type"]))
		if shape is None:
			raise ValueError(f"`{cell['type']}` is not a struct of the base "
			                 "image (packed with metadata?)")
		kind, value = cell_value_of(base, shape)
		bounded = (1 if "min" in cell else 0) | (2 if "max" in cell else 0)
		if bounded and kind == 0:
			raise ValueError(f"`{key}`: `{cell['type']}` has no value to bound")
		entries += _struct.pack(
			_ENTRY, intern(registry), intern(key), shape,
			1 if key.endswith("/") else 0,
			{"lower": SAFE_LOWER, "higher": SAFE_HIGHER}.get(
				str(cell.get("safe", "")), 0),
			kind, bounded, value,
			int(cell.get("min", 0)), int(cell.get("max", 0)),	# type: ignore[call-overload]
			int(cell.get("min_size", 0)),			# type: ignore[call-overload]
			int(cell.get("max_size", NONE)),		# type: ignore[call-overload]
			intern(cell.get("default")), intern(cell.get("unit")),	# type: ignore[arg-type]
			intern(cell.get("summary")), int(cell.get("window", 0)))	# type: ignore[arg-type, call-overload]
		for policy, said in dict(cell.get("words", {})).items():  # type: ignore[call-overload]
			for word in said:
				words += _struct.pack("<III", row, intern(policy),
				                      intern(word))
	live = [(_STRINGS, bytes(pool), 1),
	        (_REGISTRY | _ANCILLARY, bytes(entries), _struct.calcsize(_ENTRY)),
	        (_REGISTRY_WORDS | _ANCILLARY, bytes(words), 12)]
	live = [one for one in live if one[1]]
	body_at = 20 + 16 * len(live)
	directory, bodies, offset = bytearray(), bytearray(), body_at
	for kind, blob, stride in live:
		directory += _struct.pack("<IIII", kind, offset, len(blob) // stride,
		                          stride)
		bodies += blob
		offset += len(blob)
	return bytes(MAGIC + _struct.pack("<HHIII", _FORMAT_VERSION, 0,
	                                  body_at + len(bodies), len(live), 20)
	             + directory + bodies)


def attach(base: Image, rows: bytes) -> Image:
	"""`base` with the registry `rows` holds in place of its own: the rows'
	strings appended to its pool, their ids moved by the pool's length."""
	written = load(rows)
	shift   = len(base.strings)

	def moved(entry: RegistryEntry) -> RegistryEntry:
		def at(held: int) -> int:
			return NONE if held == NONE else held + shift
		return dataclasses.replace(
			entry, registry=at(entry.registry), key=at(entry.key),
			default=at(entry.default), unit=at(entry.unit),
			summary=at(entry.summary),
			words=[(at(policy), at(word)) for policy, word in entry.words])
	return dataclasses.replace(base, strings=base.strings + written.strings,
	                           registry=[moved(one) for one in written.registry])

