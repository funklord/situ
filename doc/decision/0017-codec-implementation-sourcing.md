# 0017: one codec implementation, in C, with a per-language plugin slot

Status: accepted
Date: 2026-07-27
Phase: before 11 (backends)

## Context

Section 20.1 now plans backends for C++, Rust and Python after C. That raises a
question the C-only world never had to answer: when a Rust program uses a situ
schema with `codec crc32`, whose CRC32 runs?

Two things situ generates are easily confused here, and they want opposite
answers:

- **Accessors** -- getters, setters, views, validators. Pure layout arithmetic:
  shifts, masks, offsets. There is no algorithm to get wrong.
- **Codecs** -- CRC, Reed-Solomon, COBS, AES-GCM. Real algorithms, some of them
  with properties (constant time, in particular) that are hard-won and easy to
  lose.

The evidence that codecs are different in kind is in this repository. The
generated Reed-Solomon decoder had a real bug: the generator polynomial was
built leading-coefficient-first while the division loop indexed from the low
end, so the encoder divided by a reversed polynomial and every decode started
from garbage. Finding it took a Python model of the whole chain and a
stage-by-stage comparison. Generating that algorithm four times means four
chances to introduce four different variants of that bug, and four sets of
vectors to keep in step.

## Decision

**Accessors are always native to the target language.** Making a Rust caller
go through C to read a `u16` at offset 4 would throw away the entire reason a
Rust backend is interesting, and there is no correctness argument for it.

**Codecs have one implementation, and it is the C one.** Every other backend
binds to it through its own foreign-function interface. C is not less safe than
the alternatives when it is written correctly, and one implementation that is
correct is worth more than four that are each nearly correct. This applies to
both tiers: tier-1 codecs were always extern and supplied by the user, and
tier-2 derived codecs now generate C that the other backends call rather than
generating themselves.

**A per-language plugin slot exists from the start, and is empty.** The
language already has the hook. `impl` binds an implementation to a signature
and was designed so "a hand-tuned assembly routine, a DMA-driven hardware unit
or a vendor library can replace the default without changing one byte of the
capability map". A target qualifier extends it to this case:

```situ
impl crc32 derived;                       // the default: C, bound from anywhere
impl crc32 derived for rust;              // a native Rust implementation
impl crc32 extern "vendor_crc32" for c;   // a vendor routine, C only
```

The unqualified form stays what it is today and means what it means today. A
qualified one overrides it for one target and changes nothing else -- above
all, not the property signature, which is the whole interface the lattice
reads. That is what makes this safe to decide per codec per language rather
than once and globally.

## Consequences

**Generated code gains a dependency the toolchain does not have.** "No
dependencies, vendors trivially into an embedded build environment" is true of
`situc` and was true of its output. It stays true of C output. Rust, C++ and
Python output that uses a codec will link the C runtime. This is a real change
to the project's story and is stated here rather than discovered later.

**Rust pays for this in `unsafe`.** An `extern "C"` call is an unsafe block, in
a backend whose argument is that the capability system becomes compile-time.
The generated code must say so at the call site rather than burying it: a
reader auditing a Rust codebase needs to see where the unsafe surface is and
why. Section 26.11 expects the Rust backend to expose places the C backend
papered over; this is one it introduces instead, and it is the price of not
having four Reed-Solomons.

**Python pays for it in a build step**, which is the friction Python users
least expect. Whether pure-Python fallbacks are worth the fragmentation they
cost is exactly the question this record answers "no" to, so a Python program
that wants a codec builds the C runtime. If that proves untenable in practice,
it is the plugin slot that resolves it -- `impl crc32 derived for python` --
and not a second default.

**Native implementations are deferred indefinitely, not scheduled.** Rust
adoption is far enough out that speculative work on native Rust codecs would be
guesswork. C++ and Python land first and neither of them wants one: C++ links C
for free and idiomatically, and Python's performance expectations do not
justify a second implementation.

## Alternatives considered

