# 0026: a packed layout image, read by a separate interpreter, late

Status: accepted, and scheduled late (26.33)
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
