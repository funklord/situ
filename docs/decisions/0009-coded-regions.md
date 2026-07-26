# 0009: `coded(C) { ... }` is the general transform region

Status: accepted
Date: 2026-07-27
Phase: 7

## Context

Section 13.5 defines propagation "given a region `R` with codec `C` and interior
schema `S`", and section 26.7 makes implementing that table part of phase 7.

No construct in the grammar attaches a codec to a region. The only thing close
is `sealed(codec, nonce = ref) { ... }`, and that is listed under section 14.1
as part of the cryptographic model, which section 26.8 assigns to phase 8.

So the table phase 7 must implement describes a construct phase 8 owns. Phase 7
cannot be tested as specified -- its acceptance criteria talk about a
length-preserving seekable codec yielding in-place interior mutation, verified
in the generated API surface -- without some way to say "this region is
transformed by this codec".

## Decision

`coded(C) { ... }` is the general form: a block of members transformed by a
codec, with the codec's properties deciding what the interior keeps.

```situ
coded body(aes_ctr_128) {
    u16 inner_kind;
    u32 inner_seq;
}
```

`sealed` then becomes `coded` plus authentication, which is the relationship
section 13 already implies: a transform over a region is the general thing and
encryption is one instance of it. Section 13.1 is explicit that the property
signature is the only interface to the lattice, and nothing in that signature is
cryptographic -- `authenticated` is one flag among nine.

Phase 8 adds `sealed` as `coded` with a tag and the `VerifyGated` stage. Nothing
in this decision makes that harder: the propagation rows written here read
property signatures, so `sealed` contributes the authentication half and reuses
the transform half unchanged.

## Why not defer the table to phase 8

Because the table is the phase 7 deliverable, and because writing it against
`sealed` would tangle the transform rules with the crypto ones. Section 13.3's
decidability rule -- the compiler reasons only about property signatures, never
about transform semantics -- is much easier to hold when the construct under
test has no cryptographic meaning to be tempted by.

Testing it separately also caught something: the prohibition on referencing
transform output has to apply to *any* coded region, not only a sealed one. A
size expression that read a decoded value would make in-place mutability
undecidable whether or not the transform happened to be a cipher.

## Alternatives considered

**Implement `sealed` in phase 7 for its codec half.** Rejected: it implements
ahead of the phase plan, and section 0 rule 2 is explicit that a phase is done
when its criteria pass rather than when the code looks complete. It would also
leave `sealed` half-built across two phases, which is where the crypto model can
quietly acquire a gap.

**Attach a codec to an `opaque` region by attribute.** Reuses existing syntax
and needs no keyword. Rejected: section 13.5 propagates to an *interior schema*,
and an opaque region has none. The construct genuinely needed is a block of
members, not a byte range.

**Defer the whole table to phase 8.** Rejected: it leaves phase 7 with nothing
testable, and the property algebra is what phase 12 slots into. Getting it wrong
quietly for a phase would be expensive to unwind.

## Note for review

This introduces a keyword project.md does not name. Section 0 rule 3 permits
choosing surface syntax where a construct is marked OPEN, and this construct is
not marked at all -- it is simply absent -- so the choice is worth checking. If
`sealed` was meant to be the only transform region, the section 13.5 table needs
a note saying so and phase 7's acceptance criteria need rewording.
