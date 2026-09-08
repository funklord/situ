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
from situc.layout import solve
from situc.pack import Program, pack
from situc.parser import parse, parse_text
from situc.resolve import resolve
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


# -- a directive is a claim about the file it is written in ----------------


def test_an_import_does_not_supply_a_byte_order(tmp_path: Path) -> None:
	"""`endian` is mandatory because situ never guesses a byte order where
	the wrong choice is undetectable at run time (17.0). An import used to
	answer for it: a file declaring none inherited the last imported one and
	compiled, so the refusal fired for a lone file and not for one that
	imported anything.

	The whole point of the rule is that nobody chooses a byte order by
	accident, and an import is the most accidental way there is."""
	write(tmp_path, "lib.situ",
	      "target buffer;\nendian little;\nstruct lib { u16 x; }\n")
	app = write(tmp_path, "app.situ",
	            'import "lib.situ";\nstruct app { u16 y; }\n')

	with pytest.raises(SituError) as raised:
		resolve(load(app), solve(load(app)))
	assert "no endianness in scope" in str(raised.value)


def test_each_file_keeps_its_own_byte_order(tmp_path: Path) -> None:
	"""And the other half, which the fix must not break: importing a file
	whose byte order differs from yours is legitimate and useful. Positional
	scoping is per FILE -- one file may still describe layers that disagree,
	which is what `scopes_of` exists for -- and it stops at the file
	boundary."""
	write(tmp_path, "lib.situ",
	      "target buffer;\nendian little;\nstruct lib { u16 x; }\n")
	app = write(tmp_path, "app.situ",
	            "target buffer;\nendian big;\n"
	            'import "lib.situ";\nstruct app { u16 y; }\n')

	resolved = resolve(load(app), solve(load(app)))
	orders = {name: {e.placement.name: e.placement.endian
	                 for e in struct.entries}
	          for name, struct in resolved.structs.items()}

	assert orders["app"]["y"] is ast.Endian.BIG
	assert orders["lib"]["x"] is ast.Endian.LITTLE


def test_an_imported_target_may_not_disagree(tmp_path: Path) -> None:
	"""`target` is the compilation's rather than one struct's, and it was
	taken from the FIRST directive in the merged list -- which `import`
	splices in ahead. A schema declaring `target file` on its own first line
	resolved to `buffer`: the importer's claim discarded outright rather
	than merely overridden.

	Refused rather than resolved either way, because both resolutions drop
	a claim somebody wrote and neither is visible in the output."""
	write(tmp_path, "lib.situ",
	      "target buffer;\nendian big;\nstruct lib { u16 x; }\n")
	app = write(tmp_path, "app.situ",
	            "target file;\nendian big;\n"
	            'import "lib.situ";\nstruct app { u16 y; }\n')

	with pytest.raises(SituError) as raised:
		load(app)
	# The rendered form, because the point of the refusal is that it names
	# BOTH files: neither line is wrong on its own, and a diagnostic
	# pointing at one of them reads as though it were.
	shown = raised.value.diagnostic.render()
	assert "disagrees about `target`" in shown
	assert "target file" in shown and "target buffer" in shown
	assert "lib.situ" in shown and "app.situ" in shown


def test_an_imported_target_that_agrees_is_fine(tmp_path: Path) -> None:
	"""The usual case, and the reason the rule is a comparison rather than a
	ban: every schema in this tree says `target buffer`, so importing one
	says nothing new and is refused by nothing."""
	write(tmp_path, "lib.situ",
	      "target buffer;\nendian big;\nstruct lib { u16 x; }\n")
	app = write(tmp_path, "app.situ",
	            "target buffer;\nendian big;\n"
	            'import "lib.situ";\nstruct app { u16 y; }\n')

	assert {struct.name for struct in load(app).structs()} == {"lib", "app"}


def test_an_imported_strictness_does_not_reach_the_importer(
		tmp_path: Path) -> None:
	"""`strictness` is the same shape as `target` and had the same defect:
	first wins, imports first, so an imported `strictness = lenient` made
	the importer lenient from a line it never wrote. 14.5 is a security
	position, which is what makes inheriting it quietly the wrong way for
	it to travel."""
	write(tmp_path, "lib.situ",
	      "target buffer;\nendian big;\nstrictness = lenient;\n"
	      "struct lib { u16 x; }\n")
	app = write(tmp_path, "app.situ",
	            "target buffer;\nendian big;\n"
	            'import "lib.situ";\nstruct app { u16 y; }\n')

	with pytest.raises(SituError) as raised:
		load(app)
	assert "disagrees about `strictness`" in str(raised.value)


def test_the_signature_publishes_this_file_s_directives(
		tmp_path: Path) -> None:
	"""The wire signature is a committed contract, and it listed every
	directive in the merged list -- so a schema that imported one said it
	was big AND little endian, and both `buffer` and `file`. Two answers to
	one question is no contract at all."""
	from situc import wire

	write(tmp_path, "lib.situ",
	      "target buffer;\nendian little;\nstruct lib { u16 x; }\n")
	app = write(tmp_path, "app.situ",
	            "target buffer;\nendian big;\n"
	            'import "lib.situ";\nstruct app { u16 y; }\n')

	schema   = load(app)
	rendered = wire.render(schema, resolve(schema, solve(schema)), "app.situ")

	assert "endian big" in rendered
	assert "endian little" not in rendered