**Native codecs per language where the algorithm is simple.** Table-driven
codecs -- CRC, Manchester, COBS -- are trivially correct in any language, so
generating them natively costs little and buys Rust its `unsafe`-free story.
Rejected because the line between "simple enough" and "not" is a judgement that
would be made once per codec per backend, and every one of those judgements is
a chance to be wrong. A rule that admits exceptions by difficulty is not a rule.

**Generate every codec in every language.** The honest maximal answer, and the
one that keeps each backend self-contained with no FFI anywhere. Rejected on
the Reed-Solomon evidence: the cost is not writing the code, it is being sure
four implementations agree, and situ's whole argument is that one description
should not become several that drift.

**Decide per backend when each is built.** Rejected because that is how C++ and
Rust end up with different answers, which is the outcome this record exists to
prevent.

## Amendment, 2026-08-27: a C gap that was not this record's deferral

This record defers **native implementations per language** -- a Rust or
Python codec written instead of linking the C one -- and says why: C++ links
C for free, Python's performance expectations do not justify a second
implementation, and Rust adoption was far enough out that speculative work
would be guesswork.

That is not the same thing as a kernel description C itself declines to
implement, and the two had been conflated. Measured: a `shift_register`
kernel of width 1, 4, 12, 24, 48 or 64 was accepted by the language, had its
signature derived correctly, appeared in the capability map -- and then
`gen-derived` emitted a comment telling the author to write the code
themselves. Only 8, 16 and 32 generated. Nothing in `kernels.py` refuses a
24-bit register; it validates the feedback source and never looks at width.
The tuple `(8, 16, 32)` in the emitter was the whole of it.

It is fixed, and the fix was already in the same file. `_polynomial` had met
this and solved it -- "a 24-bit CRC accumulates in a `uint32_t`, and every
shift is masked back" -- so `_shift_register` now takes its accumulator from
the same `_accumulator` helper and masks where the word is wider than the
register. The three widths that already worked emit byte-identical C, which
is the property that made the change safe to take: it is checked by
regenerating against `git show HEAD:` rather than asserted.

**The distinction is worth keeping** because the two failures look alike
from outside -- both end in "bind an `impl ... extern`" -- and only one of
them is a decision. A described kernel that C declines is a gap. A codec
that Rust links from C is this record's answer working.

**What remains genuinely undecided here is unchanged**, and one thing is
sharper: `stuffing` returns no implementation for any input at all, and
unlike the sub-byte CRC refusal -- which `kernels.py` states with a reason
the author can read -- nothing says why. That is a gap or a decision, and
which it is has not been written down.

## Amendment, 2026-08-27: what the one implementation costs

This record rests on two things it asserts rather than measures: that one C
implementation is *correct* -- "one implementation that is correct is worth
more than four that are each nearly correct" -- and that binding to it is
cheap for every other backend. Both were measured today. The first was not
true at the time of measuring, and the second is true of C++ and false of
Rust.

**Nothing outside this project had checked a generated CRC since 4 August.**
A differential test held `situ_crc32` and `situ_crc16_ccitt` against
`zlib.crc32` and `binascii.crc_hqx`. 17724a0 -- a commit adding network
oracles, whose message never mentions it -- deleted the test and left its
imports behind, and an unused import fails nothing, so the only outside
check on any derived codec vanished under a green suite for three weeks.
The commit's own subject is "every one required to be able to fail".

It is restored, and wider than it was. The standard kernels carry nine
polynomial codecs; the two independent implementations in the standard
library reach two of them. The other five CRCs are now checked against the
check value their catalogue entry publishes for "123456789", which is
weaker evidence and is labelled as such in the test: the check value comes
from the same catalogue the kernel parameters were transcribed from, so it
is not a second implementation. What it does catch is the transcription --
a wrong poly, init, xorout or reflect lands on a different value -- and
that is the failure that actually happens. The two Reed-Solomons are named
as reachable by neither, with the reason. A guard reads the schema and
refuses a tenth polynomial codec that is in none of the three.

