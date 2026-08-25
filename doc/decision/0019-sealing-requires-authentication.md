# 0019: what a codec must be before it may seal

Status: accepted
Date: 2026-07-28
Phase: resolves open questions 11 and 12

## Context

Two of section 27's open questions turned out to be the same question asked
from different ends, and answering either without the other leaves a hole.

**Question 11** asked whether `[secret]`'s ban on data-dependent access
(section 14.6) extends into *generated* codec implementations, and whether a
constant-time Reed-Solomon is realistic. It does extend, obviously: a table
indexed by plaintext leaks that plaintext through the cache whether the table
was written by hand or emitted by situ, and the emitted one is table-driven by
construction. Whether a constant-time RS is realistic is beside the point --
situ does not generate one, and the question is what to do about that.

**Question 12** asked how a systematic FEC block and a `sealed` region compose,
and answered itself: both orders must be expressible and the order must never
be inferred. Pipelines already do that -- `a |> b` says which runs first -- so
nothing was outstanding there.

What was outstanding, and what neither question named, is that `sealed(C)`
accepted any codec at all. `sealed(crc32)` built without complaint. A CRC
authenticates nothing, so the stage gate would hand out the interior on a
`verified` flag that nothing had checked: the ceremony of section 14.3 with
none of its substance, and a type carrying a promise the cryptography never
made.

## Decision

Two refusals, at the point a region names its codec.

**A codec that does not declare `authenticated` may not seal.** `sealed` means
"this region is verified before it is readable"; a codec with no tag cannot
make that true. `coded(C)` remains for a transform that genuinely does not
authenticate, and the diagnostic names it.

**A codec with a `derived` implementation may not seal.** Situ generates
table-driven code, and over the plaintext of a sealed region that is a
cache-timing channel. Sealing takes a tier-1 `extern` implementation, where the
timing properties are the supplier's to state and situ's only job is to say
which bytes go in.

That second one is the answer to question 11 in full: **the obligation extends,
situ cannot discharge it, so situ refuses rather than pretending.** It is the
same move decision 0017 makes about codecs generally -- one implementation, and
where situ cannot be the right one, it says so.

## Consequences

`std/kernels.situ` may not seal, and should not: every codec in it is derived.
The standard library's AEADs live in `std/codecs.situ` as tier-1 signatures
with extern implementations, which is exactly where a sealing codec belongs.

A schema wanting a sealed region gets a diagnostic naming the two ways out --
declare the codec authenticated if it truly is, or bind an extern
implementation -- rather than discovering at review time that its gate checked
nothing.

The refusal is at the schema level rather than the backend level, so all four
backends inherit it without knowing about it.

## Alternatives considered

**Warn rather than refuse.** A warning about a gate that verifies nothing is a
warning about a security property, and section 14.5's position is that a
construct whose meaning is silently nothing is refused. A gate that checks
nothing is worse than no gate, because the type system tells a reader it is
safe.

**Allow derived codecs to seal, and mark the region non-constant-time in the
capability map.** Attractive -- it is situ's usual move, cost the thing and
state it. Rejected because the axis does not exist and inventing one for this
would be inventing a security claim situ cannot verify: a `constant_time` axis
would be trusted declaration all the way down, which is what tier 1 already is.
Better to route the case to tier 1 than to grow an axis that only ever carries
somebody's word.

**Require `[secret]` before refusing.** Narrower, and wrong in the direction
that matters: a sealed region's plaintext is secret whether or not a field
inside it carries the attribute, because sealing is what makes it secret.
