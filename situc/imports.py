"""`import "other.situ";` -- splicing another file's declarations in.

WHAT IS RESOLVED, AND WHERE. A path is read relative to the *importing file's
directory*, the way `#include "..."` is and for the same reason: there is no
search path, so which file an import names does not depend on how the compiler
was invoked. A search path would make that ambiguous, and 17.0 refuses an
ambiguity rather than picking a default nobody can see.

AND `import std "..."` READS FROM WHERE SITU INSTALLED ITS OWN SCHEMAS, which
is the other half of that rule rather than an exception to it. 17.0's
objection is to a search *path* -- a list, where "which of these won?" has no
visible answer. This is one directory, which situc finds the way `bin/situc`
finds its own package, so an import still names exactly one file and still
does not depend on the invocation. What it does depend on is which situ is
installed, and that is the point: a consumer describing a JSON-based format
should not have to write `../../../usr/share/situc/std` into a schema that
then only compiles on one machine.

The two are spelled differently on purpose and neither falls back to the
other. A fallback would mean that adding a file beside your own silently
changes which file an existing import names, which is the shadowing hazard
`#include "..."` has and the reason C keeps `<...>` separate.

WHAT ARRIVES. Every top-level declaration of the imported file, merged into
one flat namespace. Nothing is renamed and nothing is qualified: an imported
`codec aes_gcm_128` is `aes_gcm_128` here. Two declarations reaching one name
are refused by `check_unique_declarations`, which has caught that since it was
written and needs nothing new to catch it across files.

TRANSITIVELY, because a file's imports are expanded before its declarations
are handed back. `std/kernels.situ` importing something would carry it to
whoever imports `std/kernels.situ`, which is what makes a library of contracts
usable at all -- a consumer cannot be asked to know what its dependency needs.

ONCE PER COMPILATION. A file imported twice contributes its declarations once,
so the diamond -- two imports that both import a third -- is not a redefinition.
Without that the flat namespace would make every diamond an error, which is a
rule nobody could work with.

AND CYCLES ARE REFUSED. A imports B imports A has no fixed point that a flat
merge can reach, and the alternative to refusing is a compiler that either
recurses forever or silently drops one edge.
"""

from __future__ import annotations

from pathlib import Path

from situc import ast
from situc.diagnostics import Source, error

__all__ = ["expand"]


def expand(schema: ast.Schema, source: Source,
		seen: set[str] | None = None) -> None:
	"""Replace every `import` in `schema` with the named file's declarations.

	`seen` carries the files already spliced into this compilation, so a
	diamond costs nothing and a cycle is caught. It holds resolved absolute
	paths, because two spellings of one file are one file.
	"""
	directives = [decl for decl in schema.decls
	              if isinstance(decl, ast.ImportDirective)]
	if not directives:
		return

	# The importing file's directory is needed only by a relative import, and
	# asking for it unconditionally would refuse a schema parsed from a string
	# that imports nothing but `std` -- which is the case an editor and the
	# language server are in.
	here = (_directory(source, next(d for d in directives if not d.library))
	        if any(not d.library for d in directives) else None)
	if seen is None:
		seen = {_key(Path(source.path))}

	arrived: list[ast.Decl] = []
	for directive in directives:
		if directive.library:
			target = _library_target(directive)
		else:
			assert here is not None
			target = (here / directive.path).resolve()
		if _key(target) in seen:
			# Already here, whether from a diamond or a cycle. Both are
			# answered by contributing nothing a second time.
			continue

		text = _read(target, directive)
		seen.add(_key(target))

		from situc.parser import parse_decls

		inner_source = Source(str(target), text)
		inner = parse_decls(inner_source)
		expand(inner, inner_source, seen)
		arrived.extend(inner.decls)

	# The directives go, and what they named takes their place -- ahead of
	# this file's own declarations, so a type is declared before the struct
	# that names it in any listing of the merged schema.
	schema.decls[:] = [*arrived,
	                   *(decl for decl in schema.decls
	                     if not isinstance(decl, ast.ImportDirective))]


def library_root() -> Path | None:
	"""The directory situ's own schemas live in, or None.

	Found the way `bin/situc` finds its package and for the same reason:
	situc has to run from a bare interpreter with nothing installed, so it
	locates itself rather than being told where it is. Two shapes, checked in
	order -- the source tree, and an installed prefix where `make install`
	puts the package in `<prefix>/lib/situc`.

	**It is the root that HOLDS the schema directories, not one of them**,
	so that `<prefix>/share/situc` mirrors the source tree entry for entry
	and one path names the same file in both. The alternative -- a root of
	`<tree>/std`, which this returned for an hour -- makes an installed
	layout that categorises schemas impossible to mirror in a source tree
	that does not, and the two then disagree about what
	`import std "protocol/x.situ"` means. Mirroring costs a `std/` in the
	spelling and buys the property that matters: a schema that compiles here
	compiles against an installed situ, unchanged.

	`situc --print-std-dir` prints what this returns, so a build system can
	compute the path without a schema having to contain one.
	"""
	package = Path(__file__).resolve().parent
	for candidate in (package.parent,
	                  package.parent.parent / "share" / "situc"):
		if (candidate / "std").is_dir():
			return candidate
	return None


def _library_target(directive: ast.ImportDirective) -> Path:
	"""Where `import std "x"` reads from, or a diagnostic saying why not."""
	root = library_root()
	if root is None:
		raise error(
			f"cannot find situ's own schemas to read `{directive.path}` from",
			directive.span,
			label = "no installed schema directory",
			notes = ["`import std` reads from the directory `make install` "
			         "puts `std/*.situ` in, and situc could not locate it "
			         "from its own package",
			         "`situc --print-std-dir` prints where it looked",
			         "an import without `std` is read relative to this file "
			         "and needs nothing installed"],
		)
	return (root / directive.path).resolve()


def _key(path: Path) -> str:
	"""One file, one name. `resolve()` collapses `..` and follows links, so
	`std/codecs.situ` and `./std/../std/codecs.situ` are not two imports."""
	return str(path.resolve())


def _directory(source: Source, directive: ast.ImportDirective) -> Path:
	"""Where a relative import is measured from.

	The importing file's own directory, which needs the importing file to
	*have* one. A schema parsed from a string has no location, so an import
	in it names nothing -- and saying so is better than resolving against
	whatever directory the process happens to be in, which is the ambiguity
	this module exists to avoid.
	"""
	path = Path(source.path)
	if not path.name or source.path.startswith("<"):
		raise error(
			"`import` needs a schema that came from a file",
			directive.span,
			label = f"nothing to resolve `{directive.path}` against",
			notes = ["an import is read relative to the importing file's "
			         "directory, and this schema was parsed from text",
			         "compile the file itself, or declare what it needs here"],
		)
	return path.parent


def _read(target: Path, directive: ast.ImportDirective) -> str:
	try:
		return target.read_text(encoding="utf-8")
	except OSError as exc:
		raise error(
			f"cannot read `{directive.path}`",
			directive.span,
			label = exc.strerror or "unreadable",
			notes = [f"looked for {target}",
			         "`import std` reads from situ's own schema directory, "
			         "which `situc --print-std-dir` prints"
			         if directive.library else
			         "an import is read relative to the importing file's "
			         "directory, not from a search path: which file it names "
			         "does not depend on how situc was invoked"],
		) from exc
