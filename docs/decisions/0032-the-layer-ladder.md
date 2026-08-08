# 0032: six layers, chosen at invocation, not in the schema

Status: accepted
Date: 2026-08-08
Phase: unscheduled -- rungs 1 and 2 already exist in part

## Context

Consumers want different amounts of situ. `fuzzypickles` wants a decode into
owned structs and nothing else. `fuzznet` wants correlation between two
frames. A third consumer would take a full send/receive loop with timeouts if
it existed. Each of those arrived as a separate scope question -- "should situ
do X" -- and each was answered on its own merits, which is how a non-goal list
comes to be relitigated once per adopter.

The questions were wrong. **The real question was never whether situ does X,
but at which layer X lives**, and the consumer chooses the layer.

Two pieces of evidence that this ladder already exists and is unmanaged:

- `situc build` carries `--materialize` (26.30), `--owned` (26.69) and
  `--single-file` (26.70). Each is a layer choice wearing boolean-flag
  clothes, each with its own refusal rules, and nothing relates them.
- The refusals are already layer boundaries described one at a time.
  `--materialize` refuses an uncapped run in C because the index array would
  have to be allocated. `--owned` refuses a variable-length member because "a
  pointer reintroduces exactly the lifetime the caller was escaping". Both are
  the same sentence: *you asked for something the layer you are on does not
  permit*.

The governing constraint from the request: **the choice of rung is made at the
`situc` command line, not in the `.situ` file.** How much of a description a
consumer wants turned into code is not a property of the protocol.

That is a statement about the *rung*, and an early draft of this record
generalised it into "behaviour is never inferred from a schema", which is a
different claim and a wrong one. It contradicted 0030 on the same day 0030 was
written: a `relation` is conversation description living in a `.situ` file.
The corrected split is below and is the more useful one.

## Decision

**Six layers, selected by `--layer`, monotone: layer N emits everything below
it. The default is `view`, which is today's behaviour.**

Each rung answers exactly one more question "yes". That is the organizing
principle, and it is what makes the ladder a model rather than a grouping of
features.

| | `--layer` | what it emits | the new "yes" |
|---|---|---|---|
| 1 | `view` | accessors over bytes the caller owns | *(baseline)* |
| 2 | `edit` | build or resize a message whose extent is not fixed | may it allocate? |
| 3 | `relate` | predicates over two messages (0030) | may it look at two messages? |
| 4 | `frame` | a byte stream in, whole messages out | may it hold bytes between calls? |
| 5 | `converse` | match a reply to its request | may it hold messages between calls? |
| 6 | `drive` | send, receive, retransmit, time out | may it own I/O? |

**Rung 6 owns I/O and never owns the clock.** The two were bundled in a first
draft of this table -- "may it own I/O and the clock" -- and `fuzznet` asked
for them to be separated before the rung exists. It is right, and the answer
is not a seventh rung: owning the clock is not a permission worth granting at
any rung. **Time enters as a parameter.** A step function taking `now_ms`
keeps every retransmission and timeout deterministic, so ten simulated minutes
run in a loop with no sleep in it and a timeout bug reproduces every run. A
wall clock read inside the state machine makes every test of it a race, and
this family has already paid for that once -- a scenario that failed in a full
run and passed on the same commit when idle, racing a twenty-second deadline
nobody had written down. That was one deadline in a UI, and rung 6 is made of
deadlines.

I/O is injectable for the same reason: a caller-supplied vtable lets a test
substitute a transcript, never open a socket, and inject loss, reorder and
duplication without a network. A convenience wrapper that reads the clock and
calls the injectable path underneath is fine and is *not* the state machine --
it keeps the tested path and the shipped path one program.

**Names, not numbers, in the CLI.** Inserting a rung would renumber every
committed build script and silently change what it asks for. `--layer 4` is a
question about this document's revision; `--layer frame` is a question about
the artifact.

### What is not a layer

