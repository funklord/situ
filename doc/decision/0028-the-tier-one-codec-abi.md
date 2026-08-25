# 0028: one ABI for a tier-1 codec, named after the symbol its `impl` binds

Status: accepted
Date: 2026-08-02
Phase: after 26.35

## Context

Three things named a tier-1 codec's implementation, and no two agreed.

`impl aes_gcm_128 extern "my_aes_gcm_128"` binds a symbol (13.1). Nothing
emitted it: the string appeared in no generated file, so the binding was
documentation.

`gen-codec-tests` declared and called `situ_codec_<codec>_encode` and
`situ_codec_<codec>_decode`, five arguments returning `situ_err_t`. That shape
appears nowhere else in the project -- not in the specification, not in any
accessor, not in the `impl`. A user who wrote an implementation to satisfy the
property tests had written a function nothing calls; a user who wrote one for
their own program had to write a second wrapper to run the tests. The harness
had never been run, and could not be.

The accessors, meanwhile, called the *kernel* shape for derived codecs --
`uint32_t situ_manchester_802_3_decode(const uint8_t *in, uint32_t bits,
uint8_t *out)` -- which is right for a generated implementation and has
nothing to do with a codec the compiler never sees. And a coded region with a
tier-1 codec got no decode accessor at all, on 13.6a's reasoning that only a
`table` kernel's shape was settled.

So the tier-1 tier -- the one the whole transform system is designed around,
the only one that may seal (14.3) -- had no interface.

## Decision

The tier-1 ABI is two functions, named after the symbol the `impl` binds:

```c
situ_err_t <symbol>_encode(const uint8_t *in, uint32_t in_len,
                           uint8_t *out, uint32_t out_cap, uint32_t *out_len);
situ_err_t <symbol>_decode(const uint8_t *in, uint32_t in_len,
                           uint8_t *out, uint32_t out_cap, uint32_t *out_len);
```

- **The symbol is the `impl`'s**, not the codec's. That is what `extern "my_x"`
  has meant since 13.1 was written, and it is what makes an implementation
  swappable: two schemas may bind the same contract to different code.
- **`situ_err_t` rather than a count.** A tier-1 codec can fail where a table
  lookup cannot. `SITU_ERR_BOUNDS` for a buffer too small,
  `SITU_ERR_CONSTRAINT` for input it does not admit.
- **A capacity, not an allocation** (invariant 4), and `*out_len` written on
  success only.
- **`gen-codec-tests` attacks this**, so a suite and an implementation link.
- **A coded region's decode accessor calls it**, in C, C++ and Rust; Python
  names it in the note that says why it does not (decision 0017).
- **A codec with no `impl`, or a derived one, gets no tier-1 suite**, and the
  generated file says which and why. A signature may exist with no
  implementation (13.1), and a derived one's properties follow from its own
  kernel.

## Alternatives considered

**Keep `situ_codec_<codec>_*` and document it.** Fewer moving parts, and
wrong: it names the *contract* where the thing being called is the
*implementation*, so two schemas binding one contract to two implementations
would collide at link time. It also leaves `extern "my_x"` meaning nothing.

**Make the harness speak the kernel shapes instead.** That is the right answer
for tier 2 and is the next piece of work, but a kernel shape is per-family --
`(in, bits, out) -> bits` for a table, a nibble in and a byte out with a
correction flag for Hamming -- and a tier-1 codec has no family. The two tiers
need two harnesses, which is what 13.1 means by the signature being the
interface and the implementation not being one.

**Leave the tier-1 decode accessor out.** 13.6a's original reasoning: guessing
a signature would be a header naming a function nobody agreed to write. That
was correct while no signature existed. Naming one is what this record does,
so the accessor is no longer a guess.

## Consequences

- `gen-codec-tests` runs. `test/schema/edges.situ` binds `doubling` to
  `my_doubling`, `test/generated/codec_impl.c` implements it, and four
  declared properties -- length, deterministic, invertible, seekable -- are
  checked against a running implementation by `make test-c`. Breaking the
  implementation fails the matching test, which is the first evidence that the
  harness attacks anything.
- `std/codecs.situ` emits nineteen refusals rather than nineteen suites, and
  says why for each: it is a library of contracts with no `impl`, which is
  what it is for.
- A tier-1 coded region decodes in three of the four backends.
- Anyone who had written an implementation against the old harness has to
  rename two functions. Nobody had: the harness could not be linked, which is
  how this was found.