None of that argues against this record. One implementation is still better
than four. But it was the premise the record leans hardest on, and it was
being taken on trust.

**The Rust binding costs more than the record expected, and the reason
given does not transfer to it.** The deferral is justified by the other two
backends: "C++ links C for free and idiomatically, and Python's performance
expectations do not justify a second implementation." Measured, that is
exactly right for C++ -- `runtime/cpp/situ.hpp` includes `situ.h` already,
so a codec adds nothing a C++ consumer was not already linking.

Rust is the opposite case, and this record could not have known it: Rust
adoption was "far enough out that speculative work would be guesswork". It
is not far out now -- the Rust backend spans the layer ladder to `drive`
and has a tokio driver -- and `runtime/rust/situ_rt.rs` is `#![no_std]`
with no C dependency whatever: no `extern "C"`, no `libc`, no `#[link]`, no
`cc`. The C runtime is not something a Rust consumer already has. It is a
dependency the first codec introduces.

The whole of what that costs, for a schema whose only codec is derived:

    extern "C" {
        fn situ_scramble_decode(input: *const u8, len: u32, out: *mut u8) -> u32;
    }

One `unsafe` block, a C toolchain in the build, and a `no_std` story that
now depends on linking C -- for a 16-bit shift register described by its
taps, seed and feedback. This record anticipated the `unsafe` and said it
was "the price of not having four Reed-Solomons". The price is the same
whether the codec is a Reed-Solomon or an LFSR, which is the part worth
looking at again.

**What is not being proposed is "native where the algorithm is simple".**
This record rejected that and the rejection stands: the line between simple
enough and not "is a judgement that would be made once per codec per
backend, and every one of those judgements is a chance to be wrong."

The question worth re-opening is a different one, and it is not a
judgement. `derived` is a keyword the schema already carries, and it
already partitions exactly the codecs situ computes from the ones it does
not. "Derived codecs are native in every backend, extern codecs are FFI in
every backend" admits no exceptions and needs no per-codec ruling.

Two things bearing on the drift argument have changed since:

- **The derivation is not per-backend.** Of `situc/codegen/c/derived.py`'s
  1734 lines, 739 are Python computing tables, masks and accumulator
  widths, and 707 are C being spelled -- the rest comment and blank. A
  second backend re-spells; it does not re-derive. That is the division this record already accepts for
  accessors, where "there is no algorithm to get wrong" -- here the
  algorithm is in the Python, and it is written once however many backends
  read it.
- **Drift is caught mechanically now.** The Reed-Solomon bug this record
  cites as its evidence was found by hand, with a Python model built for
  the purpose. Six readers are held to each other by the differential
  suite, and as of today the generated CRCs are held to outside
  implementations and published check values as well. "Four chances to
  introduce four different variants of that bug" is a real cost and no
  longer an unmonitored one.

**What this asks.** Nothing is changed by this amendment; the decision
above stands as written. The question put to the holder is narrower than
the one the record deferred:

> Should `impl <codec> derived` generate native Rust, rather than an
> `extern "C"` binding to the C implementation?

C++ and Python are not part of it. The record's reasoning holds for both,
and this amendment confirms the C++ half by measurement.

**And the slot this record leans on is not there.** Both this record and
project.md's summary of it say a per-language plugin slot "exists ... and
is empty", spelled `impl crc32 derived for rust`. Measured, it is absent
rather than empty: the parser refuses the qualifier outright --

    error: expected `;` after the impl binding, found `for`

-- and `ImplDecl` carries `codec`, `kind` and `symbol`, with nothing to
qualify them by target.

That is flagged and not resolved here, because which side is wrong is a
real question with an answer somebody holds: the syntax may have been
designed and never built, or built and dropped. It does not change the
question above, which asks what the *default* should be for Rust. It does
mean there is currently no override to fall back on, so today the C
binding is not one choice among two -- it is the only one.