**Codecs.** They were proposed as a rung -- "using encoders and decoders to
manipulate packets" -- and they are a *construct*, present from layer 1, where
`sealed` and `coded` regions already generate calls into extern codecs today.
Only one codec case needs anything more: a region whose `expansion =
unbounded` cannot report its output extent without decompressing, so it
consumes layer 2's storage. It is a consumer of a rung, not a rung.

Collapsing it is most of what turns eight plausible layers into six real ones.

**`--owned` and `--materialize` stay.** They are the *shape* of the emitted
code; the layer is the *invariant* it holds to. Keeping the two axes separate
is what lets `--layer edit --materialize` be meaningful -- see below.

## What the schema states, and what the invocation chooses

The rungs are emitters. What they emit from is the schema, and the upper rungs
need the schema to say more than bytes:

| rung | what the schema must state |
|---|---|
| `relate` | a `relation`: which fields of two messages must agree (0030) |
| `frame` | how a message's extent is found -- already stated, and `_required()` already generated from it |
| `converse` | which relation pairs a request with its reply, and what identifies the exchange |
| `drive` | the retransmission and timing contract: on expiry retransmit, at most N times, giving up how |

**These are protocol facts, not deployment settings.** Two endpoints that
disagree about which field correlates an exchange do not interoperate, and one
that retries where the other does not is a bug in exactly the class situ
exists to catch. Putting them in the schema puts them where `situc diff`
already reports a regression and `situc map` already reports a cost. A CLI
flag would make the timing contract a convention maintained by whoever is
paying attention, which is the failure mode the whole project is a reaction
to.

**Shape in the schema; value overridable at invocation.** A timeout has a
shape -- this exchange has one, expiry retransmits, at most N times -- and a
value. The schema declares the shape and may declare a default value. A
deployment may override the value at `situc` invocation and may never
introduce a shape. A satellite link and a LAN running one protocol want
different numbers and the same policy; this is the split that gives them
both, and it is the same bargain `max` on a scan already strikes, where the
schema states the bound and the consumer lives within it.

**The invariant across every rung: no rung invents a fact the schema did not
state.** A `drive` emitter emits the policy the schema declares; where the
schema declares none, the rung is absent rather than defaulted. A generated
scheduler with a timeout nobody wrote down is exactly the hidden convention
this tool exists to replace.

## Three properties this must be built with

**Every layer is an emitter over the same `ResolvedSchema`.** The layout
solver, the propagation table (invariant 1) and the capability lattice never
learn that layers exist. A rung is a consumer of the resolved schema exactly
as the four backends are. This is the property that stops the core rotting as
the ladder grows upward, and a rung that cannot be written this way is a rung
that has been designed wrong.

**The layer is the invariant statement.** `--layer view` guarantees invariant
4; `--layer edit` does not. This makes `no_alloc(X)` decidable -- Section 16
currently records it as one of four predicates the compiler "names and cannot
decide", because "generated code never allocates, so it always holds; the
predicate would be a lint, not a requirement". The layer decides it, with no
schema keyword. The capability map gains a layer row, so `situc diff` catches
a schema edit that pushes a consumer up a rung, which is the trap that makes a
wrongly-written schema visible rather than merely refused.

**It reinterprets an existing refusal rather than adding one.** `--materialize`
refuses an uncapped run in C today. Under the ladder that is not a special
rule: it is "you are on layer 1". `--layer edit --materialize` is then
permitted and means something exact. An existing rule falling out of a new
model, instead of surviving beside it, is the evidence that the model fits.

## A rung adds files and changes none

**`--layer N` emits every file `--layer N-1` emits, byte-identical, plus new
ones.** This is an invariant, not an aspiration, and it is what makes the
ladder cheap to climb: a consumer who has reviewed and committed the generated
output of one rung sees only *additions* in the diff when they move up, and
has no audited file to re-audit.

