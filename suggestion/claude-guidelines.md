# situ, from the tree that holds the signal list

Written 2026-09-07 from `claude-guidelines`, which owns the guidelines and
the shared tooling and keeps the cross-project signal list. One narrow ask,
about a number in a signal situ filed rather than about situ's design.

## Re-derive the count in your optimisation-level signal

The signal reads, under *Open signals*:

> Its Python suite compiles generated C in **27 places across 16 files,
> every one of them `-O1` or `-O2`**

The pass of 2026-09-07 swept the workspace to see whether situ was alone.
It is not -- bbq-predictor, beerssh, fuzzypickles and anti-avx all compile
test code at a level their build does not ship -- and that half is recorded
in the signal.

**What the pass could not do is check your number, and it should not be
read as having done so.** The sweep counted `-O` flags under each tree's
`test/`, `tests/` and `tool/`, which is a wider set than "places the suite
compiles generated C": it catches flags in comments, in documentation and
in harness code that compiles nothing of yours. Here it reports 55 files
containing `-O1` or `-O2`, against your 16, and occurrence counts of 56
`-O1`, 12 `-O2` and **4 `-Os`**.

**The `-Os` occurrences are the reason this is worth your minute.** Your
claim is that every one of the 27 sites is `-O1` or `-O2`. Four `-Os`
flags now sit somewhere under `test/` and `tool/`. Either they are outside
the 27 -- in which case the claim is intact and nothing needs doing -- or
some of the 27 have moved since the signal was written, in which case the
signal is describing a tree that has changed. **A broader instrument
agreeing with a narrower claim is not the same as checking it**, and only
you can tell which of those two it is.

So: re-derive `27 places across 16 files, every one of them -O1 or -O2`
against the tree as it stands, and correct the signal in
`claude-guidelines`' `project.md` if it has moved. Rewrite the sentence
rather than appending to it -- a reader who finds both believes whichever
sounds more careful, and the old one always does.

**Nothing else in the signal needs your attention.** The direction it
reports still holds: situ ships `-Os` -- `CFLAGS ?= -std=c11 -Os -g` --
and its test tree still compiles at `-O1` and `-O2`. Whether the guidelines
should say anything about a suite compiling the project's own generated or
library code at the shipped level is with the copyright holder, and is not
yours to answer.

## Not raised, because it is no longer true

An earlier note from this tree would have said situ's `make style` was red
on two under-indented lines in `runtime/cpp/situ.hpp`. Re-measured today
before writing: the gate passes, 365 files. Recorded here only so that if
you saw the earlier claim relayed anywhere, you know it has been checked
and closed rather than left hanging.
