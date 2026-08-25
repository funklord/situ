# 0039: `[size = N]` pins a member's footprint without forgetting its length

Status: accepted
Date: 2026-08-24
Phase: after 0038

## Context

`[size = N]` has been in `ATTRIBUTE_NAMES` since the layout chapter was
written, and read by nothing. 26.60 found it because the advisor was
*recommending* it -- "pin the region with `[size = N]`" -- so a reader who took
the advice got a schema that compiled, changed nothing, and produced the same
suggestion on the next run. The advice was corrected there and the attribute
was refused with `UNIMPLEMENTED_ATTRS`, which is the honest holding position
and not an answer. It has been parsed, refused and undefined since.

The advice was wrong in its own right, and that is worth separating from the
construct. What reaches that branch of the advisor is an unbounded *scan*, and
what bounds a scan is `max N`. Nothing about that says a pinned footprint is a
bad idea; it says the advisor was recommending it in the wrong place.

**What the language cannot say today.** A fixed-footprint buffer whose
meaningful content is a prefix of it:

    u8  used;
    u8  buf[used];      // keeps the relationship, costs the static offsets
    u8  buf[64];        // keeps the offsets, forgets `used` entirely

The author must choose. The first leaves `buf` dynamically sized, so every
member after it loses its absolute offset. The second fixes the layout and
throws away the fact that `used` bounds the content, so no generated
`validate` ever checks `used <= 64` -- the schema has stopped describing the
format it started with.

The shape is ordinary rather than exotic: a fixed `char name[64]` with a length
byte, a fixed-slot record, a padded attribute. Situ can describe both halves
and not the two together.

## Decision

**`[size = N]` makes a member occupy exactly N bytes, while its extent
expression continues to say how many of them are meaningful.**

Two facts, kept apart, where the language currently forces a choice between
them:

- **Footprint** is N. The layout is `Fixed(N)`, so members after it keep
  absolute static offsets and the struct is statically allocatable.
- **Length** is whatever the extent expression evaluates to. The accessor
  returns that, not N; a reader of `buf` gets `used` bytes.
- **`validate` checks the relation**, refusing a message whose extent exceeds
  its footprint. That check is the reason the construct is worth having:
  without it, `u8 buf[64]` beside a `used` field is a schema saying less than
  the format does.

**Refused where it would mean nothing, or where two things would say one
thing:**

- a member with no array -- a scalar's width is its type's, and 14.5 refuses a
  construct whose meaning is silently nothing;
- an array whose size is already a literal, which either repeats N or
  contradicts it;
- `[remaining]`, which runs to the end of the frame by definition;
- a delimited member, where `until` already decides where it stops;
- an N below the extent's provable minimum, which is a schema that cannot
  validate any message.

**`[size]` and `[max]` are complementary rather than alternatives**, and the
difference is what 26.60's advice got wrong. `[max = N]` bounds the *value* a
length field may hold, which leaves the member `Bounded(0, N)` and dynamic.
`[size = N]` fixes the *footprint* and says nothing about the value. A schema
describing a fixed buffer with a used-length prefix wants both, and they are
checked against each other: a `[max]` above the pinned size is a contradiction
worth refusing.

## Alternatives considered

**Drop it from the vocabulary.** The other honest disposition, and the one
26.60 left open. It costs the language the shape entirely: an author writing a
fixed buffer with a length prefix has to describe one half and drop the other,
and nothing records which half was dropped.

**Spell it `[padded_to = N]`.** Reads as though situ emits the padding. It does
not: the bytes are on the wire whatever situ does, and the attribute describes
them rather than producing them. `size` is also the word the rest of the
document already uses for extent.

**Infer it from `[max]` on the driving field.** A bound on a value is not a
commitment about footprint. A sender may legitimately put 10 in a field capped
at 1500 and send 10 bytes; inferring a pin would silently change the format.

**Let `u8 buf[64]` carry a separate `require used <= 64`.** Expressible today,
and it puts the relation in a requirement rather than in the layout -- so the
check exists and the *reason* does not. It also does not survive a reader
asking what `buf` is: the schema still says a flat 64-byte array.

## Consequences

This is wire-visible and broad. It reaches the layout solver, all four
backends, both walkers, the capability map and the wire signature -- an
extent that is `Fixed(N)` where the schema's expression is dynamic is a new
shape for each of them, and the accessor returning a length different from the
footprint is the part every backend has to agree about.

It is therefore not a small change, and the record exists so that the size is
agreed before the code rather than discovered during it.
