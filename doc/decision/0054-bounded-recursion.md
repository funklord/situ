# 0054: recursive types, bounded by a declared depth

Status: accepted
Date: 2026-09-06
Phase: after 0053

## Context

`project.md`'s v0 constraint says it in one line: "No recursive types in v0.
Recursive schema types make size and capability computation non-terminating.
Rejected at parse time with a clear error." `wellformed.py` enforces it, and
the diagnostic is a good one -- it names the cycle and cites the section.

It is also increasingly the wrong answer, and the copyright holder has
decided to let it go. The pressure is not theoretical. openmlx4 met it on
2026-09-06 trying to describe FS2's section chain and was refused correctly;
JSON, CBOR, ASN.1, DNS name compression and every nested container format
are the same shape. A schema language that describes byte layouts and cannot
describe a container holding a container is declining a large part of its
subject.

**What the refusal protects is real and none of it needs the refusal.** Four
things depend on the type graph being acyclic:

- **Size computation.** `size_bits` over a cycle does not terminate.
- **Capability computation.** The lattice walk has the same shape.
- **`--owned`.** An owned struct for a recursive type needs a pointer or a
  worst-case array, and 26.69 already declines to choose either for a member
  whose size the data decides.
- **The walkers.** Both are non-recursive by rule (4115: "no recursion,
  bounded stack, no VLAs").

Every one of those is fixed by a **bound**, not by a prohibition. A maximum
depth makes the size finite, the lattice walk terminating, the worst-case
array a number, and the walker's stack a quantity somebody chose.

## The mechanism already exists, one layer down

This is the argument for the shape below rather than for some other one.
`walker/c/situ_walk.c` has carried a depth bound since the walker was
written:

    /* How deep a measurement may nest before this build refuses.
     * ...
     * On a device the stack is the arena's neighbour, so the depth is a
     * number here rather than a property of the input.
     *
     * Eight, because the deepest nesting in this repository's corpus is
     * three and a walker that refuses a legitimate schema is worse than
     * one that costs a few frames. Refused by name when it is reached,
     * never guessed at. */
    #define WALK_DEPTH_MAX 8u

Two sentences there decide most of this record. "The depth is a number here
rather than a property of the input" is the whole reason a reader-side limit
is a different thing from a format's own. And "refused by name when it is
reached" is 26.113's rule -- *reaching a limit is a refusal, not a short
answer* -- which the walk learned by absorbing "this build cannot measure
that element" into "the run ends here" and producing wrong values
indistinguishable from right ones.

The walker's error set already separates the two kinds:

    SITU_WALK_CONSTRAINT   the bytes violate the schema
    SITU_WALK_UNSUPPORTED  "a statement about this build rather than
                            about the bytes"

and `situ.h` draws the same line for a different reason -- `SITU_ERR_TRUNCATED`
is separate from `SITU_ERR_BOUNDS` because "conflating them makes a receiver
treat normal progress as hostile."

So situ has already decided, twice, that *the message is wrong* and *this
program will not go further* are different answers. Recursion does not
introduce that distinction; it is the first construct where the author has
to state both.

## The decision

Recursive types are permitted where the schema states a depth, and there are
two depths because they answer different questions.

**`depth = N` -- the format's own limit.** Part of the layout contract. A
message nested deeper than `N` is **malformed**: the generated `validate`
refuses it with `SITU_ERR_CONSTRAINT`, exactly as `[max = 12]` refuses a
thirteenth month. It is what makes `size_max` a number, so it is the one the
solver reads.

    struct value [depth = 32] { ... value items[count]; ... }

**`limit = N` -- this reader's cap.** Not part of the format, and a message
deeper than it is **well formed and refused anyway**. It gets its own error
class, `SITU_ERR_DEPTH`, because a receiver that logs a resource refusal as
a malformed message reports its own configuration as an attack -- which is
the sentence `SITU_ERR_TRUNCATED` already exists for. It does not
participate in size computation: `size_max` follows `depth`, always, or the
declared footprint would change when somebody hardened a reader.

Both are optional and their absence is not a default. A recursive struct
with no `depth` is refused as it is today, with the same diagnostic plus the
remedy -- because a format whose nesting is genuinely unbounded is one whose
worst case nobody has thought about, and that is the finding rather than a
gap in this language.

## Why two rather than one

A single number cannot say both things, and collapsing them loses the half
that matters for security.

- **`limit` below `depth` narrows what the program accepts**, silently, in a
  way the format does not sanction. That is usually correct -- it is what
  every real DNS resolver, every protobuf reader (100) and every JSON parser
  does, and RFC 8259 section 9 explicitly sanctions it -- but it is a local
  decision and a reader has to be able to see it. It goes in the capability
  map.
- **`limit` above `depth` buys nothing** and should be diagnosed as the
  no-op it is, rather than sitting in a schema looking like a defence.
- **`depth` alone, with no `limit`, is a format that trusts its input**,
  which for a decompression-bomb shape -- DNS pointers, nested ASN.1, zip --
  is the vulnerability rather than the baseline.

The names are deliberately not synonyms. `depth` is a property of the
format, so it reads as a fact; `limit` is a property of this build, so it
reads as a choice. Where they are equal the schema says so rather than
omitting one, because an absent `limit` and a `limit` equal to `depth` are
different claims: nobody thought about it, against somebody decided the
format's own bound was safe here.

## What it costs, stated rather than discovered

- **`size_max` grows with `depth`,** multiplicatively where a recursive
  member sits inside a run. A `[depth = 32]` on a struct with a per-level
  cost of 8 bytes is a 256-byte worst case; the same inside a `[max = 64]`
  run is not. The map must show it, and a schema that produces an absurd
  bound has learned something about itself -- openmlx4 met exactly this
  from the other end, a missing `[max]` deriving `size=20..17179869200`,
  and called the 17 GB the schema's silence becoming visible.
- **Everything below a recursive member is `Dynamic`**, which is what a
  variable-length member already does. No new lattice machinery.
- **`--owned` needs the worst-case array**, and 26.69's rule stands: it is
  the caller's choice to make, so `--owned` refuses a recursive type with a
  reason rather than choosing a shape.
- **The walker's `WALK_DEPTH_MAX` becomes the binding limit** whenever a
  schema's `depth` exceeds it, and the walker must say so by name rather
  than truncating a measurement. That is 26.113 again, and it is the one
  place the existing code needs a check it does not have: today the eight is
  compared against nothing the schema declared.

## What is deliberately left open

~~Whether the recursion may be **mutual** -- `a` containing `b` containing
`a`.~~ **Amended 2026-09-07 (26.288): mutual cycles are described.** The
reasoning above needed no changing -- the bound applies to a strongly
connected component rather than to a struct -- and the open question was the
diagnostic. Its answer is that the cycle is the unit: **every struct in a
cycle declares `[depth]` and they all declare the same one**, so the number
is unambiguous wherever a walk enters, the fact is in front of whoever is
reading either struct, and a diagnostic names them all rather than whichever
was listed first. `depth` counts nested structs, not turns of the cycle:
`[depth = 8]` over `expr <-> item` admits eight nested structs, four of
each.

~~**A cycle through a variant arm is refused for now.**~~ **Amended
2026-09-07 (26.289): it is a cycle like any other**, and the day's
refusal rested on the proxy this paragraph already named -- a variant
with no maximum read as unbounded, and a recursive arm has none because
its maximum has no closed form rather than because it is the rest of the
view. The resolved `Arm` carries `opaque` now and the check asks it. The
arm is the third member kind that reaches a recursive type, after a run's
span and an ordinary nested member, and carries the depth as they do.
`example/json` is the worked case: a value contains an object, an object
contains members, and a member contains a value.

Whether `limit` belongs in the schema at all, rather than as a build flag.
The argument for the schema is that it is then in the artifact `map --check`
diffs and cannot be changed without review. The argument against is that it
is not a property of the format, so a schema shared between two programs
carries one program's decision. Recorded rather than settled.
