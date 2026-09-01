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
from walker.walk import (BITS_PER_BYTE, Bytes, Refused, View, acquire,
                         offset_bits, size_bits, write_scalar)

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
	#: What the schema says a write to this member would do, out of the
	#: image's metadata tail. `None` where the image was packed without it,
	#: which is a different answer from any value the lattice has.
	mutate: str | None = None
	auth: str | None = None

	@property
	def readable(self) -> bool:
		return self.value is not None

	@property
	def writable(self) -> bool:
		"""Whether the schema permits a write at all.

		`readable`'s mirror, and it had none: the image has carried the
		capability vectors since 26.33 split the tail off for this reader,
		`situ-edit` asks for them with `--metadata`, and nothing read them --
		so an editor could show a field and not say whether writing it was
		legal, which is the one thing a *file* editor most needs to know
		(26.177).

		`None` mutate means the image did not say, and that is not the same
		as permission: an editor that treats silence as yes is the failure
		this property exists to prevent.
		"""
		return self.mutate is not None and self.mutate != "Immutable"

	@property
	def write_cost(self) -> str:
		"""What a permitted write costs, in the terms the lattice uses."""
		if self.mutate is None:
			return "the image does not say"
		if self.mutate == "Immutable":
			return "refused: the schema does not let anyone write this"

		moves = ("" if self.mutate == "InPlaceFixed"
		         else "; the bytes after it move" if self.mutate == "Shifting"
		         else "; the whole region is re-transformed"
		         if self.mutate == "RewriteRequired"
		         else "; an append needs slack")
		tag = "" if self.auth != "Covered" else "; a tag has to be recomputed"
		return f"{self.mutate}{moves}{tag}"


@dataclass
class Document:
	"""One message, opened against one image."""

	image: Image
	buffer: bytearray
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
				                  f"cannot be placed: {why}",
				                  mutate = image.capability_of(index, "mutate"),
				                  auth   = image.capability_of(index, "auth")))
				continue

			value = held.get(local)
			note  = "" if value is not None else "cannot be read"
			if placement.type_struct != NONE:
				note = "a nested struct; open it as its own document"

			rows.append(Field(local, at, wide, value, note,
			                  mutate = image.capability_of(index, "mutate"),
			                  auth   = image.capability_of(index, "auth")))

		return rows

	def _members(self) -> dict[str, int]:
		"""Local name -> placement index, for the members `fields` shows."""
		image = self.image
		found: dict[str, int] = {}
		for index in image.members(image.structs[self.struct]):
			if image.placements[index].kind not in (FIELD, RESERVED):
				continue
			name = image.name_of(index)
			found[name.rpartition(".")[2] or name] = index
		return found

	def set(self, name: str, value: int) -> list[str]:
		"""Store a value, if the schema permits it. Returns what it cost.

		Three refusals and one warning, in that order, because they are
		different things and an editor that ran them together would either
		refuse a legal write or allow an illegal one:

		- **the image did not say.** Packed without its metadata tail, it
		  carries no capability vectors, and silence is not permission.
		- **the schema forbids it.** `mutate = Immutable` is a checksum, a
		  derived field, a read-only register: there is no setter anywhere in
		  situ for these and there is not one here.
		- **the write does not fit the member**, which `write_scalar` checks
		  and which is a range error rather than a permission one.

		The warning is coverage. A write to a member a tag authenticates
		leaves that tag stale, and **situ does not recompute it** -- 14.1
		puts computing a checksum with the caller, and this tool is not the
		exception. So the write happens and the staleness is reported, which
		is the honest half: refusing would make the field uneditable, and
		silently recomputing would be this tool inventing a value the schema
		says is somebody else's.
		"""
		index = self._members().get(name)
		if index is None:
			# Without the metadata tail there are no names either, so a
			# lookup fails before the capability check does -- and "no member
			# named `destination_port`" would be a lie about a member that is
			# right there. Which half is missing decides which is said.
			if not self.image.placement_names:
				raise Refused(
					f"`{name}`: this image was packed without its metadata "
					f"tail, so it carries neither names nor capabilities; "
					f"pack it with `--metadata`")
			raise Refused(f"no member named `{name}` in `{self.name}`")

		mutate = self.image.capability_of(index, "mutate")
		if mutate is None:
			raise Refused(
				f"`{name}`: the image does not say whether this may be "
				f"written; pack it with `--metadata`")
		if mutate == "Immutable":
			raise Refused(
				f"`{name}`: the schema does not let anyone write this")

		# A fixed scalar written in place may still *shift* the layout: udp's
		# `length` is `InPlaceFixed` and decides how long the payload is, so
		# storing 40 in an eight-byte message leaves it claiming a 32-byte
		# payload and nothing after the write can be read. 0034's table has
		# that as its second row -- "anything that shifts layout" -- and puts
		# it behind 26.99; this is the first row's door, and it arrived
		# through it.
		#
		# Measured rather than analysed: write into a copy, walk it again,
		# and compare where every member starts and how long it is. That
		# catches the case whatever caused it, including the ones nobody
		# enumerated, and it is the walk itself answering rather than a
		# second model of what drives what.
		candidate = Document(self.image, bytearray(self.buffer), self.struct)
		write_scalar(candidate.view(), index, value)

		before, after = self._extents(), candidate._extents()
		if before != after:
			moved = sorted(name for name in before
			               if before.get(name) != after.get(name))
			raise Refused(
				f"`{name}`: writing this moves {', '.join(moved)}, and a "
				f"shifting write is not built (0034: it needs the "
				f"invalidation model of 12.3)")

		self.buffer[:] = candidate.buffer

		if self.image.capability_of(index, "auth") == "Covered":
			return [f"`{name}` is covered by a tag, which is now stale: "
			        f"situ does not compute it (14.1)"]
		return []

	def _extents(self) -> dict[str, tuple[int, int] | None]:
		"""Where every member starts and how long it is, or `None` where the
		walk cannot say.

		Every member keeps a key either way, so the comparison is over one
		key set and a member that stops being placeable differs from one that
		is placed. Recording `None` rather than dropping the key is for the
		reader: a map missing a name says nothing about why."""
		view  = self.view()
		image = self.image
		found: dict[str, tuple[int, int] | None] = {}
		for index in image.members(image.structs[self.struct]):
			name = image.name_of(index)
			try:
				found[name] = (offset_bits(view, index), size_bits(view, index))
			except Refused:
				found[name] = None
		return found

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


def open_document(image_bytes: bytes, message: Bytes,
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

	# A `bytearray`, and a copy. Mutable because a document may now be
	# written to (0034's write path), and copied because that write must not
	# reach the caller's buffer or the file behind it: persisting is a
	# separate and explicit step, so an edit that is never saved changes
	# nothing anywhere.
	return Document(image, bytearray(message), chosen)
