# 0014: `endian` and `bit_order` are positional, and `native` is the C compiler's

Status: accepted
Date: 2026-07-27
Phase: 8

## Context

Two bugs in the same construct, both silent.

**A directive applied to the whole file, last one winning.** `_file_scope`
scanned every declaration and kept the final `endian`, so this compiled with
the struct on line 2 becoming host-order:

```situ
endian big;
struct first { u16 x; }
endian native;
struct second [allow_host_dependent] { u16 y; }
```

Nothing could have caught it. Both readings produce a valid layout, and the
capability map was correct for the reading the compiler chose -- it just was
not the reading anyone would have from reading top to bottom.

**`native` was resolved by the machine running situc.** `_host_is_little()`
read `sys.byteorder`, so generating on x86 and compiling for a big-endian
target emitted `situ_get_le32` for a field whose native order is big. Correct
whenever the generating and running machines agree, silently wrong the rest of
the time, and warning-free either way. The `endian_marker` path already did it
correctly, through `__BYTE_ORDER__` at C compile time; the scalar path did not.

## Decision

**Positional scoping.** A directive applies to declarations that follow it and
to nothing before it. A second one changes the scope from that point on.

Beyond fixing the bug, this is what lets one file describe a protocol whose
layers disagree about byte order -- network-order framing around a
little-endian device record -- without giving every struct an `[endian = ...]`
attribute.

**`native` resolves in C, never in situc.** The runtime defines `SITU_HOST_BIG`
from the compiler's own macros and offers `situ_get_ne16/32/64`,
`situ_put_ne16/32/64` and `situ_bits_get_ne`/`situ_bits_set_ne`. The constant
folds, so a native accessor costs exactly what a fixed-order one does. The
marker's `_HOST` constant now reads the same `SITU_HOST_BIG`, so there is one
decision point rather than two that could disagree.

**Where the host order cannot be determined, the runtime refuses.** `#error`
rather than assuming little endian, with `SITU_HOST_BIG` documented as the
override. A wrong guess is undetectable until the bytes are on the wire.

## What `native` still does not say

It means the order of the machine the generated code is compiled for. That is
the writer's order, and a writer is not always the machine that matters: a
server producing frames for a weaker client wants the *client's* order, which
is not its own. `native` cannot express that and should not try.

Both alternatives already exist and are better:

- Name the order outright. A schema that says `endian little` because the peer
  is little-endian is describing the format rather than the builder, and it is
  canonical where `native` is not.
- Use an `endian_marker`, so the order travels with the data and the receiver
  does not have to be told out of band.

So `native` stays what it was meant for -- in-memory and same-machine IPC --
and the diagnostic behind `[allow_host_dependent]` now says which trap it is
guarding, rather than only that host order is non-canonical.

## Alternatives considered

**Keep file-wide scoping and reject a second directive.** Simpler, and it would
have closed the bug. Rejected: it also closes the multi-endian case, which is a
real thing protocols do and which situ has no other ergonomic answer for.

**Emit `#if __BYTE_ORDER__` around each native accessor.** Rejected: it puts a
preprocessor forest through generated code for a decision that belongs in the
runtime once.

**Drop `endian native` entirely**, on the grounds that a schema should never
depend on the builder. Tempting, and the argument above is most of the way
there. Rejected because the in-memory IPC case is legitimate, the construct
already demands `[allow_host_dependent]` to be reached, and the capability map
already reports it as non-canonical -- so it is loud rather than a trap.
