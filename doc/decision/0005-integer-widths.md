# 0005: Widths that are a whole number of bytes are byte-aligned scalars

Status: accepted
Date: 2026-07-26
Phase: 1

Resolves project.md open question 6, and reconciles a contradiction inside
section 8.1.

## Context

Section 8.1 states the packing rule twice, and the two statements disagree:

> Non-power-of-two widths are legal and imply bit packing (`u3`, `u12`, `u48`).

> Widths >= 8 that are multiples of 8 are byte-aligned by default; others
> participate in bit packing with the surrounding fields.

`u48` is a multiple of 8, so the first sentence calls it bit-packed and the
second calls it byte-aligned. The same disagreement covers `u24`, `u40` and
`u56`.

Open question 6 asks a related question: whether non-power-of-two widths above 8
bits should be constrained to byte-aligned positions or allowed to pack.

## Decision

The second statement governs. A width that is a whole number of bytes is a
byte-aligned scalar; everything else participates in bit packing.

- `u8`, `u16`, `u24`, `u32`, `u48`, `u64`: byte-aligned scalars. Not packed.
  Byte-alignment is what "non-power-of-two widths are legal" was buying, and
  `u24` and `u48` are common in real formats (RGB triples, 48-bit MAC
  addresses, 48-bit sequence numbers).
- `u1` through `u7`, `u12`, `u20`, and every other non-multiple of 8: bit
  packed, at any bit offset, per section 8.2 and the active `bit_order`.

Open question 6 is answered "allow packed", with the straddle rule doing the
gating rather than a width restriction.

## Why packing is allowed rather than restricted

The straddle rule of section 8.2 already governs the dangerous case, and it
governs it more precisely than a width restriction could. A field crossing a
byte boundary needs `[allow_straddle]` on the enclosing struct, because
straddling silently forces a multi-byte read-modify-write.

A packed width above 8 bits always crosses a byte boundary, whatever its
starting offset: 12 bits cannot fit inside one byte. So `u12` is legal but
always requires `[allow_straddle]`, and the author is told exactly what it
costs. That is the section 17.0 principle working as intended -- the safe option
is silent, the unsafe option is loud -- and it is strictly more informative than
rejecting `u12` outright.

Restricting packed widths to byte-aligned starts would also have been an odd
rule: a `u12` starting at a byte boundary still ends mid-byte, so the next field
is unaligned regardless. The restriction would prevent nothing.

The solver tracks offsets in bits from phase 2 (section 26.2), so no part of
this is expensive to represent.

## Consequences

- `types.ScalarType.is_bit_packed` is `bits % 8 != 0`, and is the single place
  this rule is expressed.
- `crosses_byte_boundary` is `is_bit_packed and bits > 8`, used by the straddle
  check when phase 2 computes bit offsets.
- Widths run from 1 to 64 inclusive. A leading zero in the width (`u08`) is
  rejected rather than normalised, because it reads as though it might mean
  something other than `u8`.
- `f16`, `f32`, `f64` are byte-aligned by the same rule and are never packed.

## Alternatives considered

**Take the first sentence: all non-power-of-two widths pack.** Rejected. It
would make `u24` a bit-packed type that always needs `[allow_straddle]`, which
is hostile for a width that appears in real formats specifically because it is
three whole bytes. It would also make the packing rule depend on
power-of-two-ness, which has no structural meaning here -- what matters is
whether the width is a whole number of bytes.

**Constrain packed widths above 8 bits to byte-aligned starts.** Rejected as
described above: it prevents nothing, because such a field still ends mid-byte.

**Forbid non-power-of-two widths above 8 bits entirely.** Rejected: `u24` and
`u48` are useful and unambiguous, and forbidding them would push authors into
describing a 48-bit field as `u8[6]`, which loses the value semantics and the
endianness handling.

## Confirmed

Reviewed and confirmed by the project owner: a width not divisible by 8 is bit
packed. project.md section 8.1 has been amended to state the rule once instead
of twice, and open question 6 is marked resolved. This decision now records the
reasoning behind what the document says rather than a reading of it.
