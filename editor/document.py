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
from typing import Literal

from walker.image import LITTLE, NONE, Image, load
from walker.owned import decode
from walker import report
from walker.report import FIELD, RESERVED
from walker.walk import (BITS_PER_BYTE, Bytes, Refused, View, acquire,
                         is_run, offset_bits, size_bits, struct_extent,
                         write_bytes, write_scalar)

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
	#: Where this struct starts in the buffer, and where its frame ends.
	#:
	#: A chunked container is the reason: a PNG is a signature and a run of
	#: chunks, so reading one chunk meant cutting the file up first, and a
	#: nested member's note said "open it as its own document" with no way
	#: to do that (26.581). `limit` is the frame -- a struct ending in
	#: `[remaining]` reads to it, so handing one the rest of the file is a
	#: different question from handing it the 25 bytes a chunk declares.
	at: int = 0
	limit: int | None = None

	@property
	def name(self) -> str:
		return self.image.struct_name(self.struct)

	@property
	def measured(self) -> int | None:
		"""How many bytes this struct's own members account for, here.

		Distinct from `extent`, which is the WINDOW. A chunk opened at
		offset 8 with no length is given the rest of the file, and what it
		occupies is the twelve bytes of frame plus the length it declares:
		knowing both is what lets a reader step to the next record instead
		of working the arithmetic out by hand (26.582).

		`None` where the walk cannot say, and that is not the same as zero.
		Zero is an answer -- a DNS name whose first label does not fit holds
		no labels and is zero bytes long -- so the two are kept apart here
		for the reason `fields()` keeps "cannot be placed" apart from
		"cannot be read".
		"""
		try:
			return struct_extent(self.view())
		except Refused:
			return None

	@property
	def extent(self) -> int:
		"""How many bytes this document's WINDOW holds.

		Not `len(buffer)`, which is the file: a chunk opened at offset 8 of
		a 4 KiB PNG is 25 bytes, and a header line saying 4096 would be
		describing the file while every offset below it described the
		chunk.
		"""
		end = len(self.buffer) if self.limit is None else self.limit
		return end - self.at

	def view(self) -> View:
		return acquire(self.image, self.buffer, self.struct,
		               at = self.at, limit = self.limit)

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

		# Which member the schema refuses this message over, if any (0051).
		# `report.failed_check` is the identity half, built in 26.231 and
		# recorded there as "usable and nothing uses it yet"; this is a
		# consumer, and the channel it uses is the `note` the frontends
		# already render.
		#
		# The CHECK's name rather than a sentence, because a sentence a
		# schema wrote is `when`'s and is not built. `max` is a rendering of
		# an identity rather than the identity itself, which is the split
		# 0051 takes: a consumer keys on the id, a person reads the word.
		refused = report.failed_check(image, view, self.struct)
		blamed  = refused[0] if isinstance(refused, tuple) else None
		because = refused[1] if isinstance(refused, tuple) else ""

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
				# NAMING THE STRUCT AND THE OFFSET, because the note asks
				# the reader to do something and both are what it takes to
				# do it. It said "a nested struct; open it as its own
				# document" and named neither -- and until 26.581 there
				# was no `--offset` to pass, so the one instruction this
				# tool gives a reader was for something it did not
				# support.
				#
				# ABSOLUTE, where the offset column is view-relative. The
				# column describes where the member sits in this struct;
				# this number goes on a command line, so it is the one
				# the command takes. A document opened at 8 reporting a
				# chunk's `data` at 8 would send the reader to the
				# signature.
				note = (f"a nested `{image.struct_name(placement.type_struct)}"
				        f"`; open it with --offset {self.at + at}")
			if local == blamed:
				# Appended rather than replacing: a field can be unreadable
				# AND be the one the schema refuses over, and a note that
				# dropped either half would answer a question nobody asked.
				note = f"{note}; refused: {because}" if note \
				       else f"refused: {because}"

			rows.append(Field(local, at, wide, value, note,
			                  mutate = image.capability_of(index, "mutate"),
			                  auth   = image.capability_of(index, "auth")))

		return rows

	def messages(self) -> list[tuple[str, str, str]]:
		"""What the schema says about this message beyond its layout (0051).

		`(severity, name, text)` per `when` whose predicate holds. A document
		concern rather than a field one, which is the one place this departs
		from what 0051 wrote down: that record says the editor "puts the text
		in the `note` it already has", and a `when` is a predicate over a
		struct rather than over a member -- so there is no member whose row it
		belongs on. `failed_check` above is the per-member half and keeps the
		`note` channel; this is the struct-scoped one and needs its own.
		"""
		return report.messages(self.image, self.view(), self.struct)

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

	def set(self, name: str, value: int | bytes) -> list[str]:
		"""Store a value, if the schema permits it. Returns what it cost.

		Three refusals and one warning, in that order, because they are
		different things and an editor that ran them together would either
		refuse a legal write or allow an illegal one:

		- **the image did not say.** Packed without its metadata tail, it
		  carries no capability vectors, and silence is not permission.
		- **the schema forbids it.** `mutate = Immutable` is a checksum, a
		  derived field, a read-only register: there is no setter anywhere in
		  situ for these and there is not one here.
		- **the write does not fit the member**, which `write_scalar` and
		  `write_bytes` check and which is a range error rather than a
		  permission one. A number is held to the member's width and sign; a
		  byte run is held to its exact length, since a run written shorter
		  or longer moves what follows it.

		The last part is coverage, and a write to a covered member goes
		through `_recompute`. **Which tag situ recomputes is the image's to
		say and not this tool's** (26.579): a checksum whose schema names
		the codec that computes it is brought up to date, and an AEAD tag
		is reported stale, because 14.1 puts a cryptographic tag with the
		caller and situ owns no AES-GCM. The write happens either way --
		refusing would make the field uneditable -- and the note says
		which of the two happened, naming the span it summed where it
		computed one.

		This said *situ does not recompute it* for as long as the image
		could not tell the two apart, and the result was a PNG written
		with a stale CRC under a note claiming the arithmetic was
		somebody else's.
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
		candidate = Document(self.image, bytearray(self.buffer), self.struct,
		                     self.at, self.limit)
		if isinstance(value, bytes):
			write_bytes(candidate.view(), index, value)
		else:
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
			return self._recompute(name, index)
		return []

	def _recompute(self, name: str, index: int) -> list[str]:
		"""Bring every tag covering `index` up to date, where situ can.

		14.1 puts computing a checksum with the caller, and for an AEAD tag
		that is the whole story: situ owns no AES-GCM. **It was never the
		whole story for a checksum whose schema names the codec that
		computes it** -- `is crc32`, decision 0053 -- and this tool said it
		was, wrote a PNG chunk with a stale CRC, and reported *situ does not
		compute it*. The write was corrupt and the reason was false
		(26.579).

		So the two cases are separated by asking the image rather than by
		one sentence about both: a tag whose kernel it carries is
		recomputed, and one it does not is reported exactly as before.
		"""
		from walker import derived

		notes: list[str] = []
		for tag, held in self.image.tags.items():
			if held.first == NONE or held.last == NONE:
				continue
			if not held.first <= index <= held.last:
				continue

			label = self.image.placement_names[tag] \
				if tag < len(self.image.placement_names) else f"#{tag}"

			if not derived.computes(self.image, tag):
				notes.append(
					f"`{name}` is covered by `{label}`, which is now "
					f"stale: its schema names no codec situ computes, so "
					f"recomputing it is the caller's (14.1)")
				continue

			try:
				at, span = self._covered_span(held)
				# `at` is view-relative, because `offset_bits` is, and
				# the buffer is not: a document opened at offset 8 would
				# otherwise sum the eight bytes of a PNG signature and
				# stop eight short of the chunk's end. Converted once,
				# here, rather than in `_covered_span` -- which stays in
				# the units `_hole` compares against.
				base = self.at
				value = derived.compute(
					self.image, tag,
					bytes(self.buffer[base + at:base + at + span]),
					self._hole(tag, at, span))
				self._store(tag, value)
			except (Refused, derived.Uncomputable) as why:
				# NAMING THE STALENESS FIRST, in the same shape as the
				# note above it. The reason a tag could not be recomputed
				# is worth having, and it is not the fact the caller has
				# to act on: the message is invalid until somebody fixes
				# the tag. Two tests pin the word, and they were right to
				# -- the first draft of this said only "could not be
				# recomputed", which a caller can read as "no change".
				notes.append(
					f"`{name}` is covered by `{label}`, which is now "
					f"stale: it could not be recomputed, because {why}")
				continue

			notes.append(f"`{label}` recomputed over {span} bytes at {at}")
		return notes

	def _hole(self, tag: int, at: int, span: int) -> tuple[int, int] | None:
		"""Where the tag's own bytes sit inside the span it covers (14.2).

		`None` when it sits outside, which is PNG's case: its CRC follows
		the bytes it covers. Every internet checksum is the other case,
		and `derived.compute` needs the offset RELATIVE to the covered
		span rather than to the message, since only this caller knows
		where that span began.
		"""
		view  = self.view()
		start = offset_bits(view, tag) // BITS_PER_BYTE
		wide  = size_bits(view, tag) // BITS_PER_BYTE
		if start < at or start + wide > at + span:
			return None
		return start - at, wide

	def _store(self, tag: int, value: int) -> None:
		"""Put a computed checksum where the tag's bytes are.

		A scalar goes through `write_scalar`, which knows the placement's
		byte order. A BYTE RUN -- PNG's `u8 crc[4]` -- has none of its own,
		and the schema says so deliberately: what has an order is the
		NUMBER the codec produces, which the image carries separately
		because WOZ2 stores its CRC little-endian where PNG stores its
		big. Every checksum in this repository is big, so an assumption
		here would be right by luck and wrong on the first schema that is
		not.

		The width is the kernel's rather than the run's, since a tag may
		be written wider or narrower than its code is: `[truncated]` is
		the schema's word for the second.
		"""
		if not is_run(self.image, tag):
			write_scalar(self.view(), tag, value)
			return

		held   = self.image.tags[tag]
		kernel = self.image.kernels[held.codec]
		wide   = (kernel.width + BITS_PER_BYTE - 1) // BITS_PER_BYTE
		order: Literal["little", "big"] = \
			"little" if held.endian == LITTLE else "big"
		write_bytes(self.view(), tag, value.to_bytes(wide, order))

	def _covered_span(self, held: object) -> tuple[int, int]:
		"""The bytes a tag covers, as `(offset, length)`.

		From the first covered MEMBER to the end of the last, which is what
		the image carries and why: `covers(summed)` names regions, and an
		`authenticated` region has no placement of its own to measure.
		"""
		view  = self.view()
		first = offset_bits(view, held.first) // 8	# type: ignore[attr-defined]
		last  = held.last				# type: ignore[attr-defined]
		end   = offset_bits(view, last) // 8 + size_bits(view, last) // 8
		if end < first:
			raise Refused("a tag's coverage ends before it begins")
		return first, end - first

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
		struct: str | None = None, at: int = 0,
		length: int | None = None) -> Document:
	"""Open a message against a packed image.

	`struct` names which layout to read it as; without one the first is
	taken, which is what a single-struct schema wants and what a reader of
	a larger one will immediately want to override.

	`at` and `length` are the window, for a struct that does not begin at
	byte zero of the file -- a PNG chunk, an ID3 frame, a TIFF directory.
	Without them a chunked container had to be cut up with `dd` before this
	tool could read any of it, which is the gap 26.581 closes.
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
	held = bytearray(message)
	# Refused HERE rather than at the first read, because a window outside
	# the file is a caller's mistake and every answer below it would be
	# about bytes nobody has. `acquire` checks it too -- it is the one
	# bounds check -- and this says which argument was wrong.
	end = len(held) if length is None else at + length
	if at < 0 or at > len(held):
		raise Refused(f"offset {at} is outside a {len(held)}-byte message")
	if end > len(held):
		raise Refused(f"offset {at} plus length {length} reaches {end}, "
		              f"past a {len(held)}-byte message")

	return Document(image, held, chosen, at,
	                None if length is None else end)
