# 0020: the lattice models delimiter-framed data

Status: accepted
Date: 2026-07-29
Phase: takes the decision open question 1 was waiting on

## Context

Open question 1 asked for `until`-delimited arrays and was resolved as
"not-now, deliberately". The reasoning was that the construct was not the
question; underneath it was whether the capability lattice should model
delimiter-framed data at all, and the position taken was that it should not:

> where no field has an offset, so `offset`, `access` and `address` have
> nothing to say and eleven of thirteen axes are vacuous

**That claim is false, and the tree already disproves it.** A struct whose
elements are variable-sized -- where element N cannot be found without walking
the N-1 before it -- produces this today:

```
struct stream size=1 access=Sequential mutate=Shifting address=Unstable
  stream.items      offset=AbsoluteStatic(0x01) size=Bounded(0, 65280) access=Sequential mutate=Shifting
  stream.items[]    offset=FrameStatic(0x00)    size=Bounded(1, 256)   access=Sequential mutate=Shifting
  stream.trailer    offset=Dynamic              size=Unbounded         mutate=Shifting address=Unstable
```

Every axis says something, and the two that say the most -- `access` and
`mutate` -- say exactly what a reader of a delimited format needs to hear.
Delimiter-framed data is the same shape: the boundary is found by walking. The
only difference is that the walk matches bytes instead of reading a length.

The propagation rule that produces `Sequential` there is `dynamic-element-type`,
whose blame reads "element N cannot be found without walking the N-1 elements
before it". That sentence is already about delimited data. It was written for
something else.

The stronger argument is the other direction. `canonical` exists to say that
several byte sequences encode one value, and text is where that is *most*
often true: case-insensitive tokens, optional whitespace, leading zeros, `\n`
against `\r\n`. Binary formats are canonical by default and situ mostly has
nothing to report; text formats are not, and situ has a great deal to report.
The axis was built for this and had never met the case it fits best.

## Decision

**The lattice models delimiter-framed data, and situ describes text protocols
to the extent that a capability map is a useful thing to have about them.**

Two axis values are added, because two real distinctions were being collapsed.

**`offset = Scanned`**, weaker than `Dynamic`. `Dynamic` means arithmetic over
values already read: one addition per field, cannot fail. `Scanned` means a
search: linear in the distance to the delimiter, and it can fail -- the
delimiter may not be there. Reporting both as `Dynamic` would hide a cost
difference and, worse, a failure mode. A caller who knows a field is `Scanned`
knows that reaching it is O(n) and that reaching it can return an error, and
neither is true of `Dynamic`.

**`repr = TextConverted`**, weaker than `ValueConverted`. A byte swap is total:
every bit pattern of the right width is some value. A decimal parse is not --
`"12x4"` is not a number, so conversion is a fallible operation and the getter
has to be able to say so. The width in the buffer also varies with the value,
which `ValueConverted` never does.

**Where the delimiter may occur in the content, the schema says how.** A bare
`until D` means the first match, and situ *enforces* that the content cannot
contain `D`: `validate` refuses such a field and the setter refuses such a
value. That is not a restriction invented here -- content containing the
delimiter is unrepresentable in the format, since writing it back would produce
a different framing. Enforcing it makes the field `Canonical` and makes the
round trip total.

Where a protocol genuinely admits the delimiter inside a field, that
enforcement would reject valid data, so the author says how the delimiter is
made inert: `[quoted = '"']` or `[escape = '\\']`. Both relax the scan and cost
`canonical = NonCanonical`, because two spellings then encode one value.

Situ cannot detect which case a protocol is in -- only the author knows whether
a comma inside a CSV field is possible -- so this is stated rather than
inferred, and the safe reading is the silent one (invariant 9).

## Consequences

For HTTP, the automatic exclusion *is* the CRLF-injection check. A header value
containing a bare CR or LF is exactly the bug class, and a schema written here
gets the validator generated rather than remembered.

Eleven of thirteen axes were said to be vacuous. On the worked example in 8.6
the count is zero: every axis carries a value that differs between a text field
and a binary one, and six of them are weakened in ways a caller must plan for.

`26.21`'s position is reversed and the section says so rather than being
deleted. What the old position got right is kept: situ is not becoming a parser
generator, and a full grammar (alternation, repetition, rule references) stays
out, because a parse tree has no offsets to be static about and the capability
map would have nothing to say about one. The line is that situ describes where
bytes *are* and what they cost to reach, in text as in binary.

## Alternatives considered

**Reuse `Dynamic` for scanned offsets.** Fewer values, and wrong in the
direction that matters: it would report a fallible O(n) search as though it
were an addition. The whole product is the cost being visible.

**Treat a delimited field as `size = Unbounded` and leave the offset axis
alone.** Half the story. `Unbounded` says how big it might be; it does not say
that finding the *next* field requires reading this one, which is the property
that makes random access impossible.

**Require an explicit maximum on every delimited field**, so `size` stays
`Bounded` and no scan is unbounded. Attractive for embedded targets, and
rejected as a blanket rule because it would refuse formats that are genuinely
unbounded and force a fiction. It is available where a caller wants it --
`until D max N` bounds the scan and turns a runaway into a diagnostic -- and
the map records which of the two a field is.