def test_a_character_means_what_its_own_file_says(tmp_path: Path) -> None:
	"""The packed image and the generated code have to agree about a byte.

	A `CharLiteral` is resolved where it is written, against the encodings
	in scope THERE -- `expr.py` reads `expr.code` and says so. The packer
	re-derived it instead, from the first `encoding` directive in the merged
	list, which after an `import` is not the file the literal was written
	in.

	The section sign separates them: 0xA7 in ISO-8859-1 and 0xFD in
	ISO-8859-5, one byte in both, so nothing refuses and only the number
	differs. Four compiled backends compared against one byte and the walker
	against another -- a disagreement in the input to the tool that exists
	to find disagreements."""
	write(tmp_path, "lib.situ",
	      "target buffer;\nendian big;\nencoding iso8859_5;\n"
	      "struct lib { u8 x; }\n")
	# Written directly rather than through `write`, which is ASCII: the
	# whole point of this schema is a byte that is not.
	app = tmp_path / "app.situ"
	app.write_text("target buffer;\nendian big;\nencoding iso8859_1;\n"
	               'import "lib.situ";\n'
	               "struct item { u8 sep; }\n"
	               "struct app { item entries[] while (sep == '\xa7'); }\n",
	               encoding="latin-1")

	schema = parse(Source(str(app), app.read_text(encoding="latin-1")))
	found  = [decl for decl in _every_node(schema)
	          if isinstance(decl, ast.CharLiteral)]
	assert len(found) == 1

	# What the compiler folds it to, and what the image records: one fact,
	# so one number.
	assert found[0].code == 0xA7
	assert Program().character(found[0]) == 0xA7


def _every_node(node: object):
	"""Every `ast.Node` under this one, so a literal can be found wherever a
	construct happens to keep it."""
	if isinstance(node, ast.Node):
		yield node
	for name in getattr(node, "__dataclass_fields__", ()):
		held = getattr(node, name)
		for one in (held if isinstance(held, (list, tuple)) else [held]):
			if isinstance(one, ast.Node):
				yield from _every_node(one)


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


#: An imported file with a `whitespace` set the root does not share, and two
#: members asking what whitespace means: one with `skip`, one with `[trim]`.
#:
#: The pair is the assertion. Either attribute alone can be read against the
#: wrong file and look right, because every schema in the corpus is one file
#: and the two answers coincide there.
WS_INNER = ("target buffer;\nendian big;\n"
            "whitespace '\\t';\n\n"
            "struct inner {\n"
            "\tu8  word[]  until \",\" [trim];\n"
            "\tu8  led  skip;\n"
            "}\n")

WS_OUTER = ("target buffer;\nendian big;\n"
            "whitespace ' ';\n\n"
            'import "inner.situ";\n\n'
            "struct outer {\n\tu8  c;\n\tinner  held;\n}\n")


def _ws_schema(tmp_path: Path):
	write(tmp_path, "inner.situ", WS_INNER)
	root = write(tmp_path, "outer.situ", WS_OUTER)
	schema = load(root)
	return schema, resolve(schema, solve(schema))


def _member(resolved, struct: str, name: str):
	for entry in resolved.structs[struct].entries:
		if entry.placement.name == name:
			return entry.placement
	raise AssertionError(f"no member {struct}.{name}")


def test_trim_and_skip_agree_about_whitespace_in_an_imported_file(
		tmp_path: Path) -> None:
	"""Two attributes, one question, and they gave two answers.

	`skip` resolves its set in the parser, from the file the member is
	written in, so an imported struct has always used its own file's
	whitespace. `[trim]` read the ROOT's, on the reasoning that a directive
	is a claim about the file it is written in (26.295) -- which is the
	right rule for `target`, `endian` and `strictness`, because those
	configure the compiler, and the wrong one for `whitespace`, which is a
	vocabulary the members of a file are written against, like the encoding
	a character literal resolves in (26.296).

	So `[trim]` in `inner.situ` removed the root's space where `skip` two
	lines below it removed inner's tab. Held as the RELATIONSHIP between the
	two rather than against the literal byte: a schema with different sets
	cannot make this pass for the wrong reason.
	"""
	_, resolved = _ws_schema(tmp_path)
	trimmed = _member(resolved, "inner", "word")
	led     = _member(resolved, "inner", "led")

	assert tuple(led.skip) == tuple(trimmed.trim_set), (
		f"`skip` takes {tuple(led.skip)} and `[trim]` takes "
		f"{tuple(trimmed.trim_set)} in one file")
	# And it is the IMPORTED file's set rather than the root's, which is
	# what makes the agreement above the right agreement rather than both
	# of them reading the root.
	assert tuple(trimmed.trim_set) == (0x09,)
	assert tuple(led.skip) != (0x20,)


def test_the_image_carries_the_trim_set_per_member(tmp_path: Path) -> None:
	"""And so does the walker, which is the sixth reader.

	The four backends emit the set beside each member, so making them
	member-aware is a change they carry on their own. The image had one
	file-level section, so a walker would have gone on trimming the root's
	set -- a disagreement introduced by fixing the backends alone, which is
	worse than the shared mistake it replaced.
	"""
	from walker.image import load as load_image
	from walker.walk import acquire
	from walker import report

	schema, resolved = _ws_schema(tmp_path)
	image = load_image(pack(schema, resolved, metadata=True)[0])

	shape = image.struct_names.index("inner")
	index = image.members(image.structs[shape])[0]

	# A tab is inner's whitespace and a space is not, so the two spellings
	# separate the sets rather than merely exercising one.
	view = acquire(image, b"\thi\t,X", shape)
	assert report._trim_span(view, index, 4) == (1, 2)

	view = acquire(image, b" hi ,X", shape)
	assert report._trim_span(view, index, 4) == (0, 4)
