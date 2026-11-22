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
from dataclasses import asdict
from collections.abc import Sequence
from pathlib import Path

import json

from editor import view
from editor.document import Document, open_document
from walker.walk import Refused

__all__ = ["apply_edits", "as_json", "image_bytes", "open_from",
           "read_message", "render", "render_rows"]


def image_bytes(path: Path, situc: Path) -> bytes:
	"""The image for a schema or an image.

	Opening a `.situ` runs `situc pack`; nothing here imports the compiler.
	0026 keeps the two apart for reasons about the generated code rather
	than about packaging, and 0034 keeps that boundary by making it a
	process boundary. A machine with no compiler opens a pre-packed image
	and loses only the convenience.
	"""
	if path.suffix != ".situ":
		return _read(path)

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


def _read(path: Path) -> bytes:
	"""A file's bytes, or the reason it has none in a sentence.

	A message or image that is not there was a Python traceback in all
	three frontends, the window's status line included -- the one input
	every user mistypes. `situc` already says it this way for a schema.
	"""
	try:
		return path.read_bytes()
	except OSError as why:
		raise SystemExit(f"cannot read {path}: {why.strerror}") from None


def read_message(path: Path, as_hex: bool) -> bytes:
	raw = _read(path)
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


def apply_edits(document: Document, assignments: list[str],
		byte_assignments: list[str]) -> list[str]:
	"""Every `--set` then every `--set-bytes`, returning what each one cost.

	In order, and stopping at the first refusal: a half-applied edit written
	to a file is worse than none, and a reader of the output has to be able
	to say which writes happened.
	"""
	notes: list[str] = []

	for assignment in assignments:
		name, sep, text = assignment.partition("=")
		if not sep:
			raise Refused(f"`--set {assignment}` is not `name=value`")
		try:
			value = int(text, 0)
		except ValueError:
			raise Refused(f"`--set {assignment}`: `{text}` is not a number") \
				from None
		notes.extend(document.set(name.strip(), value))

	for assignment in byte_assignments:
		name, sep, text = assignment.partition("=")
		if not sep:
			raise Refused(f"`--set-bytes {assignment}` is not `name=hex`")
		try:
			raw = bytes.fromhex(text.strip())
		except ValueError:
			raise Refused(f"`--set-bytes {assignment}`: `{text}` is not "
			              f"hex") from None
		notes.extend(document.set(name.strip(), raw))

	return notes


def element_of(where: str, said: str) -> tuple[str, int]:
	"""`NAME:N` as the member's name and the element's number."""
	name, sep, number = where.rpartition(":")
	if not sep or not name.strip() or not number.strip().isdigit():
		raise Refused(f"`{said}` does not name an element as NAME:N")
	return name.strip(), int(number)


def apply_splices(document: Document, inserts: list[str],
		removals: list[str]) -> list[str]:
	"""Every `--insert NAME:N=HEX`, then every `--remove NAME:N` (26.1147),
	returning what each cost; in order, stopping at the first refusal, as
	`apply_edits` does."""
	notes: list[str] = []
	for typed in inserts:
		where, sep, text = typed.partition("=")
		if not sep:
			raise Refused(f"`--insert {typed}` is not `NAME:N=hex`")
		name, which = element_of(where, f"--insert {typed}")
		try:
			value = bytes.fromhex(text.strip())
		except ValueError:
			raise Refused(f"`--insert {typed}`: `{text}` is not hex") \
				from None
		notes.extend(document.insert(name, which, value))
	for typed in removals:
		name, which = element_of(typed, f"--remove {typed}")
		notes.extend(document.remove(name, which))
	return notes


def render(document: Document) -> list[str]:
	"""The document as lines. Offsets and sizes in bytes, values as they are.

	A field the walk could not read keeps its row and carries its reason. An
	editor that dropped it would show a message missing something it has.
	"""
	return [line for line, _ in render_rows(document)]


