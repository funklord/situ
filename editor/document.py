"""What the editor knows about one message: the core of decision 0034.

A document is an image, a buffer, and the fields the two produce between
them. It renders nothing and displays nothing -- the CLI, the TUI and the GUI
each ask it the same questions and answer them their own way.

WHAT IT WRITES AND WHAT IT REFUSES. This said READ-ONLY, DELIBERATELY and
that none of 0034's write table was here, which stopped being true in three
places and was the first thing a reader of this module saw (26.605). Three
of that table's four rows are answered now:

  - **a fixed scalar in place** is `set()`, since 26.179;
  - **a tag-covered field** is recomputed where the schema names a kernel
    and refused where it does not, since 26.579;
  - **a write that shifts the layout** is refused by MEASURING -- the write
    goes into a copy, the copy is walked, and every member's offset and
    size compared -- because 12.3's invalidation model is not built here.
    26.99 finished rung 2 in the four backends and the walk, which is a
    different thing and is what this docstring used to cite.

**AND AN INVARIANT CANNOT GO STALE**, which is the fourth row and wants no
maintenance. The field an invariant derives is `mutate = Immutable` and
refused by name; anything that would move a size it reads is refused as a
shifting write. Both halves carry weight, and
`test_no_permitted_write_leaves_an_invariant_stale.py` breaks each to show
it -- with the first off a write leaves `total` at 1 against three payload
bytes, with the second off at 3 against one.
"""

from __future__ import annotations

from collections.abc import Sequence
import itertools
from dataclasses import dataclass, field as dataclass_field
from typing import Literal

from walker.image import LITTLE, NONE, Image, load
from walker.owned import each
from walker import report
from walker.report import FIELD, MARKER, REGION, RESERVED, SEALED
from walker.report import UNVERIFIED_OK
from walker.walk import (BITS_PER_BYTE, Bytes, Refused, Unsupplied, View,
                         acquire,
                         chosen_arm, is_run, marker_order, offset_bits,
                         argument, parameters_of, read_bytes, read_scalar,
                         scalar_range, size_bits, struct_extent, write_bytes,
                         write_scalar)

__all__ = ["Document", "Field", "open_document"]


