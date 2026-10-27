"""Every spelling of a `parameter`'s argument carries one prefix (26.706).

`traverse.argument_name` prefixed C's argument and said the other three
backends did not need it, "because they put the value on the view object,
where a schema's name cannot collide with anything". The view object is
exactly where it collides. Measured over seven names declared as a
`parameter`, of 28 backend-and-name pairs seven did not build:

	bytes   rust  error[E0415]  identifier bound more than once
	data    rust  error[E0415]  -- and py SyntaxError
	have    rust  error[E0308]  mismatched types, over a local
	cls     py    SyntaxError   duplicate argument in function definition
	msg     py    SyntaxError
	offset  py    SyntaxError

`parameter u8 bytes` is the sharpest of them: Rust's view holds the message
in a field called `bytes`, so the struct declared `bytes` twice and every
`self.bytes` in the module read the number rather than the buffer. Python's
four are `at`'s own parameter list -- `cls`, `msg`, `offset`, `length` --
and a duplicate there is a `SyntaxError`, so the module does not import,
which is the one failure Python cannot defer to a call.

C took all seven, being the backend that prefixed.

**The sweep is the test, because the sites are not where anybody looks.**
Prefixing the four obvious helpers left 205 bare spellings across the four
backends and nine modes: an owned copy's field in all four, the frame
reader's field and its stored argument, and in Rust and Python a second
count-read path whose own comment called itself "the second of the two
spellings, which is where one of them gets missed". A test that generated
one backend's view would have passed with every one of them in place.
"""

from __future__ import annotations

import ast
import importlib
import re
from pathlib import Path
from typing import cast

import pytest
from every_schema import ROOT					# noqa: F401
from situc import traverse
from situc.layout import solve
from situc.parser import parse_text
from situc.resolve import resolve

#: A name no schema and no runtime would choose, so that every occurrence in
#: generated text is this parameter and the check needs no exception list.
NAME = "zzqq"

SCHEMA = ("endian little;\n"
          "\n"
          "struct probe {\n"
          f"\tparameter u8 {NAME} [stream];\n"
          f"\tu8 payload[{NAME}];\n"
          "\tu16 checksum;\n"
          "}\n")

BACKENDS = ("c", "cpp", "rust", "python")

#: The view and the two layers that carry an argument of their own. `relate`,
#: `converse` and `drive` emit the frame layer beside their own files, so the
#: three are reached through `frame` rather than listed again.
LAYERS = ("view", "edit", "frame")


def _generated(backend: str, layer: str) -> dict[str, str]:
	schema   = parse_text(SCHEMA, path="probe.situ")
	resolved = resolve(schema, solve(schema))
	if layer == "view":
		return cast("dict[str, str]", importlib.import_module(
			f"situc.codegen.{backend}").generate(
				schema, resolved, "probe").files())
	return dict(importlib.import_module(
		f"situc.codegen.{backend}.{layer}").generate(
			schema, resolved, "probe"))


def _uncommented(text: str) -> list[tuple[int, str]]:
	"""The lines of a braced language with `//` and `/* */` blanked out.

	Comments and doc text name the schema's own member and are right to --
	`s.zzqq` is what the schema calls it -- so a text search finds those and
	says nothing. What is being checked is the identifiers.
	"""
	out, inside = [], False
	for number, line in enumerate(text.splitlines(), 1):
		kept = []
		at = 0
		while at < len(line):
			if inside:
				end = line.find("*/", at)
				if end < 0:
					at = len(line)
				else:
					at, inside = end + 2, False
				continue
			if line.startswith("//", at):
				break
			if line.startswith("/*", at):
				inside = True
				at += 2
				continue
			kept.append(line[at])
			at += 1
		out.append((number, "".join(kept)))
	return out


def _bare_braced(text: str) -> list[str]:
	found = []
	for number, line in _uncommented(text):
		for hit in re.finditer(rf"(?<![\w]){NAME}\b", line):
			if line[max(0, hit.start() - 4):hit.start()] != "arg_":
				found.append(f"line {number}: {line.strip()[:70]}")
	return found


