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
