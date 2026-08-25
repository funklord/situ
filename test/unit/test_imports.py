"""`import "other.situ";` -- splicing another file's declarations in (17.0a).

The directive parsed and resolved nothing for a long time, and the honest
half of that was a note saying so. These tests hold the shape it resolves in:
relative to the importing file, flat, transitive, once per compilation, and
refusing a cycle.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from situc import ast
from situc.diagnostics import SituError
from situc.parser import parse, parse_text
from situc.diagnostics import Source

BUFFER = "target buffer;\nendian big;\nbit_order msb_first;\n\n"

CODEC = """codec aead {
	granularity = byte;
	length_preserving;
	seekable;
	authenticated;
	invertible;
	deterministic;
}
"""


def write(root: Path, name: str, text: str) -> Path:
	path = root / name
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(text, encoding="ascii")
	return path


def load(path: Path) -> ast.Schema:
	return parse(Source(str(path), path.read_text(encoding="ascii")))


def test_an_imported_codec_can_be_used(tmp_path: Path) -> None:
	"""The whole point, and what the old diagnostic apologised for."""
	write(tmp_path, "lib.situ", CODEC)
	root = write(tmp_path, "use.situ",
	             'import "lib.situ";\n' + BUFFER
	             + 'impl aead extern "x";\n'
	             + "struct s {\n\tsealed b(aead) { u16 v; }\n\ttag u8[16];\n}\n")
	schema = load(root)
	assert any(decl.name == "aead" for decl in schema.codecs())


def test_a_path_is_relative_to_the_importing_file(tmp_path: Path) -> None:
	"""Not to the working directory, and not to a search path: which file an
	import names must not depend on how situc was invoked."""
	write(tmp_path, "sub/lib.situ", "struct base { u8 b; }\n")
	root = write(tmp_path, "sub/use.situ",
	             'import "lib.situ";\n' + BUFFER + "struct s { base b; }\n")
	assert any(decl.name == "base" for decl in load(root).structs())


def test_imports_are_transitive(tmp_path: Path) -> None:
	"""A consumer cannot be asked to know what its dependency needs, which is
	what makes a library of contracts usable at all."""
	write(tmp_path, "base.situ", "struct base { u8 b; }\n")
	write(tmp_path, "mid.situ", 'import "base.situ";\nstruct mid { u8 m; }\n')
	root = write(tmp_path, "top.situ",
	             'import "mid.situ";\n' + BUFFER
	             + "struct top { base b; mid m; }\n")
	names = {decl.name for decl in load(root).structs()}
	assert {"base", "mid", "top"} <= names


def test_a_diamond_is_not_a_redefinition(tmp_path: Path) -> None:
	"""Two imports that both import a third. Contributing the file twice
	would make every diamond a duplicate declaration, which is a rule nobody
	could work with."""
	write(tmp_path, "base.situ", "struct base { u8 b; }\n")
	write(tmp_path, "left.situ", 'import "base.situ";\nstruct left { u8 l; }\n')
	write(tmp_path, "right.situ", 'import "base.situ";\nstruct right { u8 r; }\n')
	root = write(tmp_path, "top.situ",
	             'import "left.situ";\nimport "right.situ";\n' + BUFFER
	             + "struct top { base b; left l; right r; }\n")
	names = [decl.name for decl in load(root).structs()]
	assert names.count("base") == 1


def test_a_cycle_terminates(tmp_path: Path) -> None:
	"""A imports B imports A. There is no fixed point a flat merge reaches,
	and the alternative to stopping is a compiler that recurses forever."""
	write(tmp_path, "b.situ", 'import "a.situ";\nstruct b { u8 y; }\n')
	root = write(tmp_path, "a.situ",
	             'import "b.situ";\n' + BUFFER + "struct a { u8 x; }\n")
	names = {decl.name for decl in load(root).structs()}
	assert {"a", "b"} <= names


def test_a_missing_file_says_where_it_looked(tmp_path: Path) -> None:
	root = write(tmp_path, "use.situ",
	             'import "nope.situ";\n' + BUFFER + "struct s { u8 x; }\n")
	with pytest.raises(SituError) as caught:
		load(root)
	text = caught.value.diagnostic.render()
	assert "cannot read `nope.situ`" in text
	assert "looked for" in text


def test_a_duplicate_across_files_is_refused(tmp_path: Path) -> None:
	"""Nothing is renamed and nothing is qualified, so two declarations
	reaching one name collide -- which the duplicate gate has caught since it
	was written and needs nothing new to catch across files."""
	write(tmp_path, "lib.situ", "struct s { u8 a; }\n")
	root = write(tmp_path, "use.situ",
	             'import "lib.situ";\n' + BUFFER + "struct s { u8 b; }\n")
	with pytest.raises(SituError):
		load(root)


def test_an_import_needs_a_file_to_resolve_against() -> None:
	"""A schema parsed from a string has no directory, and resolving against
	whatever directory the process happens to be in is the ambiguity this
	resolves relative-to-the-file to avoid."""
	with pytest.raises(SituError) as caught:
		parse_text('import "lib.situ";\n' + BUFFER + "struct s { u8 x; }\n")
	assert "needs a schema that came from a file" in caught.value.diagnostic.render()
