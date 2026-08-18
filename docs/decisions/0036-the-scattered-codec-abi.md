# 0036: a second tier-1 ABI, in place and over a list of spans

Status: accepted
Date: 2026-08-18
Phase: after 14.1a

## Context

`covers(...)` on a `coded` region (14.1a) says a transform runs over spans
outside the region. It was added expressible and checkable but only partly
emittable: where the covered spans were adjacent the generated accessor
widened the pointer and length it already had, and where they were not, all
four backends refused with a comment saying why.

The refusal was honest and the reason was the ABI. 13.2a hands an
implementation one pointer and one length, so a transform over spans with
something uncovered between them has no single range to be given. Neither way
round it was acceptable:

- **Gather the spans into a temporary.** Copies exactly what a zero-copy
  accessor exists to avoid, and 13.2a has nowhere to say the result must be
  scattered back.
- **Call the codec once per span.** A different operation. A mask derived per
  call is not the mask derived once and spread across the spans, and the
  schema does not say which was meant.

The case that wanted it is QUIC header protection, which is the reason the
clause exists at all: a mask over the first byte and the packet number, with
the connection id between them.

## Decision

A second pair, reached by a region whose `covers` clause is non-empty:

```c
situ_err_t <symbol>_encode_spans(const situ_span_t *spans, uint32_t count);
situ_err_t <symbol>_decode_spans(const situ_span_t *spans, uint32_t count);
```

- **In place, so no output buffer.** Confined to length-preserving codecs,
  which 14.1a already requires of a `covers` clause. In place is meaningful
  only where the answer is the same size as the question.
- **Every run in one call.** Handing the codec the whole list is the only
  reading that does not guess between the two operations above.
- **An addition, not a replacement.** A region with no clause keeps 13.2a, so
  no existing implementation breaks. The two shapes answer different
  questions: 13.2a is out-of-place and may change length, which `stuffing`
  needs and an in-place pair cannot express; this is in-place and scattered,
  which a mask needs and a single pointer cannot express.
- **The clause decides, not the layout.** A contiguous coverage comes here as
  a list of one span rather than being routed to 13.2a for being adjacent.
- **Adjacent covered members are merged**, so `count` is the number of
  separated pieces rather than of names written down.
- **`situ_span_t` is `{ uint8_t *base; uint32_t len; }`**, mutable because the
  pair works in place. Rust emits a `#[repr(C)]` equivalent, since C is what
  reads it and the layout is the contract.
- **Both directions are emitted.** The contiguous form needs only a decode
  accessor because the plaintext lives in the caller's buffer; an in-place
  transform has to be reversible from the same side.

## Alternatives considered

**Replace 13.2a with a span list.** One ABI rather than two, and wrong twice
over. It breaks every implementation written against a contract this project
published and documented, for no gain to the codecs that were happy with it.
And it does not actually serve the scattered case: gathering from a span list
into a contiguous output still leaves the masked bytes needing to be written
back to the positions they came from, so the copy the widening was meant to
avoid reappears at the other end.

**Spans in and spans out.** General, and unimplementable for a length-changing
codec: the output span layout is not knowable before the transform runs.

**Route contiguous coverage through 13.2a and split coverage through the new
pair.** Fewer span lists of length one. Rejected because it makes the required
symbol depend on the layout rather than on the schema's own words: inserting a
field between two covered spans would change which function an implementation
must provide, discovered as an undefined symbol at link time.

**Leave it refused and wait for a protocol to force the question.** What the
refusal already was. The clause exists because header protection exists; a
vocabulary that can describe a construct and not generate it is half a
feature, and the half that was missing is the one that runs.

## Consequences

- QUIC's header protection shape is emittable. `tests/schemas/edges.situ`
  carries both a contiguous coverage, which merges to one span, and a split
  one whose uncovered `cid` sits in the gap and is left alone.
- `tests/generated/codec_impl.c` implements the scattered pair, so the
  accessors have something to call and the generated tests run against a real
  implementation rather than asserting about an absent one.
- A tier-1 codec used with a `covers` clause needs two more functions. Nothing
  in the tree was using one, the feature being days old, so no implementation
  had to change.
- Situ still does not sequence the transform against a tag covering the same
  bytes. That is the protocol's, per 13.1, and is recorded as open in 14.8.