def _bare_python(text: str) -> list[str]:
	"""Through `ast`, which sees identifiers and not docstrings."""
	found = []
	for node in ast.walk(ast.parse(text)):
		kind = None
		if isinstance(node, ast.Name) and node.id == NAME:
			kind = "name"
		elif isinstance(node, ast.Attribute) and node.attr == NAME:
			kind = "attribute"
		elif isinstance(node, ast.arg) and node.arg == NAME:
			kind = "argument"
		elif isinstance(node, ast.keyword) and node.arg == NAME:
			kind = "keyword"
		if kind is not None:
			found.append(f"line {getattr(node, 'lineno', 0)}: {kind}")
	return found


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("layer", LAYERS)
def test_no_spelling_of_the_argument_is_bare(backend: str, layer: str) -> None:
	"""One prefix, everywhere the argument is named.

	Not a claim about collisions -- that is the next two tests -- but about
	agreement: a field in one place and an argument in another, spelled two
	ways, is how `self.bytes` came to mean the buffer in one line and the
	number in the next.
	"""
	files = _generated(backend, layer)
	assert files, f"{backend} emits nothing for the {layer} layer"

	seen = 0
	for name, text in sorted(files.items()):
		if NAME not in text:
			continue
		seen += 1
		bare = (_bare_python(text) if name.endswith(".py")
		        else _bare_braced(text))
		assert not bare, (
			f"{name} spells the argument without the prefix:\n  "
			+ "\n  ".join(bare[:8]))
	assert seen, (
		f"{backend}'s {layer} layer never names the argument, so this "
		"asserted nothing -- the schema or the layer has moved")


@pytest.mark.parametrize("backend", BACKENDS)
def test_every_backend_spells_it_the_same_way(backend: str) -> None:
	"""The point of the prefix living in `traverse` rather than four times.

	A caller holding one block size for four generated modules learns one
	name for it. That was already `argument_name`'s stated reason and it was
	true of C alone.
	"""
	text = "".join(_generated(backend, "view").values())
	assert f"arg_{NAME}" in text, f"{backend} does not carry the prefix"


def test_the_prefix_is_the_word_c_has_always_used() -> None:
	"""Pinned because C's generated API already carries it.

	The other three moved to C's spelling rather than all four moving to a
	new one, so this word is not free to change: it is in every published C
	header that has a `parameter` in it.
	"""
	assert traverse.ARGUMENT_PREFIX == "arg_"
	assert traverse.argument_spelling("n", lambda name: name) == "arg_n"


#: Keywords in at least one of the four target languages, so that each
#: backend's sanitiser escapes the bare name: Rust writes `r#fn`, C++ and
#: Python add a trailing underscore.
KEYWORD_NAMES = ("class", "fn", "match", "type", "new", "int")


@pytest.mark.parametrize("name", KEYWORD_NAMES)
@pytest.mark.parametrize("backend", BACKENDS)
def test_the_prefix_retires_the_keyword_escape(
		backend: str, name: str) -> None:
	"""The prefix goes on BEFORE the sanitiser, and the order is the test.

	`arg_` plus a name is a keyword in none of the four languages, so no
	escape is called for -- but a sanitiser run first escapes anyway and
	the prefix lands in front of the escape. That gave Rust `arg_r#fn`,
	which is not an identifier at all, and it retires a C++ data member
	spelled `arg_class__` that predates the prefix: `bare_name` suffixed
	the keyword and `_argument_local` added the underscore every member of
	a frame reader carries, and two underscores anywhere in an identifier
	are reserved to the implementation.
	"""
	schema = SCHEMA.replace(NAME, name)
	parsed = parse_text(schema, path="probe.situ")
	held   = resolve(parsed, solve(parsed))
	text   = "".join(importlib.import_module(
		f"situc.codegen.{backend}").generate(parsed, held, "probe")
		.files().values())

	assert f"arg_{name}" in text, "the argument is not prefixed"
	assert "arg_r#" not in text, "the prefix landed in front of an escape"
	assert f"arg_{name}__" not in text, (
		"two underscores in an identifier are reserved to the "
		"implementation")
