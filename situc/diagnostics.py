"""Diagnostic construction and rendering.

Diagnostic quality is the product (project.md section 17), so this module is
the one every other pass reports through. Blame chains arrive in phase 3; the
structures here already carry the secondary labels they will be built from.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from enum import Enum


class Severity(Enum):
	ERROR	= "error"
	WARNING	= "warning"
	NOTE	= "note"


@dataclass(frozen=True)
class Source:
	"""One schema file, held whole so diagnostics can quote it."""

	path: str
	text: str

	def line_starts(self) -> list[int]:
		starts = [0]
		for index, char in enumerate(self.text):
			if char == "\n":
				starts.append(index + 1)
		return starts

	def locate(self, offset: int) -> tuple[int, int]:
		"""Return the 1-based (line, column) of a byte offset."""
		starts = self.line_starts()
		line   = bisect.bisect_right(starts, offset) - 1
		return line + 1, offset - starts[line] + 1

	def line_text(self, line: int) -> str:
		starts = self.line_starts()
		if not 1 <= line <= len(starts):
			return ""
		begin = starts[line - 1]
		end   = self.text.find("\n", begin)
		return self.text[begin:] if end < 0 else self.text[begin:end]


@dataclass(frozen=True)
class Span:
	"""A half-open range of a source file."""

	source: Source
	start: int
	end: int

	@property
	def line(self) -> int:
		return self.source.locate(self.start)[0]

	@property
	def column(self) -> int:
		return self.source.locate(self.start)[1]

	def text(self) -> str:
		return self.source.text[self.start : self.end]

	def to(self, other: Span) -> Span:
		return Span(self.source, min(self.start, other.start), max(self.end, other.end))


@dataclass(frozen=True)
class Label:
	"""A span with a note, rendered as a quoted line and a caret run."""

	span: Span
	message: str = ""


@dataclass
class Diagnostic:
	severity: Severity
	message: str
	primary: Label | None			= None
	labels: list[Label]			= field(default_factory=list)
	notes: list[str]			= field(default_factory=list)

	def render(self) -> str:
		blocks   = [f"{self.severity.value}: {self.message}"]
		labelled = ([self.primary] if self.primary else []) + self.labels

		# One gutter width for the whole diagnostic keeps the bars aligned
		# across the quoted blocks.
		width = max((len(str(label.span.line)) for label in labelled), default=1)

		for index, label in enumerate(labelled):
			blocks.append(_render_label(label, width, arrow=index == 0 or bool(self.labels)))

		pad = " " * width
		if labelled and self.notes:
			# Section 17 closes the quoted block with a bare bar before the
			# notes start.
			blocks.append(f"{pad} |")

		for note in self.notes:
			blocks.append(f"{pad} = {note}")

		return "\n".join(blocks)

	def to_dict(self) -> dict[str, object]:
		"""Machine-readable form for `--diagnostics=json`.

		Emitted so the advisor, editors and CI can consume diagnostics without
		parsing prose (project.md section 17). The shape is committed and
		snapshot-tested: adding a key is compatible, renaming one is not.
		"""
		return {
			"severity": self.severity.value,
			"message":  self.message,
			"primary":  _label_dict(self.primary) if self.primary else None,
			"labels":   [_label_dict(label) for label in self.labels],
			"notes":    list(self.notes),
		}

	def __str__(self) -> str:
		return self.render()


def _label_dict(label: Label) -> dict[str, object]:
	line, column = label.span.source.locate(label.span.start)
	end_line, end_column = label.span.source.locate(label.span.end)
	return {
		"file":         label.span.source.path,
		"line":         line,
		"column":       column,
		"end_line":     end_line,
		"end_column":   end_column,
		"text":         label.span.text(),
		"message":      label.message,
	}


def _render_label(label: Label, width: int, arrow: bool) -> str:
	line, column = label.span.source.locate(label.span.start)
	raw          = label.span.source.line_text(line)

	# A tab in the quoted line would put the caret run in the wrong place, and
	# no tab width is prescribed in this project. Count each tab as one column.
	quoted	= raw.replace("\t", " ")
	extent	= max(1, min(label.span.end, label.span.start + len(raw)) - label.span.start)

	pad	= " " * width
	number	= str(line).rjust(width)
	caret	= " " * (column - 1) + "^" * extent
	tail	= f" {label.message}" if label.message else ""

	lines = []
	if arrow:
		lines.append(f"{pad}--> {label.span.source.path}:{line}:{column}")
	lines.append(f"{pad} |")
	lines.append(f"{number} | {quoted}")
	lines.append(f"{pad} | {caret}{tail}")
	return "\n".join(lines)


class SituError(Exception):
	"""A diagnostic raised as control flow.

	Carries the diagnostic rather than a string so the CLI can render it in the
	section 17 format and, from phase 3, emit it as JSON.
	"""

	def __init__(self, diagnostic: Diagnostic) -> None:
		super().__init__(diagnostic.message)
		self.diagnostic = diagnostic


def error(message: str, span: Span, label: str = "", notes: list[str] | None = None) -> SituError:
	"""Build the common case: one message, one span, optional notes."""
	return SituError(Diagnostic(
		severity = Severity.ERROR,
		message  = message,
		primary  = Label(span, label),
		notes    = notes or [],
	))


def not_yet_implemented(construct: str, span: Span, phase: int,
		notes: list[str] | None = None) -> SituError:
	"""Reject a construct the current phase does not accept.

	project.md section 26.1 requires the phase number in the message, so a user
	meeting one of these knows whether to wait or to rewrite the schema. Extra
	notes go after that, for a construct whose partial form is supported and
	where saying which part is the useful half of the message.
	"""
	return error(
		f"{construct} is not yet implemented",
		span,
		label = "not accepted by this build",
		notes = [f"planned for phase {phase} (project.md section 26)", *(notes or [])],
	)
