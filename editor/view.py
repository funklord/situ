"""What a frontend shows of a document, decided once for every frontend.

0034 keeps the editing RULES in one place; this keeps the DISPLAY there
too. Until 26.1113 the Qt window had its own copy of what a field's value
reads as, which rows may be edited, whether an edit is hex or a number,
and what the header says -- four decisions made in Python for the text
table and the TUI and again in C++ for the window, which is two places for
them to disagree. Measured before the move: a `[secret]` field read
`<withheld: 4 secret byte(s)>` in the table and `<withheld: secret>` in the
window, and the window showed no write marker at all, only the sentence.

So a frontend asks this for the heading, a row per field and the trailer,
and draws them: the CLI as lines (`text.render`), the curses TUI as the
same lines with a cursor, and the window -- in a window or on a terminal
through qtty -- as cells it reads from `--format json`'s `view`. None of
them derives a cell from a field.
"""

from __future__ import annotations

from dataclasses import dataclass, field as _field

from editor.document import Document, Field
from walker.walk import Refused

__all__ = ["Row", "heading", "prompt", "put", "rows", "trailer",
           "write_marker"]


@dataclass(frozen=True)
class Row:
	"""One field as every frontend shows it.

	`value` is whole -- a byte run's every byte in hex -- and a frontend
	with a fixed width shortens it itself, since how wide a line is is the
	one thing only the frontend knows. `edit` is how a typed value is put:
	`hex` for a byte run, `number` otherwise, None where the schema lets
	nobody write it; `put` is the act, so the choice is made here and not
	at a prompt or in a cell.
	"""

	offset: str
	size:   str
	name:   str
	value:  str
	marker: str
	note:   str
	cost:   str
	edit:   str | None
	opens:  bool
	#: An enum's members as `name (value)`, in declaration order, and
	#: whether a value it does not name is admitted -- what a window offers
	#: in a searchable list and a prompt accepts by name (26.1114). Empty
	#: for a field that is not an enum.
	choices: list[str] = _field(default_factory=list)
	admits_other: bool = True
	#: The lowest and highest number a write may store, as text because a
	#: 64-bit bound is not a double; empty where the field is not a scalar
	#: (26.1114). A number field refuses at the key what is outside them.
	low:  str = ""
	high: str = ""


def heading(document: Document) -> list[str]:
	"""The struct, how much of the window it measures, and the verdict.

	The WINDOW's length, not the file's (26.581), and what the struct
	measures where that is less (26.582) -- `M of N` only when they differ,
	and an unmeasurable extent said rather than left out. The verdict on
	the first line because it qualifies every row (26.595), with why on a
	second where it is not `ok`.
	"""
	where_from = "" if document.at == 0 else f" at {document.at}"
	measured = document.measured
	if measured is None:
		how_long = f"{document.extent} bytes, extent unknown"
	elif measured < document.extent:
		how_long = f"{measured} of {document.extent} bytes"
	else:
		how_long = f"{document.extent} bytes"
	verdict = document.verdict
	spoken = "" if verdict is None else f"  [{verdict[0]}]"
	lines = [f"{document.name}  {how_long}{where_from}{spoken}"]
	if verdict is not None and verdict[0] != "ok":
		lines.append(f"validate: {verdict[1]}")
	return lines


def trailer(document: Document) -> list[str]:
	"""What the schema says about the message as a whole (0051).

	After the fields, since a `when` is a predicate over the struct and has
	no row; the name beside the text because the name is the contract.
	"""
	return [f"{severity}: {name} -- {text}"
	        for severity, name, text in document.messages()]


def rows(document: Document) -> list[Row]:
	return [_row(field) for field in document.fields()]


