# situ, from a project that evaluated it and chose it

Written 2026-08-04 from `netcfgd`, which needs a compact authenticated wire
format for a LAN-only remote protocol (`wire/` plus an `agent/` that terminates
it on the daemon host). situ was tried from a clone -- the tree is under active
work and must not be built in -- with a real probe rather than a toy: an
envelope with an uncovered version byte, an `authenticated { }` region carrying
command, target host, sender, nonce, expiry and capability, a
`sealed(chacha20_poly1305, nonce = nonce) { }` body, a 16-byte tag, and
`require canonical(envelope)` plus `require verify_gated(envelope.sealed)`.

The answer was **yes**. This file is what was persuasive, what was not obvious,
and what the decision is still open on -- from someone deciding whether to
depend on it, which is a different vantage point from maintaining it.

## The three things that actually made the case

Not the feature list. These three, in this order:

1. **The verify gate is a type.** The sealed interior is reachable only through
   a `situ_envelope_sealed_t`, and the only thing that produces one is
   `situ_envelope_sealed_open(view, verified, out)`, which returns
   `SITU_ERR_TAG` when `verified` is false. Every interior accessor takes that
   type.

   In C this is a discipline the compiler enforces rather than a proof -- the
   struct can be hand-assembled -- but it moves "parse before verify" from
   something a reviewer must catch to something the type system asks about.
   That is the single strongest argument in the tool and it is not what the
   README leads with.

2. **A stale tag cannot be transmitted.** Every setter on a covered field marks
   the message dirty, and no transmittable buffer is yielded until `finalize`
   recomputes. "Mutated a field and forgot the MAC" is a bug class, handled by
   construction.

3. **`gen-fuzz` emits the harness, and `wire` emits a reviewable byte-level
   contract to commit and diff.** This project's standard for a hand-rolled
   parser is a fuzz target and a frozen witness; situ meets it by removing the
   hand-rolled parser. `situc wire` is `doc/schema/` and `make schema-bless`
   in another language, which is why it read as familiar rather than as a new
   obligation.

**Suggestion:** lead with (1). "A schema compiler for byte-exact layouts" is
accurate and undersells it -- plenty of tools generate accessors, and almost
none make the verify gate unforgeable-by-accident in C. The capability model
saying what it *cannot* generate is the second thing worth leading with, for
the same reason: it is the sentence that makes a cautious project trust the
first one.

## Composition, which was the thing least expected to work

`impl chacha20_poly1305 extern "ncfg_monocypher_aead";` -- a codec declared with
its properties and bound to an implementation the caller supplies. situ decides
layout, coverage and gating; Monocypher does the arithmetic.

That this composes rather than competes is what made situ adoptable at all. A
project that has already chosen its crypto library and audited it will not
swap it to gain a code generator. **This deserves to be prominent**: the
question "do I have to use your crypto?" is asked early and answered late.

## Versioning, section 19: the part that changed how the protocol was designed

*Version is a field, not metadata* is the sentence, and it landed before a byte
was written -- the envelope now carries a version discriminant from the first
commit because of it.

Two things there are better than what most projects invent for themselves:

- `[since = N]` is **enforced append-only** with every member keeping a static
  offset, rather than being a convention reviewers are asked to uphold. The
  distinction between "we agreed to only add at the end" and "adding elsewhere
  does not compile" is the whole value.
- The three-way split of what gets called "compatible" -- wire (found out by
  deployed peers, silently), API (found out by the build), cost (found out by
  nobody) -- is the clearest statement of it seen anywhere. `situc diff` answers
  the third and part of the second **and says so**, rather than claiming to be
  a compatibility linter. Being explicit about what a tool does not check is
  what makes the part it does check usable as evidence.

## What was not obvious, and cost time

- **Whether the dependency is on a generator or a library.** It is a generator:
  generated C is checked in, so a person building netcfgd needs no `situc`. For
  a project whose whole pitch is a small dependency budget, that is the
  difference between adoptable and not -- and it was worked out by reading the
  output, not from the front page. One sentence: *"situc is a build-time tool;
  ship the generated sources and your users need nothing."*
- **What a codec `impl` is allowed to be.** Getting to `extern "..."` took
  reading. A worked example of binding a third-party AEAD -- declaration,
  extern, the exact C signature expected -- would have removed most of the
  probe's uncertainty. This is the most likely first real question for the
  use case the README names.

## Still open here, and probably a question others will have

**Vendor `situc`, commit the generated sources, or both.** Committing the
output is settled (it is what makes the dependency build-time only). Whether
the *compiler* is vendored is not: without it, a schema change requires
fetching a matching situc, and "matching" is a version compatibility question
about the generator that `situc diff` explicitly does not cover.

