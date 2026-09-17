# 0050: external arguments, and which of them a schema may take

Status: accepted 2026-09-04. Built in four passes:

- `--define`, 2026-09-15 (26.362).
- `parameter`, `[stream]` and their checks, 2026-09-17 (26.386), refused
  by every generator until a view could carry one (26.387, 26.390).
- The descriptions that place nothing: the map and the wire signature name
  a parameter rather than placing it (26.390, 26.391); the dissector reads
  a `[stream]` one from a preference (26.392); `situ verify` supplies one
  with `--arg` (26.394); and `situc explain` says what its axes describe
  rather than printing a position that is the next member's (26.404).
- The views, 2026-09-17: Python (26.393), Rust (26.395), C++ (26.396) and
  C (26.397). None emits an accessor for the parameter itself (26.398).
  What still declines are the generators that emit a second artifact
  over a schema, and a test asserts that partition rather than a list
  of cells that is now empty (26.400, 26.401).
- The packed image and both walkers, 2026-09-17 (26.403). The flag went
  into a free bit of an existing byte, so the record did not grow and
  `std/image.situ.map` and `.wire` are unchanged -- which is why the
  `[stream]` question below does not arrive through the image.

- The per-layer generators, 2026-09-17: 30 of them across four backends,
  not the six layer names this record first guessed. Those that acquire a
  whole message take the argument; the drivers refuse per relation,
  because a driver builds a view of every datagram it receives and where
  it would be told is a second design this record does not settle.
- The corpus struct, 2026-09-17 (26.409). It needed six sweeping
  generators changed first -- three taught to skip the argued struct,
  three whose refusal was unnecessary (26.405, 26.407) -- which is not
  what "lands with the backends" said.

Still to do: lifting the refusal on a struct that takes an argument being
a member of another, which needs a decision about how a parent's
arguments compose with a child's; `situc gen-tamper`, which declines a
schema carrying one and which nothing sweeps the corpus through, so it
costs nothing today; and whether the two cross-checking harnesses should
eventually COMPARE parameterised structs rather than skip them. Both ask
the same question and neither is settled:

- The four-way differ found 26.27, 26.31, 26.47 and 26.222, so it is
  exactly where a fresh cross-backend disagreement is likely.
- The two-walker identity sweep skips an argued struct for the same
  reason (26.409).

Where the argument comes from is the open half, and it is one decision
for both: a driver's whole input is one hex string on argv, so the
choices are a constant baked in (which compares one argument for ever --
a fixture chosen so the right answer passes rather than so a wrong one
differs), slicing it off the front of the draw (variation for free, but
four languages must slice identically), or a second argv element
(cleanest, and it changes an interface `fourway.answers`, `probe.py` and
both walker suites share). Two
shapes reach C functions that take a view alone and so fail at the
compiler rather than silently -- a run of variable-sized structs sized by
an argument, and a delimited or `while` run's stride helpers -- and no
schema in this tree has either (26.397). A THIRD was found and fixed, and
a FOURTH found and left: a nested member behind a parameter now takes the
tail, while a variant ARM holding a nested struct is broken in several
sites at once and is recorded rather than half-fixed (26.408). That the
list of unplumbed shapes grew from two to four the first time anybody
looked is the argument for the corpus struct below, not a footnote to
it.

What a change to `[stream]` means for wire compatibility is open, and
26.391 says why
Date: 2026-09-04
Phase: raised by the copyright holder while reading 15.2

## Context

Real formats vary on facts the message does not carry. A TLS record's layout
follows a cipher suite negotiated in an earlier handshake; an 802.11 frame's
optional fields follow capabilities exchanged before it; an MMC command's
response width follows a card class read from a different register; a raw
volume's directory offsets follow a block size the volume header does not
repeat. In each case the bytes in front of the reader are not enough, and the
missing fact is small -- usually one integer.

**situ already admits the principle, for one thing.** `prefix(...)` is
coverage over bytes the message does not contain, and the generated header
says why in as many words:

> Its contents are not situ's to supply -- they come from a layer this schema
> does not describe, which is the whole reason the clause exists.

So "the caller knows something the message does not" is not new. What
`prefix` supplies is an *algorithm's input*. What this record is about is a
*layout's shape*, and the difference is the whole question.

**And `const` exists with no way to set one from outside.** `const` parses,
`_declared_names` scopes it, and nothing in `situc build` overrides it -- so
even the easiest case, a fact fixed before the code is generated, has no
spelling today.

## The two axes

Everything in this design falls out of crossing two questions.

**When is the argument known?**

1. **At generation.** A deployment constant: this product uses 4 KiB blocks.
2. **Per stream.** Negotiated once and then fixed: a cipher suite, a card
   class.
3. **Per message.** Varying between one buffer and the next.

**What may the argument affect?**

