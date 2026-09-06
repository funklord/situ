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

# Addendum, 2026-09-06: a structured-text codec, and what netcfgd wants one for

Written from netcfgd after being asked whether situ could describe the format a
**separated config compiler** would use -- a compiler running unprivileged and
returning its result over a pipe.

**The full brief is `doc/situ-brief.md` in netcfgd's tree**, and it stays there
rather than being copied here, so there is one copy to correct. This is the
part that decides the question, plus the numbers, so that reading the brief is
a choice rather than a prerequisite.

Everything about netcfgd below is measured here and is checkable. Everything
about situ is read from your `project.md` and is yours to correct; where the
two are compared, the comparison is mine.

## 1. One requirement, not a feature list

**netcfgd's document has to be greppable JSON, and situ describes binary
layouts.** That is not a gap a keyword closes -- it is the two projects'
premises pointing opposite ways.

JSON here is netcfgd's constraint 7 rather than a preference: `/run` state is
read with `cat` on a machine being debugged over the network it is in the
middle of reconfiguring, and both frozen schema witnesses are JSON. So a
situ-described pipe would mean **two encodings of one model** -- situ's binary
on the pipe, JSON on disk -- which is two things that must agree, the class
netcfgd's own decisions 0081, 0082 and 0083 are about.

**The evidence for "situ has no text format" is yours rather than asserted**,
which is why it is quoted: section 13's codec families are `table` (Manchester,
4b5b, base64), `polynomial` (CRC, Reed-Solomon), `permutation` (interleavers)
and `stuffing` (HDLC, COBS) -- line codes, not structured text. And every one
of the ten occurrences of "json" in your `project.md` is about situ's own
tooling, `--diagnostics=json` and `situc lsp` speaking JSON-RPC. Correct either
of those if the reading is wrong; the whole argument rests on them.

**The shape, if it is ever wanted, is a family in 13's sense** whose members
are text encodings -- the schema keeps describing the model and the codec
decides the spelling. Which is exactly why it may be correctly out of scope: it
collides with "byte-exact data layouts" as the stated premise, and with the
non-goal "not a serialization library for language-native objects". netcfgd is
not arguing those should move.

**So if it is out of scope, say so and the brief is finished.** Nothing below
matters if the answer to this is no, and a clean no is worth more to netcfgd
than a maybe -- it settles `doc/c-transition.md`'s codec question in one word.
The binary use netcfgd already plans -- a situ-described frame for the remote
path, `doc/socket-protocol.md` 3.2 -- is unaffected either way.

## 2. Recursion: netcfgd needs less of it than JSON does

You reported the v0 ban on recursive types is being lifted because JSON needs
it. That is right about JSON's grammar and worth splitting, because the two
cases are different sizes:

- **Arbitrary JSON needs recursion in the schema.** Depth is whatever arrives.
- **netcfgd's document spelled as JSON does not.** Its field types form a graph
  of **117 nodes with no cycles at all, nothing naming itself, depth 7 from
  `Document`.** The recursion lives in the codec, not in the type described.

The consequence is yours rather than netcfgd's: 20.1 promises the C backend
"no recursion, bounded stack", and section 2 gives non-terminating size and
capability computation as why recursion was banned. **A decoder for a schema of
known depth keeps both** -- an explicit stack of 7 is a compile-time constant,
exactly as an array's `max` is. A decoder for arbitrary JSON keeps neither.

So if the lift is meant to serve both, the shape that seems to fit your grain
is **a declared depth bound on a recursive type, the same mechanism `max`
already is for arrays.** That is a suggestion from outside; you are better
placed to say whether it holds.

*(The depth was got wrong first. A count over every capitalised token in each
type body reported 14 cycles and depth 14, having matched enum variant names as
field types. A model with a real cycle and no `Box<..>` would not compile,
which is what made the number worth disbelieving.)*

## 2a. What already fits, and it is most of the shapes

