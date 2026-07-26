# Capability axes (normative)

Extracted from project.md section 11.1. Where the two disagree, project.md is
authoritative and this file is the bug.

Each field and region carries a **capability vector**: one value per axis. The
axes are independent. Each is a lattice with a defined weakening order.
Constructs weaken axes; nothing strengthens them.

Vector A is at least as strong as B when A is at least as strong as B on every
axis. This is a product lattice, so incomparable vectors exist, and that is
fine: the compiler never needs a total order, only meet.

Meet is computed pointwise. A struct's vector is the meet of its members'
vectors, plus whatever the struct construct itself imposes.

## The axes

Values are listed strongest first.

| Axis | Domain | Meaning |
|---|---|---|
| `size` | `Fixed(n)` > `Bounded(lo,hi)` > `Unbounded` | byte extent |
| `offset` | `AbsoluteStatic(n)` > `FrameStatic(n)` > `Dynamic` | position knowledge |
| `access` | `Random` > `Sequential` | can element N be reached directly |
| `mutate` | `InPlaceFixed` > `InPlaceSlack` > `Shifting` > `RewriteRequired` > `Immutable` | write cost |
| `address` | `Stable` > `FrameStable` > `Unstable` | can a pointer be held |
| `align` | `Aligned(n)` > `Unaligned` | relative to message base |
| `repr` | `MemoryIdentical` > `ValueConverted` > `ConditionallyConverted(f)` | is the value literally the bytes |
| `atomic` | `AtomicWord` > `NonAtomic` | single-instruction access possible |
| `canonical` | `Canonical` > `CanonicalGiven(f)` > `NonCanonical` | exactly one valid encoding |
| `stage` | `CompileTime` < `ParseTime` < `TransformTime` < `VerifyGated` | when resolvable |
| `auth` | `Uncovered` / `Covered(tag)` | which tag covers these bytes |
| `secrecy` | `Public` / `Secret` | affects the generated API |
| `effect` | `Pure` > `EffectOnRead` / `EffectOnWrite` / `EffectBoth` | MMIO side effects |

## Notes on the axes that are easy to get wrong

**`repr`.** A big-endian `u32` on a little-endian host is `ValueConverted`: the
value is not the memory. In-place mutation of such a field is a
read-swap-write, not a store, and a caller cannot take a pointer to the value.
`ConditionallyConverted(f)` means the swap decision is a parse-time branch on
field `f`, a byte-order marker (section 8.3). The branch is on a public,
layout-irrelevant value, so it is not a side channel.

**`canonical`.** `CanonicalGiven(f)` means the format admits more than one
encoding of a value, but exactly one given the value of field `f`. This is the
right classification for a byte-order-marked format.

The consequence for signing, stated as a rule: **verify over received bytes,
never over re-encoded bytes.** A writer is deterministic even when the format is
not, because it always emits host order plus the matching marker. Requirements
must therefore distinguish `deterministic_writer(X)` from `canonical(X)`.

**`atomic`.** Bit fields are never atomic: writing one is a read-modify-write of
the containing byte. Multi-field updates are never atomic in v0. The system
makes no atomicity promise it cannot keep.

**`stage`.** The only axis that increases rather than weakens. Treat it
uniformly as monotone in the direction of less usable.

**`auth`.** Not ordered. It is a set-valued tag identity. Mutating bytes with
`Covered(t)` marks tag `t` dirty (section 14.2).

## The locality rule

Stated once, because every propagation rule depends on it:

> A construct with dynamic size weakens the `offset` and `address` axes of every
> *subsequent* member of its enclosing frame, and of nothing else. It does not
> weaken members of parent frames before it, and it does not weaken its own
> interior.

This locality is what makes islands of staticness work, and it is the reason
frame-relative staticness is the common case rather than an exception.

## Implementation invariants

1. The propagation table (project.md section 11.3) is data, not code. Adding a
   construct means adding a row and a test, never editing scattered
   conditionals.
2. No capability may be strengthened by any construct. If an implementation
   seems to need that, the axis definition is wrong: stop and ask.
3. Every diagnostic has a blame chain. A diagnostic without one is a bug.
