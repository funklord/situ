# 0029: a checksum may cover its own bytes, given what they read as

Status: accepted
Date: 2026-08-03
Phase: 26.39

## Context

Section 14.2 refused a tag declared inside the region it covers, and the
diagnostic said why: "computing it would take its own bytes as input". That is
true of a cryptographic tag and false of the checksum family.

RFC 1071 defines the Internet checksum over the header *including* the checksum
field, with that field taken as zero while the sum runs. IPv4, ICMP, TCP and
UDP all carry one, and all four are worked examples in this repository. GPT's
header CRC is the same shape with the same filler; a tar header's octal sum is
the same shape with spaces.

Four askers, where invariant 31 asks for two, and the refusal was the only
thing standing between `example/ipv4` and describing its own header.

## Decision

**`[self_as = N]` on a tag.** The tag may then sit inside the region it covers,
and `N` is what its own bytes read as while the algorithm runs.

It carries a value rather than being a flag because tar's is not zero.

Without it, a tag inside its own coverage stays the error it was, and the
diagnostic now names the attribute as the way out. With it and *without* being
inside its own coverage, the attribute is an error too: it would read as a
claim about how the checksum is computed, and invariant 12 says a declared
property that cannot arise is worse than silence.

## What the compiler emits

Unchanged in kind. The algorithm is the caller's -- a signature says what a
transform does, never how (13.1) -- so what is generated is what only the
compiler knows, and there are now two such things rather than one:

  - `X_covered()`, the span the algorithm runs over, and
  - `X_self_span()` with `X_SELF_AS`, where inside that span the tag's own
    bytes are and what they read as instead.

The bytes stay in the buffer. Generated code never allocates (invariant 4), so
there is no copy-with-a-hole to hand out, and substituting the filler is the
caller's loop -- three lines of it, in `test/generated/test_icmp.c`.

**A tag does not cover itself.** Coverage means "writing these bytes leaves
that tag stale", which is false of the bytes the tag is written *into*: it
would tell a caller that computing the checksum invalidates the checksum.

## Alternatives considered

**A `checksum_zeroed` region kind.** A second region kind whose bytes the
algorithm skips. Rejected: coverage is over regions and the tag is a member, so
this would make the tag's own declaration name a region containing only itself
-- more syntax for the same fact, and it says nothing about tar's spaces.

**Compute it.** situ could grow an `internet_checksum` kernel and emit the
sum. Rejected here rather than on the merits: section 13.1 puts the algorithm
outside the compiler deliberately, and the whole point of the tag machinery is
that it works for algorithms situ has never heard of. A kernel would be a
separate decision about the codec set, not about coverage.

**Skip the bytes rather than substitute them.** Handing out two spans -- before
the tag and after it -- would suit a one's-complement sum and break a CRC,
where the zeroed bytes are shifted through and change the answer. Substitution
is what both specifications actually say.