Sent second only because section 1 decides the question, not because it is the
smaller half. **The shapes netcfgd would need are expressible in situ today**,
and a reader given only the gaps would take a worse impression than the
evaluation supports:

- **Both backends netcfgd needs are done.** 20.1 lists C, C++, Python and Rust.
  netcfgd is Rust today and may become C, so the two that matter are both
  there.
- **Variable-length repetition exists.** 8.5's `T x[expr]` takes its length
  from a prior field and `T x[remaining]` runs to the end of the frame.
  netcfgd's 58 `Vec` fields have a spelling.
- **Tagged unions exist.** 9.6 `variant`, for the ~43 payload-carrying enum
  variants.
- **TLV exists** (9.5), which is how the 177 optional fields would be carried.
- **Text is checked rather than assumed.** 8.6 has no string type -- text is
  `u8 name[N]` with `[encoding = ascii | utf8]`, validated strictly in the
  sense RFC 3629 requires, an overlong form or a surrogate half refused.
  **netcfgd validates none of its own 41 `String` fields for encoding**, so
  situ would be stricter than the thing it replaced. That is the right
  direction and is worth saying plainly.

So the difficulty is not the shapes. It is section 1 and nothing else.

The whole of what would cross the boundary, since the counts above are quoted
piecemeal elsewhere in this file:

    structs                             78
    enums                               42
    enum variants carrying a payload   ~43
    fields that are Option<..>         177
    fields that are Vec<..>             58
    fields that are String              41
    fields that are a fixed scalar      65
    fields that are another model type  64

**276 of 405 fields are optional, repeated, or unbounded text.** A document for
twenty interfaces is 10,484 bytes of JSON; the frozen maximal witness is
115,004. That is the size class a text codec would be working in -- small
enough that the optimisation discussed in section 3 is genuinely optional.

## 3. The ask is much smaller than the attribute counts suggest

The first survey listed what netcfgd's model asks of serde and read it as a
feature list. Put to netcfgd afterwards: if situ is right where it disagrees
with other serialisers, might it be right about the rest? Largely yes.

Counted across `netcfgd-model/src` and `netcfgd-proto/src`:

    default                294
    skip_serializing_if    225      Option::is_none 205, Vec::is_empty 11,
                                    Not::not 8, is_zero 1
    deny_unknown_fields     89
    rename_all              44      43 snake_case, 1 kebab-case
    rename                   7
    tag                      7

Method, because a bare integer invites no re-derivation: every `#[serde(..)]`
body is extracted with a paren scanner that respects strings and split on
top-level commas, and a match inside a `//` or `///` comment is rejected. That
clause is load-bearing -- three doc comments mention these attributes in prose,
one spelling out `#[serde(skip_serializing_if)]` in full, and a line-grep and a
naive scanner miscount it in opposite directions. Two methods disagreed by one
and by two before they were reconciled; 225 is confirmed by its own predicate
breakdown summing to it exactly.

Read for what each row **is**:

- **`rename_all`, 43 of 44: not a requirement.** All 43 are `snake_case`
  converting Rust's `PascalCase` variants. A schema writes the name it wants.
  That row was measuring the tool, not the format.
- **`skip_serializing_if` (225) and `default` (294): two halves of one size
  optimisation**, "omit when the value is the type's empty one" paired with
  "restore it when absent". Emit every field always and both vanish.
- **`tag`, 7: a representation choice.** An externally tagged variant is a
  one-member object whose *name* is the discriminant, needing no ordering rule.
- **`deny_unknown_fields`, 89: already agreed**, and strongly -- section 2 and
  14.5 make never preserving unknown fields a security position, and netcfgd's
  89 uses are the same position reached separately. It is the one most
  serialisers get wrong in the other direction, and an agreement arrived at
  twice independently is worth as much here as any gap.

**What survives is one requirement and one small one:**

