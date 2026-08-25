# 0021: the generated dissector is Lua

Status: accepted (recorded after the fact)
Date: 2026-07-30
Phase: 20.3, and the reason it was never written down

## Context

`situc gen-dissector` has emitted Lua since it was written, and nothing said
why. It was asked directly, which is a good sign the answer belonged in a file
rather than in whoever wrote it.

Wireshark takes a dissector two ways: a Lua script, or a C plugin.

## Decision

**Lua.**

## Why

**No build step, and nothing to match.** A Lua dissector is a file the user
drops in `~/.local/lib/wireshark/plugins`. A C plugin has to be compiled
against the headers of the exact Wireshark it will load into, and Wireshark's
plugin ABI moves between minor releases -- a plugin built for 4.0 does not load
in 4.2. situ generates this file to be *committed beside the schema*, which
means it has to keep working across the versions of Wireshark its readers
happen to have, and a compiled artifact cannot.

**The cost that would justify C is not paid here.** A dissector is a debugging
aid. It runs over a capture a human is looking at, which is bounded by what a
human can look at. Nothing about this is hot, and the C plugin's only real
advantage is speed.

**The generated code is meant to be read.** A user is being asked to trust a
file that claims to describe their protocol; the answer to "does this match my
schema?" should be legible without a compiler. That argues for the higher-level
language on its own, and it is why the emitted Lua uses arithmetic rather than
`bit.band` -- Wireshark ships Lua BitOp in most builds and not in all of them,
and a dissector that fails to load is worse than one that divides.

## What it costs

**A second implementation of the layout, in a second language.** This is the
real objection and it is answered structurally rather than by care: the
dissector reads the same `traverse` walk, the same `extent_parts`, the same
`arm_members` as the four code backends, and asks the same
`has_computable_extent`. Only the reads are Lua's. A construct that gains a
row gains it for all of them.

**It is never executed.** Section 22 says so and this is where it bites: the
build environment has no Lua interpreter, so `gen-dissector` is checked
structurally and against the layout rather than against Wireshark's acceptance.
`test_blocks_balance` is the cheapest guard against the one error that makes
the file useless, and it is not a Lua parser.

One consequence worth naming, because it is a genuine semantic dependency:
the emitted `a and b or c` idiom for a conditional length is correct **because
zero is truthy in Lua**. In Python or Rust the same shape would return the
wrong branch for a zero-length arm. It is the kind of thing that would be found
by running the file, and is not.

## Alternatives

**A C plugin.** Rejected above: the ABI churn defeats the point of committing
the artifact.

**Emit nothing, and describe the layout for a hand-written dissector.** This is
what most schema languages do, and it is why most protocols do not have one.
The value of `gen-dissector` is that it costs a user one command; a description
they then have to transcribe costs them an afternoon and drifts immediately.

**A dissector in the schema-agnostic "generic dissector" format some tools
take.** None of them is Wireshark, which is the tool people actually have.
