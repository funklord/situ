# 0059: a cap on a `remaining` run

Status: proposed
Date: 2026-09-30
Phase: raised by `fuzznet`, from a record body it cannot bound

## Context

**A `[remaining]` run is the only variable member with no way to state a
cap.** Every other one has a spelling:

    u8  body[] until "," max 470;     the cap is syntax after the run form
    u8  name[n];                      the count bounds it
    u8  tail[remaining];              nothing

`fuzznet` reported the gap from two real layouts -- a tree node body and a
catalog's inline content. Both are RECORD BODIES whose real cap lives in
the enclosing record (`body_len` minus the header), so a body-only schema
has no in-body field to size them from and writes `content[remaining]`.
The region then reads `size=Unbounded`, which is honest and is all the
schema can say.

**What they asked for is `[max = 470]` on the member, and situc refuses
it**:

    `[max]` means nothing here
      belongs on a scalar field -- an array has no single value to bound,
      and a size cap is spelled `max N` after `until`

**That refusal is correct and it names the answer.** `[min]`, `[max]` and
`[must_eq]` are claims about A VALUE, which `validate` compares; an array
has no single value. Section 14.5's rule -- an attribute has to sit where
something reads it -- is what keeps one attribute from meaning two things,
and the message already points at the spelling the language uses for this
meaning.

**Measured, so the shape of the win is not guessed:**

    struct d { u8 kind; u8 body[] until "," max 470; }
      body   size_bits=8   size_max_bits=3760
      struct 16..3768

    struct b { u8 kind; u8 content[remaining]; }
      content size_bits=0  size_max_bits=None
      struct 8..unbounded

**And nothing may follow a `[remaining]` member** -- `layout` refuses it
by name -- so a cap here moves no offset. It sets one member's upper
bound and the struct's, and nothing else in the layout changes.

## Decision

**The cap is syntax after the run form, as it is for a delimited run:**

    u8  content[remaining] max 470;

Same word, same position, same meaning as `until D max N`: the largest
this run may be. A reader who knows one knows the other, which is the
whole reason not to spell it a second way.

**What it is NOT is `[max = N]`.** That attribute is a bound on a value
and this is a bound on an extent; giving it the second meaning in one
position is exactly what 14.5 exists to prevent, and the refusal message
above would have to be rewritten to say when it means which.

## The open question this record cannot settle by itself

**Does the cap BOUND THE READ or REFUSE A LONGER FRAME?** The two
existing answers point different ways and only the holder can choose.

`until D max N` caps the SCAN: the generated code stops looking after N
bytes, and 26.431 is the entry where an arm's accessor ignored the cap
and read past it. A scan can stop early because the delimiter may not be
there at all.

A `remaining` run has no scan. Its length IS the frame, so:

- **Cap the read** -- the member is `min(remaining, N)` bytes and anything
  past N is unread. Consistent with `until`, and it silently drops bytes
  the message contains.
- **Refuse a longer frame** -- `validate` fails when the remainder exceeds
  N. Consistent with what a cap MEANS on a record whose true bound is
  external, which is `fuzznet`'s case, and it makes a schema that
  under-states the cap reject valid messages.

**`fuzznet`'s own words favour the second**: their cap is "the record's
`body_len` minus the header", a fact about the enclosing format that a
body-only schema is restating. A message exceeding it is malformed, not
truncatable.

## Alternatives considered

**`[size = N]` (0039).** Pins the footprint to exactly N and leaves the
extent expression saying how many bytes are meaningful. That is a
different construct -- a fixed field with slack -- and using it here would
make every such body 470 bytes on the wire.

**Leave it Unbounded.** Honest, and what the schema says today. The cost
is that `situc advise` tells an author to bound an unbounded member and
the author has no way to, which is the shape 14.5's own rule calls out:
a schema that cannot state what the format guarantees.

**A bound in the ENCLOSING record.** The correct answer where the whole
format is one schema, and unavailable to `fuzznet`, who compile the body
alone. Worth saying because it is the reason this is a language gap
rather than a modelling mistake.

## Consequences

**The size axis reports `Bounded` where it reported `Unbounded`**, so
capability maps change for any schema adopting it, and `situc wire`'s
`size=` line gains an upper bound.

**Four backends, the walker and the image.** A cap is a number the
generated code and both walkers must agree on, which is the six-site
shape 26.144's checklist describes: the AST field, the parser clause,
the layout extent, the backends, the image and the C walker, plus the
unparser, which drops a field it does not render.

**A schema in the corpus must use it**, or the differential and the
compile gates read a construct nobody wrote. `edges.situ` is where a
construct no worked example carries goes.
