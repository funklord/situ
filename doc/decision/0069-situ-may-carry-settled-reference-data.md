# 0069: Situ may carry settled reference data, not only format descriptions

Status: proposed -- raised by the copyright holder 2026-11-03, and it needs a
heavy discussion before anything is designed or built
Date: 2026-11-03

## Context

The holder's observation, in their terms: in the same way situ ships codecs
and encodings, it could contain **a certain amount of very settled and
authoritative lists** -- country-code lookups, timezones, or similar. The
value named is the update path: **a situ-based protocol could then evolve by
a situ update rather than requiring every user to update their software all
the time.**

The analogy is situ's own. `std/kernels.situ` and `std/codecs.situ` are
already shipped library content rather than descriptions of somebody else's
format, so there is a precedent for situ carrying standard material and not
only a language for describing it.

## What this record is for

**Nothing here is decided and nothing is designed.** The holder asked that
it be marked for later and discussed heavily first, so this record exists to
hold the idea where it will be found rather than to propose a mechanism. A
mechanism described in detail becomes the obvious answer by weight of
description alone, which is exactly what a discussion-first instruction is
asking to avoid.

## Questions a discussion would have to settle, not answered here

- **Section 2 is where the non-goals live**, and this touches the line
  between describing a format and carrying data. Whether that line moves,
  and where to, is the first question and it is the holder's.
- **"Very settled" is doing a great deal of work and the examples differ
  sharply.** ISO 3166 country codes change rarely and by committee;
  **tzdata changes several times a year** and already has a canonical
  distribution channel that operating systems track. A list's volatility and
  whether somebody else already distributes it are different questions from
  whether situ *could* carry it.
- **The update path is the value and also the exposure.** If a protocol's
  consumers get new data by updating situ, then situ's release cadence
  becomes the cadence at which the world's changes reach them -- which is a
  commitment about releases rather than about code.
- **What a list IS, in situ's terms.** An enum, a table, a lookup a codec
  reads, something a schema imports -- each has different consequences for
  the capability map, for `situc wire`'s contract, and for what a generated
  accessor can promise. This is the part most worth not guessing at.

## Whose

The holder's, explicitly and on their own instruction. Recorded rather than
acted on.