If there is an intended answer -- a pinned version marker in the generated
header, a `situc --version` contract, a policy on generator output stability
across releases -- it is worth stating. **Every serious adopter reaches this
question**, and each one inventing a different answer is how a generated-code
ecosystem becomes unpleasant.

> **Answered, in 21.1.** Every generated file now names its generator --
> `Generated by situc 1.0 from x.situ`, from the same `VERSION` the package
> and `situc --version` read -- so a regeneration under a different situc is
> a visible diff line rather than a silent substitution. The stability
> policy is stated with it: the committed `.situ.wire` is the compatibility
> oracle, not the generator version; output text may improve between
> versions, and byte-stable emitted code across versions is deliberately
> not promised, because it would freeze every emitter bug.

## The design pressure situ created, which is a compliment

Knowing the schema will take over more of what is hand-written -- encryption
first, plausibly chunking after -- changed how the surrounding code is being
laid out: the framing state machine gets its own translation unit with the
chunk *header* already a schema struct, so that when situ expresses it, the
file it replaces is **one file**. Nothing above `wire/` learns that any of it is
generated.

A tool people design *around*, in the expectation that it will grow into the
space, is being trusted rather than merely used. The rule that produced it --
**anything a schema could say, the schema says**, never a hand-written check
beside a generated accessor duplicating one -- might be worth stating in situ's
own documentation. Two statements of one rule is how they come to disagree, and
the one nobody edits is the generated one.

---

# Addendum: provenance, and one thing worth generating

## How direct this evidence is

Worth stating plainly, because it changes how much weight the rest deserves.

The probe described above -- the envelope, the `sealed(chacha20_poly1305)` body,
`require canonical`, `require verify_gated`, the generated C compiling clean
under `-Wall -Wextra -std=c11` -- was run from `netcfgd` against a clone, and
this file is written from that project's own written record of it. It is not a
fresh run by the person assembling these notes.

That matters in one direction only: the *observations* are first-hand and
recorded at the time, but nothing here has been re-checked against a newer
`situc`. If any of it reads as stale, it is, and the record's date (2026-08-04)
is the thing to trust.

## Ship the test that proves the gate refuses

The strongest thing in situ is that **the verify gate is a type**: the sealed
interior is reachable only through a value that `..._open()` will not produce
when `verified` is false. That is the sentence that decided the adoption.

But this family's whole working method is that **a gate nobody has watched fail
is not evidence**. Every check here is broken on purpose and observed going red
before it is trusted, because the alternative is a green result that was green
for the wrong reason. Applying that standard to situ produces an awkward
question that a prospective adopter will ask early:

> How do I demonstrate that the gate actually refuses?

Today the answer is to hand-write a test that tampers with a tag, or corrupts a
canonical encoding, or mutates a covered field and skips `finalize` -- which is
hand-writing exactly the class of code the generator exists to remove, and
getting it subtly wrong is easy. `gen-fuzz` is adjacent but different: a fuzz
harness explores, it does not assert *this specific guarantee holds*.

> **Shipped: `situc gen-tamper` (26.131).** The generated harness takes your
> verifier as a callback -- the primitive, the key and the constant-time
> comparison stay yours, per the division 14.6 now states -- and drives it
> across the schema's own coverage geometry: every covered byte and every
> tag byte flipped one at a time, refusal required for each; and for a fixed
> layout, every byte outside coverage flipped with the answer required not
> to change, which is what catches a verifier covering more than the schema
> says. The unit test's control is a deliberately lying verifier that
> ignores one covered byte: the harness names that byte's offset. Your
> sentence -- a gate nobody has watched fail is not evidence -- is the
> file's header comment.

**Suggestion: generate the negative tests alongside the accessors.** The schema
already knows every property being claimed -- what is covered, what is gated,
what must be canonical, which fields dirty the message. That is precisely the
list of things that should fail, and it is a list situ has and the author does
not:

- a flipped bit inside a covered region makes `..._open()` return `SITU_ERR_TAG`;
- a non-canonical encoding is rejected where `require canonical` was declared;
- a covered field mutated without `finalize` yields no transmittable buffer;
- an interior accessor is unreachable without a verified handle (a compile-fail
  case, so a `-fsyntax-only` fixture rather than a runtime one).

For adopters this converts "situ says the gate is a type" into "our test suite
demonstrates the gate refusing, and it goes red if the schema stops saying so."
For situ it is a regression suite over its own core promise, derived from the
same declarations, which is the argument for it independent of any user.

It also fits the rule stated in netcfgd's own design notes for living beside
situ -- *anything a schema could say, the schema says* -- one layer up: anything a
schema could **prove**, the schema should generate the proof of.

## On this directory

By the time this addendum was written, `suggestion/` had filled up with files
from sibling projects and been settled as **one file per project that wrote
it**, not per author. That is the right axis: what matters to a maintainer
reading these is which real tree hit the problem and what it needed, not who
typed it. Noted because the first draft of this file got the name wrong.
