"""An owned value out of a walked message: rung 2, interpreted (26.99).

The compiled backends hand back a struct whose variable members point into
backing the caller supplied, because C, C++ and Rust have nowhere else to put
bytes that must outlive the message. Python has `bytes`, so the walker takes
no backing for the same reason `situc build --target python --layer edit`
takes none: the language already owns what the parameter exists to arrange.

**This is the shape 0034's editor wants.** Open an image and a buffer, get
field names against values you can hold, change one, write it back. The
writing half does not exist yet -- the walk is read-only -- and this is the
half that does.

WHOLE OR NOTHING. A member the walk cannot place makes the whole decode fail,
naming it. That is `validate`'s rule and it is right for the same reason: a
partial owned value reports the fields it managed and is silent about the one
it did not, which reads as a message that simply lacked it.
"""

from __future__ import annotations

from walker.image import NONE, Image
from walker.report import FIELD, RESERVED
from walker.walk import Refused, View, read_bytes, read_scalar

#: What an owned decode can hand back. A scalar is a number and a run of
#: bytes is bytes; anything else is refused rather than approximated.
Value = int | bytes


def _readable(image: Image, index: int) -> bool:
	placement = image.placements[index]
	if placement.kind not in (FIELD, RESERVED):
		return False
	# A nested struct is a shape rather than a value. The editor will want to
	# descend into one; that is a tree, and this is one level of it.
	return placement.type_struct == NONE


def decode(view: View) -> dict[str, Value]:
	"""Every member of this struct, copied out of the message.

	The values own themselves: the buffer may go afterwards, which is the
	whole of what rung 2 buys and the reason the editor can hold a document
	after closing the file it came from.

	Keys are member names where the image carries them -- `situc pack
	--metadata` -- and `placement[N]` where it does not, which is what
	`name_of` answers. A device omits the
	names and loses nothing it executes; a reader wants them, which is the
	split 26.33 designed the tail around.
	"""
	image = view.image
	out: dict[str, Value] = {}

	for index in image.members(view.shape):
		if not _readable(image, index):
			continue

		placement = image.placements[index]
		# `name_of` answers `placement[N]` itself where the metadata tail
		# was omitted, so there is no fallback to write here.
		name  = image.name_of(index)
		local = name.rpartition(".")[2] or name

		try:
			if placement.element_bits == 8 and (placement.array_count != NONE
			                                    or placement.size_code != NONE):
				# `bytes`, not whatever slicing the buffer produced. A
				# bytearray view over a bytearray message is a copy and so
				# survives, but it is mutable -- and an "owned value" a
				# caller can edit in place is a different promise from the
				# one this makes.
				out[local] = bytes(read_bytes(view, index))
			else:
				out[local] = read_scalar(view, index)
		except Refused as why:
			# Whole or nothing: a partial value is silent about what it
			# missed, which reads as a message that lacked the field.
			raise Refused(f"`{local}` cannot be read, so there is no owned "
			              f"value of this message: {why}") from why

	return out
