"""A language server for situ schemas (section 26.19).

The largest remaining item and the least like the rest of this codebase: a
long-running process speaking a protocol, where everything else is a batch
compiler that runs once and exits.

What it carries that an editor cannot get elsewhere is not syntax colouring.
It is the capability vector of the field under the cursor, the blame chain for
a requirement that fails, and the advisor's costed suggestions -- all of which
are already computed. `situc explain` and `situc advise` are this information
behind a different door, so the work here is the door.

Standard library only, as everywhere else: JSON-RPC over stdio is a length
header and a JSON body, and a dependency for that would be a poor trade
against section 22's rule about vendoring.

Full-document sync, deliberately. A schema is a few hundred lines and parsing
one is microseconds; incremental sync would be a cache to keep coherent in
exchange for time nobody is waiting on.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from typing import Any, BinaryIO

from situc import requirements
from situc.diagnostics import Diagnostic, Severity, SituError, Source
from situc.layout import solve
from situc.parser import parse
from situc.resolve import ResolvedSchema, resolve

#: LSP severities. The protocol's numbering, not situ's.
LSP_ERROR: int   = 1
LSP_WARNING: int = 2
LSP_INFO: int    = 3
LSP_HINT: int    = 4

SEVERITY = {
	Severity.ERROR:   LSP_ERROR,
	Severity.WARNING: LSP_WARNING,
	Severity.NOTE:    LSP_INFO,
}


@dataclass
class Analysis:
	"""What one pass over a document produced, whether or not it got far."""

	source: Source
	diagnostics: list[Diagnostic]
	resolved: ResolvedSchema | None = None
	#: The AST as well as the resolved model: struct declarations keep their
	#: spans, and resolution does not carry them forward.
	schema: Any = None


def analyse_text(uri: str, text: str) -> Analysis:
	"""Everything the server knows about a document.

	Unlike the CLI's `analyse`, this never raises: an editor asks about a
	document in whatever state the user has left it, and half-written is the
	normal state rather than the exceptional one.
	"""
	source = Source(_path_of(uri), text)

	try:
		schema   = parse(source)
		resolved = resolve(schema, solve(schema))
	except SituError as exc:
		return Analysis(source, [exc.diagnostic])

	try:
		outcomes = requirements.discharge(schema, resolved)
	except SituError as exc:
		return Analysis(source, [exc.diagnostic], resolved, schema)

	found = [outcome.diagnostic for outcome in outcomes
	         if outcome.diagnostic is not None and outcome.is_error]
	found += requirements.warnings(outcomes)
	found += requirements.deferrals(outcomes)

	return Analysis(source, found, resolved, schema)


def _path_of(uri: str) -> str:
	return uri[len("file://"):] if uri.startswith("file://") else uri


# ---------------------------------------------------------------------------
# Translating situ's diagnostics into the protocol's
# ---------------------------------------------------------------------------


def to_lsp_diagnostic(source: Source, diagnostic: Diagnostic) -> dict[str, Any]:
	"""One diagnostic, with its blame chain kept.

	The chain is the product (section 17), so it travels in `relatedInformation`
	where an editor will show it, rather than being flattened into the message
	and lost to the first newline.
	"""
	primary = diagnostic.primary
	span    = primary.span if primary else None

	related = [
		{
			"location": {"uri": _uri_of(source),
			             "range": _range(source, label.span)},
			"message": label.message,
		}
		for label in diagnostic.labels
	]
	related += [{"location": {"uri": _uri_of(source),
	                          "range": _range(source, span) if span
	                          else _whole_first_line()},
	             "message": note}
	            for note in diagnostic.notes]

	return {
		"range":    _range(source, span) if span else _whole_first_line(),
		"severity": SEVERITY.get(diagnostic.severity, LSP_INFO),
		"source":   "situ",
		"message":  diagnostic.message + (f" -- {primary.message}" if primary else ""),
		"relatedInformation": related,
	}


def _uri_of(source: Source) -> str:
	return f"file://{source.path}"


def _range(source: Source, span: Any) -> dict[str, Any]:
	start_line, start_col = source.locate(span.start)
	end_line, end_col     = source.locate(span.end)
	return {
		"start": {"line": start_line - 1, "character": start_col - 1},
		"end":   {"line": end_line - 1,   "character": end_col - 1},
	}


def _whole_first_line() -> dict[str, Any]:
	"""Where a diagnostic with no span goes. Better than nowhere."""
	return {"start": {"line": 0, "character": 0},
	        "end":   {"line": 0, "character": 0}}


# ---------------------------------------------------------------------------
# The features worth having
# ---------------------------------------------------------------------------


def hover_at(analysis: Analysis, line: int, character: int) -> str | None:
	"""The capability vector of whatever the cursor is on.

	This is the reason to run a server rather than a linter. A field's vector
	is thirteen axes of consequence that the source text does not show, and
	`situc explain` already computes every one -- it just needs a cursor rather
	than a path.
	"""
	if analysis.resolved is None:
		return None

	offset  = _offset_of(analysis.source, line, character)
	matches: list[tuple[int, Any]] = []

	for struct in analysis.resolved.structs.values():
		for entry in struct.entries:
			span = getattr(entry.placement, "span", None)
			if span is None or not span.start <= offset < span.end:
				continue
			matches.append((span.end - span.start, entry))

	if not matches:
		return None

	# The narrowest match: a nested field's span sits inside its parent's,
	# and the cursor is on the inner one.
	#
	# `min` and then *all* of them, rather than the first: a struct declared
	# once and used twice resolves to one member per use, and every one of
	# them has this same source span. Keeping only the first showed one
	# vector and said nothing about the other, and they are not the same
	# vector -- `adv_report.event_type` is `offset = AbsoluteStatic(0x00)`
	# and unweakened, while `le_advertising_report.reports[].event_type` is
	# `FrameStatic(0x00)`, weakened, with a remedy. One line of source, and
	# the editor answered with whichever struct happened to be declared
	# first. Across the tree 218 spans carry more than one member and 165 of
	# those disagree about at least one axis.
	narrowest = min(width for width, _ in matches)
	chosen    = [entry for width, entry in matches if width == narrowest]
	return _render_vector(chosen[0], chosen[1:])


def _render_vector(entry: Any, others: list[Any] | None = None) -> str:
	from situc.capability import Axis

	placement = entry.placement
	lines     = [f"**{placement.path}** : `{placement.type_name}`", ""]

	strongest: list[str] = []
	weakened: list[str]  = []
	for axis in Axis:
		value = entry.vector.get(axis)
		text  = f"`{axis.value}` = {value.render()}"
		(weakened if _is_weakened(entry, axis) else strongest).append(text)

	# A bullet means weakened, always. The `elif` here used to render the
	# strongest axes as bullets too, so a member with *nothing* weakened and
	# one with *everything* weakened produced the same thirteen bullets and
	# no `Unweakened:` line -- two opposite states with identical evidence,
	# which is invariant 154. 137 members in the tree are the first case and
	# none is the second, so what a reader actually saw was a full list of
	# consequences on a field that has none. It misread as weakenings three
	# times over while this was being measured, which is the evidence.
	lines.extend(f"- {text}" for text in weakened)
	if weakened and strongest:
		lines.append("")
	if strongest:
		lines.append("Unweakened: " + ", ".join(strongest))

	lines.extend(_render_blame(entry))
	lines.extend(_render_other_uses(entry, others or []))
	return "\n".join(lines)


def _render_other_uses(entry: Any, others: list[Any]) -> list[str]:
	"""The same source line's other members, and where they disagree.

	Only the axes that differ, and only their values: a full vector each
	would be thirteen lines fifteen times over on the worst span in the tree,
	and what the reader cannot get anywhere else is which axis moved. Where
	nothing differs the use is still named, because "this member is reached
	twice" is itself the thing the source text does not show.
	"""
	from situc.capability import Axis

	if not others:
		return []

	lines = ["", "**Also reached as:**"]
	for other in others:
		differs = [f"`{axis.value}` = {other.vector.get(axis).render()}"
		           for axis in Axis
		           if other.vector.get(axis).render()
		           != entry.vector.get(axis).render()]
		detail = ", ".join(differs) if differs else "the same vector"
		lines.append(f"- `{other.placement.path}` -- {detail}")
	return lines


def _render_blame(entry: Any) -> list[str]:
	"""Why each weakened axis is what it is, and what to do about it.

	The vector alone says `mutate` is `Immutable` and stops there, which
	answers the smaller half of the question: a reader looking at a field
	wants to know *what did that* and *what may I do instead*, and the
	propagation table has carried both since 11.3. `situc explain` printed
	them and hover did not, so the editor gave a weaker answer than the CLI
	for the same field -- which this module's own docstring says it must not.
	"""
	if not entry.weakenings:
		return []

	lines = ["", "**Why:**"]
	for weakening in entry.weakenings:
		lines.append(f"- `{weakening.effect.axis.value}` = "
		             f"{weakening.effect.value.render()} -- "
		             f"{weakening.rule.construct}: {weakening.effect.because}")
		if weakening.rule.remedy:
			lines.append(f"  - *remedy:* {weakening.rule.remedy}")
	return lines


def _is_weakened(entry: Any, axis: Any) -> bool:
	from situc.capability import DOMAINS

	value = entry.vector.get(axis)
	return bool(DOMAINS.get(axis)) and value.base != DOMAINS[axis][0]


def _offset_of(source: Source, line: int, character: int) -> int:
	starts = source.line_starts()
	if not 0 <= line < len(starts):
		return 0
	return starts[line] + character


def code_actions(analysis: Analysis, line: int, character: int) -> list[dict[str, Any]]:
	"""The advisor's suggestions, for whatever the cursor is near.

	Section 18.2's catalog is already ranked and costed, and `situc advise`
	prints it. An editor is where a reader would rather see it, because the
	suggestion is about a field they are looking at.

	These are informational rather than fixes. A suggestion like "reorder the
	members so this one is last" is a change with a cost the author has to
	agree to, and applying it silently would be situ making a design decision
	on somebody's behalf.
	"""
	if analysis.resolved is None:
		return []

	from situc import advise

	offset = _offset_of(analysis.source, line, character)
	found  = []

	for suggestion in advise.suggest(analysis.resolved):
		span = suggestion.span
		if not span.start <= offset < span.end:
			continue
		found.append({
			"title": f"{suggestion.subject}: {suggestion.summary}",
			"kind":  "refactor",
			"diagnostics": [],
			"disabled": {"reason": suggestion.cost.render()},
			"data": {
				"rule":   suggestion.rule,
				"detail": suggestion.detail,
				"cost":   suggestion.cost.render(),
				"yields": suggestion.yields,
			},
		})

	return found


def definition_at(analysis: Analysis, line: int,
		character: int) -> dict[str, Any] | None:
	"""Where the type under the cursor is declared.

	A schema names types far more than it declares them, and the declaration is
	usually the thing a reader wants next.
	"""
	if analysis.resolved is None or analysis.schema is None:
		return None

	offset = _offset_of(analysis.source, line, character)
	named  = None

	for struct in analysis.resolved.structs.values():
		for entry in struct.entries:
			span = getattr(entry.placement, "span", None)
			if span is not None and span.start <= offset < span.end:
				if named is None or (span.end - span.start) < named[0]:
					named = (span.end - span.start, entry.placement.type_name)

	if named is None or named[1] is None:
		return None

	for decl in list(analysis.schema.structs()) + list(analysis.schema.enums()):
		if decl.name == named[1]:
			return {"uri": _uri_of(analysis.source),
			        "range": _range(analysis.source, decl.span)}
	return None


def symbols(analysis: Analysis) -> list[dict[str, Any]]:
	"""The outline: structs, and their fields beneath them."""
	if analysis.resolved is None or analysis.schema is None:
		return []

	STRUCT, ENUM, FIELD = 23, 10, 8		# LSP SymbolKind
	found: list[dict[str, Any]] = []

	for decl in analysis.schema.enums():
		found.append({
			"name":           decl.name,
			"kind":           ENUM,
			"range":          _range(analysis.source, decl.span),
			"selectionRange": _range(analysis.source, decl.span),
			"children":       [],
		})

	for decl in analysis.schema.structs():
		struct = analysis.resolved.structs.get(decl.name)
		if struct is None:
			continue

		children = [
			{
				"name":           entry.placement.path.split(".", 1)[-1],
				"kind":           FIELD,
				"range":          _range(analysis.source, entry.placement.span),
				"selectionRange": _range(analysis.source, entry.placement.span),
				"detail":         str(entry.placement.type_name or ""),
			}
			for entry in struct.entries
			if getattr(entry.placement, "span", None) is not None
		]

		found.append({
			"name":           decl.name,
			"kind":           STRUCT,
			"range":          _range(analysis.source, decl.span),
			"selectionRange": _range(analysis.source, decl.span),
			"children":       children,
		})

	return found


# ---------------------------------------------------------------------------
# The protocol
# ---------------------------------------------------------------------------


class Server:
	"""JSON-RPC over stdio.

	Framing is a `Content-Length` header, a blank line, and a JSON body. That
	is the whole of it, which is why there is no dependency here.
	"""

	def __init__(self, stdin: BinaryIO, stdout: BinaryIO) -> None:
		self.stdin     = stdin
		self.stdout    = stdout
		self.documents: dict[str, str] = {}
		self.running   = True

	# -- framing -------------------------------------------------------

	def read_message(self) -> dict[str, Any] | None:
		length = 0
		while True:
			line = self.stdin.readline()
			if not line:
				return None
			text = line.decode("ascii", "replace").strip()
			if not text:
				break
			if text.lower().startswith("content-length:"):
				length = int(text.split(":", 1)[1])

		if length <= 0:
			return None
		body = self.stdin.read(length)
		parsed: dict[str, Any] = json.loads(body.decode("utf-8"))
		return parsed

	def send(self, payload: dict[str, Any]) -> None:
		body = json.dumps(payload).encode("utf-8")
		self.stdout.write(f"Content-Length: {len(body)}\r\n\r\n".encode("ascii"))
		self.stdout.write(body)
		self.stdout.flush()

	def reply(self, request_id: Any, result: Any) -> None:
		self.send({"jsonrpc": "2.0", "id": request_id, "result": result})

	def notify(self, method: str, params: Any) -> None:
		self.send({"jsonrpc": "2.0", "method": method, "params": params})

	# -- dispatch ------------------------------------------------------

	def serve(self) -> int:
		while self.running:
			message = self.read_message()
			if message is None:
				break
			self.handle(message)
		return 0

	def handle(self, message: dict[str, Any]) -> None:
		method  = message.get("method")
		params  = message.get("params") or {}
		call_id = message.get("id")

		if method == "initialize":
			self.reply(call_id, {
				"capabilities": {
					"textDocumentSync":      1,	# full
					"hoverProvider":          True,
					"documentSymbolProvider": True,
					"codeActionProvider":     True,
					"definitionProvider":     True,
				},
				"serverInfo": {"name": "situc-lsp"},
			})
		elif method == "initialized":
			pass
		elif method == "shutdown":
			self.reply(call_id, None)
		elif method == "exit":
			self.running = False
		elif method in ("textDocument/didOpen", "textDocument/didChange"):
			self._store(params)
		elif method == "textDocument/didClose":
			self.documents.pop(_uri(params), None)
		elif method == "textDocument/hover":
			self.reply(call_id, self._hover(params))
		elif method == "textDocument/documentSymbol":
			self.reply(call_id, self._symbols(params))
		elif method == "textDocument/codeAction":
			self.reply(call_id, self._actions(params))
		elif method == "textDocument/definition":
			self.reply(call_id, self._definition(params))
		elif call_id is not None:
			# A request situ does not answer still needs an answer, or the
			# editor waits for one that never comes.
			self.reply(call_id, None)

	# -- handlers ------------------------------------------------------

	def _store(self, params: dict[str, Any]) -> None:
		uri = _uri(params)
		document = params.get("textDocument", {})

		if "text" in document:
			self.documents[uri] = document["text"]
		else:
			changes = params.get("contentChanges") or []
			if changes:
				self.documents[uri] = changes[-1].get("text", "")

		self._publish(uri)

	def _publish(self, uri: str) -> None:
		analysis = analyse_text(uri, self.documents.get(uri, ""))
		self.notify("textDocument/publishDiagnostics", {
			"uri": uri,
			"diagnostics": [to_lsp_diagnostic(analysis.source, diagnostic)
			                for diagnostic in analysis.diagnostics],
		})

	def _hover(self, params: dict[str, Any]) -> dict[str, Any] | None:
		uri      = _uri(params)
		position = params.get("position") or {}
		analysis = analyse_text(uri, self.documents.get(uri, ""))

		text = hover_at(analysis, position.get("line", 0),
		                position.get("character", 0))
		if text is None:
			return None
		return {"contents": {"kind": "markdown", "value": text}}

	def _actions(self, params: dict[str, Any]) -> list[dict[str, Any]]:
		uri      = _uri(params)
		at       = (params.get("range") or {}).get("start") or {}
		analysis = analyse_text(uri, self.documents.get(uri, ""))

		return code_actions(analysis, at.get("line", 0), at.get("character", 0))

	def _definition(self, params: dict[str, Any]) -> dict[str, Any] | None:
		uri      = _uri(params)
		position = params.get("position") or {}
		analysis = analyse_text(uri, self.documents.get(uri, ""))

		return definition_at(analysis, position.get("line", 0),
		                     position.get("character", 0))

	def _symbols(self, params: dict[str, Any]) -> list[dict[str, Any]]:
		uri = _uri(params)
		return symbols(analyse_text(uri, self.documents.get(uri, "")))


def _uri(params: dict[str, Any]) -> str:
	document: dict[str, Any] = params.get("textDocument") or {}
	return str(document.get("uri", ""))


def main() -> int:
	return Server(sys.stdin.buffer, sys.stdout.buffer).serve()
