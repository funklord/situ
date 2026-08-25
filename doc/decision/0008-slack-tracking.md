# 0008: Slack needs no field in the view; the limit already carries it

Status: accepted
Date: 2026-07-27
Phase: 5

Resolves project.md open question 4, which section 27 assigns to this phase.

## Context

`InPlaceSlack` sits between `InPlaceFixed` and `Shifting` on the mutate axis: the
write lands in place when the new value fits the extent already there, and
shifts everything after it when it does not.

Open question 4 observes that this implies the runtime knows a region's
allocated capacity separately from its current size, and asks where that belongs
in the view struct.

Nothing in the static subset produces `InPlaceSlack`. It first becomes reachable
in phase 6, where a varint whose new value encodes to the same length stays in
place (section 8.1.1) and a TLV region accepts an append when slack exists
(section 9.5), and again in phase 7 for a block-granular codec (section 13.5).

The decision is due now anyway, because the view struct is ABI. Adding a field
to it later is a breaking change for every schema already compiled against it.

## Decision

No field is added. A view is `{ base, limit, generation }` and stays that way.

`limit` **is** the capacity. It is established once, at acquisition, from
whatever extent the schema says the region can hold. The *used* extent is not a
second stored number: it is read from the data, either from the length field
that drives the region or by walking it.

So slack is `view.limit - used`, and both terms are already available:

```c
situ_err_t err = situ_Message_opts_view(view, &opts);   /* limit = capacity */
uint32_t   used = situ_Message_opts_len(view);          /* read from the data */
/* room to grow = opts.limit - used */
```

`situ_in_bounds(view, offset, extent)` is already the check a grow-in-place
needs, and it is already in the runtime.

## The consequence that does need care

A sub-view of a variable-length region must be acquired with the region's
**maximum** extent, not its current one. Acquire it at the current length and a
grow-in-place fails its own bounds check immediately, which would make
`InPlaceSlack` unreachable in practice while looking correct in the map.

Where a region has no declared maximum, its capacity is whatever remains in the
enclosing frame, which is `enclosing.limit - offset`. That is the honest answer:
an unbounded region's slack genuinely is "whatever is left".

Phase 6 must generate region sub-views on that basis. Recorded here rather than
discovered there.

## Why not a capacity field

**It would cost every view.** Views are passed by value on targets where four
bytes per call matter, and the field would be dead weight for every fixed-size
struct in every schema -- which is most of them.

**It would be a second source of truth.** Capacity would then live both in
`limit` and in `capacity`, and any accessor that set one without the other would
produce a silent bounds bug. Section 25's single-source-of-truth rule applies to
runtime state as much as to the AST.

**It does not actually answer the question.** The value a mutation needs is
`used`, not `capacity`, and `used` has to come from the data whatever the view
carries: it is a property of the message, not of the handle.

## Alternatives considered

**A `capacity` field alongside `limit`.** The literal reading of the open
question. Rejected as above.

**A distinct slack-bearing view type for variable-length regions.** Keeps fixed
structs paying nothing. Rejected: it doubles the accessor surface for regions
that the `limit` already describes, and section 12.2 is explicit that the
generated accessor type must be identical whether a struct sits at a static or
dynamic position. Two view families is exactly what that warns against.

**Defer to phase 6.** Rejected: the view struct is ABI, so this has to be
settled before anything is compiled against it, which is what makes it a phase 5
question rather than a phase 6 one.
