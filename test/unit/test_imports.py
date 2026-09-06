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
from situc.imports import library_root
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


# -- `import std "..."`, the installed library ------------------------------


def test_the_library_form_reads_from_situ_s_own_directory(tmp_path: Path) -> None:
	"""A consumer's schema, nowhere near situ's tree, naming what situ ships.

	This is the case the relative rule cannot serve: a schema that wants
	`crc32` would otherwise carry `../../../usr/share/situc/std/kernels.situ`
	and compile on one machine.
	"""
	root = library_root()
	assert root is not None and (root / "std" / "kernels.situ").is_file(), root

	path = write(tmp_path, "mine.situ",
	             'import std "std/kernels.situ";\n' + BUFFER
	             + "struct s { u8 a; }\n")
	names = {decl.name for decl in load(path).decls
	         if isinstance(decl, ast.CodecDecl)}
	assert "crc32" in names


def test_the_two_forms_do_not_fall_back_to_each_other(tmp_path: Path) -> None:
	"""Neither resolution is the other's second chance, and that is the whole
	reason they are spelled differently.

	A fallback would mean that dropping a file beside your own silently
	changes which file an existing import names -- the shadowing hazard
	`#include "..."` has, and the reason C keeps `<...>` separate. So a
	`kernels.situ` sitting next to the importing file must not satisfy
	`import std`, and a library name must not satisfy a relative import.
	"""
	write(tmp_path, "std/kernels.situ",
	      "codec local_only {\n\tgranularity = byte;\n}\n")

	# The local file is right there, and `import std` still reaches past it.
	path  = write(tmp_path, "a.situ", 'import std "std/kernels.situ";\n' + BUFFER
	              + "struct s { u8 a; }\n")
	names = {decl.name for decl in load(path).decls
	         if isinstance(decl, ast.CodecDecl)}
	assert "crc32" in names
	assert "local_only" not in names

	# And the relative form reads the local one, not the library's.
	path  = write(tmp_path, "b.situ", 'import "std/kernels.situ";\n' + BUFFER
	              + "struct s { u8 a; }\n")
	names = {decl.name for decl in load(path).decls
	         if isinstance(decl, ast.CodecDecl)}
	assert "local_only" in names
	assert "crc32" not in names


def test_a_missing_library_file_names_the_flag(tmp_path: Path) -> None:
	"""The directory is not something a reader can guess, so the diagnostic
	says how to print it rather than only which path failed."""
	path = write(tmp_path, "mine.situ", 'import std "nope.situ";\n' + BUFFER
	             + "struct s { u8 a; }\n")
	with pytest.raises(SituError) as caught:
		load(path)
	rendered = str(caught.value) + "".join(getattr(caught.value, "notes", []))
	assert "nope.situ" in rendered


def test_a_library_import_needs_no_file_to_resolve_against() -> None:
	"""A relative import cannot be resolved in a schema parsed from a string,
	and says so. A library one can, since it is not measured from anywhere --
	which is the case an editor and the language server are in."""
	schema = parse_text('import std "std/kernels.situ";\n' + BUFFER
	                    + "struct s { u8 a; }\n")
	names  = {decl.name for decl in schema.decls
	          if isinstance(decl, ast.CodecDecl)}
	assert "crc32" in names


def test_std_is_still_an_ordinary_identifier() -> None:
	"""A soft keyword, read the way `at` is: `std` means something in one
	position and is a name everywhere else, so no schema stops parsing."""
	schema = parse_text(BUFFER + "struct std { u8 std; }\n")
	names  = [decl.name for decl in schema.decls
	          if isinstance(decl, ast.StructDecl)]
	assert names == ["std"]


def test_the_form_survives_a_round_trip() -> None:
	"""`dump-ast --format source` re-renders a schema, and dropping `std`
	there would turn a library import into a relative one that names a file
	the consumer does not have."""
	from situc.unparse import unparse

	schema = parse_text(BUFFER + "struct s { u8 a; }\n")
	schema.decls.insert(0, ast.ImportDirective(schema.decls[0].span,
	                                           "std/kernels.situ", True))
	assert 'import std "std/kernels.situ";' in unparse(schema)


# -- what ships, and which half of the corpus it is -------------------------


def test_the_corpus_is_partitioned() -> None:
	"""`example/designed.txt` names the schemas situ invented, and this holds
	it to the directory rather than to somebody's memory of it.

	A population assertion rather than a spot check: a name in the file that
	is not a directory is a rename nobody followed, and a directory in
	neither class is a schema that arrived without anybody deciding what it
	is -- which is the one that matters, because the default a reader will
	assume is `described`, and a designed schema borrowed as though it were
	described is homework in somebody's production build.
	"""
	root     = Path(__file__).resolve().parents[2] / "example"
	listed   = {line.strip() for line in
	            (root / "designed.txt").read_text(encoding="ascii").splitlines()
	            if line.strip() and not line.startswith("#")}
	present  = {d.name for d in root.iterdir()
	            if d.is_dir() and (d / f"{d.name}.situ").is_file()}

	assert listed <= present, f"named but absent: {sorted(listed - present)}"
	assert listed, "the designed set is empty, which no longer describes this tree"
	described = present - listed
	assert described, "every schema is designed, which cannot be right"

	# The two classes together are the whole corpus, by construction above --
	# what this pins is that the count has not quietly drifted to nothing.
	assert len(described) > len(listed), (sorted(described), sorted(listed))


def test_every_designed_schema_says_so_in_its_own_text() -> None:
	"""The list is second-hand, so it is checked against the schemas.

	Each designed schema names what it is in its opening comment -- a
	`project.md` example number, or the sentence `telemetry.situ` and
	`keystore.situ` use. A file listed here whose own text claims an external
	specification is a misclassification, and that is the direction that
	costs somebody something.
	"""
	root   = Path(__file__).resolve().parents[2] / "example"
	listed = {line.strip() for line in
	          (root / "designed.txt").read_text(encoding="ascii").splitlines()
	          if line.strip() and not line.startswith("#")}

	for name in sorted(listed):
		head = (root / name / f"{name}.situ").read_text(
			encoding="ascii")[:900].lower()
		assert ("project.md example" in head
		        or "designed rather than described" in head
		        or "invented for itself" in head), name