def _choices(image: Image, index: int
		) -> tuple[bool, tuple[tuple[str, int], ...]] | None:
	held = image.choices_of(index)
	if held is None:
		return None
	admits, members = held
	return admits, tuple(members)


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
	#: `[secret]` (14.6). A frontend must not render the VALUE of one: the
	#: schema says these bytes are a key, and `situ-edit` printed them in
	#: full -- a `u8 key[8] [secret]` came out as its hex (26.586). Carried
	#: beside the value rather than instead of it, so `readable` stays a
	#: fact about the walk and the withholding is the display's decision.
	secrecy: str | None = None
	#: Whether an access is a pure load or store. `EffectOnRead` is the
	#: register whose read pops a FIFO or clears an interrupt, and no
	#: frontend carried this axis in any form -- `Field` had no member for
	#: it at all, so neither renderer could have shown it (26.586).
	effect: str | None = None
	#: A nested struct's type and where it starts in the BUFFER -- what
	#: `--struct` and `--offset` take to open it as its own document. The
	#: note says the same in prose for a person; this is it as data, for a
	#: frontend that opens it on a click rather than asking somebody to
	#: retype it.
	nested: tuple[str, int] | None = None
	#: Where a member that is not whole bytes sits, in BITS from the view's
	#: start, and how many it takes -- None for a whole-byte member, which
	#: `offset` and `size` already say exactly. Byte numbers alone put
	#: NTP's `leap`, `version` and `mode` all at offset 0, size 0
	#: (26.1113): three fields, one place, no width.
	bits: tuple[int, int] | None = None
	#: An enum-typed field's members, `(admits another value, ((name,
	#: value), ...))`, from the image's tail (26.1114): what an editor
	#: offers as choices. None where the field is not an enum or the image
	#: was packed without its metadata.
	choices: tuple[bool, tuple[tuple[str, int], ...]] | None = None
	#: The lowest and highest value a write may store, from the walker's
	#: own rule (`scalar_range`); None for anything but a scalar (26.1114).
	bounds: tuple[int, int] | None = None

	@property
	def withheld(self) -> bool:
		"""Whether a frontend must not show this field's VALUE (26.586).

		`[secret]` says the bytes are a key or a password, and both
		frontends printed them: a `u8 key[8] [secret]` came out as its hex
		in the table and as the same hex in `--format json`. The corpus's
		three secrets all sit inside sealed regions and so are never
		reached, which is why nothing noticed -- the hole was waiting for
		the first schema to mark a top-level field.

		The row stays, because dropping it would hide that the message has
		the field at all, which is the reasoning `fields()` already applies
		to a member it cannot read. What is withheld is the value.

		There is deliberately NO option to print it. Adding one is a
		decision about what this tool is for and belongs to the holder, and
		a flag invented here would be the kind of thing somebody finds in
		a shell history.
		"""
		return self.secrecy == "Secret"

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
	#: The struct's `parameter` members (0050), positionally and in
	#: declaration order. `report.SUPPORTED` names `needs-arguments` as one
	#: of its seventeen probes, and this frontend could neither report it
	#: nor satisfy it: there was no channel for an argument, so a struct
	#: that takes one crashed with a traceback out of `render` -- the view
	#: being lazy, the document opened and then exploded (26.598).
	args: tuple[int, ...] = ()
	#: The document this one was opened from, and the placement in it this
	#: one is (26.1141). A run resized inside a window moves what frames the
	#: window -- a chunk's `length`, and then whatever frames the chunk --
	#: and the chain is how the write finds them. None for a document
	#: opened on its own, whose window is all it knows.
	parent: "Document | None" = dataclass_field(default=None, repr=False,
	                                            compare=False)
	slot: int = -1

	@property
	def name(self) -> str:
		return self.image.struct_name(self.struct)

	def open_nested(self, index: int) -> "Document":
		"""The nested struct in field `index`, as a document of its own over
		THIS document's buffer.

		What `--struct` and `--offset` do from a command line, without the
		copy `open_document` makes: an edit in the nested document is in
		this one's bytes, which is what a frontend opening it on a keypress
		wants -- one buffer, written out once. Refused where the field is
		not a nested struct, and where the struct takes arguments, which
		its parent hands it (0068) and a person would have to supply.
		"""
		field = self.fields()[index]
		if field.nested is None:
			raise Refused(f"`{field.name}` is not a nested struct")
		name, at = field.nested
		names = [self.image.struct_name(i)
		         for i in range(len(self.image.structs))]
		chosen = names.index(name)
		if parameters_of(self.image, chosen):
			raise Unsupplied(f"`{name}` takes arguments its parent hands it; "
			                 f"open it with --struct {name} --offset {at} "
			                 "and --arg")
		limit = None if field.size is None else at + field.size
		return Document(self.image, self.buffer, chosen, at, limit,
		                parent=self, slot=self._members()[field.name])

	def open_element(self, index: int, which: int) -> "Document":
		"""Element `which` of the run of structs in field `index`, as a
		document over this one's buffer, as `open_nested` opens a member.

		`open_nested` on a run opened its FIRST element only: a PNG file's
		`chunks` gave the IHDR chunk and no way to reach the `tEXt` after it
		(26.1141). The elements are where the run's own walk puts them.
		"""
		field = self.fields()[index]
		if field.nested is None:
			raise Refused(f"`{field.name}` is not a run of structs")
		slot = self._members()[field.name]
		spans = report._run_elements(self.view(), slot)
		if spans is None:
			raise Refused(f"`{field.name}` is not a run whose elements this "
			              f"walk can place")
		if not 0 <= which < len(spans):
			raise Refused(f"`{field.name}` has {len(spans)} element(s)")
		at, size = spans[which]
		chosen = self.image.placements[slot].type_struct
		return Document(self.image, self.buffer, chosen, at, at + size,
		                parent=self, slot=slot)

	@property
	def verdict(self) -> tuple[str, str] | None:
		"""What `validate` says about the whole message (26.595).

		`(word, why)`, or `None` where the image cannot say -- packed
		without its metadata tail, or a struct this walker declines.

		One of `report.SUPPORTED`'s seventeen probes, and 0034 asks this
		frontend for every one. Nothing surfaced it: a refusal reached a
		row as `refused: <check>` on the member it blamed, which is the
		identity 0051 built and says nothing when the failure is BOUNDS
		and blames no member.

		It is what makes the arm row safe to read. On an empty message
		every reader answers that the discriminant is zero -- "a
		discriminant the frame does not reach reads as ZERO, which is what
		the four backends do" -- so the arm row names the arm zero
		selects, truthfully, about a message that has none of it. Without
		a verdict beside it that reads as a fact about the bytes.
		"""
		held = report._validate(self.image, self.view(), self.struct)
		if held is None:
			return None
		if held == report.OK:
			return ("ok", "every check this image carries holds")
		if held == report.ERR_BOUNDS:
			return ("bounds", "the frame does not reach what the layout "
			                  "needs, so the rest of this is what the "
			                  "schema says and not what the bytes hold")
		return ("constraint", "a check the schema states does not hold")

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
		return acquire(self.image, self.buffer, self.struct, self.args,
		               at = self.at, limit = self.limit)

	def parameters(self) -> list[str]:
		"""What this struct's arguments are called, in the order to pass
		them.

		`walk.parameters_of` answers with placements because a device omits
		the image's tail, so keying an argument by name would make it a
		thing only a tooling walker could take. This IS a tooling walker and
		has the tail, so it can say `version` where the walker says 1 --
		which is the difference between a usable message and a count.
		"""
		return [self.image.name_of(index).rpartition(".")[2]
		        or self.image.name_of(index)
		        for index in parameters_of(self.image, self.struct)]

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
			placement   = image.placements[index]
			opens: tuple[str, int] | None = None

			# A VARIANT, WHICH THE EDITOR SHOWED NOTHING OF (26.595). The
			# filter below keeps FIELD and RESERVED, so a variant was
			# dropped and its arms are not members of the struct at all --
			# so for icmp, mqtt, dns and json, whose meaning IS the
			# variant, the tool printed a message with a hole in it. The
			# walker's own listing says `body_echo ok=1` for the same
			# bytes; 0034 asks this frontend for every probe
			# `report.SUPPORTED` names, and `arm_value` was one nothing
			# surfaced.
			#
			# `walk.chosen_arm` and not a second reading of the
			# discriminant: it was extracted from `_variant_bits` for
			# exactly this reason, because two copies of "which arm does
			# this discriminant select" would be two answers to the
			# question the differential oracle exists to compare.
			if index in image.arms:
				rows.append(self._arm_row(view, index))
				continue

			# EVERY KIND GETS A ROW (26.597). This was an allow-list --
			# `kind not in (FIELD, RESERVED)` and skip -- over a table
			# whose eight members `walker/report.py` named four of, so
			# four kinds rendered as nothing at all. Swept over the
			# corpus: 18 members across 12 structs, and for `slip` and
			# `protobuf` the dropped member IS the whole message, so this
			# tool printed a header and no rows whatever.
			#
			# A sentence per kind rather than a branch per kind, because
			# what failed was the allow-list and not which kinds were on
			# it: a kind nothing here has a sentence for now renders with
			# its number instead of vanishing, which is the one behaviour
			# that cannot go stale as the enum grows.
			name  = image.name_of(index)
			local = name.rpartition(".")[2] or name

			try:
				first = offset_bits(view, index)
				count = size_bits(view, index)
			except Refused as why:
				rows.append(Field(local, None, None, None,
				                  f"cannot be placed: {why}",
				                  mutate  = image.capability_of(index, "mutate"),
				                  auth    = image.capability_of(index, "auth"),
				                  secrecy = image.capability_of(index,
				                                               "secrecy"),
				                  effect  = image.capability_of(index,
				                                                "effect")))
				continue

			at   = first // BITS_PER_BYTE
			wide = count // BITS_PER_BYTE
			bits = (None if first % BITS_PER_BYTE == 0
			        and count % BITS_PER_BYTE == 0 else (first, count))

			value = held.get(local)
			if value is None and placement.parameter:
				# A PARAMETER'S VALUE IS THE ONE THING IN ITS ROW NOBODY
				# HAS TO READ: the caller supplied it, and `view.args`
				# holds it. `owned.decode` reads the message, so it
				# answers nothing here -- and the row said *cannot be
				# read* about the number the caller had just typed on the
				# command line (26.598).
				try:
					value = argument(view, index)
				except Unsupplied:
					value = None
			if value is None:
				value = self._read_kind(view, index, placement)
			note  = "" if value is not None else "cannot be read"

			says = report.KIND_SAYS.get(placement.kind)
			if says is None and placement.kind not in (FIELD, RESERVED):
				says = (f"a placement of kind {placement.kind}, which this "
				        f"frontend has no sentence for")
			if placement.kind == REGION:
				# WHICH region, because the three are not one thing to a
				# reader. `report`'s gate probe answers `refused=1
				# opened=1` -- the gate's CLAIM rather than a measurement,
				# which is why it needs no cipher run to be comparable --
				# and the fact a person needs from it is whether there is
				# a gate at all. `[allow_unverified_read]` is the
				# construct whose whole purpose is to give the guarantee
				# up (14.3), and a row that read the same either way would
				# hide the one difference that changes what the bytes are
				# worth (26.599).
				flags = image.region_flags.get(index, 0)
				if flags & SEALED and flags & UNVERIFIED_OK:
					says = ("a sealed region whose schema waives the "
					        "guarantee with `[allow_unverified_read]`, so "
					        "there is no gate to open")
				elif flags & SEALED:
					says = ("a sealed region: its interior reads only "
					        "through a gate that a failed verification "
					        "refuses (14.3)")
			if placement.kind == MARKER:
				# `walk.marker_order` and not a second comparison of the
				# held value against `image.markers`: it is the function
				# the walk itself branches on, made public for this
				# caller (26.597). Two answers to "which order did this
				# marker state" is the fault `chosen_arm` was extracted
				# to stop.
				try:
					says = f"{says}, and it states {marker_order(view)}"
				except Refused as why:
					says = f"{says}, and {why}"
			if says is not None:
				note = f"{note}; {says}" if note else says

			# A CHECKSUM THAT DOES NOT MATCH ITS BYTES. The editor
			# recomputed a covered tag after a write and said nothing about
			# the one it OPENED: a PNG chunk whose CRC was a bit off showed
			# its value and `[ok]`, where `verify` refuses the file. Asked
			# of the same `derived.compute` the write uses, and encoded the
			# way `_store_in` writes, so the check and the write cannot
			# disagree about byte order (26.1124's example found it).
			stale = self._tag_mismatch(view, index)
			if stale is not None:
				note = f"{note}; {stale}" if note else stale

			# A LOCATED MEMBER IN A WINDOW IS READING THE CONTAINER
			# (26.583). `at expr` is measured from the buffer and not from
			# the view -- `out->base = msg->base + at` in every generated
			# backend, and the walker subtracts the base to match. That is
			# right, and for a document opened at an offset it is useless:
			# a BMP embedded at byte 8 has `bfOffBits = 54`, which is 54
			# bytes into the CONTAINER, ten bytes short of its own pixels.
			#
			# Measured on a 2x2 BMP at offset 8: the run shown is
			# `0000...ff0000ff0000` where the image's own pixels are
			# `0000ff0000ff...`. Both readers agree and both are right
			# about the format; what is wrong is the question.
			#
			# Said here rather than refused, because the schema and the
			# window are both legitimate and only the reader knows whether
			# the offset is a container's or a file's.
			if placement.located_code != NONE and self.at:
				located = (f"its offset is measured from the start of the "
				           f"buffer (9.8) and not from this window, so it "
				           f"lands {self.at} byte(s) before this struct's "
				           f"own base")
				note = f"{note}; {located}" if note else located
			# A run of bytes that `[holds]` a struct opens as that struct,
			# framed by the run (26.898) -- the shape behind an outer length,
			# where the struct's own run and the length framing it both move
			# when the run grows (26.1141).
			held_struct = (placement.type_struct
			               if placement.type_struct != NONE
			               else image.holds.get(index, NONE))
			if held_struct != NONE:
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
				opens  = (image.struct_name(held_struct), self.at + at)
				nested = (f"a nested `{opens[0]}`; "
				          f"open it with --offset {opens[1]}")
				# APPENDED, like every other note on this row. It assigned,
				# so it dropped `cannot be read` from a nested struct the
				# frame does not hold -- and, once the other kinds started
				# rendering, the sentence saying what an indexed run IS
				# (26.597). The `blamed` branch below already argues this
				# in as many words and this branch did the opposite.
				note = f"{note}; {nested}" if note else nested
			if local == blamed:
				# Appended rather than replacing: a field can be unreadable
				# AND be the one the schema refuses over, and a note that
				# dropped either half would answer a question nobody asked.
				note = f"{note}; refused: {because}" if note \
				       else f"refused: {because}"

			rows.append(Field(local, at, wide, value, note,
			                  mutate  = image.capability_of(index, "mutate"),
			                  auth    = image.capability_of(index, "auth"),
			                  secrecy = image.capability_of(index, "secrecy"),
			                  effect  = image.capability_of(index, "effect"),
			                  nested  = opens, bits = bits,
			                  choices = _choices(image, index),
			                  bounds  = (scalar_range(view, index)
			                             if isinstance(value, int)
			                             else None)))

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

	def _read_kind(self, view: View, index: int,
			placement: object) -> int | bytes | None:
		"""One of the kinds `owned.decode` does not answer for.

		`decode` is rung 2 and reads fields, so a marker, a region, a tlv
		run or an indexed run was absent from its answer -- and the row
		for one then said *cannot be read* about bytes this walker reads
		happily. Measured on `slip`, whose whole message is one region:
		82 bytes read, and a listing claiming none of them (26.597).

		The walker's own readers, chosen by kind rather than tried in
		turn. A marker is a scalar and is read big-endian whatever it
		says, which `read_scalar` already does by consulting
		`marker_order`; the span kinds are runs and `read_scalar` refuses
		them by name. `None` where the frame does not reach it, which is
		the one case `cannot be read` is the truth.
		"""
		kind = getattr(placement, "kind", FIELD)
		if kind in (FIELD, RESERVED):
			return None
		try:
			if kind == MARKER:
				return read_scalar(view, index)
			return read_bytes(view, index)
		except Refused:
			return None

	def _members(self) -> dict[str, int]:
		"""Local name -> placement index, for the members a write can name.

		Narrower than `fields` since 26.597, which stopped dropping the
		other six kinds from the listing. Whether any of them should be
		WRITABLE is a separate question and not one to settle while fixing
		a listing: a marker is a byte-order flag, so storing one changes
		how every other member of its struct reads, and that is 0034's
		write path to decide rather than this map's.
		"""
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
		if isinstance(value, bytes):
			held = self._extents().get(self.image.name_of(index))
			if held is not None and held[1] != len(value) * BITS_PER_BYTE:
				return self._resize(name, index, value, held)

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

	def _resize(self, name: str, index: int, value: bytes,
			held: tuple[int, int]) -> list[str]:
		"""A byte run written at a new length: the first slice of 0034's
		shifting write (26.1115).

		The run's bytes are spliced in and the scalar that sizes it moves by
		the same number -- `n` for `u8 name[n]`, `len` for `len - 4` -- and
		the result is ACCEPTED ONLY BY MEASUREMENT, as `set` accepts a fixed
		write: the copy is walked again, and everything before the run must
		sit where it sat, everything after it exactly `delta` bytes on, and
		every other member read the same value. Which scalar sizes the run
		is not read out of the size program; every writable scalar ahead of
		the run is tried, and exactly one must pass -- two that would both
		give a consistent message are refused as ambiguous rather than one
		picked.

		Two things this does not do, and refuses rather than gets wrong:

		- **A window inside a larger message.** A parent's length, an outer
		  frame, an offset table elsewhere in the buffer -- 12.3's
		  invalidation model is what would find and move them, and it is not
		  built. So the document must be the whole buffer.
		- **Two fields at once** -- a run sized by one field inside a
		  struct framed by another. The walk then disagrees with every
		  single candidate and the refusal names what moved.
		"""
		if self.at != 0 or (self.limit is not None
		                    and self.limit != len(self.buffer)):
			if self.parent is None:
				raise Refused(
					f"`{name}`: a run's length can change only in a "
					f"document over the whole buffer, or one opened from "
					f"its parent -- a window opened on its own has a parent "
					f"whose lengths would go stale (0034, 12.3)")
			return self._resize_through(name, index, value, held)

		start, bits = held
		at = self.at + start // BITS_PER_BYTE
		old = bits // BITS_PER_BYTE
		delta = len(value) - old
		image = self.image
		before = self._extents()
		values = self._values(self.view())
		here = image.name_of(index)
		local = here.rpartition(".")[2] or here

		def splice() -> bytearray:
			return bytearray(self.buffer[:at]) + bytearray(value) \
				+ bytearray(self.buffer[at + old:])

		# AND THE MESSAGE STILL CONFORMS where it did. Every member reading
		# its old value is not enough: a field DERIVED from the run's size --
		# `invariant total == size(payload)` -- keeps its old value and is
		# now wrong, and it is Immutable, so nothing here may move it. The
		# verdict is what checks an invariant, so a write that turns `ok`
		# into anything else is not one this slice may make (the case
		# `test_no_permitted_write_leaves_an_invariant_stale` pins).
		was_ok = self.verdict is not None and self.verdict[0] == "ok"
		broke: list[str] = []		# why a layout that fit did not conform

		def holds(candidate: "Document", moved: str | None) -> bool:
			"""The walk moved exactly the run's tail by `delta`, the run
			reads back as written, every other member reads the same, and
			a message that conformed still does."""
			try:
				after = candidate._extents()
				read = candidate._values(candidate.view())
				verdict = candidate.verdict
			except Refused:
				return False
			shifted = self._shifted_by(before, after, start, delta, here)
			if was_ok and (verdict is None or verdict[0] != "ok"):
				if shifted and verdict is not None:
					broke.append(verdict[1])
				return False
			return self._shifted_by(before, after, start, delta, here) \
				and read.get(local) == value and all(
					read.get(key) == held_value
					for key, held_value in values.items()
					if key not in (local, moved))

		# A run the FRAME sizes -- `[remaining]` over the whole buffer --
		# needs nothing else to move, and a field changed for no reason
		# would be a second edit nobody asked for. So the splice alone is
		# tried first, and taken alone when it holds.
		plain = Document(image, splice(), self.struct, self.at, None)
		if holds(plain, None):
			self._take(plain.buffer)
			notes = [f"`{name}` is {len(value)} byte(s) now, and what "
			         f"follows it moved with it"]
			if image.capability_of(index, "auth") == "Covered":
				notes.extend(self._recompute(name, index))
			return notes

		element = image.placements[index].element_bits // BITS_PER_BYTE
		steps = [delta]
		if element > 1 and delta % element == 0:
			steps.append(delta // element)

		# What each other member must read as afterwards: the same.
		passing: list[tuple[str, Document, int]] = []
		for other in image.members(image.structs[self.struct]):
			if other == index:
				continue
			where = before.get(image.name_of(other))
			if where is None or where[0] >= start:
				continue		# only what comes before the run
			short = image.name_of(other).rpartition(".")[2]
			old_value = values.get(short)
			if not isinstance(old_value, int) or isinstance(old_value, bool):
				continue
			if image.capability_of(other, "mutate") in (None, "Immutable"):
				continue
			# By the bytes, or by the ELEMENTS a run of wider ones counts:
			# `u16 w[n]` grown by four bytes needs `n` to move by two.
			for step in steps:
				candidate = Document(image, splice(), self.struct, self.at,
				                     None)
				try:
					write_scalar(candidate.view(), other, old_value + step)
				except Refused:
					continue
				if holds(candidate, short):
					passing.append((short, candidate, step))
					break

		if not passing and broke:
			raise Refused(
				f"`{name}`: a shifting write of {len(value)} byte(s) where "
				f"there are {old} would leave the message not conforming: "
				f"{broke[0]}")
		if not passing:
			raise Refused(
				f"`{name}`: a shifting write of {len(value)} byte(s) where "
				f"there are {old} needs the field that sizes the run to "
				f"move too, and no single writable field ahead of it does "
				f"that and leaves the rest of the message reading as it did")
		if len(passing) > 1:
			raise Refused(
				f"`{name}`: {' and '.join(f'`{one}`' for one, _, _ in passing)} "
				f"could each be what sizes it; set the run at its length and "
				f"say which by hand")

		sizer, candidate, step = passing[0]
		self._take(candidate.buffer)
		notes = [f"`{name}` is {len(value)} byte(s) now, `{sizer}` moved by "
		         f"{step:+d}, and what follows it moved with it"]
		for touched in (index, self._members().get(sizer)):
			if touched is not None and \
					image.capability_of(touched, "auth") == "Covered":
				notes.extend(self._recompute(
					image.name_of(touched).rpartition(".")[2], touched))
		return notes

	def _resize_through(self, name: str, index: int, value: bytes,
			held: tuple[int, int]) -> list[str]:
		"""`_resize` in a window opened from its parent (26.1141).

		The same measured search, at every level from this window out to
		the document over the whole buffer: here the run grows by `delta`,
		and in each parent the member holding the level below grows by the
		same. At each level either nothing moves with it -- the frame sizes
		it -- or one writable scalar ahead of it moves by `delta` (here, by
		elements too, as `_resize` steps). A combination holds when every
		level measures as `_resize` measures one: what precedes the grown
		member sits where it sat, what follows is `delta` bytes on, every
		other member reads the same, and a level that conformed still
		conforms. The fewest fields moved wins, and two combinations at that
		count are refused as ambiguous rather than one picked.

		12.3's invalidation model would find the stale lengths by
		dependency; this finds them by measurement, along the one path the
		window was opened down -- an offset table elsewhere, or a length in
		a sibling, is not on it, and a combination that leaves one stale is
		refused by the walk rather than written.
		"""
		chain: list[Document] = [self]
		while chain[-1].parent is not None:
			chain.append(chain[-1].parent)
		root = chain[-1]
		if root.at != 0 or (root.limit is not None
		                    and root.limit != len(root.buffer)):
			raise Refused(
				f"`{name}`: the outermost document this was opened from is "
				f"itself a window, so what frames it cannot be seen")
		if any(doc.buffer is not root.buffer for doc in chain):
			raise Refused(f"`{name}`: the documents along this chain hold "
			              f"different buffers")

		image = self.image
		start, bits = held
		at = self.at + start // BITS_PER_BYTE
		old = bits // BITS_PER_BYTE
		delta = len(value) - old
		here = image.name_of(index)
		local = here.rpartition(".")[2] or here
		element = image.placements[index].element_bits // BITS_PER_BYTE

		def short(of: int) -> str:
			return image.name_of(of).rpartition(".")[2] or image.name_of(of)

		# Per level: what it was, the member that grows in it, and what may
		# move with it -- `(placement, step)`, None for nothing.
		levels = []
		for depth, doc in enumerate(chain):
			before = doc._extents()
			values = doc._values(doc.view())
			grows = index if depth == 0 else chain[depth - 1].slot
			where = before.get(image.name_of(grows))
			if where is None:
				raise Refused(f"`{name}`: `{doc.name}` cannot place the "
				              f"member this window is")
			steps = [delta]
			if depth == 0 and element > 1 and delta % element == 0:
				steps.append(delta // element)
			options: list[tuple[int, int] | None] = [None]
			for other in image.members(image.structs[doc.struct]):
				if other == grows:
					continue
				spot = before.get(image.name_of(other))
				if spot is None or spot[0] >= where[0]:
					continue
				was = values.get(short(other))
				if not isinstance(was, int) or isinstance(was, bool):
					continue
				if image.capability_of(other, "mutate") in (None, "Immutable"):
					continue
				options.extend((other, step) for step in steps)
			verdict = doc.verdict
			levels.append((doc, before, values, grows, where[0],
			               verdict is not None and verdict[0] == "ok",
			               options))

		def limit_of(doc: Document) -> int | None:
			return None if doc.limit is None else doc.limit + delta

		def attempt(choice: tuple[tuple[int, int] | None, ...]
				) -> bytearray | None:
			grown = bytearray(root.buffer[:at]) + bytearray(value) \
				+ bytearray(root.buffer[at + old:])
			for (doc, _, values, *_rest), moved in zip(levels, choice):
				if moved is None:
					continue
				was = values[short(moved[0])]
				assert isinstance(was, int)
				try:
					write_scalar(Document(image, grown, doc.struct, doc.at,
					                      limit_of(doc), doc.args).view(),
					             moved[0], was + moved[1])
				except Refused:
					return None
			for depth, ((doc, before, values, grows, begins, was_ok, _),
			            moved) in enumerate(zip(levels, choice)):
				again = Document(image, grown, doc.struct, doc.at,
				                 limit_of(doc), doc.args)
				try:
					after = again._extents()
					read = again._values(again.view())
					verdict = again.verdict
				except Refused:
					return None
				if was_ok and (verdict is None or verdict[0] != "ok"):
					return None
				if not self._shifted_by(before, after, begins, delta,
				                        image.name_of(grows)):
					return None
				if depth == 0 and read.get(local) != value:
					return None
				skip = {short(grows)} | ({short(moved[0])} if moved else set())
				if any(read.get(key) != was for key, was in values.items()
				       if key not in skip):
					return None
			return grown

		combos = list(itertools.product(*(options for *_, options in levels)))
		if len(combos) > 4096:
			raise Refused(f"`{name}`: {len(combos)} ways to move the fields "
			              f"framing this window are too many to measure")
		combos.sort(key=lambda choice: sum(one is not None for one in choice))
		passing: list[tuple[tuple[tuple[int, int] | None, ...], bytearray]] = []
		for choice in combos:
			moved_count = sum(one is not None for one in choice)
			if passing and moved_count > sum(
					one is not None for one in passing[0][0]):
				break
			grown = attempt(choice)
			if grown is not None:
				passing.append((choice, grown))
		if not passing:
			raise Refused(
				f"`{name}`: a shifting write of {len(value)} byte(s) where "
				f"there are {old} needs what frames it to move too, and no "
				f"field along {' < '.join(doc.name for doc in chain)} does "
				f"that and leaves the rest reading as it did")
		if len(passing) > 1:
			said = [", ".join(f"`{short(one[0])}`" for one in choice
			                  if one is not None) for choice, _ in passing]
			raise Refused(f"`{name}`: {' or '.join(said)} could each be what "
			              f"sizes it; set it at its length and say which by "
			              f"hand")

		choice, grown = passing[0]
		root.buffer[:] = grown
		for doc in chain:
			if doc.limit is not None:
				doc.limit += delta
		notes = [f"`{name}` is {len(value)} byte(s) now"]
		for (doc, *_), moved in zip(levels, choice):
			if moved is not None:
				notes.append(f"`{doc.name}.{short(moved[0])}` moved by "
				             f"{moved[1]:+d}")
		notes.append("and what follows it moved with it")
		# Innermost first: a chunk's CRC before anything covering the chunk.
		for (doc, _, _, grows, *_), moved in zip(levels, choice):
			for touched in (grows, None if moved is None else moved[0]):
				if touched is not None and \
						image.capability_of(touched, "auth") == "Covered":
					notes.extend(doc._recompute(short(touched), touched))
		return notes

	def _take(self, grown: bytearray) -> None:
		"""Adopt a buffer of a new length. A window that was the whole
		buffer stays the whole buffer: a `limit` left at the old length
		named bytes that are no longer there (or left new ones out), and
		the next walk refused the window. Measured: a run shrunk by three
		with `--length` equal to the file."""
		self.buffer[:] = grown
		if self.limit is not None:
			self.limit = len(self.buffer)

	@staticmethod
	def _shifted_by(before: dict[str, tuple[int, int] | None],
			after: dict[str, tuple[int, int] | None], start: int,
			delta: int, run: str) -> bool:
		"""Whether the walk moved exactly what a run growing by `delta`
		bytes at bit `start` should move, and nothing else."""
		if set(before) != set(after):
			return False
		for key, was in before.items():
			now = after[key]
			if was is None or now is None:
				if was is not now:
					return False
				continue
			if key == run:
				if now != (was[0], was[1] + delta * BITS_PER_BYTE):
					return False
			elif was[0] < start or (was[0] == start and was[1] == 0):
				if now != was:
					return False
			elif now != (was[0] + delta * BITS_PER_BYTE, was[1]):
				return False
		return True

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
		mine = set(self.image.members(self.image.structs[self.view().struct]))
		notes.extend(self._recompute_spanning(name, index))
		for tag, held in self.image.tags.items():
			if held.after or held.message:
				continue		# `_recompute_spanning`'s (26.1117)
			# `covers(before)` names no members: it covers whatever this
			# struct places ahead of it, and its span is the message's
			# from the first byte (26.1050, 26.1062).
			if held.before:
				if not (tag in mine and index in mine and index < tag):
					continue
			elif held.first == NONE or held.last == NONE:
				continue
			elif not held.first <= index <= held.last:
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
				if held.before:
					start = offset_bits(self.view(), tag) // BITS_PER_BYTE
					value = derived.compute(
						self.image, tag, bytes(self.buffer[:self.at + start]))
					self._store(tag, value)
					notes.append(f"`{label}` recomputed over the "
					             f"{self.at + start} bytes ahead of it")
					continue
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

	def _recompute_spanning(self, name: str, index: int) -> list[str]:
		"""`covers(after)` and `covers(message)` (26.1117): the sums whose
		span is the message's, in this struct or in a struct nested in it --
		U-Boot's data CRC and FS2's image CRC both sit in a header the
		bytes written are not in. Each is found through a view of the
		struct that declares it, and the span is the walker's own
		`message_span`, so the editor and the walker cannot disagree about
		which bytes a write stales."""
		from walker import derived
		from walker.report import message_span

		written = self.at + offset_bits(self.view(), index) // BITS_PER_BYTE
		views = [self.view()]
		names = [self.image.struct_name(i)
		         for i in range(len(self.image.structs))]
		for field in self.fields():
			if field.nested is None:
				continue
			inner, at = field.nested
			try:
				views.append(acquire(self.image, self.buffer,
				                     names.index(inner), at=at))
			except (Refused, Unsupplied, ValueError):
				continue
		notes: list[str] = []
		for view in views:
			for tag in self.image.members(self.image.structs[view.struct]):
				held = self.image.tags.get(tag)
				if held is None or not (held.after or held.message):
					continue
				span = message_span(view, tag, held)
				if span is None:
					continue
				lo, hi, hole = span
				if not lo <= written < hi:
					continue
				label = self.image.placement_names[tag] \
					if tag < len(self.image.placement_names) else f"#{tag}"
				if not derived.computes(self.image, tag):
					notes.append(
						f"`{name}` is covered by `{label}`, which is now "
						f"stale: its schema names no codec situ computes, "
						f"so recomputing it is the caller's (14.1)")
					continue
				try:
					value = derived.compute(self.image, tag,
					                        bytes(self.buffer[lo:hi]), hole)
				except derived.Uncomputable as why:
					notes.append(
						f"`{name}` is covered by `{label}`, which is now "
						f"stale: it could not be recomputed, because {why}")
					continue
				self._store_in(view, tag, value)
				notes.append(f"`{label}` recomputed over {hi - lo} bytes "
				             f"at {lo}")
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

	def _tag_mismatch(self, view: View, tag: int) -> str | None:
		"""`stored X, computes Y` for a tag of this struct whose codec situ
		computes and whose stored value disagrees, None otherwise -- for a
		tag that matches, that is not one, or that cannot be computed here.
		The spanning forms (`covers(after)`, `covers(message)`) are left to
		`verify`, which reads them over the message."""
		from walker import derived

		held = self.image.tags.get(tag)
		if held is None or held.after or held.message \
				or not derived.computes(self.image, tag):
			return None
		try:
			if held.before:
				start = offset_bits(view, tag) // BITS_PER_BYTE
				value = derived.compute(
					self.image, tag, bytes(self.buffer[:self.at + start]))
			else:
				if held.first == NONE or held.last == NONE:
					return None
				at, span = self._covered_span(held)
				value = derived.compute(
					self.image, tag,
					bytes(self.buffer[self.at + at:self.at + at + span]),
					self._hole(tag, at, span))
			first = offset_bits(view, tag) // BITS_PER_BYTE
			wide  = size_bits(view, tag) // BITS_PER_BYTE
		except (Refused, derived.Uncomputable):
			return None
		where  = slice(self.at + first, self.at + first + wide)
		stored = bytes(self.buffer[where])
		saved  = self.buffer
		self.buffer = bytearray(saved)
		try:
			self._store_in(self.view(), tag, value)
			wanted = bytes(self.buffer[where])
		except Refused:
			return None
		finally:
			self.buffer = saved
		if wanted == stored:
			return None
		return f"stale: stored {stored.hex()}, its codec computes {wanted.hex()}"

	def _store(self, tag: int, value: int) -> None:
		self._store_in(self.view(), tag, value)

	def _store_in(self, view: View, tag: int, value: int) -> None:
		"""Put a computed checksum where the tag's bytes are, through the
		view of the struct that declares it.

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
			write_scalar(view, tag, value)
			return

		held   = self.image.tags[tag]
		kernel = self.image.kernels[held.codec]
		wide   = (kernel.width + BITS_PER_BYTE - 1) // BITS_PER_BYTE
		order: Literal["little", "big"] = \
			"little" if held.endian == LITTLE else "big"
		write_bytes(view, tag, value.to_bytes(wide, order))

	def _arm_row(self, view: View, index: int) -> Field:
		"""A variant, as the arm the discriminant selected.

		The value is the arm's own name, because that is what a reader of a
		variant wants to know and what every other row here carries is a
		value. Where no arm is selected the row says so rather than
		vanishing: `default: error` is a real state and a message in it is
		one the schema refuses, which is worth seeing next to the
		discriminant that chose it.
		"""
		image = self.image
		local = image.name_of(index).rpartition(".")[2] or image.name_of(index)

		try:
			at   = offset_bits(view, index) // BITS_PER_BYTE
			wide = size_bits(view, index) // BITS_PER_BYTE
		except Refused as why:
			at, wide = None, None
			note = f"cannot be placed: {why}"
		else:
			note = ""

		try:
			arm = chosen_arm(view, index)
		except Refused as why:
			return Field(local, at, wide, None,
			             note or f"the arm cannot be read: {why}")

		if arm is None or arm == NONE:
			return Field(local, at, wide, None,
			             "no arm: the discriminant matches no case, which "
			             "`default: error` makes a refusal")

		# THE NAME IN THE NOTE AND NOT IN THE VALUE. A `Field.value` is an
		# int or bytes, so an arm name put there renders as its own hex --
		# `6563686f` for `echo`, which is the information encoded as
		# noise. A variant holds no scalar, so `None` is the honest value
		# and the note carries the answer, which is what a nested struct's
		# row already does.
		chose = image.name_of(arm).rpartition(".")[2] or image.name_of(arm)
		return Field(local, at, wide, None,
		             f"the arm `{chose}` is the one present"
		             + (f"; {note}" if note else ""))

	def relations(self) -> list[tuple[str, str, str]]:
		"""Which cross-message relations this document could be half of.

		`(name, role, struct)` per relation whose request or response is the
		struct this document was opened as. A document is one message, and a
		relation is a predicate over two -- so what a single document can
		say is which pairings it is eligible for, and `relate` below is what
		answers the predicate once the second message is in hand.
		"""
		found: list[tuple[str, str, str]] = []
		for index, held in enumerate(self.image.relations):
			for role, struct in (("request", held.request),
			                     ("response", held.response)):
				if struct == self.struct:
					found.append((self.image.relation_name(index), role,
					              self.image.struct_name(struct)))
		return found

	def relate(self, which: str, other: Document) -> tuple[bool, str]:
		"""Whether this document and `other` satisfy a named relation.

		THIS ONE IS THE REQUEST and `other` the response, because the order
		is temporal and a predicate over an unordered pair is a different
		claim: dns's `reply_to` holds for (query, reply) and not for
		(reply, query), which `test_relations` pins in both directions.

		`(held, why)` rather than a bare bool. The predicate itself is
		deliberately no richer than OK or CONSTRAINT -- a walker that said
		WHICH `must` failed would answer a question the four compiled
		backends cannot (26.95) -- so `why` names the relation and the two
		structs rather than inventing a reason.

		Built because nothing called `report.relate` outside its own tests,
		and 0034 names this tool as what a read-only editor is worth
		shipping with: *follow a relation between two messages* (26.594).
		"""
		wanted = [i for i in range(len(self.image.relations))
		          if self.image.relation_name(i) == which]
		if not wanted:
			known = [self.image.relation_name(i)
			         for i in range(len(self.image.relations))]
			raise Refused(
				f"no relation `{which}` in this image; it has "
				f"{', '.join(known) if known else 'none'}")

		index = wanted[0]
		held  = self.image.relations[index]

		# BY NAME AND NOT BY INDEX. A struct index means nothing across two
		# images: `tick` is index 0 in its own image exactly as
		# `dns_header` is in dns's, so comparing indices let a document
		# from another SCHEMA through and would have returned a verdict
		# about neither message. Caught by the test that passes two images
		# on purpose.
		#
		# Identity on the image was the first fix and was worse: every
		# `open_document` loads its own, so two documents over the same
		# bytes are different objects and the check refused the ordinary
		# case. A name comparison is what both cases actually turn on.
		#
		# The residual is two schemas that share a struct name, which this
		# cannot tell apart. Naming it rather than reaching for a
		# fingerprint: the relation is still evaluated against THIS
		# document's image, so the worst case is a predicate run over a
		# message laid out by a different schema of the same shape.
		if self.name != self.image.struct_name(held.request):
			raise Refused(
				f"`{which}` takes a {self.image.struct_name(held.request)} "
				f"first and this document is a {self.name}")
		if other.name != self.image.struct_name(held.response):
			raise Refused(
				f"`{which}` takes a {self.image.struct_name(held.response)} "
				f"second and that document is a {other.name}")

		verdict = report.relate(self.image, index, self.view(), other.view())
		return verdict == report.OK, (
			f"`{which}`: {self.name} then {other.name}")

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
		"""What rung 2 can read, member by member.

		`decode` is whole-or-nothing, which is right for an owned value and
		wrong for a display, and the editor's catch of its refusal turned
		into nothing at all: one `u16 w[n]` made every row of its struct
		say "cannot be read" (26.1116). `owned.each` reads what it can and
		leaves out the rest, and every row carries its own note.
		"""
		return each(view)


def open_document(image_bytes: bytes, message: Bytes,
		struct: str | None = None, at: int = 0,
		length: int | None = None,
		args: Sequence[int] = ()) -> Document:
	"""Open a message against a packed image.

	`struct` names which layout to read it as; without one the first is
	taken, which is what a single-struct schema wants and what a reader of
	a larger one will immediately want to override.

	`args` are the struct's `parameter` members, positionally. Checked here
	rather than at the first read for the same reason the window is: a
	caller who has not said which block size the stream negotiated has not
	chosen a layout, and every answer below would be about bytes nobody
	read. It was checked nowhere, so `acquire` raised `Unsupplied` out of
	`render` and the tool printed a traceback (26.598).

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

	# NAMING THEM, where `acquire` can only count them. Its own docstring
	# says why it counts -- a device omits the image's tail, so an argument
	# keyed by name would be one only a tooling walker could take -- and
	# this is the tooling walker. "takes 1 argument(s) and 0 were supplied"
	# does not say which, and a person holding a capture cannot act on it.
	wanted = parameters_of(image, chosen)
	if len(args) != len(wanted):
		named = ", ".join(
			f"`{image.name_of(one).rpartition('.')[2] or image.name_of(one)}`"
			for one in wanted)
		raise Unsupplied(
			f"struct `{image.struct_name(chosen)}` takes "
			f"{len(wanted)} argument(s) and {len(args)} were given"
			+ (f": {named}, in that order" if wanted else ""))

	document = Document(image, held, chosen, at,
	                    None if length is None else end, tuple(args))

	# AND ACQUIRE IT HERE, which is the half 26.598 left undone. That entry
	# fixed `needs-arguments` by checking the argument list above, and the
	# OTHER verdict `report.listing` prints in place of every row --
	# `no-view`, a frame too short for the struct -- went on reaching
	# `acquire` from `Document.view()`, lazily, with nothing in between. So
	# a zero-byte message still printed a traceback out of `render`: one
	# symptom fixed and its cause left in place (26.600).
	#
	# Naming the struct, because `acquire` cannot. "frame of 0 does not
	# reach 28" is true and says nothing about which layout wanted 28, and
	# a reader passing `--struct` has just chosen between several.
	try:
		document.view()
	except Refused as why:
		raise Refused(f"struct `{image.struct_name(chosen)}`: {why}") from None

	return document


def open_within(window: Document,
		ancestors: Sequence[tuple[str | None, int, int | None]]) -> None:
	"""Give `window` the chain of documents it was opened down (26.1141).

	`ancestors` are `(struct, at, length)`, outermost first, the first over
	the whole buffer -- what a frontend that opens one window per nested
	struct knows of the windows behind it, and all the command line can
	carry. Each is opened over `window`'s own buffer, so an edit through the
	window is in all of them, and the member of each that holds the next is
	found by containment: the one whose extent covers the next window.
	A struct of `None` is the first, as `open_document` takes it.
	"""
	image = window.image
	names = [image.struct_name(i) for i in range(len(image.structs))]
	chain: list[Document] = []
	for struct, at, length in ancestors:
		if struct is not None and struct not in names:
			raise Refused(f"--within names no struct `{struct}`")
		chain.append(Document(image, window.buffer,
		                      0 if struct is None else names.index(struct),
		                      at, None if length is None else at + length))
	chain.append(window)
	for outer, inner in zip(chain, chain[1:]):
		end = len(outer.buffer) if inner.limit is None else inner.limit
		holds = [index for index in image.members(image.structs[outer.struct])
		         if image.placements[index].type_struct != NONE
		         or index in image.holds]
		found = None
		for index, where in ((index, outer._extents().get(image.name_of(index)))
		                     for index in holds):
			if where is None:
				continue
			start = outer.at + where[0] // BITS_PER_BYTE
			stop = start + where[1] // BITS_PER_BYTE
			if start <= inner.at and end <= stop:
				found = index
				break
		if found is None:
			raise Refused(f"no member of `{outer.name}` at {outer.at} holds "
			              f"the `{inner.name}` at {inner.at}")
		inner.parent = outer
		inner.slot = found
