# 0043: pad_to(n) aligns the offset, and is padding in the map

Status: accepted
Date: 2026-08-26
Phase: after 0042

## Context

Section 8.4 has specified `pad_to(n)` since it was written -- "inserts
explicit padding to the next multiple of n" -- and the parser refuses it.
`suggestion/hydra.md` asks for it from the consumer side: every `base::Pickle`
writes four-byte padding after a variable run, and today it is spelled

    reserved u8 [align_up(length, 4) - length];

which repeats its own subexpression, and which the capability map labels
`<reserved0>` rather than padding. cpio, TLV framings and most RPC wire
formats pad the same way.

The construct is specified, so this record is not about whether to build it.
It is about the one thing 8.4's sentence leaves open, and it is a wire-visible
choice.

## The fork

`align_up(length, 4) - length` pads so the *run's own length* is a multiple of
four. "To the next multiple of n" reads as aligning the *absolute offset from
the message base* to a multiple of n. These are the same number exactly when
the run begins at an n-aligned offset -- always true in `base::Pickle`, where
every field there is itself padded to four, so the consumer's idiom and
8.4's wording have never had to disagree.

They disagree the moment a run does not start aligned: a two-byte header before
a run makes the run-local padding and the absolute-alignment padding differ by
two. A schema author writing `pad_to(4)` means one of them, and the wrong one
is a silent interoperability break -- the padding lands, the bytes parse, and a
peer computing the other alignment reads the following field two bytes off.

## Decision

**`pad_to(n)` aligns the offset to the next multiple of n**, measured from
the containing struct's view base -- see the amendment below, which narrows
"the message base" as first written. It is a member, written `pad_to(n);` where a field would go,
and it occupies `align_up(offset, n) - offset` bytes -- a constant where the
offset is static, a length expression over the preceding length fields where it
is not, computed by the solver the same way every dynamic offset already is.

Three parts:

- **Absolute, not run-local, because 8.4 says "the next multiple of n" and
  because that is what alignment means everywhere
  else** -- `[require_aligned]`, the `align` axis, the whole section.
  A run-local reading would make one word mean alignment in four places and
  something else in the fifth. Where an
  author wants the run's own length padded and the run does not start aligned,
  that is a different intent and spelled with the explicit `reserved`
  expression, which stays legal.
- **The padding is `must_be_zero` by default**, and canonical padding is the
  only kind `require canonical` accepts (8.4 and 14.5): a sender that varies
  padding bytes is varying bytes the format calls fixed, which is the
  malleability surface 8.8 is about. `pad_to(n) [preserve]` is the escape for a
  format that genuinely carries data there, the same escape a `reserved` member
  has.
- **The map and wire signature name it padding.** `pad_to(4)` on the map is a
  `padding` row with its alignment, not `<reserved0>`; on the wire signature it
  is `pad-to=4`, because the number is part of the contract -- a peer that pads
  to 8 where the schema says 4 disagrees about where the next field starts.

## Alternatives considered

**Run-local: pad the preceding run's own length to a multiple.** Matches the
consumer's current idiom byte-for-byte and needs no absolute offset. Rejected
because it makes `pad_to` mean something other than alignment in the one place
the word is not already defined, and because the absolute reading is a superset
-- it produces the same bytes wherever the idiom is used in practice, and the
right bytes where the idiom would have been subtly wrong.

**An attribute on the run, `[pad_to = 4]`, rather than a member.** Reads as a
property of the field, but padding is bytes that follow the field and belong to
no field -- a member is what they are. It also cannot express padding after a
fixed run of fixed members, where there is no single field to attach to.

**Leave it as the `reserved` expression and only teach the map to recognise
it.** Pattern-matching `align_up(x, n) - x` in the map is fragile -- it fires
on a coincidence and misses a spelling -- and it leaves the subexpression
repeated in every schema, which is the readability cost the ask is actually
about.

## Consequences

- Parser: `pad_to` becomes a member keyword; the layout solver emits its size
  as it does any dynamic extent.
- Four backends emit the size computation and the zero-check in `validate`; the
  walkers advance by it. Same surface as `[size = N]` (0039).
- Map and wire signature gain the `padding` row and the `pad-to=` fact.
- `example/` gains the construct where a format pads -- a Pickle-shaped record
  is the worked case, and cpio already has the shape by hand.
- 8.4's specified-but-unbuilt construct ships; the traffic-analysis
  `pad_random` beside it in 14.7 stays deferred, being a different thing (a
  variable pad to hide length, not a fixed pad to align).

## Amendment, 2026-09-04: the base is the containing struct's, not the message's

Everything above stands except one word. The fork this record was written to
settle -- run-local against absolute -- is decided the way it says, and for
the reason it gives: `pad_to` aligns *an offset*, not a run's own length, so
the word means what it means in `[require_aligned]` and in the `align` axis.
That is the decision and it does not move.

What the record left loose is the base that offset is measured from, and it
says "the message base". **It is the containing struct's view base.** For a
top-level struct those are the same byte, which is why nothing noticed:
`pickle_string`, `aligned_header` and `byte_run` are the tree's three
pad-bearing structs and all three are top-level, so no schema has ever put
the two readings in the situation this record exists to separate (26.202).

**The layout model settles it, and not as a preference.** A struct's extent
is a function of the struct alone -- `SIZE_FIXED`, `size(inner)` and every
`situ_X_view(msg, offset)` depend on it. Measured: `struct inner { u8 n; u8
data[n]; pad_to(4); u16 trailer; }` solves to the same 24..2088 bits at
offset 0 and at offset 16. Under the message-base reading it could not, since
`inner` at an odd offset would pad differently and its size would vary with
where somebody nested it. `SIZE_FIXED` would be a lie for any struct
containing padding, and `size(inner)` would stop being a property of `inner`.

So the three implementations do not agree by accident. `walker/walk.py`,
the generated C and the Lua dissector all align from the view they were
handed, because that is the only base a struct's own arithmetic has.

**The interoperability hazard this record opens with is unchanged, and the
answer to it is composition.** A schema author writing `pad_to(4)` inside a
struct someone else nests at an odd offset gets padding computed from a base
they can see -- their own -- rather than from one they cannot. A format that
truly aligns to the *message* aligns the nested struct itself, with
`[require_aligned]` on the member that holds it, which is the axis for saying
so and which a reader can check. Two mechanisms, each naming its own base,
rather than one whose meaning depends on where it is used.
