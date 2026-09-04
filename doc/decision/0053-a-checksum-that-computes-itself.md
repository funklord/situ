# 0053: a checksum that computes itself

Status: proposed
Date: 2026-09-04
Phase: raised by respec, from a reader whose image validated and was corrupt

## Context

respec described an image format in situ, generated a reader, and the reader
accepted a truncated file. Their diagnosis, which is the accurate one:

> situ has the construct (`checksum u8 crc[4] covers(R)`), but it declares
> coverage and staleness, not computation.

**Measured, and it is true of `tag` as well.** A `tag` over a sealed region
generates exactly three things:

    situ_keystore_tag_covered(view, &offset, &len)   the span to run over
    situ_keystore_tag_is_dirty(msg)                  has it gone stale
    situ_keystore_tag_finalize(msg)                  clear the dirty bit

and a comment telling the caller what to do with them: *"Write the result
through `situ_keystore_tag_ptr()` and then call `..._tag_finalize()`."* The
arithmetic is the caller's in both cases. `gen-tamper` does not change this
-- it takes a `situ_verify_fn` the caller supplies.

**The asymmetry that makes this worth reopening.** `sealed` and `coded` each
carry a `codec` field and name their algorithm at the site:

    sealed(aes_gcm_256, nonce = nonce) { ... }

`TagField`, which serves both `tag` and `checksum`, has no such field. So the
construct that *seals* a region names an algorithm and the construct that
*summarises* one cannot -- and there is no reason in the format for that.
It is a consequence of `checksum` having been defined as "`tag` with a
non-cryptographic algorithm", inheriting a shape that was designed around
the thing situ deliberately does not implement.

**And situ already implements the algorithms in question.** `std/kernels.situ`
declares `crc32`, `crc32c`, `crc16_ccitt`, `crc16_modbus`, `crc24_ble` and
others, each with `impl <name> derived`, and `situc gen-derived` emits a real
`situ_crc32_table[256]` and `situ_crc32()`. Both halves exist. Nothing joins
them, so every caller writes the same three lines, and respec's caller wrote
none of them and nothing said so.

**14.1 putting computation with the caller is right for the case it was
written about.** Situ does not implement AEAD: constant-time behaviour, key
handling and nonce discipline are not things a layout compiler should own,
and the `secrecy` axis and the sealed region's gate exist to work *with* a
caller's implementation rather than replace it. A CRC is not that. It has no
key, no secret-dependent branch, and situ already generates it.

## Decision

**A `checksum` may name a codec, and only one whose `impl` is `derived`.**

    checksum u8 crc[4] covers(body) is crc32;

The spelling mirrors the region constructs, which already put the codec in
parentheses at the site -- `sealed(aes_gcm_256, nonce = nonce)`. Whether the
keyword is `is`, `by`, or a parenthesised form matching `sealed` is left to
implementation; what this record fixes is that the binding exists and what it
is allowed to name.

**`derived` is the whole of the restriction, and it is not a new concept.**
Tier 2 is already the set situ generates from a kernel description. Binding
one to a checksum adds no implementation situ did not already have, which is
what makes this a wiring change rather than a policy reversal.

**A `tag` may not name one.** Its algorithms are tier 1 by design, and 14.1's
reasoning is untouched for them. A schema that wants a keyed MAC computed
still supplies the function.

**What it generates.** Two functions beside the three that exist:

- `situ_S_crc_compute(view, uint32_t *out)` -- run the codec over
  `..._crc_covered()`'s span and hand back the value, writing nothing.
- `situ_S_crc_check(view)` -- compute and compare against the stored bytes,
  returning `SITU_ERR_CHECKSUM` on a mismatch.

**A new error code rather than `SITU_ERR_CONSTRAINT`.** 0051 has just made
the case that collapsing distinct failures into one code costs a caller the
thing they need to act on, and "this file is corrupt or truncated" is a
different answer from "a field is out of range". `SITU_ERR_TAG` is the
neighbouring code and is wrong here: it means a cryptographic gate refused,
and a reader must not confuse a CRC mismatch with a failed authentication.

**`validate` does not call it.** This is the part that needs stating rather
than assuming. `validate` is a constraint walk over a view, and a checksum
over a region running to EOF is not that shape: it is a pass over the whole
message, its cost is linear in the file, and 0051 settled that the message
model is flat and runs once at the end. So the check is a function a caller
invokes at that point, and `situc build --refuse-ungenerated` is where a
schema can insist the caller has one.

## Alternatives considered

**Leave it, and keep computation with the caller.** The status quo, and the
argument for it is 14.1, which is sound for tags. Rejected because it is
being applied to a case it was not written about: situ already generates
CRC implementations, so the caller is not supplying expertise situ lacks --
they are supplying three lines of glue, and respec supplied none.

**Let `validate` compute it.** The obvious shape and the reason this record
exists as a record. It makes a bounded constraint walk unbounded, so a
caller who wanted field checks pays for a pass over the file, and a member
whose coverage runs to EOF cannot be checked in a view at all. It also
re-opens the "when does this run" question 0051 was written to close.

**Bind the codec on the region rather than the checksum.** `authenticated
body(crc32) { ... }` puts the algorithm with the bytes it runs over, which
reads well. Rejected because coverage is many-to-one: a checksum may cover
several regions, and the algorithm belongs to the summary rather than to any
one span it summarises.

**Allow any codec, not only `derived` ones.** Simpler rule, and it silently
recreates the tier-1 case: a schema naming `aes_gcm_256` on a checksum would
be asking situ for an implementation it declines to write, and the refusal
would come from somewhere further down with a worse message.

## Consequences

**A whole-file checksum becomes expressible end to end.** The coverage
already is -- `authenticated body { u8 rest[remaining]; }` under `target
file` is a region running to EOF whose interior is deliberately undescribed,
which is what respec believed they could not write. This record supplies the
half that was genuinely missing.

**`SITU_ERR_CHECKSUM` is a new code in every backend**, and the C, C++, Rust
and Python spellings of it are four renderings of one decision, per the
existing rule about the error enum.

**It does not change what `tag` does**, so nothing in the keystore or dtls
examples moves. The tamper harness and the sealed gate are untouched.

**Nothing here is built.** The corpus has no schema exercising a bound
checksum, and by 26.234's argument that is part of the work rather than a
follow-up: a construct added without a corpus schema that uses it is a
construct the differential cannot pose a case for.
