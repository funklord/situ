# 0007: aarch64 is behaviourally tested under emulation

Status: accepted
Date: 2026-07-26
Phase: 4

Closes the revisit that `docs/decisions/0004-aarch64-compile-only.md` deferred
to this phase.

## Context

Decision 0004 made aarch64 compile-only, on the grounds that the phase 0 runtime
did no multi-byte access at all, so the findings that differ between the two
targets were compile-time findings and compiling was enough to surface them. It
also said the position had to be revisited at phase 4, when generated accessors
start doing byte-order conversion and unaligned loads.

That revisit is due. The generated code now:

- reads and writes 16, 32 and 64-bit values at offsets that are frequently not
  naturally aligned -- `Header.seq` is a `u32` at offset 5
- extracts bit fields, including ones straddling a byte boundary
- branches at parse time on a byte-order marker

None of that is verified by compiling it.

## What turned out to be available

The blocker in 0004 was stated as "no aarch64 build of cmocka and no user-mode
emulator". The second half was wrong: `qemu-user` and `qemu-user-static` are
installed, providing `qemu-aarch64` and `qemu-aarch64_be`. The cmocka half
still holds.

## Decision

`make cross-test` runs `tests/cross`, which does three things:

1. **Host.** Builds and runs a self-checking probe over the generated
   accessors. It is built for the host too, so a failure elsewhere is a real
   architectural difference rather than a bug in the probe.
2. **aarch64, little endian.** Same probe, cross-compiled statically and run
   under `qemu-aarch64`. This is behavioural verification on the target, not a
   warning sweep.
3. **aarch64, big endian.** Compile only, freestanding, with a
   `_Static_assert` that the byte-order marker's host constant resolves to the
   big-endian literal.

`make test` runs all three. Each aarch64 target skips with a message when its
tool is absent, so a machine without the cross toolchain can still run the rest
of the suite.

The probe is a plain self-checking program rather than a cmocka suite, because
cmocka has no aarch64 build here. It prints the first mismatch and returns
non-zero, which is all a cross check needs.

## Why big endian is compile-only

There is no big-endian glibc in this environment, so a big-endian binary cannot
be linked or run. It can be compiled `-ffreestanding`, which is all the
generated code and the runtime need: between them they include nothing but
`<stdint.h>` and `<stddef.h>`.

That is enough for the part that actually matters. Phase 4's acceptance asks
that the generated host-order constant match the build's endianness on both a
little-endian host and an aarch64 big-endian cross build. A `_Static_assert`
answers exactly that question at compile time, so this is a real check rather
than a warning sweep with no assertions in it.

What remains unverified on big endian is runtime behaviour. The exposure is
small and bounded: every multi-byte access in the runtime is written as explicit
byte indexing rather than a cast, so there is no host-order path for a
big-endian machine to take differently. Should that stop being true -- if a
memcpy fast path is ever added for `MemoryIdentical` fields, which is the
obvious future optimisation -- this decision needs revisiting again, because
that path is precisely where host order would start to matter.

## Consequences

- The unaligned `u32` load at offset 5 and the straddling bit fields are now
  known to behave identically on x86-64 and aarch64, rather than assumed to.
- The byte-order-marker round trip is verified on both architectures: reading a
  big-endian TIFF header and writing the values back reproduces big-endian
  bytes, on a little-endian machine and under emulation alike.
- `tests/cross` is a self-contained sub-project like the others, with its own
  defaults, so it builds standalone.

## Alternatives considered

**Install an aarch64 cmocka and reuse the existing suites.** Cleanest in
principle, and it would remove the duplicate probe. Rejected for now: it needs a
cross-compiled dependency for a benefit the probe already delivers, and adding a
build-time dependency deserves its own decision.

**Keep compile-only and record why.** This was the other branch 0004 offered.
Rejected once the emulator turned out to be present: the argument for
compile-only rested on the runtime doing no multi-byte access, and that stopped
being true in this phase.

**Run the whole cmocka suite under emulation.** Not possible without the aarch64
cmocka, and the probe covers the accessors that differ by architecture.