There is precedent for the shape. `--owned` already emits "into its own
header, deliberately" (26.69), on the reasoning that a caller should choose
the expensive accessor rather than find it by autocomplete. Additive files are
the same argument one level up.

Two mechanical rules make it hold, and both are the kind that fail silently if
they are not checked:

- **Includes point down the ladder, never up.** `frame.h` may include
  `view.h`; nothing in `view.h` may know a rung above it exists.
- **No conditional compilation in a lower rung's file.** A
  `#ifdef SITU_LAYER_DRIVE` inside the view header is additivity lost while
  still appearing to hold, since the file list would not change.

**Enforced by test, per schema, per adjacent pair**: generate at rung N-1 and
at rung N, assert the file set grows and that every file in both is
byte-identical. That is cheap over the schemas already in the tree, and it is
the difference between a property and a claim -- the same reason
`test_the_generated_build_lists_every_schema` exists.

Two honest exceptions, named so they are not discovered as bugs:

- **The capability map.** If it grows a layer row, it changes between rungs
  rather than being added to. Either the layer facts go in their own artifact
  or the map stays layer-independent; unresolved, and small.
- **Shape flags are outside this invariant.** `--single-file` inlines the
  runtime by construction (26.70), so it cannot be additive and is not
  claimed to be. Additivity is a property of the *layer* axis with the shape
  held fixed.

## Where the ladder forks, and why it is kept straight anyway

**`frame` and `converse` are orthogonal, not stacked.** A datagram protocol
needs conversation without framing -- `fuzznet` is one -- and a
length-prefixed stream reader needs framing without conversation. Strict
nesting is a simplification here, not a fact, and this record says so rather
than letting a later reader discover it.

Kept linear regardless, for two reasons. For a datagram schema `frame`
degenerates to a passthrough, and 0022 *measured* that an unused
`static inline` in a header is never emitted at all -- 40 of 44 functions in
one schema. So the degenerate rung costs nothing in the artifact. And one
ordered axis a consumer picks a point on is worth more than two flags they
must reason about together, which is the failure the existing boolean flags
already demonstrate.

If a consumer ever needs `converse` without `frame` badly enough to pay for
the split, that is a decision to make then, with the case in hand.

## Alternatives considered

**A schema directive.** 0031 proposed `allocation none | caller | dynamic` in
the `.situ` file. Withdrawn by this record: the layer subsumes it, and putting
the choice in the schema makes one consumer's deployment decision into every
consumer's wire contract. A schema describes bytes.

**Numbered layers in the CLI.** Rejected above: renumbering silently changes
what a committed script asks for.

**Leaving the boolean flags alone.** They work, and each new one costs a
refusal rule that relates to nothing. The count is three and rising; the
questions arriving from consumers are ordered even though the flags are not.

**Two axes, framing and conversation as independent flags.** Honest to the
world and worse to use. Revisit if a real consumer needs it.

## Consequences

- Section 2's "Not a protocol runtime" is replaced by a narrower and truer
  non-goal: **no behaviour the schema did not state.** The schema describes
  the conversation; the rung decides how much of it becomes code; nothing is
  defaulted or inferred.
- 0030 is amended: its boundary held for what a schema construct may
  *quantify over* and was written as though it held for the whole tool.
- 0031's `allocation` directive is withdrawn. Its enumeration stands -- it is
  what layer 2 has to serve, and case E is why layer 2 exists at all.
- `no_alloc(X)` moves from tautology to a decidable predicate.
- A timeout specification is schema content: shape and optional default in the
  `.situ` file, value overridable at invocation. Section 7's grammar and
  Section 16's `must` vocabulary both grow to carry the `converse` and `drive`
  constructs, and neither is designed here.
- Invariant 8 -- nothing about the machine running `situc` may reach the
  generated code -- is worth re-reading before `drive` is built. It is the
  first rung whose output depends on libc, and the generator's host must not
  be where that dependency is decided.
