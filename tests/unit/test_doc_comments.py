"""The C and C++ comments are where a documentation tool will look for them.

Rust and Python land in their languages' own systems for free -- `///` is
rustdoc, a docstring is a docstring. C and C++ wrote plain `/* */`, which
Doxygen does not extract at all, so a run over a generated header produced an
entry per function with nothing against it while the reasons sat directly
above in the file.

There is no Doxygen in this build environment, so nothing here proves a tool
extracts them. What it proves is the structure such a tool needs, which is the
same bargain `gen-dissector` makes with Lua (26.14): the emitted artifact
cannot be executed here, so the tests hold the claims that would make it
correct if it were.

The claim worth holding is the last one. A block promoted onto the wrong
declaration is worse than one nobody extracts, because a reader is then shown
a reason against a symbol it is not the reason for.
"""

from __future__ import annotations

import re

import pytest

from situc.codegen.c import generate as generate_c
from situc.codegen.cpp import generate as generate_cpp
from situc.codegen.doc import extractable
from situc.layout import solve
from situc.parser import parse_text
from situc.resolve import resolve

PREAMBLE = "target buffer;\nendian big;\nbit_order msb_first;\n"

SCHEMA = ("struct s { u8 version; u16 length; decimal u16 code[3];"
	" u8 rest[length]; }")


def headers(body: str = SCHEMA) -> dict[str, str]:
	schema   = parse_text(PREAMBLE + body)
	resolved = resolve(schema, solve(schema))
	return {
		"c":   generate_c(schema, resolved, "unit").header,
		"cpp": generate_cpp(schema, resolved, "unit").header,
	}


# -- what a tool needs ------------------------------------------------------


@pytest.mark.parametrize("backend", ["c", "cpp"])
def test_documentation_blocks_open_with_a_doxygen_marker(backend: str) -> None:
	"""Plain `/*` is not extracted by anything. This is the one character."""
	assert "/**" in headers()[backend]


@pytest.mark.parametrize("backend", ["c", "cpp"])
def test_every_promoted_block_is_followed_by_a_declaration(backend: str) -> None:
	"""Doxygen binds a block to the declaration after it. A promoted block
	followed by a blank line documents whatever comes next, which is not what
	it is about."""
	lines   = headers()[backend].splitlines()
	orphans = []

	for index, line in enumerate(lines):
		if not line.lstrip().startswith("/**"):
			continue
		end = index
		while end < len(lines) and not lines[end].rstrip().endswith("*/"):
			end += 1
		follows = lines[end + 1] if end + 1 < len(lines) else ""
		if not follows.strip() or follows.lstrip().startswith(("/*", "*", "//")):
			orphans.append(line.strip())

	assert not orphans, (
		f"{backend}: promoted blocks with nothing to attach to:\n  "
		+ "\n  ".join(orphans))


@pytest.mark.parametrize("backend", ["c", "cpp"])
def test_a_reason_for_an_absent_accessor_stays_plain(backend: str) -> None:
	""""No `x_at`: ..." explains something that is not there. Bound to the
	next declaration it would show that reason against a different symbol."""
	header = headers("struct v { u16 n; u8 body[n]; }"
	                 "struct s { u16 n; indexed(offset_type = u16, count = n)"
	                 " { v items[]; } }")[backend]

	assert "No " in header			# the note is emitted at all
	assert "/** No " not in header


@pytest.mark.parametrize("backend", ["c", "cpp"])
def test_nothing_inside_a_function_body_is_promoted(backend: str) -> None:
	"""A comment there explains a statement, and the statement after it is not
	a declaration. Brace counting was tried for this and does not survive
	`extern "C" {` inside an `#ifdef`."""
	inner = ("\t\t" if backend == "cpp" else "\t")
	for line in headers()[backend].splitlines():
		assert not line.startswith(inner + "/**"), line


def test_the_capability_vector_travels_with_the_reasons() -> None:
	"""They are emitted as two blocks. A tool taking only the one nearest the
	declaration would show why a bound is what it is and drop the offset it
	applies at."""
	header = headers()["c"]
	block  = header[header.index("/** s.code"):]
	block  = block[:block.index("*/")]

	assert "offset=" in block or "at Absolute" in block
	assert "digits" in block			# the prose, in the same block


# -- the pass itself --------------------------------------------------------


def test_a_block_with_no_declaration_after_it_is_left_alone() -> None:
	assert extractable(["/* a note */", "", "int x;"]) == \
		["/* a note */", "", "int x;"]


def test_a_block_against_a_declaration_is_promoted() -> None:
	assert extractable(["/* a note */", "int x;"]) == \
		["/** a note", " */", "int x;"]


def test_two_blocks_against_one_declaration_are_merged() -> None:
	merged = extractable(["/* first */", "", "/* second */", "int x;"])

	assert merged == ["/** first", " *", " * second", " */", "int x;"]


def test_a_macro_counts_as_a_declaration() -> None:
	"""A size constant or a dirty bit is something a caller looks up, and
	Doxygen documents macros."""
	assert extractable(["/* a note */", "#define X 1u"])[0] == "/** a note"


def test_an_include_does_not() -> None:
	assert extractable(["/* a note */", "#include <x.h>"])[0] == "/* a note */"


def test_an_already_marked_block_is_left_alone() -> None:
	"""Idempotent: running it twice must not turn `/**` into `/***`."""
	once  = extractable(["/* a note */", "int x;"])
	twice = extractable(once)

	assert once == twice
