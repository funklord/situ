# 0066: A tlv value may be carried from one item to the next

Status: proposed
Date: 2026-10-20
Phase: 26.664

## Context

26.142 measured that **situ carries state inside codecs and nowhere in
the layout**: a `shift_register` holds a register, `stuffing` with
`unit = stream` holds line position, and nothing a schema writes can say
"this item's number is the last one plus this delta". CoAP's option
number is exactly that -- the running sum of every delta ahead of it --
so the protocol was inexpressible for two independent reasons. The
other, a value sized by a part of its own tag, is 0065.

The open register carried this as the layout half of item 13, and
26.663 narrowed the item to it after showing that 8b10b's running
disparity is codec-side and belongs to 26.139's three shapes.

## The measurement

**The walk already carries state; what it lacked was a declared one.**
An item's `next` is state between items, and `next(item)` reads *into
the item it was given* -- so the previous item's values are in scope at
the moment the next one is read, in all four backends. Nothing about
the shape of the walk had to change.

**Lookup already walks, too.** `find` iterates from `first` comparing
`item->{identity}`, which is O(n) and `access = Sequential` by
construction. So an identity that only exists part-way through a walk
costs nothing it was not already paying, and `find` needed no change at
all.

**Neither walker uses an identity.** The image carries the selector
program and the value rules; `tlv_count` counts items and nothing reads
an identity, in `walker/walk.py` or in `walker/c`. So this is a
compiler-and-backends change, and the image format does not move.

## The decision

**`tag_running = { number = previous + delta }`**, in the same braced
shape `tag_decode` has, because it is the same thing one step on: a
value named here and computed from what is in scope. What differs is
the scope.

- **`previous` is that definition's own value for the item before**, and
  zero before the first. Scoped to the definition, so two accumulators
  cannot mean each other's and the order they are evaluated in decides
  nothing. The word is the one situ's own refusal already used: 26.142
  recorded `previous is not in scope in a tag decode`, so the error
  message named the feature before it existed.
- **Beside it, the tag's decoded parts and nothing else.** Not the raw
  tag -- the decode is what takes it apart, and a second place to do
  that is a second description of one thing. Not another accumulator.
  Nothing read later, which is `tag_decode`'s rule already.
- **A carried value may be the identity and may not size a value.** An
  extent computed from it would have no bound: the parts are bounded by
  the tag's width, a sum over a region is bounded by the item count.
  Refused with that reason rather than admitted and left to the solver.
- **The backends take it as a parameter.** `read(view, at, number_in,
  out)`, `first` seeding zero, `next` passing the item's own value. That
  is what makes the walk the only way to a number: a caller holding an
  offset has nothing to pass, which is the truth about an accumulated
  value rather than a restriction invented for it.
- **Bounded like every other size (14.2b)**: signed 64-bit arithmetic,
  one clamp, because a sum over a region's worth of message-chosen
  deltas is exactly where a 32-bit accumulator wraps.

**Inference of the identity stops once a value is carried.** 0023 takes
a lone decoded part as the identity, which is right while the tag is the
only source of one; with an accumulator there are two candidates it
cannot distinguish, and keying a lookup on the delta finds an item whose
number is something else. Refused where a `known` map makes the choice
matter.

## Alternatives rejected

**`tag_identity = previous + delta`**, with no named block. Smaller --
no new argument, and `tag_identity`'s slot generalises the way
`value_rule`'s just did. Refused because the value then has no name: the
item struct needs a field, the generated code needs to print it, and
`identity` as a field name says what it is for rather than what it is.
A schema reads better for naming the thing it carries.

**A general layout accumulator, usable outside a `tlv` region.** The
honest generalisation, and the one to refuse first: nothing else in the
language has asked for it, and a recurrence available to array sizes and
offsets would put an unbounded quantity where section 10 promises a
total, decidable expression language. A `tlv` item's identity is the
case that exists; the general one can be designed when a second case
does.

**Computing the number on demand from the region's start.** No carried
state and no signature change, at O(n) per item and O(n^2) per walk --
and it duplicates the walk in a second place, which is how two readers
of one format drift.

## Consequences

CoAP's option header is expressible, number and length both. It is not
yet a worked example: `tag_type = u8` is read as a leb128 varint by
every backend, so a tag byte with its high bit set consumes the byte
after it and a delta above 7 is misread. That is a defect of the tag
reader rather than of this construct, it predates both halves of this
work, and it is recorded in 26.664 with its reproduction.

The differential gained a probe. `Probe.IDENTITIES` prints every item's
identity in order, asked only of a region that carries one -- because a
count cannot see an accumulator: a backend that seeds it wrong, or
carries the delta instead of the sum, answers every count correctly.
Sabotaging the Rust seed and the Python carry each makes the four
disagree, and both were seen to.

**A second probe about one member is not a second question until the
label says so**, which the gate found rather than the author. Every
comparison of these outputs keys one line per member -- `_by_member` in
the walker suite, and the write replay through it -- so the new line
replaced the count in C's dict, and the walker was reported as
disagreeing about a count neither side had got wrong. Two further
failures followed from that one: the write replay for the schema
declined, and the floor that counts contributing schemas dropped from 31
to 30. The label is `options:number` now, which no member's own name
collides with.

That shape is worth keeping separately from this construct: **an
invariant nobody wrote down -- one printed line per member -- was being
relied on by three separate checks**, and the probe that broke it was
green in its own test and in the four-way differential.