def _row(field: Field) -> Row:
	if field.withheld:
		# `[secret]`, so the LENGTH and not the bytes (26.586): a reader
		# still learns the field is there and how long it is.
		value = (f"<withheld: {field.size} secret byte(s)>"
		         if field.size is not None else "<withheld: secret>")
	elif isinstance(field.value, bytes):
		value = field.value.hex()
	elif field.value is None:
		value = f"({field.note})"
	else:
		value = _named(field, field.value)
	# A NOTE ON A READABLE FIELD (26.583) is its own cell: `report.
	# failed_check` names the member a schema refuses the message over,
	# and for `u8 seconds [max = 59]` holding 70 that is a readable value.
	# Where the value is missing the note already stands in its place.
	note = field.note if field.value is not None else ""
	can_write = field.writable and field.readable and not field.withheld
	if field.bits is not None:
		# BYTE.BIT and a width in bits, for a member that is not whole
		# bytes: the bit counted in walk order from the byte's first bit,
		# as `offset_bits` counts it. In bytes, NTP's `leap`, `version`
		# and `mode` all read offset 0, size 0 (26.1113).
		offset = f"{field.bits[0] // 8}.{field.bits[0] % 8}"
		size   = f"{field.bits[1]}b"
	else:
		offset = "--" if field.offset is None else str(field.offset)
		size   = "--" if field.size is None else str(field.size)
	choices = ([] if field.choices is None
	           else [f"{name} ({number})" for name, number in field.choices[1]])
	return Row(
		offset=offset, size=size,
		name=field.name, value=value, marker=write_marker(field),
		note=note or "", cost=field.write_cost,
		edit=(None if not can_write
		      else "hex" if isinstance(field.value, bytes)
		      else "choice" if choices else "number"),
		opens=field.nested is not None,
		choices=choices,
		admits_other=field.choices is None or field.choices[0],
		low="" if field.bounds is None else str(field.bounds[0]),
		high="" if field.bounds is None else str(field.bounds[1]))


def _named(held: Field, value: int) -> str:
	"""A number, and the member it is where an enum names it: `server (4)`,
	as Wireshark shows one. The number stays, because a reader comparing
	with a hex dump needs it, and a value no member names is the number
	alone."""
	if held.choices is not None:
		for name, number in held.choices[1]:
			if number == value:
				return f"{name} ({value})"
	return str(value)


def prompt(row: Row) -> str:
	"""What a line-at-a-time frontend asks with: the name, and what it
	takes where that is a list or a range -- the window's widgets said in
	words, for the curses TUI."""
	if row.choices:
		more = ", or a number" if row.admits_other else ""
		listed = ", ".join(choice.split(" (", 1)[0] for choice in row.choices)
		return f"{row.name} ({listed}{more}) = "
	if row.edit == "number" and row.low:
		return f"{row.name} ({row.low} to {row.high}) = "
	if row.edit == "hex":
		return f"{row.name} (hex, {row.size} bytes) = "
	return f"{row.name} = "


def put(document: Document, name: str, text: str) -> list[str]:
	"""Store `text` into the field `name`, read as the field shows it.

	Hex for a byte run, a number otherwise: the choice the CLI makes by
	flag (`--set-bytes` against `--set`), made here by what the field holds
	-- the person typing is looking at the value, so asking which flag they
	meant would be asking the row back. The write is `apply_edits`, the
	CLI's own, so a refusal is the CLI's word for word, and a name no field
	has is refused there too. A field the schema lets nobody write is still
	tried: the refusal says why, which is the answer the person needs.
	"""
	from editor.text import apply_edits

	named = [field for field in document.fields() if field.name == name]
	if not named:
		# The core's own words for it, which `--set` gives; reached here
		# first only because a name with no field has no kind to read.
		raise Refused(f"no member named `{name}` in `{document.name}`")
	held = named[0].value
	typed = text.strip()
	if named[0].choices is not None:
		# A member by name, or as the row shows it -- `server` or
		# `server (4)` -- so a cell edited back to what it said, or a
		# choice picked from the list, is the member's value. Anything
		# else goes through as a number, and a closed enum's refusal of a
		# value it does not name is the core's.
		bare = typed.split(" (", 1)[0] if typed.endswith(")") else typed
		for member, number in named[0].choices[1]:
			if member == bare:
				typed = str(number)
				break
	assignment = f"{name}={typed}"
	if isinstance(held, bytes):
		return apply_edits(document, [], [assignment])
	return apply_edits(document, [assignment], [])


def write_marker(field: Field) -> str:
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
