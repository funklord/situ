"""What the editor knows about one message: the core of decision 0034.

A document is an image, a buffer, and the fields the two produce between
them. It renders nothing and displays nothing -- the CLI, the TUI and the GUI
each ask it the same questions and answer them their own way.

READ-ONLY, DELIBERATELY. 0034 blocks the write path on 26.99, which has
landed, but a walk that writes is its own piece of work: writing a field that
shifts the layout drags in the invalidation model, a tag-covered field goes
stale, and an invariant must be *maintained* rather than checked. None of
that is here. What is here is the half that makes the other half worth
having, and what an editor is mostly doing anyway.
"""

from __future__ import annotations

from dataclasses import dataclass

from walker.image import NONE, Image, load
from walker.owned import decode
from walker.report import FIELD, RESERVED
from walker.walk import (BITS_PER_BYTE, Refused, View, acquire, offset_bits,
                         size_bits)

__all__ = ["Document", "Field", "open_document"]


@dataclass(frozen=True)
class Field:
	"""One member, placed and read.

	`value` is None where the walk could not read it, and `note` says why.
	An editor that silently omitted such a field would show a message
	missing something it actually has, so the row stays and carries its
	reason.
	"""

	name: str
	offset: int | None
	size: int | None
	value: int | bytes | None
	note: str = ""

	@property
	def readable(self) -> bool:
		return self.value is not None


@dataclass
class Document:
	"""One message, opened against one image."""

	image: Image
	buffer: bytes
	struct: int

	@property
	def name(self) -> str:
		return self.image.struct_name(self.struct)

	def view(self) -> View:
		return acquire(self.image, self.buffer, self.struct)

	def fields(self) -> list[Field]:
		"""Every member, in declaration order, placed and read.

		Placement and value are asked separately on purpose. A field the
		walk can locate but not read is a different thing from one it cannot
		locate at all, and an editor wants to show the first at its offset
		rather than drop it.
		"""
		view  = self.view()
		image = self.image
		held  = self._values(view)
		rows: list[Field] = []

		for index in image.members(image.structs[self.struct]):
			placement = image.placements[index]
			if placement.kind not in (FIELD, RESERVED):
				continue

			name  = image.name_of(index)
			local = name.rpartition(".")[2] or name

			try:
				at   = offset_bits(view, index) // BITS_PER_BYTE
				wide = size_bits(view, index) // BITS_PER_BYTE
			except Refused as why:
				rows.append(Field(local, None, None, None,
				                  f"cannot be placed: {why}"))
				continue

			value = held.get(local)
			note  = "" if value is not None else "cannot be read"
			if placement.type_struct != NONE:
				note = "a nested struct; open it as its own document"

			rows.append(Field(local, at, wide, value, note))

		return rows

	def _values(self, view: View) -> dict[str, int | bytes]:
		"""What rung 2 can read, or nothing where it refuses the message.

		`decode` is whole-or-nothing, which is right for an owned value and
		wrong for a display: an editor showing no fields because one of them
		is unplaceable is less use than one showing the rest and saying so.
		So a refusal here becomes empty, and every row carries its own note.
		"""
		try:
			return decode(view)
		except Refused:
			return {}


def open_document(image_bytes: bytes, message: bytes,
		struct: str | None = None) -> Document:
	"""Open a message against a packed image.

	`struct` names which layout to read it as; without one the first is
	taken, which is what a single-struct schema wants and what a reader of
	a larger one will immediately want to override.
	"""
	image = load(image_bytes)
	if not image.structs:
		raise Refused("this image describes no structs")

	chosen = 0
	if struct is not None:
		names = [image.struct_name(i) for i in range(len(image.structs))]
		if struct not in names:
			raise Refused(f"no struct `{struct}` in this image; it has "
			              f"{', '.join(names)}")
		chosen = names.index(struct)

	return Document(image, bytes(message), chosen)