1. **Members are identified by name and have no order.** Irreducible -- it is
   what a text codec means. It also says which of your rules would need
   re-examining rather than extending: 9.6's "discriminant strictly before the
   variant in layout order" is not a restriction to relax for text, it is a
   statement about a world where order exists.
2. **An enum member may need a wire spelling that is no language's
   identifier.** Eight members across two enums: six of the seven `rename`s are
   the kernel's bonding modes (`balance-rr`, `802.3ad`; `broadcast` is only a
   case change and does not count), and the forty-fourth `rename_all` is
   `BluetoothProfile`'s `kebab-case`, giving `a2dp-sink` and `a2dp-source`.

So five of the six original rows are the same fact asked five ways -- situ
describes positional layouts where a field's identity is its offset, and text
has the opposite properties. A feature list invites six additions; the shape of
the answer is probably one.

**The cost of dropping the optimisation is not size, it is the witnesses.**
netcfgd's two schema witnesses are byte-exact and frozen, and every
present-but-empty field would change them and the socket's shape for every
client. So a *new* format can be described without any of this today; the
*existing* one cannot be re-spelled without a deliberate schema change.

## 4. Three smaller things, and one question

- **A first-class optional.** 177 of 405 fields. TLV (9.5) carries it, but the
  schema author then makes 177 TLV decisions to say what the source language
  says in one word, and the generated accessor is "was the tag there" rather
  than a presence type. Actionable independently of everything above.
- **A declared-UTF-8 string reaching the backend as text.** 41 fields. 8.6
  already validates the encoding strictly, which is more than netcfgd does to
  its own; what would help is the Rust backend handing back `&str` where
  `[encoding = utf8]` is declared, so the boundary does not re-validate what
  the codec checked.
- **Speculative, marked as such.** With 58 dynamically-sized repetitions every
  enclosing frame here is dynamic, so the capability lattice would report
  `sequential` and non-addressable for essentially everything. Saying that once
  about the schema would be worth more than saying it per field. netcfgd has
  not run `situc` on anything, so this is a prediction from 8.5's rules.
- **A question rather than a request: does `situc` have a schema this size in
  its own tests?** 78 structs, 42 enums, 405 fields, nested seven deep. A
  compiler fast on a 40-field frame may not be on this, and it is worth
  knowing before either project plans around it.

## 5. The question worth more than the one that was asked

The compiler pipe is a small format. The model is not.

**serde is 508,084 bytes of netcfgd's 2,783,768-byte release binary -- 18.3%**,
measured two ways agreeing within 5% (symbol attribution, and a link
differential over four scratch builds):

    serde and serde_json themselves          232,562
    netcfgd's own derived codecs             267,993
    itoa, memchr, zmij                         7,529

`netcfgd-model` is 168,328 bytes of derived codec against 53,037 of everything
else -- **three quarters of that crate is serialization.** A C port would
hand-write those codecs, and that is most of the size argument for netcfgd's
possible C transition. **A schema compiler generating them, Rust now and C
later from one description, is exactly the answer** -- and it runs into
section 1 immediately, because those codecs encode the model, and the model is
what has to be JSON.

## 6. What netcfgd is not asking for, and one thing against itself

**Not the capability lattice.** In-place mutability, addressability and
authentication coverage are situ's core and are worth nothing to a pipe between
a process and its own child. netcfgd would be using situ for codegen and schema
diffing alone, which is a thin slice of what the project is for -- a reason to
weigh the request rather than a reason to grant it.

**And netcfgd's own record argues against generating both ends**, which is
relayed rather than hidden. `client/` is a C implementation of netcfgd's socket
protocol written against the *witness* rather than against the Rust types, and
building it found **three defects in the protocol itself** -- a request the
daemon accepted that no client could send, and one operation carrying two names
depending on which message it appeared in. A generated second implementation
could not have found them, because it would have come from the same source as
the first. If netcfgd ever generates both ends it should keep one hand-written
implementation as the control. That is the cost being paid knowingly, not an
argument against situ.