- **Meaning only.** Which constraint applies, how an enum reads, whether a
  member is required. Nothing moves.
- **Position.** A size or an offset, so every member after it moves.

**The second axis is the one situ has already taken a position on**, and it
did so without calling it that. `[since = N]` gates a member on a version,
and 19.4 built it *append-only* on purpose -- `layout.py` records that its
"offset is still static". situ has exactly one construct conditioned on a
fact outside the member, and it was designed so the fact never moves a byte.

## The fifth description decides the rest

0049 settled that "a rule four of five can follow is not a rule", and the
Lua dissector is the constraint here as it was there.

- A **generation-time** argument is baked into the emitted Lua like any
  other constant. All five, no cost.
- A **per-stream** argument could reach a dissector as a Wireshark
  preference -- the generator emits none today, and this is the case that
  would justify one. A preference is per-dissector and not per-packet, which
  is exactly the shape of a per-stream fact.
- A **per-message** argument cannot reach a dissector at all. Nothing in the
  capture carries it and no preference varies packet to packet.

Crossed with the other axis, only one cell is actually lost: a per-message
argument that moves a member leaves the dissector unable to place anything
after it. It would decline from that point -- the `_LOST` cursor 26.201
built already does this honestly -- but a dissector that stops at the first
parameterised field is most of a dissector thrown away.

## Decision

**Take the arguments that cost nothing, and refuse the one cell that costs
the fifth description its cursor.**

- **`--define name=value` sets a declared `const` at generation.** No
  lattice change, no artefact change, no new syntax: the layout is as static
  afterwards as it is today. This is most of the real cases and it is the
  cheap half.
- **`parameter u8 name;` declares a run-time argument**, and is a member of
  zero width. Reusing a member rather than inventing a second expression
  world is the point: sizes, `at expr`, `[since]`, `require` and `invariant`
  all read fields already, and none of them needs to learn that this one
  costs no bytes. The view constructor takes it; `situ verify` takes it on
  the command line; the walker's `acquire` takes it.
- **A `parameter` may decide meaning freely, and may move a member only
  where it is fixed for the stream.** `[stream]` on the declaration says so,
  and is what lets a dissector carry it as a preference. Without it, a
  parameter that reaches a size or an offset expression is refused, naming
  the member it would have moved.
- **The wire signature names every parameter**, for 0048's reason. Two peers
  that disagree about an argument disagree about the bytes, and a contract
  that does not mention it is a contract that cannot be checked. The
  capability map names it too, since a reader comparing two maps needs to
  know the layout was described under an assumption.

## Alternatives considered

**Let a parameter move anything, and let the dissector decline.** The
honest version of the refusal above, and it is tempting because the
machinery exists. Rejected because the loss is silent at schema-writing
time: an author adds one parameter to a size expression and the dissector
stops describing the rest of the struct, with nothing in the schema saying
so. 14.5's rule is the same one -- a schema that states what the generated
code does not enforce is worse than one that states nothing -- and this is
its mirror: a schema whose cost lands on a description the author never
runs.

**Model it as a second view, like a relation's two messages.** 0030 already
has a construct that takes something the message does not contain, and a
pseudo-header is a struct. Rejected because a relation is a *predicate* over
two acquired views, and this is a fact that governs how one view is
acquired at all -- it has to be known before there is a view to relate.

**Generic schemas, instantiated per argument.** `schema<BLOCK=4096>` and a
family of generated modules. This is the most powerful answer and the one
that keeps every artefact static: one map and one signature per
instantiation, no conditional anything. Rejected for this record and worth
revisiting: it is a language feature rather than a construct, it multiplies
the generated surface by the argument's range, and it cannot express the
per-stream case at all, which is the one with real formats behind it.

**Do nothing and let the caller pre-parse.** A reader that knows the block
size can hand situ a slice and describe the rest. This works, and it is what
everybody does today. What it cannot do is state the dependency: the
capability map says the layout is static when it is static *given* something
the map does not mention, which is the same silence `prefix` exists to
break for a checksum's input.

## Consequences

- `--define` needs a refusal for a name that is not a declared `const`, and
  for a value the const's declared type cannot hold.
- `parameter` is a new member kind with `size = Fixed(0)` and no offset. The
  lattice needs a value for its `offset` axis, or an exemption -- 26.209's
  question about an arm's offset is the nearest precedent for what a member
  with no position answers.
- Four backends gain a parameter on the view constructor, which is the
  API-shape change 0046 flagged for its own reason; the two are worth
  landing together if both are accepted.
- The dissector gains a preference per `[stream]` parameter, which is the
  first preference it has ever emitted.
- `situ verify`, `situc map` and `situc wire` all grow a way to supply one,
  and the map and signature grow a line naming it.
- **A parameter is not a secret and not a capability.** It says what shape
  the bytes have, not who may read them, and nothing in this record lets a
  schema gate access on one.
