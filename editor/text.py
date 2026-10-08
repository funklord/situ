"""Opening a document, and rendering one as text.

Shared by the CLI and the TUI because both need exactly this and neither
should have its own version: two ways to open a file is two ways for them to
disagree about what a file is. 0034 makes the CLI the *reference* frontend,
which is a statement about what it can do rather than about where the code
lives -- and the first attempt did put it in the CLI, with the TUI importing
that script by path. That is a worse answer wearing the shape of a better
one.

Rendering is here for the same reason. It is a display decision, but it is
the same display decision twice, and a GUI that wants a different one simply
does not call this.
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path

import json

from editor.document import Document, Field, open_document

__all__ = ["as_json", "image_bytes", "open_from", "read_message",
           "render"]


def image_bytes(path: Path, situc: Path) -> bytes:
	"""The image for a schema or an image.

	Opening a `.situ` runs `situc pack`; nothing here imports the compiler.
	0026 keeps the two apart for reasons about the generated code rather
	than about packaging, and 0034 keeps that boundary by making it a
	process boundary. A machine with no compiler opens a pre-packed image
	and loses only the convenience.
	"""
	if path.suffix != ".situ":
		return path.read_bytes()

	# `--metadata` because this is the reader 26.33 split the tail off for:
	# an embedded walker wants a small table, a tooling walker wants names
	# and capability vectors. Without it every field is `placement[N]`,
	# which is a device's view of a document rather than a person's.
	built = subprocess.run(
		[str(situc), "pack", "--metadata", "-o", "/dev/stdout", str(path)],
		capture_output=True)
	if built.returncode != 0:
		raise SystemExit(f"`situc pack` refused {path.name}:\n"
		                 f"{built.stderr.decode('utf-8', 'replace').rstrip()}")
	return built.stdout


def read_message(path: Path, as_hex: bool) -> bytes:
	raw = path.read_bytes()
	if not as_hex:
		return raw
	try:
		return bytes.fromhex("".join(raw.decode("ascii", "replace").split()))
	except ValueError as why:
		raise SystemExit(f"{path.name} is not hex: {why}") from why


def open_from(schema: Path, message: Path, situc: Path,
		struct: str | None = None, as_hex: bool = False,
		at: int = 0, length: int | None = None,
		args: Sequence[int] = ()) -> Document:
	return open_document(image_bytes(schema, situc),
	                     read_message(message, as_hex), struct, at, length,
	                     args)


def render(document: Document) -> list[str]:
	"""The document as lines. Offsets and sizes in bytes, values as they are.

	A field the walk could not read keeps its row and carries its reason. An
	editor that dropped it would show a message missing something it has.
	"""
	# The WINDOW's length, not the file's (26.581). A chunk read at an
	# offset otherwise announced the size of the image it sits in, above a
	# list of offsets that were all about the chunk.
	#
	# And what the struct MEASURES where that is less, because the window
	# is whatever the caller gave and a reader stepping through a run needs
	# the record's own length (26.582). `M of N` only when they differ, so
	# a document opened whole or framed exactly reads as it always did --
	# and a measurement the walk cannot make says so rather than being
	# left out, since a missing number and an unmeasurable struct are
	# different findings.
	where_from = "" if document.at == 0 else f" at {document.at}"
	held = document.measured
	if held is None:
		how_long = f"{document.extent} bytes, extent unknown"
	elif held < document.extent:
		how_long = f"{held} of {document.extent} bytes"
	else:
		how_long = f"{document.extent} bytes"
	# THE VERDICT, on the header line, because it qualifies every row under
	# it (26.595). `validate` is one of `report.SUPPORTED`'s probes and
	# nothing surfaced it: a refusal reached a row only where it blamed a
	# member, so a BOUNDS failure -- which blames none -- was invisible
	# while every field said "cannot be read" and the variant row named an
	# arm confidently.
	# `said` and not `held`, which this function already binds to the
	# measured extent twenty lines up. A short generic name in a long
	# function is how a shadow gets written, and mypy is what said so.
	said   = document.verdict
	spoken = "" if said is None else f"  [{said[0]}]"
	lines  = [f"{document.name}  {how_long}{where_from}{spoken}"]
	if said is not None and said[0] != "ok":
		lines.append(f"{' ' * 14}validate: {said[1]}")
	for field in document.fields():
		where = "--" if field.offset is None else f"{field.offset:>4}"
		wide  = "--" if field.size is None else f"{field.size:>3}"
		if field.withheld:
			# `[secret]`, so the LENGTH and not the bytes (26.586). A
			# reader still learns the field is there and how long it is,
			# which is what the row is for; the schema said the contents
			# are a key.
			shown = (f"<withheld: {field.size} secret byte(s)>"
			         if field.size is not None else "<withheld: secret>")
		elif isinstance(field.value, bytes):
			shown = field.value.hex()
			if len(shown) > 32:
				shown = shown[:32] + "..."
		elif field.value is None:
			shown = f"({field.note})"
		else:
			shown = str(field.value)
		row    = f"  {where} +{wide}  {field.name:<24} {shown}"
		marker = _write_marker(field)
		lines.append(f"{row.ljust(50)}{marker}".rstrip() if marker else row)

		# A NOTE ON A READABLE FIELD WAS DISCARDED (26.583). The branches
		# above render `field.note` only in place of a MISSING value, so a
		# field that reads fine and carries a note showed the value and
		# nothing else -- and the note that matters most is exactly that
		# shape: `report.failed_check` names the member a schema refuses
		# the message over, which for `u8 seconds [max = 59]` holding 70
		# is a readable value.
		#
		# Measured: this frontend printed `seconds 70` while `--format
		# json` carried `refused: max` for the same document. So the text
		# tool -- the default one -- showed a message the schema rejects
		# as though it were fine, and the channel 0051 built and 26.231
		# wired had one consumer of two.
		#
		# A continuation line rather than a wider marker column, because a
		# note is prose and can run past any column that keeps the table
		# aligned.
		if field.note and field.value is not None:
			lines.append(f"{' ' * 14}{field.note}")

	# What the schema says about the message as a whole (0051), after the
	# fields rather than among them: a `when` is a predicate over the struct,
	# so there is no row it belongs on. The name is printed beside the text
	# because the name is the contract a consumer keys on and the text is a
	# default rendering it may replace.
	for severity, name, text in document.messages():
		lines.append(f"  {severity}: {name} -- {text}")
	return lines


def _write_marker(field: Field) -> str:
	"""What a write to this field would cost, where it is not simply a store.

	Nothing for the ordinary case, so the marker means something when it is
	there. An editor of *files* is the case 0047 was raised for, and the one
	question it has to answer before writing a byte is whether the schema
	permits it -- which the image has carried since 26.33 and nothing read
	until 26.177.
	"""
	if field.mutate is None:
		return ""

	# FIVE VALUES ON THIS AXIS AND THIS NAMED THREE (26.584). `mutate` is
	# `InPlaceFixed > InPlaceSlack > Shifting > RewriteRequired > Immutable`,
	# and `InPlaceSlack` fell through to the empty marker -- which is
	# `InPlaceFixed`'s answer and means "an ordinary store". Measured over
	# the corpus: 9 placements are `InPlaceSlack`, `mqtt.packet.length`
	# among them, and they showed exactly what the 857 free ones showed.
	#
	# The cost it hides depends on the VALUE rather than on the member: a
	# varint that re-encodes to the same length moves nothing and a longer
	# one moves everything after it, and a block-granularity codec
	# re-transforms the containing block. So the marker is weaker than
	# `moves` on purpose, and `write_cost` carries the sentence for a
	# reader who wants it.
	#
	# `InPlaceFixed` is still unmarked, and deliberately: it is the
	# ordinary case, and a marker on 857 of 1129 placements would be noise
	# that makes the other four mean less.
	held = []
	if field.mutate == "Immutable":
		held.append("read-only")
	elif field.mutate == "InPlaceSlack":
		held.append("needs slack")
	elif field.mutate == "Shifting":
		held.append("moves")
	elif field.mutate == "RewriteRequired":
		held.append("rewrites")
	if field.auth == "Covered":
		held.append("tag")

	# THE EFFECT AXIS, which no frontend carried in any form (26.586):
	# `Field` had no member for it, so neither renderer could have shown it.
	# register.situ's `fifo` is `[ro, on_read = pop]` -- reading pops a
	# FIFO, which its own comment calls the strongest effect there is -- and
	# the marker said `[read-only]`, which is the mutate axis answering a
	# different question.
	#
	# The axis value in prose rather than a verb, because the verb is the
	# schema's and the image does not carry it: `on_read = pop` and
	# `on_read = clear` reach this as the same `EffectOnRead`. Saying which
	# would be inventing it.
	if field.effect == "EffectOnRead":
		held.append("effect on read")
	elif field.effect == "EffectOnWrite":
		held.append("effect on write")
	elif field.effect == "EffectBoth":
		held.append("effect on read and write")

	return f"[{', '.join(held)}]" if held else ""


def as_json(document: Document) -> str:
	"""The document as structured data, for a frontend that is not Python.

	The C++ window drives `situ-edit` rather than reimplementing the
	document model, because a second implementation is precisely what 0034
	forbids -- three frontends with their own idea of what a field costs is
	the failure `traverse.py` exists to prevent. So the process boundary
	0034 already uses for `situc pack` carries the model as well, and this
	is what crosses it.

	`--format json` rather than parsing the table: `advise` and `diff` both
	offer one, so a reader of this tool already knows to ask.
	"""
	return json.dumps({
		"struct": document.name,
		# THE WINDOW, in both numbers, because every offset in this object
		# is view-relative and a `bytes` describing the file would be the
		# only member of it that was not (26.581). `at` is here so the
		# change is visible to a frontend rather than silent: a reader
		# that wants the file's length can add the two.
		"bytes":  document.extent,
		"at":     document.at,
		# What the struct itself occupies, where the walk can say. `null`
		# rather than absent, and never conflated with 0, which is a real
		# answer (26.582). Both frontends get it: wiring one and not the
		# other is the mistake 26.581 recorded.
		"measured": document.measured,
		"fields": [
			{
				"name":   field.name,
				"offset": field.offset,
				"size":   field.size,
				# WITHHELD HERE TOO, because a frontend reading the model
				# is a frontend showing it to somebody (26.586). The text
				# table and this carried the same hex for a `[secret]`
				# field, and fixing one would have left the other.
				"value":  (None if field.withheld
				           else field.value.hex()
				           if isinstance(field.value, bytes)
				           else field.value),
				"kind":   ("bytes" if isinstance(field.value, bytes)
				           else "int" if field.value is not None else "none"),
				"note":   field.note,
				# `readable` beside `writable`, because a frontend asking one
				# question should not have to derive the other from `kind`.
				"readable":   field.readable,
				# What a write would do. `mutate` is null where the image was
				# packed without its metadata tail, and a frontend must read
				# that as "not told" rather than as permission.
				"writable":   field.writable,
				"mutate":     field.mutate,
				"auth":       field.auth,
				# The two axes no frontend carried (26.586). `withheld`
				# beside `secrecy` so a consumer does not have to know
				# which value of the axis means it.
				"secrecy":    field.secrecy,
				"withheld":   field.withheld,
				"effect":     field.effect,
				"write_cost": field.write_cost,
			}
			for field in document.fields()
		],
		# Struct-scoped, so a sibling of `fields` rather than a key inside
		# one. Absent from an image packed before the section existed, which
		# reads as an empty list: a frontend must not take that for "the
		# schema says nothing", any more than a null `mutate` means yes.
		"messages": [
			{"severity": severity, "name": name, "text": text}
			for severity, name, text in document.messages()
		],
	}, indent=1)
