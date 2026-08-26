# 0042: a conversation key is the bytes, at whatever width the relation says

Status: accepted
Date: 2026-08-26
Phase: after 0040

## Context

A relation's equality constraints identify an exchange, and the `converse`
and `drive` rungs match a response to its pending request by that identity.
`key_sides` packs the compared fields into one `uint64_t` -- `KEY_BITS = 64`
-- and refuses two things past that width: a key totalling more than 64
bits, and any `bytes_equal` part at all, each with the same argument spelled
two ways: "two exchanges that collided would be matched to each other", and
hashing "would make two exchanges that collided indistinguishable".

14.8 lists the consequence as the crypto model's last unexpressible
construct: TLS resumption matches on a 32-byte session id, QUIC on a
connection id up to 20 bytes, WireGuard and Noise identify a peer by a
32-byte static key. A tool that correlates on eight bytes cannot describe
the correlation those protocols perform, and "what a key should be when it
exceeds a word is a question about collisions rather than about codegen".

**The refusal's own reasoning is the design's centerpiece, kept rather than
overturned.** The module refuses hashing because a collision silently
matches one exchange to another -- a wrong pairing that no later check can
see, in the layer whose one job is pairing. That argument does not say the
key must be small; it says the key must be *exact*. Width was never the
principle. One word was the cheap representation, and the principle
survives any width that compares every byte.

## Decision

**A conversation key is the exact bytes of the fields the relation
compares, at the width the relation declares, up to 32 bytes.**

- **The language decides the width; backends spell the representation.**
  `relation.py` computes each relation's key layout at compile time: total
  width, and the reads that fill it. A key of 64 bits or fewer keeps
  today's packed `uint64_t` and today's generated code, byte for byte. A
  wider key becomes `uint8_t key[N]` in the slot and `memcmp` in the
  match, with `N` the relation's own total -- a compile-time constant, so
  the caller-allocated table stays statically sized and the cost sits in
  the type the caller already sizes ("the slots are yours and so is their
  number").
- **`bytes_equal` parts become admissible**, which is the half that
  unblocks the protocols: a 32-byte session id enters the key as the 32
  bytes the schema declared, compared exactly. The hashing refusal stays,
  now unreachable rather than merely enforced: there is no width at which
  the representation is a digest.
- **The ceiling is 32 bytes, and it is a named number, not a principle.**
  It covers every identifier 14.8 names -- TLS 32, Noise and WireGuard 32,
  QUIC at most 20 -- and it exists so a schema cannot draft a kilobyte of
  payload into the slot table. A protocol that outgrows it moves the
  number the way this record moves `KEY_BITS`: by its own record, with its
  own named protocol.
- **A variable-length part stays refused.** A key part must have a width
  the compiler can see -- a scalar, or a byte array of literal length.
  Two byte strings of different lengths sharing a prefix would compare
  equal in a length-blind memcmp or force a length into the key, and
  either choice is a design decision no protocol on the list requires:
  every named identifier is fixed-width where it is matched.

## Alternatives considered

**Hash wide keys into the existing word.** Refused by the module since the
rung was built, for the reason kept above: a collision is a silently wrong
pairing in the pairing layer. Nothing about a wider world changes it.

**Let the caller supply the key storage and the comparison.** Moves the
table out of the generated code -- but `drive` matches internally, so the
key representation is not separable from the rung that uses it, and a
caller-side key invites exactly the truncated-copy shortcuts this record
exists to rule out.

**No ceiling.** A slot table entry sized by whatever the schema says, with
nothing to stop a `[remaining]`-adjacent mistake making every slot a frame
buffer. The ceiling is cheap, covers the field, and moves by record.

**A digest with collision handling (chaining, verification on match).**
Correct in a hash table and wrong here: the slots are caller-allocated
flat storage on embedded targets, the verify step needs the original
bytes, and storing them to verify against is the wide key with extra
steps.

## Consequences

- `relation.py`: `key_sides` admits fixed-width `bytes_equal` parts,
  computes a per-relation representation (packed word or byte string), and
  the two `Refused` messages narrow to the cases that remain -- a
  variable-length part, and a total past 32 bytes.
- `converse` and `drive` in all four backends spell the byte-string case;
  existing packed-word relations regenerate byte-identically, which is the
  no-regression check.
- A worked example earns the construct a directory -- Noise-style
  peer-by-static-key being the smallest honest case of a 32-byte match --
  and the four-backend differential runs over it.
- 14.8's last unexpressible construct falls; the section's list empties.
