# 0037: a transform covering authenticated bytes says which order

Status: accepted
Date: 2026-08-18
Phase: after 13.2b

## Context

`covers(...)` (14.1a) lets a `coded` region transform bytes outside itself, and
13.2b gave it an ABI that can reach spans with a gap between them. Neither said
what happens when those bytes are also covered by a tag.

The generated code answered by omission: applying the transform marked nothing
dirty, so a tag covering the transformed bytes stayed clean whether or not it
still matched them. That is a decision, made silently, and 14.8 recorded it as
open rather than let it stand.

Two orders are coherent:

- The tag is computed over the untransformed bytes and the transform is applied
  afterwards. QUIC's header protection works this way -- the AEAD's associated
  data is the *unprotected* header, and the mask goes on last.
- The transform runs first and the tag covers its output, so the tag
  authenticates exactly what a peer reads off the wire.

Both terminate. Both are used. Neither follows from the structure.

## Decision

**The schema says, with `[tag_order = after]` or `[tag_order = before]` on the
`coded` region, and it is an error to leave it unsaid where a tag covers what
the transform reaches.** It is equally an error to state it where no tag is
involved, since the attribute would decide nothing.

`after` means the tag covers the transform's input; `before` means it covers
the output.

**The attribute decides the generated signature**, which is what keeps it from
being documentation:

- `after` -- the accessor takes a view. Applying the transform does not
  invalidate a tag computed over the untransformed bytes.
- `before` -- the accessor takes the message as well and marks the covering
  tags dirty on success. There is no way to call it without somewhere to record
  the staleness.

The bit is set only on `SITU_OK`: a codec that refused its input has not
changed the bytes.

**Situ does not run the sequence.** It says which order is required and makes
the wrong one visible. Performing it is the caller's, per 13.1.

## Why this is 17.0's case and 0011's is not

Decision 0011 has situ choose an order with nobody being asked: an outer tag
covers an inner tag's own bytes, so innermost-first is the only order that
terminates. There is no choice to express, and deciding a data dependency is
not the same as knowing an algorithm -- 13.1's rule is about the latter, which
is why 0011 does not violate it.

Here both orders terminate and both are real. There is no answer to derive, so
17.0 governs instead: the schema resolves the ambiguity explicitly, because the
wrong choice is undetectable at run time. Both orders produce a message of the
same length with the same fields at the same offsets, and a peer that disagrees
reports a failed tag -- which points at the key, the nonce, or the data long
before it points at the order two constructs were applied in.

## Alternatives considered

**Default to `after` because QUIC does.** One protocol's convention promoted to
a language default, in the exact place 17.0 forbids it: silent, and wrong in a
way that surfaces as a tag failure somewhere else. It also reads as though
situ knew something about the protocol that it does not.

**Always mark the tag dirty.** Safe-looking and wrong for `after`, which is the
common case: the tag genuinely still matches the bytes it was computed over,
and a message that refused to be transmittable until a correct tag was
recomputed would be a false alarm the caller learns to route around.

**Never mark it, and document the hazard.** What the code did before this. The
hazard is real and a comment does not carry it: under `before` the tag is stale
the moment the transform runs, and nothing said so.

**Infer it from the codec's properties.** `authenticated` on a codec says the
transform authenticates, not where it sits relative to somebody else's tag. No
property in 13.2 carries the answer, and adding one would be spelling the same
choice in a place further from where it is made.

## Consequences

- A schema whose transform touches authenticated bytes states the order or does
  not compile. `test/schema/edges.situ` carries neither case, its masking
  regions having no tag; the unit tests carry both.
- Under `before` the accessor's signature changes, which is a source change for
  anyone who had one. Nothing in the tree had: the clause is days old.
- The three backends that call the codec carry the order in the signature.
  Python, which does not call it (decision 0017), says it in the note -- and
  needs it more, since a Python caller sequencing by hand has no signature to
  remind them.
