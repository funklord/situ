# 0038: a codec declares the sizes it produces and requires

Status: accepted
Date: 2026-08-21
Phase: after 14.1a

## Context

`example/packet` declares an authenticated region and its tag:

    codec aes_gcm_128 {
        authenticated;
        length_preserving;          // ciphertext core; tag declared separately
        ...
    }

    sealed(aes_gcm_128, nonce = nonce) { ... }
    tag u8[16];

Nothing connects the `16` to `aes_gcm_128`. The codec vocabulary is
`authenticated`, `deterministic`, `error_propagating`, `expansion`,
`granularity`, `invertible`, `kernel`, `length_preserving`, `seekability`,
`seekable` and `systematic` -- every one about *shape*, and the only one that
carries a number answers a different question.

**`expansion` is not the tag width, and the motivating case is exactly where
they part.** It says how a region's own length changes under the transform.
Where a codec appends its overhead, `expansion = +16` does encode the tag;
where the tag is a separate field, as here, the codec is `length_preserving`,
expansion is zero, and it says nothing at all about the sixteen bytes beside
it. An inference from expansion would be right for one spelling and silently
wrong for the other.

So `tag u8[1]` compiles, and so does `tag u8[64]`. A deliberate truncation and
a typo are the same text -- which matters because truncation is legitimate:
OSCORE uses an eight-byte tag on constrained links, so a rule that banned
narrow tags would ban a real protocol rather than a mistake.

The same hole exists for the nonce. `sealed(codec, nonce = field)` names the
field and the field has a width, and nothing says whether that width is the one
the primitive wants.

## Decision

**A codec may declare `tag_bytes` and `nonce_bytes`, and where both sides speak
they must agree.**

Three parts, and the third is a scope cut rather than a feature.

**1. Both settings are optional.** Silence claims nothing, which is already the
rule the codec accumulator states: "a signature that says nothing claims
nothing, which is the only safe reading of silence in a declaration the
compiler cannot verify". An extern codec's implementation belongs to somebody
else, and an author who does not know its tag width must still be able to
declare it.

**2. Where a codec states a size and a schema states the matching field, they
are checked:**

- A tag field **wider** than `tag_bytes` is an error, with no exemption. No
  spelling gets more authentication out of a primitive than it produces, so
  there is nothing an author could mean by it.
- A tag field **narrower** is an error *unless* it carries `[truncated]`. That
  is what makes OSCORE sayable and keeps a typo an error: the attribute is the
  author stating that the loss is intended.
- A nonce field whose width differs from `nonce_bytes` is an error, with no
  exemption either way. A nonce is an input rather than a result, and a
  truncated one is simply a different nonce.
- Where the codec is silent, nothing is checked and the schema is no worse off
  than today.

`[truncated]` belongs on a tag field and nowhere else, and says so in the
placement table (26.117) rather than being left for a later sweep to discover.

**3. `key_bits` is deliberately not added.** No construct in this language
names a key: a key is out-of-band by construction, so a declared key width
would have nothing in any schema to check against. That is precisely the shape
`[nonce]`, `[trusted]` and `[covers]` had -- vocabulary read by nothing, which
26.60 and 26.117 spent two passes removing. The width goes in when the
construct that gives it something to check does, and not before.

**Units are named rather than implied.** `tag_bytes` and `nonce_bytes`, not
`tag` and `nonce`. `expansion = +16` carries no unit and gets away with it
because expansion is only ever bytes; a tag and a nonce are bytes while a key
is conventionally bits, so the moment a third setting arrives two units are in
play. A unit in the name costs less than a unit in a comment, and cannot drift
from it.

## What this does not settle

**Key selection and conversation keys wider than 64 bits.** 14.8 names both --
DTLS's 16-bit epoch, QUIC's key-phase bit, WireGuard's receiver index, and
`KEY_BITS` being too narrow for a 32-byte session identifier. Neither follows
from this record and neither is blocked by it any longer.

**Whether the widths reach the capability map and the wire signature.** They
are enforced facts under this decision rather than recorded ones, so recording
them is safe -- but 26.117 left open whether an entry in the wire signature
that nothing enforces should count as placement at all, and that question is
adjacent enough to answer once rather than twice.

## Alternatives considered

**Require every codec to declare both.** Breaks every existing declaration and
forces an author to state what they may not know. It also converts an
unverifiable claim into a mandatory one, which is the opposite of the
accumulator's rule about silence.

**Refuse a narrow tag outright.** Simple, and it bans OSCORE. The whole
difficulty here is that truncation is a real design choice rather than an
error, which is why it needs to be *sayable* rather than prevented.

**Express the permitted range on the codec, as `tag_bytes = 8..16`.** Puts one
schema's decision inside the shared primitive: `std/codecs.situ` is written
once and consumed by every schema, and a range there says every consumer may
truncate. It also makes the common case indistinguishable from the permissive
one.

**Infer the tag width from `expansion`.** Wrong for the case that motivates
the record, as above: `packet`'s codec is length-preserving and its tag is a
separate field.

**Leave it, and document the hazard in 14.8.** What is there now. The section
already says a declared `tag u8[8]` cannot be compared against what the codec
produces; a document saying a check is absent is not a check.

## Consequences

- `std/codecs.situ` gains `tag_bytes` and `nonce_bytes` on its authenticated
  codecs, which is where the widths are actually known.
- `example/packet` gains a real check on its `tag u8[16]`, and is the worked
  example the record is written from.
- A schema that truncates a tag says so in one word, and one that truncates by
  accident stops compiling.
- The three constructs 14.8 lists as unexpressible are unblocked but not
  addressed; each needs its own record.
