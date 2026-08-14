# 0030: cross-message relations, as a predicate over two views

Status: accepted
Date: 2026-08-08
Phase: after 26.94

## Context

`fuzznet` is a shared authenticated datagram protocol that `fuzzypickles`,
`netcfgd` and a planned `raidcfgd` will link. Its frame is already a schema --
`wire/frame.situ` compiles, and `situc map`, `wire` and `advise` all run
against it. `suggestions/fuzznet.md` records what writing that schema taught,
and asks one scope question: can situ describe a relationship between two
messages rather than the layout of one.

The question arrived with a correction attached, which is the reason to settle
it in writing. `fuzznet`'s design document claimed, in two other projects'
documents as well as its own, that situ "does not run a protocol -- its
non-scope list rules out service and RPC definitions entirely". That claim was
wrong. It came from Section 19.2, which is about what a `.proto` will not
translate, and was read as a general scope statement. Section 2 says nothing
about protocols at all, and establishing the real position took a grep proving
a negative: "conversation", "correlation", "request/response" and "session"
appear nowhere in eleven thousand lines except as ordinary English.

A wrong claim about scope propagated into two repositories because the scope
was stated nowhere. That is the cost this record is paying off, and it is why
the boundary below is written as much for the reader deciding *not* to use
situ as for the one adopting it.

## What a protocol's invariants actually look like

Four examples motivated the request. Sorting them is what makes the boundary
non-arbitrary rather than a line drawn where the current implementation
happens to stop:

| invariant | shape |
|---|---|
| a response carries the request's identifier | two views |
| a retransmission is byte-identical except in its header | two views, plus a predicate that does not exist yet |
| a chunk's `index` is below its own `chunks` count | **one** view -- already a plain `require` |
| an acknowledgement names a sequence number that was actually sent | quantifies over the set of messages sent |

Only the first is served by what this record decides. The third needs nothing
new and was miscounted as a cross-message case. The fourth is the important
one: it needs the set of what was sent, which is a store with insertion and
expiry, and Section 2 forbids the allocation that implies. It is not excluded
because it is hard. It is excluded because there is no parameter a pure
predicate could take that would answer it.

## Decision

**A `relation` is a named, pure predicate over exactly two views. It holds no
state, allocates nothing, and does not know which messages exist.**

```ebnf
decl          = const_decl | enum_decl | struct_decl | codec_decl
              | register_decl | requirement | invariant | relation_decl ;

relation_decl = "relation" ident "(" param "," param ")" "{" { must } "}" ;
param         = ident ":" qualified ;
must          = "must" expr ";" ;
```

```situ
relation response_to(request: fzn_frame, response: fzn_frame) {
	must response.head.msg   == request.head.msg;
	must response.head.index <  request.head.chunks;
}
```

Three parts of that are load-bearing.

**Parameter order is temporal.** The first parameter is the message seen
first. A dissector needs to say "response to frame N" and a fuzz harness needs
to know which message to copy bytes *from*; making order carry it means
neither needs a second declaration, and there is no way to write a relation
that omits the fact.

**The body says `must`, not `require`.** Section 16 fixes `require` as a
build-time capability gate whose failure is a compile error. A relation body
is a run-time check over two values. Reusing `require` would give one word two
meanings in a language whose stated rule is one word per concept, and the
run-time vocabulary already has a root: `must_eq` and `must_be_zero`, whose
failures are already `SITU_ERR_CONSTRAINT`.

**Exactly two parameters.** A third is rejected naming its phase. Three-way
relations are, in every case examined, quantification over a set wearing a
disguise -- and that is the case this record excludes on purpose.

## What the compiler emits

```c
/* Check the `response_to` relation. Pure: reads two views, holds nothing,
 * allocates nothing, and does not know which frames exist. The caller owns
 * the pairing; this answers only whether a pairing is well-formed.
 *
 * SITU_ERR_CONSTRAINT   a `must` did not hold
 * SITU_ERR_STAGE        a field read here sits behind a verify gate that
 *                       has not been passed on that view
 */
situ_err_t situ_rel_response_to(situ_view_t request, situ_view_t response);
```

Three properties of that signature are absences, and each is evidence the
construct fits rather than intrudes:

