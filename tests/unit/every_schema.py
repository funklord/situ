"""Every schema this repository builds.

Six files wanted this list and four of them had a shorter one: `examples/`
only. That is not a detail. `tests/schemas/edges.situ` exists to carry the
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

#: Not a sample. Which constructs a sample happens to contain is not something
#: anyone chose -- the same argument `gen-fuzz` makes for a harness per schema.
SCHEMAS = sorted(
	[*ROOT.glob("examples/*/*.situ"),
	 *ROOT.glob("std/*.situ"),
	 *ROOT.glob("tests/schemas/*.situ")])


def ids(paths: list[Path]) -> list[str]:
	"""Parametrize ids that name the directory too: three of these are
	`codecs.situ` or `edges.situ` to a reader of the file name alone."""
	return [path.parent.name + "/" + path.name for path in paths]
