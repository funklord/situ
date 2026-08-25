# 0040: a sealed region names the field that selects its key

Status: accepted
Date: 2026-08-25
Phase: after 0038

## Context

Every protocol that outlives one key carries a field that says which key a
message is under: DTLS a 16-bit epoch, QUIC a key-phase bit, WireGuard a
32-bit receiver index. 14.8 lists key selection first among the constructs
real protocols need and situ cannot express -- "zero occurrences of epoch,
key phase or key id in this document" -- and 0038 unblocked it without
settling it.

What the construct has to be is sized by what `nonce = field` turned out to
be (26.127): an argument consumed entirely by `wellformed` -- no backend, no
capability map, no wire signature reads it -- whose value is its checks and
its sayability. Key selection is the same shape. Situ never holds key
material (0038 refused `key_bits` for exactly that reason), so selecting a
key is not a computation the generated code performs; it is a fact about the
format that today cannot be written down, checked for ordering, or shown to
a peer.

**And one anticipated payoff is thinner than 26.127 assumed, which belongs
in the record rather than discovered during implementation.** That entry
expected key selection to relax the nonce-reuse refusal -- "regions under
provably distinct keys are the case that loosens this check". Within one
message it is nearly empty: two sealed regions naming the *same* selector
field see the same value, so the same key, and the refusal must hold; two
regions naming *different* selector fields still cannot be proven to select
different keys, because two fields can hold one value. Distinctness of key
is not a structural fact, and the refusal stays unconditional. The rekey
case the relaxation imagined -- old-key and new-key data in one datagram --
is real in DTLS and it is two *messages* in one datagram, which situ frames
as two structs already.

## Decision

**`sealed(codec, nonce = n, key = epoch)` names the field whose value
selects the region's key, and the same rules that hold the nonce hold it.**

Three parts:

**1. The ordering check, for the same reason the nonce has one.** The
selector must name a field declared before the region: a decoder cannot pick
the key after decoding what the key decrypts. Same rule, same check
(`check_nonce_references` becomes the home of both), same diagnostic shape.

**2. The selector is a value.** An integer-domain field -- the same test
`declared_value_bounds` applies (26.125): unsigned, signed, or a bit run. A
key-phase *bit* is the QUIC case and must pass; a delimited byte run has no
value and must not (26.113's rule, already enforced for run conditions).

**3. Sayable now, recorded later.** Like the nonce argument, the selector
reaches no backend and no committed artifact yet. Whether both should
appear in the capability map and wire signature -- a peer genuinely needs
to know which field selects the key, which is a stronger case than most
recorded facts have -- is the question 26.117 left open for the wire
signature generally, and it stays answered-once rather than answered here.

**What this does not do, stated so the absence is a decision.** No key
material, no key schedule, no ABI change: the caller who implements the
codec already reads any field through the generated accessors, the selector
included. No coverage requirement on the selector: DTLS authenticates its
epoch and QUIC its key-phase bit, but WireGuard's receiver index is
deliberately outside the AEAD -- it routes, it is not trusted -- so
requiring coverage would refuse a real protocol. An uncovered selector is
the advisor's to point at, not an error.

## Alternatives considered

**An attribute on the field (`u16 epoch [key]`).** The shape `[nonce]` had,
and 26.60 removed: an attribute pointing at nothing, read by nothing, while
the argument on the region does the work. The association belongs on the
region because a selector may serve one region and not another.

**Relaxing the nonce-reuse refusal for distinct selectors.** See context:
distinctness of key is not provable from distinctness of field, and the
same field trivially selects the same key. Nothing relaxes.

**Requiring the selector inside tag coverage.** Right for DTLS and QUIC,
refuses WireGuard. A layout language can record where the selector sits;
whether an unauthenticated selector is acceptable is the protocol's threat
model, not its layout.

**Waiting for conversation keys (26.95) and doing both at once.** The
receiver index is genuinely both -- it selects the key *and* identifies the
conversation -- but the conversation machinery caps keys at 64 bits and has
its own open record. Naming the selector now costs nothing there; a later
record may declare `key = field` on a region and a conversation key the
same field without conflict.

## Consequences

- `check_nonce_references` grows the parallel checks for `key`; the
  nonce-reuse refusal is untouched.
- `example/` gains the construct where a protocol carries it -- a DTLS
  record header is the natural worked example, being the smallest of the
  three.
- 14.8's first unexpressible construct becomes expressible; key width
  (0038's deliberate omission) remains out until a construct needs it, and
  this is not that construct -- a selector's width is the field's own.
