"""Making the C and C++ comments visible to a documentation tool.

Rust and Python land in their languages' own systems for free: `///` is
rustdoc and a docstring is a docstring. C and C++ wrote plain `/* */`, which
Doxygen does not extract at all -- so a Doxygen run over a generated header
produced an entry per function with no documentation against it, while the
reasons sat directly above in the file.

The comments were never the problem. They carry what a signature cannot: the
capability vector, why a bound is 999 rather than 65535, which mistake the
bound prevents. What was missing is one character.

Doing it here rather than at each site is deliberate. Forty functions across
two backends write these blocks, and a rule applied in forty places is a rule
that will be applied in thirty-nine -- which is the lesson this repository has
learned repeatedly about questions asked in more than one place. It is also
the correct layer: whether a comment is extractable is a fact about rendering,
not about what the comment says.
"""

from __future__ import annotations

#: A line that a documentation comment could attach to. Doxygen binds a block
#: to the declaration immediately after it, so a block followed by anything
#: else documents nothing and is left alone.
def _is_declaration(line: str) -> bool:
	stripped = line.strip()
	if not stripped:
		return False
	if stripped.startswith(("/*", "*", "*/", "//")):
		return False
	# `#define` included on purpose: a size constant or a dirty bit is
	# something a caller looks up, and Doxygen documents macros.
	if stripped.startswith("#") and not stripped.startswith("#define"):
		return False
	return True


def _opens_block(line: str) -> bool:
	stripped = line.lstrip()
	return stripped.startswith("/*") and not stripped.startswith(("/**", "/*!"))


def _closes_block(line: str) -> bool:
	return line.rstrip().endswith("*/")


def _block_at(lines: list[str], start: int) -> int | None:
	"""Index of the line closing the comment block opening at `start`."""
	if not lines[start].lstrip().startswith("/*"):
		return None
	end = start
	while end < len(lines) and not _closes_block(lines[end]):
		end += 1
	return end if end < len(lines) else None


def _body(lines: list[str], start: int, end: int) -> list[str]:
	"""A block's text, without its opener or its closer."""
	out = []
	for index in range(start, end + 1):
		text = lines[index]
		at   = text.find("/*")
		if index == start and at != -1:
			text = text[:at] + text[at + 2:].lstrip("*")
		if index == end:
			text = text.rstrip()
			if text.endswith("*/"):
				text = text[:-2].rstrip()
		text = text.strip()
		if text.startswith("*"):
			text = text[1:].lstrip()
		out.append(text)

	while out and not out[-1]:
		out.pop()
	return out


def _documents_an_absence(body: list[str]) -> bool:
	"""Whether a block explains something that is deliberately not there.

	"No `cells_at`: ...", "No minimum: ...". Every one of these is a reason for
	an accessor or a bound that does not exist, so binding it to whatever
	declaration follows would show the reason against a different symbol. A
	comment nobody extracts beats one extracted onto the wrong thing.
	"""
	return bool(body) and body[0].lstrip().startswith("No ")


def extractable(lines: list[str], indent: str = "") -> list[str]:
	"""Promote `/*` to `/**` for every block that documents a declaration.

	A block followed by a blank line, another comment, or the end of the file
	is left as it is -- and that is not an oversight. Several of them document
	an accessor that is *absent*: "No `cells_at`: the element type is not a
	struct this build can frame." Promoting one would bind it to whatever
	declaration came next, which is a different function, and a tool would
	then show a reason against something it is not the reason for. A comment
	nobody extracts beats one extracted onto the wrong symbol.
	"""
	out   = list(lines)
	index = 0

	while index < len(out):
		if not _opens_block(out[index]):
			index += 1
			continue

		# Indentation rather than brace depth, so a comment inside a function
		# body is left alone: it explains a statement, and the statement after
		# it is not a declaration a tool would document. Brace counting was
		# tried and does not survive `extern "C" {` inside an `#ifdef`, whose
		# opener and closer sit in different conditional arms.
		#
		# This generator controls its own formatting, so the level is exact:
		# file scope in C, one tab for a class member in C++.
		if out[index][:len(out[index]) - len(out[index].lstrip())] != indent:
			index += 1
			continue

		end = index
		while end < len(out) and not _closes_block(out[end]):
			end += 1
		if end >= len(out):
			break

		follows = out[end + 1] if end + 1 < len(out) else ""
		if not _is_declaration(follows):
			index = end + 1
			continue

		# Absorb a block sitting directly above with only a blank line
		# between: the capability vector is emitted separately from the prose
		# that follows it, and a tool taking only the block nearest the
		# declaration would show the reasons and drop the offsets. Two blocks,
		# one declaration, one thing to say about it.
		if _documents_an_absence(_body(out, index, end)):
			index = end + 1
			continue

		blocks = [(index, end)]
		while True:
			first = blocks[0][0]
			if first < 2 or out[first - 1].strip() or not _closes_block(out[first - 2]):
				break
			above = first - 2
			while above > 0 and not out[above].lstrip().startswith("/*"):
				above -= 1
			if not out[above].lstrip().startswith("/*"):
				break
			# Stop rather than absorb: a note about something absent sitting
			# above an accessor is about a different symbol, and merging it in
			# would attach that reason to this one. Absorbing blindly meant one
			# such note above a field suppressed the whole merge instead.
			if _documents_an_absence(_body(out, above, first - 2)):
				break
			blocks.insert(0, (above, first - 2))

		indent = out[index][:out[index].index("/*")]
		body: list[str] = []
		for start, stop in blocks:
			if body:
				body.append("")
			body.extend(_body(out, start, stop))

		block = [indent + "/** " + body[0]] if body else [indent + "/**"]
		block += [(indent + " * " + line).rstrip() for line in body[1:]]
		block.append(indent + " */")

		out[blocks[0][0]:end + 1] = block
		index = blocks[0][0] + len(block)

	return out