**No new failure class.** `SITU_ERR_CONSTRAINT` already means "must_eq, max,
must_be_zero violated". An eighth class would have to arrive in all four
runtimes, and `test_the_failure_classes_match_the_runtimes` exists precisely
because one added by hand to three of them went unnoticed. A design needing a
new class is paying a real tax; this one does not.

**No `situ_msg_t`, therefore no staleness check.** The two views come from two
different messages with independent generation counters, so there is no single
`msg` to check against. This matches `validate`, which also takes a bare view
-- consistency with the existing family rather than a two-message checked
variant invented for one caller.

**No new `stage` value.** `suggestions/fuzznet.md` floats "knowable only from a
message already seen" as a fifth stage, neighbouring `CompileTime <
ParseTime < TransformTime < VerifyGated`. Rejected, and the signature is the
argument: because the predicate is *parameterised* over both views, every
field is `ParseTime` within its own view and the existing axis answers
everything already. `SITU_ERR_STAGE` then falls out for free -- a `head.msg`
inside a `sealed` block inherits its gate with no extra rule. A stage value
would instead let a lone struct reference another message, and that field's
accessor would need somewhere to look the other message up. That lookup is the
store, and the store is the boundary gone.

Naming is `situ_rel_<name>` rather than `situ_<name>_validate`. The existing
scheme is `situ_<struct>_<op>` and a relation has no owning struct, so the
struct slot has nothing to put in it.

Comparison is on **values**, not bytes: a big-endian `u16` against a
little-endian `u32` compares correctly through the `repr` axis. That is
exactly where a hand-written correlation check gets fumbled, and it is
available here for nothing.

## Why this is worth more than the C it deletes

Stated plainly because the adoption argument is easy to overstate, and an
overstated one is discovered on contact.

**Directly, it removes little.** The check itself is one to three comparisons
per pair. `fuzzypickles` reports about fifteen request/response pairs in
`core/src/control.h`; `fuzznet` has its chunking header. That is tens of lines.
The correlation *table* -- the bulky part -- stays in the consumer
permanently, by the decision above.

**The value is in two artifacts already generated.** The equality constraints
*are* the conversation key: `must response.head.msg == request.head.msg` tells
`gen-dissector` what to hash a conversation on and what "response to frame N"
means, with no second declaration. For an encrypted protocol, where the
payload is opaque, correlation is most of the remaining value a dissector can
add, and today it is hand-written Lua per protocol. `gen-fuzz` reads the same
constraint as "copy these bytes from A into B", which is the difference
between a harness that reaches the interesting code and one that reproduces
the failure mode where a target reaching nothing looks identical to a clean
run.

The rule `netcfgd` asked situ to state -- anything a schema could say, the
schema says, never a hand-written check beside a generated accessor
duplicating one -- is the general form of this, and a correlation check is one
of the last places a consumer still writes one by hand.

## Alternatives considered

**A new `stage` value.** Rejected above; it converts a parameter into a
lookup.

**`require` in the body.** Rejected: it overloads a word that means
compile-time failure with one that means run-time failure.

**Byte-range identity in this record.** The retransmission case wants
`must identical(first.body, again.body);`. Field equality needs nothing new;
byte identity needs a new predicate and is only decidable where the range is
`canonical`, which situ already computes. Split out rather than smuggled in,
so that the predicate arrives with its own reasoning and its own refusal
message.

**Growing to protocol dynamics** -- retransmission, timers, windows,
congestion. Rejected, and worth recording that the consumer who would benefit
argued against it first. Those need a state machine, which contradicts two
Section 2 non-goals directly (no dynamic allocation, and not a runtime
interpreter), and Section 0's rule that the capability lattice wins: a
scheduler has no capability vector and nothing to contribute to one. The line:
**relations are data about bytes and belong here; behaviour is a program and
does not.**

## Consequences

- Section 2 gains a line saying what situ does about protocols, so the next
  reader does not have to prove a negative by grepping.
- Section 7 gains `relation_decl`; Section 16 gains the `must` statement and
  its relationship to `require`.
- `gen-dissector` gains conversation tracking, and `gen-fuzz` gains sequence
  awareness. Neither is unlocked by anything else.
- Nothing in this record requires allocation, a timer, or a new failure class.
  That is a deliberate property, and a later proposal that breaks it is
  changing this decision rather than extending it.

## Amendment, 2026-08-08: the boundary was the language's, not the tool's

Everything above is as first written and the technical content stands. One
framing in it was too wide, on the same day, and decision 0032 corrects it.

This record closes with "relations are data about bytes and belong here;
behaviour is a program and does not", and reads throughout as though
retransmission, timers and correlation tables are out of situ altogether.
That is too wide. 0032 puts framing, correlation and a send/receive loop on a
layer ladder above the `relate` rung this record defines, and a schema states
the conversation those rungs emit -- the pairing, the retry policy, the
timeout's shape and default.

**The distinction that survives is about state, not about behaviour.** A
schema *describes*; it does not *hold*. This record's real result is that a
`relation` needs no store, which is why it sits at rung 3 and costs nothing --
and the four-example table above still sorts correctly, because what it sorts
on is whether a store is required.

That resolves the ack case, which this record wrote off as permanently
inexpressible. "An acknowledgement names a sequence number that was actually
sent" was rejected because there is no parameter a pure predicate could take
that would answer it. True, and it was the wrong conclusion: the *schema*
can state the rule perfectly well, and the *set* is held by generated
`--layer converse` code. Description in the file, state in the emitted code.
The predicate this record defines simply is not the construct that expresses
it.

What remains ruled out at rung 3, unchanged: a `relation` is two views, pure,
and allocates nothing.

## Amendment, 2026-08-14: an identifier is often not a number

The table at the top of this record opens with "a response carries the
request's identifier". In an authenticated protocol that identifier is a
public key or a nonce -- thirty-two bytes, essentially never an integer -- and
until now the first example this decision gives could not be written unless
the identifier happened to be scalar. `==` and `!=` between two fixed-size
arrays of the same element type and the same length are emitted now, in all
four backends.

**The refusal that blocked it was never decided.** Nothing in this record
mentioned arrays, `project.md` did not either, and no test pinned it. What it
had was a neighbour: it stood immediately beside the float refusal, which *is*
a judgement and says so -- "an exact comparison of one is rarely what a wire
contract means" -- and inherited the look of one by proximity. Its own wording
asserted a definition rather than a reason. Raised by fuzznet, whose
`same_message` correlates on a `u8[32]` sender and had to enforce it by hand
in the one place where getting it wrong is a security bug rather than a bug.

**It is a different operation, not a relaxed scalar.** Everything else here
resolves to `(signed, bits)` and widens toward a 64-bit comparison; equal-length
bytes have nothing to widen and no signedness to reconcile. So the plan carries
a `ReadBytes` binding and a `BytesEqual` beside the expression rather than in
it. C spells it `memcmp`, C++ the same over a span, Rust and Python with `==`
on a slice. Putting an array operand in the expression tree would have made
four backends each decide what one means, which is what this module exists to
prevent.

**Three refusals are kept, each with its own reason rather than one shared
one.** Differing lengths, because a comparison holding only over the shorter
one answers a question the schema did not ask, and a mismatch is almost
certainly a mistake. Ordering, because an identifier has no order: `<` over two
keys is a question nobody asked. One side an array and the other a scalar,
which keeps the message it always had, about the side that is wrong.

**Two consequences, both recorded rather than discovered later.**

The packed image does not carry these. `pack.compile_relation` trusts this
module to have refused everything it cannot emit, and that stopped being true:
the program would have carried a scalar read of a run, which the walker refuses
at evaluation time rather than at pack time. Such a relation is `unencodable`
now, with the reason. Teaching the image means a new opcode in three
implementations -- the packer, `walker/vm.py` and `walker/c/situ_walk.c` --
with the drift test that ties them together, and that is its own piece of work.

Rung 5 still refuses an array-keyed exchange, and now says why. A packed
conversation key is one 64-bit word; thirty-two bytes do not fit, and hashing
them would make two exchanges that collided indistinguishable -- which is the
failure the width check exists to prevent. So `relate` reaches the example this
record opens with, and `converse` does not. What a conversation key should be
when it exceeds a word is a question about collisions rather than about
codegen, and it is not answered here.
