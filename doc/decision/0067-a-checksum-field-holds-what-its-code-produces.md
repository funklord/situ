# 0067: A checksum field holds what its code produces

Status: proposed
Date: 2026-10-22
Phase: 26.674

## Context

26.673 measured that a narrow `checksum` generates a check no message can
pass. `checksum u8 sum[1] covers(s) is crc32 [truncated]` passed all three
adopter gates and all four backends; the compiled C round trip computed
`0x6d1b11a6`, stored `0xa6`, and got `SITU_ERR_CHECKSUM` back on a message
the generated API had just written. The comparison is
`(uint32_t)situ_base(view)[crc_at] == want`, so the left side is at most
255 and no byte the caller could store makes it true. C++ and Rust emit the
same widening compare and Python the same thing through `int.from_bytes`.

0038 made truncation sayable, and its consequences are entirely about the
compiler check. It placed `[truncated]` "on a tag field and nowhere else",
where a tag field is both `tag` and `checksum` -- and codegen was never
revisited.

**Why the width check could not see it, which is two faults rather than
one.** It read the codec off the *covered region*, and only `sealed`
carries one: `ast.Authenticated` has no codec field at all, while a
checksum names its algorithm on the field, as `is crc32`. So the check
fired for exactly one configuration -- a tag over a sealed region, which is
0038's own worked example in `example/packet` -- and for no checksum
anywhere. And it read widths in bytes, through a helper answering `None`
when the width is not a whole number of bytes, so a `u7` field was skipped
as unmeasurable when seven bits is both knowable and knowably narrower than
thirty-two.

## Decision

**A `checksum` field has to be wide enough to hold the value its codec
produces.** Narrower is refused with no exemption, and the diagnostic names
the narrow kernels.

**Wider is accepted, and this is where a checksum differs from a tag.** A
narrow code in a wider field is ordinary -- the value is zero-extended and
the comparison stays exact -- so there is nothing undetectable about it the
way a truncation's direction is, and no 17.0 argument for refusing it. A
tag keeps its refusal in that direction, because extra bytes there
authenticate nothing rather than holding a smaller number.

This record first said "exactly as wide", and the full gate disproved it:
see *What the gate caught* below.

**`[truncated]` is a tag's attribute and is refused on a checksum.** The
two halves of the family verify differently, which is the whole of the
reason and is measured rather than assumed: for an authenticated region
situ generates **no comparison at all**. It emits
`situ_s_sealed_open(view, verified, out)`, which takes the caller's verdict
as a flag, and hands over the tag's bytes through a pointer. The codec --
somebody else's code -- does the comparing, holding both the bytes and
their length, so it applies its own truncation convention and OSCORE's
eight bytes stay sayable. A checksum's comparison is generated here, so
truncating one asks *situ* which end of the value a format keeps.

**That is section 17.0 rather than a new rule.** situ does not guess where
the wrong choice is undetectable at runtime -- which is why it refuses a
multi-byte scalar with no `endian` -- and which end of a CRC a truncation
keeps is exactly such a choice. Until now every backend guessed, by
comparing the whole computed value against the narrow stored one.

**The codec's width comes from the kernel, not from a declaration.**
`tag_bytes` where declared, else `expansion_add`, which is in bits since
0046 and *is* the width for a codec that appends its check: a five-bit CRC
adds five. Not one codec in `std/kernels.situ` declares `tag_bytes` and all
thirteen publish a width through their kernel, so reading only the
declaration left every CRC in the standard library unchecked.

## Consequences

- **No `.situ` schema changes.** The eleven comparable check fields across
  `example/` and `test/schema/` were already exact, both sub-byte ones
  included -- USB's five-bit CRC in a `u5` and MMC's seven-bit one in a
  `u7`, the latter verified against a real CMD0 in 26.372. One fixture
  embedded in a Python test changed: a Reed-Solomon (255,223) case read
  `checksum u8 c[2]` where that code's parity is 32 symbols, and the field
  is `c[32]` now. Its subject is whether a backend names a kernel it
  declines to write, which the width never entered.
