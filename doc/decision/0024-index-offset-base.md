# 0024: what an offset in an index table is measured from

Status: accepted
Date: 2026-07-31
Phase: after 26.31; needed before any backend can walk an `indexed` region

## Context

Section 9.3 has written the construct this way since long before there was a
parser for it:

```situ
indexed(offset_type = u16, count = hdr.n, base = table_start) {
    record entries[];
}
```

`base` was never parsed. The solver read `offset_type` and `count`, recorded a
table width and a count, and dropped the third argument on the floor -- which
went unnoticed because no backend walked the table, so nothing ever needed to
know where an offset pointed.

It needs an answer now, and the answer cannot be inferred from the schema. Real
formats disagree:

| format | offsets measured from |
|---|---|
| FlatBuffers vtable | the table's own start |
| TIFF IFD | the start of the file |
| ZIP central directory | the start of the archive |
| TrueType `loca` | the start of a *different* table (`glyf`) |

All four are legitimate and situ describes formats it did not design. A
compiler that picked one would read some formats correctly and others silently
wrongly: an offset resolved against the wrong base lands on real bytes and
yields a plausible value, which is the failure mode invariant 9 exists to
refuse.

## Alternatives considered

**Always the region.** Simple, self-contained, and wrong for three of the four
formats above. Situ would be unable to describe TIFF's IFD chain, which it
already describes elsewhere with `at expr`.

**Always the message.** Matches TIFF and ZIP, and makes the FlatBuffers case --
the one section 9.3 names as the construct's inspiration -- unexpressible
without arithmetic the schema has no way to write.

**Infer it from whether the offsets fit inside the region.** Rejected on sight.
It is a guess that depends on the data, so the same schema would mean different
things for different messages, and a short message would silently change the
meaning of a format.

**Require it always.** The region case needs no reader to think about it: an
offset that cannot name a byte outside the region is bounded by the region's
own extent. Making a schema author declare the safe case is noise, and noise is
what makes a declaration stop being read.

## Decision

`base` names what offsets are measured from, and defaults to the region.

| written | offsets measured from |
|---|---|
| nothing | the start of the indexed region -- the table's own first byte |
| `base = region` | the same, said out loud |
| `base = message` | the start of the message, which is what `at expr` means (9.8) |
| `base = <member>` | the start of that member |

Invariant 9's rule decides which one is silent: *the safe option is silent and
the unsafe option is loud*. Region-relative is the safe one, and not by taste --
an offset measured from the region can be bounds-checked against the region's
own extent, so a malformed table is caught by the check that is there anyway.
The other two can name any byte in the message, and a reader of the schema
should be able to see that without inferring it.

A member named by `base` must be declared before the region, the same rule a
size expression follows and for the same reason: the base has to be readable at
the moment the table is walked. Naming a later member, or one that does not
exist, is an error listing what is in scope.

`base = table_start` as section 9.3 wrote it is not a fourth form -- it was the
placeholder for this decision, and reads as `base = <member>` where a member
called `table_start` exists.

## Consequences

Two of the three forms make the region's addressing weaker than the lattice
currently records, and that is a separate question from this one: an offset
into the whole message is not bounded by the region's extent, so the bounds
check cannot be amortised at the region boundary the way section 20.2 amortises
at a frame. Section 9.8 already argues exactly this for `at expr`, and the
`indexed` propagation row will need the same treatment when the walk lands.

This decision settles where an offset points. It does not settle what the
element at that offset is -- an element whose extent the region cannot compute
is still unreachable, which is the rest of the frontier item.
