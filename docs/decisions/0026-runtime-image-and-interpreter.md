# 0026: a packed layout image, read by a separate interpreter, late

Status: accepted, and scheduled late (26.33); amended 2026-08-07 -- the
walker is a separate binary in this repository rather than a separate
project. See *Amendment* at the end; everything above it is as first written.
Date: 2026-08-02
Phase: after 26.32

## Context

The question arrived from a real device: a radio whose framing must change
without a firmware rebuild. Ship a description of the new format, load it, and
parse. The same shape serves tooling -- one dissector rather than one per host
language -- and test harnesses.

Section 2 says situ is "not a parser combinator library. The schema is
declarative and the layout solver is a compiler pass, not a runtime
interpreter." That non-goal is load-bearing: it is what makes an offset a
constant, an operation *absent* rather than refused, and generated code
allocation-free. It is also a statement about `situc`, not about what may
consume `situc`'s output.

Three things make an interpreter more tractable here than it looks:

- **The traversal is already one thing.** `situc/traverse.py` answers every
  question a walker asks -- `classify`, `offset_plan`, `region_extent`,
  `covered_run`, `containment_order`, `decodes_here` -- and the four backends
  plus `gen-dissector` are five spellings of it. An interpreter is a sixth
  whose language is a table walk.
- **The expression language is total and tiny** (section 10): literals, field
  references, arithmetic and bitwise operators, comparisons, `size`, `offset`,
  `count`, `remaining`, `min`, `max`, `align_up`. No calls, no recursion, no
  iteration, no floating point. That is a bytecode of about twenty opcodes with
  a decidable evaluator.
- **The layout is already data.** `ResolvedSchema` is placements with offsets
  in bits, sizes, kinds, endianness, delimiters, codecs and capability vectors.

## Decision

**A packed layout image is a `situc` output; the interpreter that reads it is a
separate project.** `situc pack` emits the image; nothing in this repository
walks one at run time. The non-goal above stands unchanged for `situc`, whose
invariants keep exactly one master.

**The image format is itself described by a situ schema**, and the interpreter
reads it through generated accessors. Self-hosting rather than a hand-rolled
parser, because everything this repository already does then applies to it:
`gen-fuzz` fuzzes the image parser, `situc wire` pins the format, the
differential check compares four backends reading images, `map --check` catches
a layout change nobody meant. An interpreter whose own parser is hand-written
is the one component nothing checks.

**The interpreter is a fifth column in the differential check.** It answers the
same questions about the same bytes as C, C++, Rust and Python, and disagrees
with none of them. Six divergences were found that way in one session -- the
acquisition check, saturating offsets in two backends, three error classes --
and every one was invisible to every other test.

**What is lost is recorded, once, plainly.** An interpreter cannot make an
operation absent. A field that is `mutate = Shifting` still has a general write
entry point that refuses at run time, where the generated API simply has no
setter. The capability map stops being the *shape* of the interface and becomes
data a caller may consult. That is a genuine weakening of the central claim,
unavoidable in an interpreter, and it belongs in the interpreter's own
documentation the way `runtime/python/situ_runtime.py` already states which
parts of the lattice Python cannot enforce.

**It is late** (26.33). Not because it is uninteresting -- because the image
format's shape depends on which consumer is primary, and because everything the
interpreter would be checked against was built in the last few weeks.

## Alternatives considered

**A runtime mode inside `situc`.** Rejected: the invariants would have two
masters, and "generated code never allocates" and "a table walk in a fixed
arena" are different promises that would end up arguing.

**Ship the schema source and parse it on the device.** Rejected: that is the
whole front end -- lexer, parser, resolver, layout solver, capability lattice --
on a target that has a radio and a kilobyte. The image exists precisely so the
solving happens once, where there is room for it.

**A hand-rolled image format with a hand-rolled parser.** Rejected for the
reason the decision gives: it would be the only artifact in the project that
nothing checks, in the component whose input is least trusted.

**Do it now.** Rejected: the differential check that keeps it honest is days
old, the probe list it would join is still growing, and the primary consumer is
not yet decided. A format designed before that choice is a format designed
twice.

## Consequences

`situc` gains one subcommand when the work starts, and section 2's non-goal
gains a sentence naming the boundary rather than being contradicted by a
project that reads its output.

## Amendment, 2026-08-07: the boundary is a binary, not a repository

**The walker lives in this repository, as a program of its own.** `situc`
still never walks an image. Everything above stands except the words
"separate project", and the reasoning above is why the change is small.

**What the original decision protects is untouched by the move.** The
alternative it rejected was *a runtime mode inside `situc`*, on the grounds
that "the invariants would have two masters" -- and that argument is about one
program claiming both to compile a layout and to interpret one. A second
binary makes no such claim: `situc` compiles and emits, the walker only ever
consumes, and no build of `situc` gains a table-walking path. "Generated code
never allocates" and "a table walk in a fixed arena" stay different promises
made by different programs, which is all the original separation was buying.

**What the repository boundary was costing is what forced the change.** This
record requires the interpreter to be "a fifth column in the differential
check", and 26.33 names the test: `test_backends_agree_under_random_bytes`,
which lives here. Across a repository boundary it cannot be that. The check
that decides whether a table walk says the same thing as four compiled
backends about hostile bytes would have to run somewhere neither repository
owns, against a walker one of them cannot see -- so the format's only real
validation sat on the far side of a line drawn to protect something the line
was not protecting. The consequence was not hypothetical: 26.79 shipped
`situc pack` and an image format that nothing had ever walked, and said so,
because there was nowhere in reach to walk it.

**Which boundary now does the work.** A build fact rather than a directory
one, and it has to be checked rather than assumed:

- the walker is its own binary with its own entry point, built by the test
  target and not by the default one, in keeping with section 24's rule that a
  plain build produces the library and the binaries;
- nothing under `situc/` imports it, and that wants a test of its own once it
  exists -- the separation is only as good as what refuses to link them;
- it reads `std/image.situ` through generated accessors like any other
  consumer, which is the self-hosting this record already required;
- it joins `make check` as the fifth column, which is the whole point.

**What is unchanged, and still belongs in the walker's own documentation.** An
interpreter cannot make an operation *absent*. A field that is `mutate =
Shifting` has a general write entry point that refuses at run time, where the
generated API simply has no setter, so under a walker the capability map stops
being the shape of the interface and becomes data a caller may consult. That
is a genuine weakening of the central claim and moving the walker closer does
not soften it -- if anything it makes saying so more urgent, since the two now
ship from one tree.

**Why this is an amendment rather than a new record.** The decision that
mattered -- image emitted by `situc`, walked by something that is not `situc`,
format described by a schema and checked like one -- is unchanged. Only the
distance was wrong, and a second record would leave two documents disagreeing
about a boundary rather than one saying where it moved and why.
