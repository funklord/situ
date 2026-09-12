# 0057: a dispatch that does not consume what it reads

Status: accepted 2026-09-12; front end built;
amended 2026-09-12 -- `example/json` is the worked example (26.339)
Date: 2026-09-12
Phase: raised by the copyright holder, from two schemas in this tree

## Context

**A `variant` switches on a FIELD, a field occupies its bytes, so an arm
begins after the discriminant.** Some formats need the discriminant to be
part of the value it selects, and two schemas here are stuck on it.

**json's `number`.** `value` switches on `kind`, and the number's first
character IS that byte. Measured by handing the generated reader a
document rather than reasoned about:

    {"a":12.5}    the `number` struct sees    2.5

The `1` was spent selecting the arm. 0056's `scaled` construct -- built
across four backends, the walker, the fuzz harness and the differential --
therefore **has no worked example**, because its only asker cannot use it
(26.333).

**argv**, from 26.253: `variant body switch (first)` hands the positional
arm `ello` where the message said `hello`. That entry calls this "the
single blocker for option grammars, and for text protocols generally",
and names HTTP's request line against its header lines as the same shape.

**`at` is not the workaround.** A located member re-addresses, which is
the natural spelling, and it is message-relative -- so the expression
would have to name the discriminant's own offset, which `at` cannot do.
It also builds uncompilable C for exactly the member shapes that want it:

    at + scalar         compiles clean
    at + fixed array    compiles clean
    at + [remaining]    implicit declaration of `situ_w_t_offset`
    at + until          implicit declaration of `situ_w_t_span`

A JSON number and an argv word are both variable-extent.

## Decision

**A member may be read without being spent.**

    struct value {
    	peek u8  kind;
    	variant body switch (kind) {
    		case '{': object as_object;
    		default:  number as_number;
    	}
    }

`kind` is read at the cursor and contributes NOTHING to the enclosing
struct's extent, so the member after it begins where it began. The arm
owns the byte; the discriminant only looked at it.

**The precedent is `before`, and it decides the shape.** 8.6.1 has two
spellings for a delimiter because a delimiter may belong to the member it
ends (`until`) or to neither side (`before`). A discriminant has that
problem one construct along: it is read to choose, and the bytes may
belong to the arm. `peek` is `before` for dispatch.

**The mechanism already exists, one construct over.** A located member
"neither follows the member before it nor puts anything after it" --
`layout` returns without advancing the cursor. A peeked member is the same
rule with a different offset: placed at the cursor as usual, and not
advancing it.

## Two questions that looked open and are answered by the tree

**"Do a struct's members still partition its bytes exactly?"** Yes, and
this was the question the work was expected to turn on. A peeked member
contributes zero bytes, and a partition admits an empty part: no byte
belongs to two members, because the peeked one owns none. json's bill
leans on that sentence twice and both survive.

**"What does the capability map say about an arm whose bytes overlap a
member before it?"** It says what it already says, because **overlapping
members are not new**. Measured:

    struct s { u8 a; u8 b[4] at 0; u16 tail; }

    struct s size=3
      s.a     offset=AbsoluteStatic(0x00) size=Fixed(1)
      s.b     offset=DataPlaced           size=Fixed(4)
      s.tail  offset=AbsoluteStatic(0x01) size=Fixed(2)

`b` reads bytes 0-3 and `tail` reads bytes 1-2. The map reports both
without complaint, and the struct is three bytes rather than seven --
so a member's `size` is already "what this reads" while the struct's is
"what its members contribute", and those are already two different
numbers. A peeked member needs no new axis and makes no claim the map
has not been making.

## Alternatives considered

**Put the marker on the `variant`** -- `switch (peek kind)`. It reads
well and says the right thing about the DISPATCH. Rejected because what
changes is the member's extent, and a variant clause that retroactively
empties a member declared above it is action at a distance: a reader
computing offsets down the struct would have to look ahead to know what
`kind` costs.

**Let the arm re-read the discriminant's bytes**, leaving the member
consuming. Rejected because the bytes are then counted twice in the
struct's extent, which is the partition question failing in the direction
that actually matters.

**A general overlap marker**, usable on any member rather than a
discriminant. It is the same machinery and it is a bigger claim -- "this
member and the next share bytes" invites a length prefix that is also
payload, which nothing here has asked for. Refused where the member is
not a discriminant, and that refusal is a diagnostic rather than a
silence, so the case can be admitted later by deleting a check.

## Consequences

**The corpus schema lands with the construct**, per 0052's consequence
and 0055's and 0056's experience of it: a construct with no corpus schema
poses no case to the four-way differential.

~~**json's `number` becomes a `scaled` number in the same change or the
one after it**~~, which is the whole point: 0056 shipped without a worked
example and this is what supplies one. **Done 2026-09-12 (26.339).** It
was the change after next: converting json found three faults in the
walker before the schema could land, and the last two are 26.338 and
26.339.

**A peeked discriminant gives its byte to EVERY arm**, which is the
consequence nobody had costed and json is where it showed. Seven arms:
three grew a member to own their brace, bracket or quote, and the three
literals became `[must_eq = "true"]` where they had carried
`[must_eq = "rue"]` precisely because the `t` was eaten. That half is an
improvement and is worth expecting rather than discovering -- a format
converting to `peek` rewrites every arm, not only the one that needed
it.