def render_rows(document: Document) -> list[tuple[str, int | None]]:
	"""`render`'s lines, each with the index of the field it belongs to.

	None for a line about the whole message -- the header, the verdict, a
	`when`. The TUI moves its cursor over fields and edits the one under it,
	and this is how it knows which one that is without re-deriving the
	layout of these lines: the renderer that decided a note gets a
	continuation line is the one that says so.

	Every cell is `view`'s, which the window draws too (26.1113); what is
	decided here is only the line -- the columns, and a byte run shortened
	to fit one.
	"""
	head = view.heading(document)
	lines: list[tuple[str, int | None]] = [(head[0], None)]
	lines += [(f"{' ' * 14}{more}", None) for more in head[1:]]
	for index, (field, row) in enumerate(zip(document.fields(),
	                                         view.rows(document))):
		where = row.offset if field.offset is None else row.offset.rjust(4)
		wide  = row.size if field.size is None else row.size.rjust(3)
		shown = row.value
		if isinstance(field.value, bytes) and not field.withheld \
				and len(shown) > 32:
			shown = shown[:32] + "..."
		line = f"  {where} +{wide}  {row.name:<24} {shown}"
		lines.append((f"{line.ljust(50)}{row.marker}".rstrip()
		              if row.marker else line, index))
		# A continuation line rather than a wider marker column, because a
		# note is prose and can run past any column that keeps the table
		# aligned (26.583).
		if row.note:
			lines.append((f"{' ' * 14}{row.note}", index))
	lines += [(f"  {said}", None) for said in view.trailer(document)]
	return lines


def _ancestors(document: Document) -> list[Document]:
	"""The documents `document` was opened down, outermost first."""
	found: list[Document] = []
	held = document.parent
	while held is not None:
		found.insert(0, held)
		held = held.parent
	return found


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
		# THE WINDOWS BEHIND THIS ONE, outermost first, as `--within` gave
		# them and as an edit left them: a run grown in this window moves
		# each one's frame by as much, and a frontend holding their
		# lengths has to follow (26.1141). Empty for a document opened on
		# its own.
		"within": [{"struct": outer.name, "at": outer.at,
		            "bytes": outer.extent}
		           for outer in _ancestors(document)],
		# THE VERDICT, which the text header has carried since 26.595 and
		# this never did -- so the window, which reads only this, could not
		# say a message was refused over a check that blames no member. The
		# same two parts as the header: the word, and why where it is not
		# `ok`. `null` where the walk gave none.
		"verdict": (None if document.verdict is None
		            else {"word": document.verdict[0],
		                  "why":  document.verdict[1]}),
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
				# A nested struct as data: its type and where it starts in
				# the BUFFER, the `--struct` and `--offset` that open it.
				# The note says the same for a person; the window opens it
				# on a double click from these (0034).
				# A member that is not whole bytes, in bits from the
				# view's start: `offset` and `size` above are its bytes
				# rounded down, which for a bit field is 0 wide.
				"bits":       (None if field.bits is None
				               else {"offset": field.bits[0],
				                     "size":   field.bits[1]}),
				# An enum's members, for an editor's choices (26.1114):
				# whether it admits a value it does not name, and each
				# member's name and value in declaration order.
				# What a write may store, from the walker's own rule.
				"bounds":     (None if field.bounds is None
				               else {"low": field.bounds[0],
				                     "high": field.bounds[1]}),
				"choices":    (None if field.choices is None
				               else {"admits_other": field.choices[0],
				                     "members": [
				                         {"name": name, "value": value}
				                         for name, value in field.choices[1]]}),
				"nested":     (None if field.nested is None
				               else {"struct": field.nested[0],
				                     "offset": field.nested[1],
				                     "length": field.size}),
				# Each element of a run of structs, where the field is one,
				# as `nested` names the run framed whole (26.1144).
				"elements":   (None if field.elements is None
				               else [{"offset": at, "length": size}
				                     for at, size in field.elements]),
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
		# WHAT TO SHOW, as `view` decides it for the text table and the TUI
		# (26.1113): the heading's lines, one row of cells per field in
		# `fields`' order, and the trailer. The window draws these and
		# derives none of them, so it cannot word a cell differently.
		"view": {
			"heading": view.heading(document),
			"rows":    [asdict(row) for row in view.rows(document)],
			"trailer": view.trailer(document),
		},
	}, indent=1)
