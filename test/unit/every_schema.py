"""Every schema this repository builds.

Six files wanted this list and four of them had a shorter one: `example/`
only. That is not a detail. `test/schema/edges.situ` exists to carry the
constructs the worked examples happen not to have (26.27), so the file most
likely to break a backend was the file the compile checks skipped -- and it did
break one, for weeks, in a way `-fsyntax-only` would have found the first time
it ran (26.31).

So the question "which schemas does this repository build?" is answered once,
here. A directory that arrives later is added in one place rather than in as
many places as remember to ask.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

#: Where the schemas are, kept apart so that each can be checked to have
#: found something. One glob going quiet is the realistic failure -- a
#: directory renamed, `ROOT` resolving somewhere else after this file moves --
#: and it is quieter than it looks: 23 tests parametrize over the result, and
#: pytest turns an empty parameter set into a skip rather than a failure.
#:
#: Measured by pointing all three somewhere that does not exist: collection
#: falls from 4154 tests to 2618, and the only complaint is two collection
#: errors from `test_dissector.py` and one other, whose `ids=lambda p: p.stem`
#: happens to crash on pytest's empty sentinel. 1536 tests leave and two
#: incidental `AttributeError`s are the whole warning. The files that pass
#: `ids=ids(...)` -- most of them -- skip in silence.
SOURCES = ("example/*/*.situ", "std/*.situ", "test/schema/*.situ")

SCHEMAS = sorted(path for pattern in SOURCES for path in ROOT.glob(pattern))

# Asserted here rather than in a test, so that the 42 files importing this
# fail at collection instead of one of them noticing later. A guard on the
# whole list would not catch the case worth catching: `std/` renamed while
# `example/` still answers drops `kernels.situ` and `image.situ` out of every
# sweep in the repository, and leaves a list long enough to look right.
for _pattern in SOURCES:
	if not any(ROOT.glob(_pattern)):
		raise AssertionError(
			f"no schema matches {_pattern!r} under {ROOT}. Every sweep in "
			f"this suite parametrizes over this list, and pytest skips an "
			f"empty parameter set rather than failing it, so this refuses "
			f"rather than letting a third of the tests disappear quietly")


def ids(paths: list[Path]) -> list[str]:
	"""Parametrize ids that name the directory too: three of these are
	`codecs.situ` or `edges.situ` to a reader of the file name alone."""
	return [path.parent.name + "/" + path.name for path in paths]


def load_schema(path: Path):  # type: ignore[no-untyped-def]
	"""Parse a corpus schema, keeping the path an `import` resolves against.

	`parse_text` discards it, and a schema parsed from a string has no
	directory to resolve against -- so a corpus schema that imports is
	refused with *`import` needs a schema that came from a file*, which is
	17.0a's designed answer arriving in a sweep that meant no harm. Thirty
	sites across fourteen files read a corpus schema that way, and the
	feature therefore had no corpus schema at all: a pair written for it
	failed 22 tests across 10 files on the first run (26.568).

	Here rather than in each file, for the reason this module exists: the
	question "which schemas does this repository build?" is answered once,
	and so is "how is one read".

	`utf-8` for every schema, where the sites it replaces said `ascii`,
	`utf-8` or nothing. The read is strictly more permissive and situc is
	what enforces a schema's declared `encoding`; a file the parser should
	refuse is refused by the parser rather than by the open.
	"""
	from situc.diagnostics import Source
	from situc.parser import parse

	return parse(Source(str(path), path.read_text(encoding="utf-8")))
