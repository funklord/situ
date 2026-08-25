# 0004: aarch64 is a compile-only target

Status: accepted
Date: 2026-07-26
Phase: 0

## Context

project.md section 24 requires that the runtime and generated code build clean
for a Cortex-A55 target, and section 26.0 sets phase 0 acceptance at `make test`
passing on host and aarch64 cross.

The development machine has `aarch64-linux-gnu-gcc` and binutils, but no aarch64
build of cmocka and no user-mode emulator. Cross test binaries can be compiled
and linked against the runtime, but not executed.

## Decision

`make cross` builds the runtime for aarch64 and verifies it is warning-clean
under `-Wall -Wextra -Werror -Wconversion -Wsign-conversion`. It does not build
or run the cmocka suite.

The cmocka suite runs on host only, in both a `SITU_CHECKED` and a release
build.

Phase 0 acceptance is read as: warning-clean compilation on both architectures,
behavioural tests on host.

## Rationale

The warnings this catches are the ones that actually differ across the two
targets: `-Wconversion` and `-Wsign-conversion` findings that depend on the
width and signedness of `char`, on pointer size, and on alignment assumptions.
Those are compile-time findings, and compiling is enough to surface them.

What is lost is the ability to catch a genuine behavioural difference, which for
this runtime would mean an unaligned access fault or an endianness assumption.
Neither is reachable in the phase 0 runtime, which does no multi-byte access at
all. It becomes reachable in phase 4, when generated accessors start doing
endianness conversion and unaligned loads.

## Revisit at phase 4

Phase 4 generates code whose whole job is byte-level access, and the
`repr = ValueConverted` path is exactly where a big-endian or alignment bug
would hide. Before phase 4 acceptance, either:

- install `qemu-user` and an aarch64 cmocka, and run the suite under emulation, or
- record why host-only behavioural testing remains sufficient.

Phase 4 also calls for a big-endian aarch64 cross build to check the generated
byte-order-marker host constant. That needs a toolchain not present here either,
and is tracked by the same revisit.
