# 0046: a checksum narrower than a byte, and where the bytes assumption lives

Status: accepted 2026-09-04; not yet built
Date: 2026-09-01
Phase: unscheduled; found while making USB expressible (26.146, 26.148)

## Context

USB's token packets carry a five-bit CRC over an eleven-bit field. CAN 2.0
carries a fifteen-bit one; MMC and SD carry seven bits in every command. None
of the three is describable here, and situ refuses them in two separate
places, each with a message that is correct about its own half:

    `c` has a 5-bit polynomial kernel
      = not a whole number of bytes
      = a checksum is appended as bytes; a width that is not a multiple of
        eight has no byte string to append

    a checksum must be a whole-byte scalar type
      = `u5` is not
      = a tag is a byte string produced by an algorithm, so it has no
        bit-level structure to describe

The first is `kernels._polynomial`, which decides whether an implementation
can be *derived*. The second is `layout.place_tag`, which decides whether a
field can be *placed*. USB needs both.

**The bytes assumption is not one line, and it was measured rather than
guessed.** Four places hold it, and they are the cost of any answer here:

- `layout._expand` scales an additive expansion as
  `codec.expansion_add * BITS_PER_BYTE`, so a codec's growth is byte-valued.
- `codegen/c/emit._covered_spans` computes
  `offset_bytes + size_bits // BITS_PER_BYTE`, and its docstring says "the
  byte range a tag authenticates".
- The emitted accessor is
  `situ_x_checksum_covered(situ_view_t view, uint32_t *offset, uint32_t *len)`
  in bytes, with `SITU_X_CHECKSUM_SELF_AS` a byte value, and three more
  backends emit the same shape.
- `runtime/c/situ.h` says "a `checksum` field tells a caller which bytes are
  covered", and every helper beside it takes
  `(const uint8_t *data, uint32_t len)`.

**What decides the design is that the coverage is sub-byte too.** USB's CRC5
does not merely occupy five bits; it covers eleven. So a bit-valued coverage
span has to be written whichever construct carries it, and "add a narrower
construct beside the tag" does not avoid the work -- it duplicates it.

## Decision

**Widen the mechanism, keep the rule.**

`tag` and `checksum` accept a sub-byte scalar and a sub-byte coverage span.
Coverage becomes bit-valued through the emitter, the generated accessor and
the runtime helpers, in all four backends. `_polynomial` accepts any width up
to 64 rather than any multiple of eight, holding the register in the next
word up and masking after every shift, which is what it already does for the
widths between the C word sizes.

**And a codec that declares `authenticated` still requires a whole-byte
tag.** No AEAD produces five bits, and 14.3's stage gate hands out a region's
interior on that tag; a sub-byte authentication tag is not a construct anyone
needs and is a construct somebody could misuse. The mechanism goes bit-valued
and the cryptographic rule does not move.

So the refusal that survives is narrower and says why: a *parity* field may be
any width, an *authentication* tag may not.

## Alternatives considered

**A distinct construct, leaving `tag` and `checksum` byte-only.** Something
like `parity u5 crc5 covers(...)`, so the cryptographic path is untouched.
Rejected on the measurement above: the coverage span must be bit-valued
either way, so this writes the same machinery a second time rather than
avoiding it, and leaves the language with a third keyword doing a job two
already do. 26.144 names that shape for 8b10b and CoAP -- answering two
instances of one absence separately is how a language grows two spellings for
one idea.

**Keep the refusal and say so.** Defensible until 26.137, which is the
copyright holder's standing answer that a protocol needing something makes it
work rather than a judgement about difficulty. USB, CAN and MMC are not
exotic.

**Make the field a plain `u5` and let the caller compute the CRC.** This is
what a hand-written reader does, and it is what situ exists to replace: the
coverage is then a comment, the staleness is unenforced, and the generated
API cannot refuse a write that invalidates it. It also works today, which is
worth saying plainly -- a schema can describe USB's token packet *layout*
right now and simply not model the check.

## Consequences

- Two refusals become one, and the survivor is about authentication rather
  than about arithmetic.
- The generated coverage accessor changes shape in four backends. That is a
  breaking change to emitted API, so it wants the same treatment as any wire
  or interface change: `situc map --check` and the committed maps will show
  it, and it is the reason this is a record rather than a commit.
- `situ.h`'s checksum helpers gain bit-addressed forms. The existing
  byte-addressed ones stay, because every current caller is byte-aligned and
  a bit-addressed helper over a whole-byte span is slower for nothing.
- CRC5, CRC7 and CRC15 become derivable, and each needs a published check
  value before it ships -- the rule every polynomial codec here is already
  held to.
- `example/usb` becomes writable. It is the reason to want this: 26.146 and
  26.148 built the NRZI conventions and the bit stuffing, and the token
  packet is the one part of the low-speed line still out of reach.
