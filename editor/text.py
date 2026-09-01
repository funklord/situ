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
		struct: str | None = None, as_hex: bool = False) -> Document:
	return open_document(image_bytes(schema, situc),
	                     read_message(message, as_hex), struct)


def render(document: Document) -> list[str]:
	"""The document as lines. Offsets and sizes in bytes, values as they are.

	A field the walk could not read keeps its row and carries its reason. An
	editor that dropped it would show a message missing something it has.
	"""
	lines = [f"{document.name}  {len(document.buffer)} bytes"]
	for field in document.fields():
		where = "--" if field.offset is None else f"{field.offset:>4}"
		wide  = "--" if field.size is None else f"{field.size:>3}"
		if isinstance(field.value, bytes):
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

	held = []
	if field.mutate == "Immutable":
		held.append("read-only")
	elif field.mutate == "Shifting":
		held.append("moves")
	elif field.mutate == "RewriteRequired":
		held.append("rewrites")
	if field.auth == "Covered":
		held.append("tag")

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
		"bytes":  len(document.buffer),
		"fields": [
			{
				"name":   field.name,
				"offset": field.offset,
				"size":   field.size,
				"value":  (field.value.hex() if isinstance(field.value, bytes)
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
				"write_cost": field.write_cost,
			}
			for field in document.fields()
		],
	}, indent=1)
