"""The C and C++ comments are where a documentation tool will look for them.

Rust and Python land in their languages' own systems for free -- `///` is
rustdoc, a docstring is a docstring. C and C++ wrote plain `/* */`, which
Doxygen does not extract at all, so a run over a generated header produced an
entry per function with nothing against it while the reasons sat directly
above in the file.

Most of this file holds the *structure* such a tool needs, which is cheap and
runs anywhere. The last tests run Doxygen itself over generated headers and
read its XML, which is the only way to know the structure was the right guess:
running it the first time found two things the structure tests could not see --
every C++ class undocumented, because the promotion pass was told about members
and not about classes, and `<reserved0>` read as an HTML tag, once per reserved
field in the tree.

The claim worth holding either way is the last one. A block promoted onto the
wrong declaration is worse than one nobody extracts, because a reader is then
shown a reason against a symbol it is not the reason for.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from every_schema import ROOT, SCHEMAS, ids
from situc.cli import analyse
from situc.parser import parse
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


# -- Doxygen itself (26.31) -------------------------------------------------

DOXYGEN = shutil.which("doxygen")

#: The one class of complaint this suite tolerates. A field's block documents
#: the field, and its setter rides along under the same block rather than
#: repeating the capability vector -- so `set_x`, the constructor and the size
#: constant come out undocumented, in C++ as in C. That is a choice about what
#: to write, not a failure to extract what was written; anything else Doxygen
#: says is this generator emitting markup it did not mean to.
TOLERATED = "is not documented"


def doxygen(tmp_path: Path, schema: Path) -> tuple[str, str]:
	"""Run Doxygen over one schema's C and C++ headers, and hand back its
	warnings and the XML it produced."""
	source, resolved, _ = analyse(schema)
	parsed = parse(source)

	inputs = tmp_path / "in"
	inputs.mkdir()
	(inputs / "unit.h").write_text(
		generate_c(parsed, resolved, "unit").header, encoding="ascii")
	(inputs / "unit.hpp").write_text(
		generate_cpp(parsed, resolved, "unit").header, encoding="ascii")

	# `EXTRACT_ALL = NO` on purpose: with it on, Doxygen lists every symbol
	# whether or not anything documented it, and the check below would pass
	# over a header with no comments in it at all.
	(tmp_path / "Doxyfile").write_text(f"""\
INPUT            = {inputs}
FILE_PATTERNS    = *.h *.hpp
OUTPUT_DIRECTORY = {tmp_path}
GENERATE_HTML    = NO
GENERATE_LATEX   = NO
GENERATE_XML     = YES
QUIET            = YES
EXTRACT_ALL      = NO
EXTRACT_STATIC   = YES
""", encoding="ascii")

	assert DOXYGEN is not None
	result = subprocess.run([DOXYGEN, str(tmp_path / "Doxyfile")],
	                        capture_output=True, text=True)
	assert result.returncode == 0, result.stderr

	xml = "\n".join(path.read_text(encoding="utf-8")
	                for path in sorted((tmp_path / "xml").glob("*.xml")))
	return result.stderr, xml


@pytest.mark.skipif(DOXYGEN is None, reason="no doxygen")
@pytest.mark.parametrize("schema", SCHEMAS, ids=ids(SCHEMAS))
def test_doxygen_reads_the_generated_headers_without_complaint(
		schema: Path, tmp_path: Path) -> None:
	"""Every schema in the repository, in both languages Doxygen reads.

	This is the check 26.31 said was missing, and it earned its keep on the
	first run: `<reserved0>` -- the compiler's own label for a field the schema
	did not name -- is markup to a documentation tool, and Doxygen said so once
	per reserved field in the tree.
	"""
	warnings, _ = doxygen(tmp_path, schema)
	unexpected = [line for line in warnings.splitlines()
	              if line.strip() and TOLERATED not in line]

	assert not unexpected, "\n".join(unexpected)


@pytest.mark.skipif(DOXYGEN is None, reason="no doxygen")
def test_doxygen_extracts_the_reasons_and_not_the_absences(
		tmp_path: Path) -> None:
	"""The two halves of the bargain, in the tool's own output.

	What must arrive: the capability vector, against the accessor it describes.
	What must not: a "No `x`: ..." block, which explains an accessor that is
	*not there* and would otherwise be shown against whatever declaration came
	next (`codegen/doc.py`).
	"""
	_, xml = doxygen(tmp_path, ROOT / "tests" / "schemas" / "edges.situ")

	assert "repr=ValueConverted" in xml
	assert "AbsoluteStatic" in xml
	assert "reserved, no accessor" in xml	# against the reserved field itself

	# The absences, which the generator writes and the tool must not attach.
	assert "No setter" not in xml
	assert "No `" not in xml