- **No backend changes, and this corrects 26.673.** That record said
  nothing in the layout carries "the stored width is less than the codec's,
  so compare only that much", which framed the fault as a missing concept
  in codegen. It is not: the comparison is right whenever the widths agree,
  and making them agree is the compiler's job. Four emitters agreeing was
  evidence of one upstream hole, not of four slips.
- **The hole closes completely rather than narrowing**, because a checksum
  situ computes has to be one situ generates -- its codec is `derived`
  (0053), and a derived kernel states its width. An extern codec, whose
  output width belongs to somebody else and is 0038's silent case, cannot
  be a checksum's at all. So 0038's "where the codec is silent, nothing is
  checked" survives for tags and has no instances for checksums.
- **0038's placement rule is narrowed**, not reversed: `[truncated]` still
  belongs on a tag field, and now on only one of the two kinds. That record
  is not edited, per the rule that a decision is superseded rather than
  rewritten.
- A reader who removes the refused attribute still does not get a schema
  that compiles, since the width is checked either way, so the placement
  diagnostic says so instead of claiming the output would be identical
  without it.

## What the gate caught

**Four targeted suites passed and the full gate failed four tests.** The
first rule was "exactly as wide", which refused three committed fixtures
storing a `crc5_usb` or a `crc7` in a `u8` -- and refused them through an
arm nobody wrote for this change. "A tag cannot be wider than what computes
it" was already there and had **never reached a checksum**, because the
resolution bug meant the whole check never ran for one. Fixing the
resolution activated a years-old rule against a population it had never
been tested on.

That is the general shape and it is worth more than the instance: **making
a dormant check run is not a small change to the check, it is a first
exposure of every arm it has.** The arms were written for the one
configuration that reached them and are untested against the rest, so the
population to re-measure is not the one the fix is aimed at.

**The census missed all three because it read `.situ` files**, and these
fixtures live in Python string literals -- a count inheriting its detector,
in `evidence.md`'s words. Re-measured the right way by parsing every
embedded fixture and catching the error: 86 parse clean and none is refused
by the width check. That detector has its own limit, which is worth stating
rather than leaving to be found: 70 fixtures are refused earlier for other
reasons, and the parser stops at the first error, so a fixture with both an
earlier fault and a width fault reads as the earlier one. The gate remains
the backstop it just proved itself to be.

## Alternatives rejected

**Implement truncation, keeping the leading bits.** Picks a convention
situ cannot verify. If a format keeps the trailing bits instead, every
schema written meanwhile is silently wrong -- the failure 17.0 exists to
prevent, and worse than the present one because it would pass its own
tests.

**Make the convention sayable, as `[truncated = leading]`.** A language
addition to serve a case no format in the corpus has, and 26.205's rule is
that a construct wants a second example before it lands. Refusing is
reversible and this is not: the attribute would have to be supported for
ever.

**Leave codegen and refuse nothing, documenting the hazard.** 0038
rejected this same option in its own words -- "a document saying a check is
absent is not a check" -- and it is weaker here, because the hazard is not
an unchecked claim but a generated function that reports every message
corrupt.

## Open, and the holder's

**Whether a checksum field wider than its code should be refused.** It is
accepted here because three fixtures rely on it and because the reading is
unambiguous, but the spare bits are unchecked: `u8` holding a `crc7` works
only while the eighth bit reads zero, and nothing says it does.
`edges.situ`'s `mmc_command` is the properly-written version of the same
layout -- `u7` beside a `reserved u1 [must_be_zero]` -- which suggests the
wider form is sloppiness that 0046 already gave the remedy for. Refusing it
is a restriction nobody asked for, so it is raised rather than taken.

**Whether a truncated checksum should be expressible at all.** The case for no
is that `std/kernels.situ` carries thirteen widths precisely so a format's
narrow check field is declared at its true width, that 0046 added sub-byte
fields for exactly that, and that a chopped CRC-32 detects errors worse
than the CRC-8 nobody would have to chop. The case for yes is a real format
that stores one, which nobody here has met. Refusal is the state that keeps
both answers available.
