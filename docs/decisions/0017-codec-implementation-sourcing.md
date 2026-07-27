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
