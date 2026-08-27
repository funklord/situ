# situ, from a library that three projects depend on

Written 2026-08-08 from `fuzznet`, the shared authenticated datagram protocol
being built for `fuzzypickles`, `netcfgd` and a planned `raidcfgd`. The vantage
is unusual and worth stating, because it changes which of situ's properties
matter: this is not one project deciding whether to depend on situ. It is a
library whose *whole product* is a wire format, which three unrelated
consumers will link, and whose maintainers cannot see each other's traffic.

The decision was made before this file: **the frame is a schema, not
hand-written C.** `wire/frame.situ` exists, compiles, and `situc map`, `wire`
and `advise` all run against it. What follows is what that first real schema
taught, and one suggestion that is a scope question rather than a bug.

## The correction this file owes you first

`fuzznet`'s design document claimed, in writing and in two other projects'
documents, that situ "does not run a protocol -- its non-scope list rules out
service and RPC definitions entirely". **That was wrong**, and the mistake is
instructive: the line came from Section 19.2's protobuf importer, about what a
`.proto` will not translate, and was read as a general scope statement.

Section 2's actual non-goals say nothing about protocols at all. The true
position is narrower and had to be established by absence: there is no
construct for retransmission, reassembly or timers; none of the thirteen phases
plans one; and **"conversation", "correlation", "request/response" and
"session" appear nowhere in eleven thousand lines** except as ordinary English
about working sessions.

**Suggestion:** say so in Section 2. A reader deciding whether to model a
protocol here currently has to prove a negative by grepping. One line -- "situ
describes a message, not a conversation; cross-message relationships are
Section N or nothing" -- would have prevented a wrong claim propagating into
two repositories.

## The suggestion: describe the relation, not the behaviour

This is the scope question, and it is put as a question because we are the
consumer asking for it, which is the weakest possible position from which to
argue that somebody else's project should grow.

**Cross-message *relations* are declarative and would fit the existing idiom.**
A protocol's invariants are mostly statements about bytes in two messages
rather than one:

- a response carries the request's identifier;
- a chunk's `index` is below its own `chunks` count;
- an acknowledgement names a sequence number that was actually sent;
- a retransmission is byte-identical to the original except in its header.

None needs a runtime, a timer or an allocation. They are `require`-shaped, and
a sketch in the existing grammar's spirit might be no more than:

```
relation response_to(request: fzn_frame, response: fzn_frame) {
	require response.head.msg == request.head.msg;
	require response.head.index < request.head.chunks;
}
```

**Two artifacts you already generate would improve the day this landed**, which
is why it is worth more than it looks:

1. **`gen-dissector`.** After decoding fields, the most valuable thing a
   Wireshark dissector does is conversation tracking -- "response to frame N",
   and the filter that follows one exchange out of a capture. situ emits
   dissectors today and cannot express the single thing that would make one
   tell that story. For an encrypted protocol, where the payload is opaque
   anyway, correlation is *most* of the remaining value.
2. **`gen-fuzz`.** A harness that knows a response must echo an identifier can
   produce sequences that get past the first check. Without it, a fuzzer
   rediscovers the correlation by luck or never reaches the interesting code
   at all -- the same "target that reaches nothing looks identical to a clean
   run" problem that shows up whenever a negative result has no positive
   control.

The `stage` axis also already reasons about *when* a value becomes knowable
(`CompileTime < ParseTime < TransformTime < VerifyGated`). "Knowable only from
a message already seen" is a recognisable neighbour of that, and might be an
axis value rather than a new subsystem.

### And the boundary, which we think you should keep

**Protocol dynamics should stay out**, and we would argue against them even
though we are the ones who would benefit. Retransmission, timers, windows and
congestion need a state machine, and that argues with two of Section 2's
non-goals directly: **no dynamic allocation, ever**, and "not a parser
combinator library -- the layout solver is a compiler pass, not a runtime
interpreter". It would also break Section 0's first rule, that the capability
lattice wins whenever anything conflicts with it: a scheduler has no capability
vector and nothing to contribute to one.

So the line we would draw: **relations are data about bytes and belong here;
behaviour is a program and does not.** A schema that says a response echoes an
identifier is still describing a layout property, just one whose scope is two
messages. A schema that says when to resend is describing a program.

## What the first real schema taught

Four things, from writing `wire/frame.situ` and reading what came back.

**1. The `require` lines are enforced, and the diagnostics are the product.**
Asking for `in_place` on a tag-covered field fails with the blame chain, the
eleven other fields sharing the weakness, and both remedies named -- move it
out of coverage, or accept the recomputation and say `in_place_dirty`. That is
better than most compilers manage and it is what makes a schema reviewable by
somebody who did not write it.

**2. `situc advise`'s cost model assumes an access pattern, and should say so.**
It suggested moving frequently-rewritten fields out of tag coverage, because
"each write costs a recomputation over 1099 bytes". True, and irrelevant here:
this frame is built once and sent, so every covered field is written *before*
the tag exists and they cost one recomputation between them, not eight.

The suggestion is not wrong, it is conditional -- and the condition is
invisible. **Suggestion:** let a schema declare its access pattern
(`access_pattern = build_once` against `mutate_in_place`), or have `advise`
name the assumption in its output. A ranked, costed suggestion whose cost model
does not match the usage is the same shape as a gate that cannot model what it
checks: the reader has to know enough to ignore it, and the reader who does not
will obey it.

**3. Alignment padding is worth less in a big-endian format than the `packet`
example implies.** `example/packet/packet.situ` pays three reserved bytes to
keep multi-byte fields on their natural boundaries, and explains the trade
well. But in a big-endian schema on a little-endian host every one of those
fields is `repr=ValueConverted`: the value is not the memory, no caller can
take a pointer, and each access is a read-swap-write whatever the offset. The
padding buys much less than it appears to.

**Suggestion:** the example is widely copied precisely because it is good, so a
sentence there -- "on a `endian big` schema targeting little-endian hosts, this
padding buys less; check the `repr` column before paying for it" -- would stop
the trade being inherited unexamined. `advise` could say it too, since it has
both facts already.

**4. The capability map found a design question on its first run**, before any
implementation existed to be attached to. The frame carries 96 bytes of fixed
overhead, of which a capability identifier and a nonce are 56 -- and seeing
that laid out is what prompted the question "why is identity in this frame
twice", which is a protocol design question rather than a layout one. That is
the tool doing something better than checking: it made a cost legible early
enough to be cheap to change. Worth saying in the README, because "actionable
design feedback" in the tagline undersells what actually happened.

## What we are not asking for

Stated so the suggestion above is not read as the thin end of something:

- **No wire format opinions.** Section 2's "situ owns no encoding" is exactly
  right and is part of why this library can adopt it -- three consumers with
  three threat models cannot be handed an encoding by their schema compiler.
- **No key management, no handshake, no session establishment.** The extern
  codec boundary is in the right place: we bind Monocypher and situ never
  learns what a key is.
- **No allocation, no scheduler, no timers**, per the boundary above.

## The one thing that would most change our answer

`fuzznet` will carry traffic that reconfigures infrastructure remotely, across
untrusted networks, for at least two of its three consumers. If situ ever grows
a **generated constant-time comparison** for tag verification, or a documented
statement about what the generated verify path does and does not leak in
timing, that would matter more to us than any feature in this file. Today we
assume the extern codec owns that and we own the comparison; a sentence
confirming the division would be worth having.

> **Answered, in 14.6.** Your assumption is confirmed by reading the emitted
> code rather than the intent: the generated code never computes or compares
> a tag anywhere -- `_open()` takes the caller's *verdict* as a parameter,
> and the one `== tag` in any backend compares a TLV wire-tag number. What
> situ generates is the geometry (covered span, self span, `self_as`); the
> primitive and its constant-time comparison are yours. 14.6 now says so in
> normative text, and says a future generated comparison would be a decision
> record rather than a drive-by.

---

# Addendum, 2026-08-08: situ is taking on full protocol handling

The maintainer has decided situ will grow full protocol handling, and that
`fuzznet` is its first tester -- verifying structure and code, not just
consuming output. This section is written on being told that, and it starts by
withdrawing an argument.

## The argument above is overruled, and here is what that costs

Everything under "And the boundary, which we think you should keep" argued
*against* this. That argument was made in good faith and from situ's own
Section 2, and it is now somebody else's call, correctly made by the person who
owns the project. It is left standing above rather than edited away, because a
recommendation that quietly becomes agreement is worth nothing the next time
one is offered.

What survives the reversal is not the conclusion but the two obstacles it
named, which do not go away by being overruled:

- **"No dynamic allocation, ever."** A protocol needs reassembly buffers, a
  retransmission queue and a table of what is in flight.
- **"Not a runtime interpreter -- the layout solver is a compiler pass."** A
  state machine runs.

**Both are answerable, and the answer is the same one that makes this
testable**, which is the whole of what follows.

## The no-allocation rule is a testing superpower, not an obstacle

If the caller supplies the state block -- as the caller already supplies every
buffer situ touches -- then a protocol instance is **a value, not a process**.
That single property buys the entire test strategy:

- a test can construct **any** state directly, including states unreachable by
  normal operation, which is where protocol bugs live;
- a test can **copy** a state, drive both halves differently, and compare;
- a state is **inspectable by memcmp**, so "did this change anything?" is
  answerable without an API for asking;
- there is no teardown, so a test that fails mid-way leaks nothing.

Compare a protocol library that owns its allocations: every one of those turns
into a mock, a hook or a leak check. **Please do not relax the rule to make
protocol handling easier.** It is the constraint that makes the result
verifiable, and I would rather write the arena than lose it.

## What I need in order to be able to verify it at all

Five requirements, in the order that a missing one would cost most. Each is
here because its absence has already cost this family real time.

**1. An injected clock. Nothing may read the wall clock.**
Timers are the substance of protocol dynamics and are untestable when the
implementation looks at `time()`. This is not hypothetical here: a `tui-scan`
case in `fuzzypickles` failed under load, and the cause was the *test* racing a
twenty-second deadline it did not know about. The fix cost a day. A protocol
with retransmission timers has a dozen such deadlines, and every one of them
becomes a heisenbug on a loaded machine unless `now` is a parameter.

`situ_proto_step(state, now_ms, ...)` -- where the caller decides what `now` is
-- makes "what happens after 400ms" a test rather than a `sleep`.

**2. An explicit step function. No threads, no sleeping, no internal loop.**
The caller drives. Given (1) and (2), a whole scenario -- send, lose the third
chunk, time out, retransmit, acknowledge -- is a deterministic sequence of
calls with no concurrency in it and no flakiness available.

**3. Transitions must be observable as data, not inferred from bytes.**
I need to assert *why*, not only *what*. "It retransmitted chunk 3 because the
timer expired" and "it retransmitted chunk 3 because a NACK arrived" produce
identical datagrams, and a test that cannot tell them apart passes when the
implementation does the right thing for the wrong reason. An event or an
enumerated last-transition on the state is enough; it does not need a log.

**4. Fault injection has to be first class, because the faults are the
subject.** A datagram protocol exists to survive loss, reordering, duplication
and delay. If the generated harness lets a tester apply exactly those four to a
transcript, the interesting tests are cheap and everyone writes them; if not,
each project rebuilds a worse version. `gen-fuzz` already exists and this is
its natural extension: fuzz the *sequence*, not only the frame.

**5. Tell me what is designed and what is built.** The phase plan with its
per-phase status was the single most useful thing in `project.md` when this
library was deciding what it could rely on. I checked whether `relation` parsed
before writing it into a schema, found it did not, and wrote nothing -- a
minute's work that avoided a schema that stops compiling. Keep that property.
For protocol handling it matters more, because a half-built state machine
*runs*, and something that runs looks finished.

## What this library brings that will find the bugs

Stated so the design can be aimed at real difficulty rather than a toy:

- **Two consumers that disagree about reliability.** `netcfgd` wants commands
  that expire and are refused when stale; `fuzzypickles` wants messages that
  survive a peer being switched off for a week. Any protocol design that
  assumes one of those is wrong for the other, and this library has to serve
  both -- see `project.md` section 13, where the same disagreement has now surfaced
  four times in different clothes.
- **Hostile networks and unreachable peers.** Neither consumer is LAN-only any
  more. Datagrams cross NAT, arrive by relay hours late, or never arrive. A
  frame that needs a live session to be interpreted is wrong here, and the
  reassembly path must bound the memory a half-finished response may hold or a
  stranger can exhaust it.
- **A real workload with a real size problem.** A `netcfgd` `status` response
  is an entire observation -- every link, address, route, backend and DNS scope
  -- and is comfortably past any UDP MTU on a router with a dozen interfaces.
  Chunking is not a feature demo here; it is the reason this is hard.
- **An adversary who benefits from confusion.** This traffic reconfigures
  infrastructure remotely. A replayed or reordered command is a configuration
  change, so "eventually consistent" is not an acceptable failure mode and
  freshness is a security property rather than hygiene.

## How I will report

So the feedback is worth acting on rather than another opinion:

- **A finding comes with a reproduction**, and where the cause is a race, with
  the deliberate version that makes it happen every time rather than the
  flaky one that found it.
- **A negative result comes with its positive control.** "The fuzzer found
  nothing" means nothing until something planted is found. This family has
  been bitten by a clean run that reached no code, by a gate over an empty file
  list, and by ten sabotage runs that all reported "not caught" because no
  rebuild had happened.
- **I will not report a passing test as evidence unless I know what it
  executed.** Where a check could have passed vacuously, I will say which
  possibility I eliminated and how.
- **Where I think a design is wrong I will say so once, with the reason**, and
  then test what was built rather than what I would have built. The argument
  withdrawn at the top of this addendum is the precedent.

## The one question worth settling before code

**Does a protocol description live in the schema, or beside it?**

A `.situ` file today is a pure description of bytes with no behaviour in it,
which is why it can be committed, diffed, and reviewed by somebody who does not
know the implementation. A state machine is behaviour. If it goes in the same
file, every existing schema's reviewability is diluted by a construct most of
them will never use; if it goes in a companion file that references the schema,
the two can evolve separately and the byte contract stays what it is.

I have no vote and a weak view -- companion file -- but the choice shapes what
I would be testing, so it is worth being explicit about before there is code to
change.

## Follow-up, same day: 0032 answers the open question, better

The question above -- schema or companion file -- is answered by decision
0032, and the answer is neither, which is better than the weak view offered
here. The layer is chosen at `situc build --layer`, not in the file: "a schema
describes bytes. What a consumer wants generated from it is not a property of
the bytes." That is the right cut, it explains three existing flags that were
layer choices in boolean clothing, and it retires the whole shape of question
this file was asking -- "should situ do X" becomes "at which layer does X
live", which is answerable once instead of once per adopter.

**One thing to separate before layer 6 is built**, and it is the single most
useful sentence this tester can offer:

> Rung 6 `drive` is described as "may it own I/O **and the clock**".

Those are two permissions and they should not travel together. Owning I/O --
calling `sendto`, holding a socket -- is what makes `drive` worth having.
Owning *the clock* is what makes it untestable, and the two are separable at
no cost:

- **"may be given a clock"** -- `situ_drive_step(state, now_ms, io)` -- keeps
  every retransmission and timeout deterministic. A test drives ten simulated
  minutes in a loop with no `sleep` in it, and a timeout bug reproduces on
  every run rather than on a loaded machine every third Tuesday.
- **"may call `clock_gettime`"** puts a wall clock inside the state machine,
  and every test of it becomes a race. This family has already paid for that
  once: a `fuzzypickles` scenario failed in a full run and passed on the same
  commit when idle, because the test raced a twenty-second deadline nobody had
  written down. That was one deadline in a UI. Rung 6 is *made of* deadlines.

The same split serves I/O: if the caller passes an io vtable, a test
substitutes a transcript and never opens a socket -- which is also how loss,
reorder and duplication get injected without a network. So the rung's question
might be read as **"may it own I/O and be driven by a clock?"**, with both
supplied by the caller, and nothing about that weakens what the rung does.

If a default is wanted for ordinary use, a `situ_drive_step_now()` that reads
the clock and calls the injectable one underneath costs a line and keeps the
testable path the real path rather than a special case for tests -- which is
the property that matters, because a test path that differs from the shipping
path tests the wrong program.

**Also noted, since it lands on this library's plans:** 0031 records that
`fuzzypickles` cannot adopt situ today because 225 call sites hold decoded
structs outliving the buffer, which makes it a `--owned` consumer at rung 2
rather than a `view` consumer. `fuzznet`'s own migration order has fuzzypickles
adopting this library last, so the two constraints meet there and neither is
urgent -- but a rung-2 requirement discovered at that point would be expensive,
and it is known now.

---

# Bug, 2026-08-14: one unimplemented feature, four inconsistent answers

Found while building `fuzznet`'s `chain/`, by running `situc build` against
`wire/frame.situ` for the first time. situc 1.0.

**The schema `fuzznet` has committed since 2026-08-08 does not build.** That
was not known here, and the reason it was not known is worth stating first,
because it is the failure this file's own reporting terms are about:
`fuzznet`'s `project.md` cited `situc wire`, `situc map`, `situc advise` and
`require canonical` as evidence the schema was good. All four still pass.
`situc build` had simply never been run, and a passing check is not evidence
for a property it does not cover. That half is ours.

## The reproduction

Six lines, no crypto, no relations:

```
target buffer;
endian big;

struct m {
	u16 total  [must_ne = 0];
	u16 index  [max = total - 1];
}
```

    situc wire  min.situ      # OK -- and see below
    situc map   min.situ      # OK
    situc build min.situ      # error: `total` is not a compile-time constant
    situc verify min.situ ... # same error

The same shape in `require` form, which is the spelling decision 0030 names:

```
require m.index < m.total;
```

    situc wire  req.situ      # OK
    situc map   req.situ      # error: field references are not compile-time
    situc build req.situ      # constants ... reading a value out of a parsed
                              # message arrives in phase 5

## What is actually wrong, which is not "build rejects it"

Refusing an unimplemented construct is correct. **Four commands disagreeing
about whether it is legal is not**, and the disagreement is not even
consistent between two spellings of one idea:

| | `[max = total - 1]` | `require m.index < m.total` |
|---|---|---|
| `wire` | accepts, **and publishes the bound as contract** | accepts |
| `map` | accepts | refuses, names phase 5 |
| `build` | refuses, blames "compile-time constant" | refuses, names phase 5 |
| `verify` | refuses | -- |

Three things follow, in the order they cost us:

1. **`situc wire` emits `max=total - 1` into the committed byte contract for
   a bound nothing can enforce.** That is the dangerous one. `fuzznet`'s
   `project.md` records, as a win, that "`frame.situ` now carries
   `[max = chunks - 1]`, which `situc wire` reports as part of the contract"
   -- so this library believed it had a check it does not have, and believed
   it on the strength of situ's own output. A contract entry for an
   unenforceable constraint reads as protection.
2. **The two diagnostics are not equally honest.** "reading a value out of a
   parsed message arrives in phase 5" is a good diagnostic: it names what is
   missing and where it lands. "only `const` values and enum members are
   compile-time constants" describes a rule without saying whether the thing
   asked for is unimplemented or wrong forever, and a reader takes it as the
   second.
3. **The phase it names is marked complete.** 26.5, "Phase 5: expressions and
   dynamic layout", reads **Status: complete**. So either the diagnostic is
   stale or the status is, and from outside there is no way to tell which.
   This is exactly the designed-versus-built property this file asked for
   and got; it has come loose in one spot.

## Where situ is right and we were wrong

Stated plainly, because we are the ones who wrote the schema:

- **Field references work fine in sizes.** `u8 payload[length]` builds. So
  situ is not refusing field references generally, only in bounds and
  predicates, which is a coherent line and not an oversight.
- **Constant bounds work fine.** `[max = 65534]` builds.
- **Every documented `max =` in situ's `project.md` is a constant**
  (`MAX_PAYLOAD`, `1500`, `1024`). A field-referencing `max` is undocumented,
  so we used a construct nobody promised, and the parser's accepting it is
  what let us believe otherwise.

## The one claim we would ask you to look at

Decision 0030's table says of "a chunk's `index` is below its own `chunks`
count": **"one view -- already a plain `require`"**, and the prose adds that
it "needs nothing new". That is true of the grammar and not of the
implementation -- the `require` spelling refuses at `map` and `build`.

That row is what sent us to write the constraint at all. `fuzznet`'s own
`project.md` repeats it approvingly, and calls the earlier miscount "the more
useful half of the mistake", because a constraint filed under "needs a
feature that does not exist" is a constraint nobody writes. The correction
was right and the constraint is still not writable.

## What we are not asking for

- **Not for phase 5 to be reopened, or for anything to be prioritised.** Our
  own step 2 is not blocked on this in a way that costs a date: `chain/` went
  first precisely because sec 7a had already assigned it to us as semantics
  rather than layout, so it needs no generated code and is built and tested.
- **Not for the bound to be silently accepted.** Refusing it is fine.

What would be worth having, cheapest first: **`wire` refusing what `build`
refuses**, so a contract cannot be published for a constraint that cannot be
checked; the constant-bound diagnostic naming the phase the way the `require`
one does; and 26.5's status or that diagnostic reconciled, whichever is
stale.

We will keep `[max = chunks - 1]` in `frame.situ` rather than dropping it,
since dropping it would lose the constraint from the one place it is written
down. If you would rather it were spelled differently, say which and we will
change it.

---

# Two findings from running `situc build`, 2026-08-14, situ `497c1ea`

The report above ends by saying `situc build` had never been run here. It
has now, with the `[max = chunks - 1]` refusal stepped around by a literal,
and two things came out. **situ diagnoses both clearly and neither is a
silent failure** -- that is worth leading with, because the first probe here
recorded one of them as silent and was wrong: it was swallowing stdout and
grepping for the word "error", so a notice read as nothing at all. The
tooling was the broken part, not situ.

What follows is therefore not "these are bugs". It is two places where what
situ declines to do collides with something one of your own decisions has
planned, and you are better placed than we are to say which side should
move.

## 1. A relation over arrays produces no predicate

    target buffer;
    endian big;
    struct m { u16 id; u8 who[32]; }
    relation ok(a: m, b: m)     { must b.id  == a.id;  }
    relation arrays(a: m, b: m) { must b.who == a.who; }

    situc build --target c --layer relate

    situc: no predicate for relation `arrays`: `b.who` is an array, and a
           relation compares one value against another
    ... wrote p_relate.h, p_relate.c        # holding `ok` only
    exit 0

The message is good: it names the relation, the field, and the rule. `ok` is
emitted, so one relation does not poison the file.

**Both of `fuzznet`'s relations are dropped by it.** `same_message` and
`reply_to` each compare `sender`, which is `u8[32]`, and `wire/frame.situ`
describes `same_message` as "THE ONE THAT MATTERS, and it is a security
property rather than tidiness" -- it is what stops two senders' chunks
reassembling into one message that authenticates as neither.

### Why this is worth raising rather than absorbing

**Decision 0030's own first example is "a response carries the request's
identifier".** In an authenticated datagram protocol the identifier that
correlates two messages is almost always a *key or a nonce* -- 32 bytes --
rather than an integer. `fuzznet` correlates on `msg` (a `u32`, which works)
and on `sender` (32 bytes, which does not), and the second is the clause
that carries the security weight: differing `msg` means two unrelated
messages, differing `sender` means an attempted splice.

So the construct 0030 designed reaches the example it was designed for only
when the identifier happens to be scalar. That is not obvious from the
record and it is what we would most like your view on.

Fixed-size arrays look like the case that could be added without disturbing
the rung: a `u8[32]` against a `u8[32]` is a bounded, allocation-free,
constant-time-able comparison over two views, which is the same shape the
rung already permits. Variable-length ones plainly are not, and we are not
asking for those.

### The severity question, which is yours and not ours

A relation that generates nothing is a notice and exit 0. That is defensible
-- it is not an error in the schema, and refusing the build would stop a
schema that is otherwise fine. But it means **a schema can declare a
relation, have it validated, appear in the committed contract, and emit
nothing**, with the only evidence a line on stdout during a build. That is
the same shape as the `[max]` finding in the report above, and this library
walked into it the same way.

If a flag existed that made "declared but not generated" a refusal, we would
turn it on. We are not asking for the default to change.

> **Built: `situc build --refuse-ungenerated`.** Opt-in, and the default is
> exactly where you left it -- a notice and exit 0, because such a schema is
> not wrong and refusing it would stop one that is otherwise fine. With the
> flag it exits non-zero, names each relation and repeats the reason, and
> writes nothing: a build that is going to fail should not leave half an
> answer for the next reader to trust.
>
> **What it counts is narrower than "every notice", and the difference is
> the whole of the design.** situ already reports several kinds of "you
> asked and got nothing", and the obvious implementation makes all of them
> fatal. Measured, that refuses `example/packet` and `test/schema/edges.situ`
> -- one refusal and eight between them, every one of the form *no owned form
> for X*. That is a fact about a shape the data decides, where the schema
> declared nothing that then vanished; folding it in would fail two schemas
> that are entirely fine and teach whoever enabled the flag to turn it off
> again.
>
> So it counts relations only -- no predicate, no conversation table, no
> driver -- which is your case exactly: a construct the author wrote, that
> was validated, that appears in the committed contract, and that compiles
> to nothing. The three tests that hold it there include the discriminating
> one: `packet` and `edges` must still build **with** the flag, and the
> naive version was watched failing it.

## 2. No owned form where the size is data-decided

    struct m {
        u16 length  [max = 1024];
        u8  payload[length];
    }

    situc build --target c --owned

    situc: no owned form for `m`: its size is decided by the data, so an
           owned struct would need a pointer or a worst-case array; neither
           is this generator's to choose

Give `payload` a fixed `[64]` and the owned form is emitted. So the rule is
clear and the refusal is principled: the generator will not choose a pointer
(allocation) or a worst-case array (silently large) on the author's behalf.

**It collides with decision 0031.** That record has `fuzzypickles` adopting
at rung 2 with `--owned`, because 225 call sites hold decoded structs that
outlive the buffer. `fuzznet`'s frame has a `Bounded(0,1024)` sealed payload,
so it has no owned form at any rung -- and `fuzznet` is what `fuzzypickles`
would be adopting. The migration path 0031 describes does not reach this
frame as it stands.

Three ways that could resolve, and the choice is not ours:

- **The schema changes**: a fixed-size payload, which costs the difference
  between the real length and the maximum on every datagram, on a frame
  sec 13 of our `project.md` is already arguing about.
- **`--owned` grows a worst-case option** the author asks for explicitly,
  which is the "neither is this generator's to choose" made choosable.
- **0031's plan for `fuzzypickles` changes**, and it adopts differently.

We have no preference and would rather not have one -- we are the consumer
asking, which is the weakest position from which to argue that somebody
else's generator should grow. What we can offer is that the collision is
real, it is between two of your own records rather than between us and you,
and it is cheaper to notice now than during a 225-call-site migration.

## What we are not asking for

- **Not for the notice to become an error by default.** Named above.
- **Not for variable-length array comparison in relations.**
- **Not for anything to be prioritised.** `fuzznet`'s own step 2 is blocked
  on the `[max]` refusal in the report above, and everything this library
  owns is built and tested without generated code.

---

# A request: fixed-size array comparison in a relation

The report above described this and deliberately did not ask for it, on the
grounds that the consumer asking is the weakest position from which to argue
that somebody else's generator should grow. `fuzznet`'s holder has now asked
us to put it as a request, so here it is as one.

## The ask, narrowly

**`==` and `!=` between two fixed-size arrays of the same element type and
the same length**, in a relation predicate. Nothing else:

- **not** variable-length arrays, whose length is a runtime value and whose
  comparison is a different problem;
- **not** ordering (`<`, `>`), which is meaningless over a key;
- **not** arrays of differing length, which should stay a refusal because it
  is almost certainly a mistake in the schema.

## Why it is worth the change

**Decision 0030's own first example asks for it.** The table there opens
with "a response carries the request's identifier". In an authenticated
protocol the identifier that correlates two messages is a public key or a
nonce -- 32 bytes -- and almost never an integer. `fuzznet` correlates on
`msg`, a `u32` which compiles today, and on `sender`, a `u8[32]` which does
not. So `relate` reaches its designed example only when the identifier
happens to be scalar, and that is not visible from the record.

**What it costs here, concretely.** `wire/frame.situ`'s `same_message` is
the clause that stops two senders' chunks reassembling into one message that
authenticates as neither. It generates nothing, so `chunk/reassembly.c`
enforces it by hand -- which is the duplication `situ` exists to remove,
appearing in the one place where getting it wrong is a security bug rather
than a bug.

**It also closes four rungs rather than one.** No predicate means no
conversation table, and no table means no driver, so `relate`, `converse`
and `drive` all emit nothing for this schema. Measured: at every rung above
`view` our frame gets the same bytes plus a stream reader. `fuzznet` stands
on `view` today for that reason and it is recorded as the answer to its own
step 4.

## What we think the change actually is, so the ask is not glib

We read `situc/relation.py`. `_leaf` returns `(signed, bits)` and everything
downstream widens operands toward a signed or unsigned 64-bit comparison, so
**an array is not a relaxed scalar -- it is a different operation**: an
equal-length byte comparison with no widening, and no signedness to
reconcile. That is a new branch through the predicate emitter and the four
backends, not a check to loosen. We would rather ask for it knowing that
than have it read as a one-line removal of a `raise`.

## One thing that might be an accident of placement

The array refusal sits immediately beside the float one, which says an exact
comparison of a float "is rarely what a wire contract means". That is a
**judgement** -- a deliberate no. The array refusal reads as the same kind of
statement because of where it stands, but it may simply be unimplemented.

Worth one line in the record either way. If it is deliberate, we will take
the other route and say so below. If it is unimplemented, the neighbouring
refusal is making it look decided.

## If the answer is no

That is a fine answer and needs no justification to us. The alternative here
is to drop `sender` from `same_message` so the two scalar clauses generate,
and we would rather not: a schema declaring the harmless clauses and omitting
the dangerous one is partial *in the direction of looking complete*, and the
next reader takes the generated predicate for the whole check. We would keep
the hand-written enforcement either way.

Knowing it is a no is worth as much to us as a yes, because it settles
`fuzznet`'s rung question permanently rather than leaving it waiting.

---

## The tier-1 codec ABI cannot express a keyed transform (2026-08-18)

`fuzznet` has finished its sealed-region work, and the report is mostly good
news: **the sealed-region ABI is complete and we built against it without
needing anything from you.** One thing did not fit, and it is the ABI rather
than an implementation.

### What worked, so the finding is not read as a complaint

`wire/frame.situc`'s frame is `hop | authenticated{head} | sealed(fzn_aead,
nonce = head.nonce){capability, payload} | tag[16]`, and the generated C gave
us everything the open path needs:

- `situ_fzn_frame_tag_covered()` -- the exact span to authenticate, computed
  from the layout rather than restated in our source. This is the one we would
  otherwise have hard-coded and got wrong the next time the schema moved.
- `situ_fzn_frame_sealed_open(view, verified, &gate)` -- and every interior
  accessor taking that gate, so the plaintext is unaddressable until something
  says the tag verified.
- `situ_fzn_frame_validate()` for shape, before any cryptography is spent.

Our whole open path is: validate, check the key commitment, run the AEAD over
`tag_covered`'s span, hand the verdict to `sealed_open`. **The discipline you
enforce is order, and it is the part we would most likely have got wrong.**

Worth saying plainly because our own document was wrong about it for weeks:
we recorded this as "waiting on situ's sealed-region ABI, and the calling
convention is still a guess". It had been exercisable since `18b3537`, which
we adopted for the relation work without noticing it unblocked this too.

### The finding

`impl fzn_aead extern "fzn_aead_xchacha20poly1305"` is **unbound in our tree
and will stay that way**, because sec 13.2a's shape cannot carry what an AEAD
needs:

```c
situ_err_t x_decode(const uint8_t *in, uint32_t in_len,
                    uint8_t *out, uint32_t out_cap, uint32_t *out_len);
```

No key, no nonce, no associated data. For us:

- the **key** is per-session and derived outside the schema's knowledge;
- the **nonce** is `head.nonce`, and the schema *states* it --
  `sealed(fzn_aead, nonce = head.nonce)` -- so this is a value the compiler
  already knows and does not pass;
- the **associated data** is the authenticated header, which is exactly the
  part of `tag_covered`'s span that is not the sealed region.

The only ways to satisfy the signature are a global holding the key and nonce,
or a thread-local. Both put mutable state in the one seam where it must not
be, and a codec bound that way would be a keyed primitive whose key is set by
action at a distance. So we call our own vtable instead.

**We are not blocked by this.** Since the accessors do not call the codec, an
unbound `impl` costs us nothing but a line in the schema that describes
something no longer true. That is the smallest part of the finding and the
easiest to act on: if a tier-1 `impl` cannot be bound for a codec of this
shape, it may be worth refusing the declaration rather than accepting one
nothing can implement -- which is the same class as the `[max]` disagreement
we reported before, where `wire` published a bound `build` could not enforce.

### What we are not asking for

Not a redesign. Three shapes seem possible and the choice is yours:

1. **A second ABI tier for keyed codecs** -- an extra `const void *params`, or
   a context pointer threaded from the call site. It changes the accessor
   signature, which may cost more than it buys.
2. **Refuse the binding** -- if `authenticated` in a codec's property set means
   the tier-1 shape cannot serve it, say so at `impl` time. Cheapest, and it
   would have told us on day one rather than after we read the ABI.
3. **Leave it and document it** -- say in sec 13.2a that an `authenticated` codec
   is expected to be driven by the caller through the gate rather than bound as
   a tier-1 impl. This matches what actually happens and costs nothing.

We would be happy with (3). The gate is the valuable part and it already
works; what cost us time was believing the `extern` declaration meant
something we would eventually be able to satisfy.

### One smaller note

`situc verify` requires a `vectors` argument. Our own reproduction script had
been running it without one for weeks and recording the usage error as a
refusal of the schema, which was our mistake and is now corrected in our
`project.md`. Mentioning it only because a reader of our earlier report would
have seen `verify` in a table of four commands and drawn the wrong conclusion
about situ.

---

## Correction: withdraw the recommendation above (2026-08-18, same day)

**We recommended the wrong thing, and it is the kind of wrong thing that gets
acted on.** The section above offers three shapes and says we would be happy
with (3) -- record in sec 13.2a that an `authenticated` codec is driven by the
caller through the gate rather than bound as a tier-1 `impl`.

That recommendation assumed the tier-1 shape reflected a deliberate scope
boundary: that situ had decided keyed transforms were the caller's business
and the ABI was drawn to match. On that assumption, writing the boundary down
is the cheapest honest fix.

**The assumption was wrong.** situ's scope is eventually to cover protocol
needs whole, including layered, nested and distributed cryptographic contexts,
and including the case where a project plugs in its own routines. Against that,
(3) is the worst of the three rather than the cheapest: it would write a
temporary gap into the specification as though it were a decision, and the next
reader would take it for one. **Please disregard it.**

We have no standing to choose between (1) and (2) and are not trying to. What
we can offer is our case as data, since it is a small instance of the general
shape rather than a special one.

### What our frame actually needs, as a data point

`fzn_frame` is `hop | authenticated{head} | sealed(fzn_aead, nonce =
head.nonce){capability, payload} | tag[16]`, and a codec serving it needs four
things:

| what | where it comes from | does the schema know it? |
|---|---|---|
| the nonce | `head.nonce`, a field of this message | **yes, and it is already declared** in the `sealed(...)` clause |
| the associated data | the `authenticated` region -- the part of the tag's span that is not the sealed region | **yes**, `tag_covered()` computes it today |
| the ciphertext extent | the sealed region | **yes**, the layout owns it |
| the key | derived per session, outside the schema entirely | **no**, and it never will be |

Three of the four are things situ already computes. Only the key comes from
outside, which suggests the gap is narrower than "the ABI cannot carry a keyed
transform" -- it may be closer to "the ABI passes none of what the layout
already knows, so even the parts situ owns have to be re-derived by the
caller."

Ours is also the simple case, and worth flagging as such if the design is
aiming at the general one: one region, one tag, one nonce, one key. The shapes
we would expect to be harder are a tag covering regions that are not
contiguous, nested sealed regions where an inner key is carried in an outer
plaintext, and a key schedule where one message's plaintext derives the key for
the next. We do not have those and are not asking for them; we mention them
because a design settled against our frame alone would be settled against the
easy case.

### What we are doing meanwhile, so nothing waits on this

`wire/seal.c` calls our own vtable and drives situ through the gate --
`tag_covered()` for the span, `sealed_open(view, verified, &gate)` for the
verdict. That works today, needs nothing from you, and is not a workaround we
resent: the gate is the part that made parse-before-verify unrepresentable in
our code, which is the property we most wanted. **Our `impl fzn_aead extern`
line stays unbound for now rather than permanently**, and we have corrected our
own `project.md` where it read as a settled position rather than a current one.


---

# Status of everything above, since none of it said (2026-08-20)

**This file reads as a stack of open reports and several of them are closed.**
That is our fault rather than yours: we filed them, you fixed them, and nobody
came back to mark them. A suggestions file that cannot be skimmed for what is
still live costs you the time it was written to save.

| report | status |
|---|---|
| The `[max = chunks - 1]` four-command disagreement | **fixed by you**, verified here |
| A relation over arrays produces no predicate | **fixed by you**, and in use |
| The narrow ask: fixed-size array comparison in a relation | **delivered** |
| No owned form where the size is data-decided | open, and we are not pressing it |
| The tier-1 codec ABI cannot carry a key | open, with our recommendation withdrawn |

**The four-command disagreement.** Re-run against your tree on 2026-08-18:
`situc wire --check`, `situc map` and `situc build --layer relate --target c`
all accept `wire/frame.situ`. `situc verify` refused, and that was our error
rather than a verdict -- it takes a `vectors` argument our reproduction never
passed it, which we have corrected on our side and mention because a reader of
the table above would otherwise count four commands where three were ever
disagreeing.

**The array relation.** `f9e5c0e` landed it in four backends, and it is not
merely present: `situ_rel_same_message` over two encoded frames is what
`chunk/test/agreement_test.c` runs against our hand-written reassembler,
which is how we learned the two disagree in exactly two places on purpose.
The predicate you emitted is doing work in our tree rather than sitting
compiled.

**One correction to our own text above, which we should have made here when we
made it at home.** The paragraph about the capability map says "the frame
carries 96 bytes of fixed overhead". It carries **144** -- 5 of hop, 91 of
authenticated header, 32 for the sealed capability, 16 of tag. 96 was already
wrong when written: it counts the plaintext prefix and omits the sealed
capability and the tag. The observation it supports is unaffected, since the
question the map raised was about identity appearing twice rather than about
the total, but the number is wrong and it is ours.

We are leaving the original text above unedited rather than rewriting history
in your tree. This section is the correction; the sections above are what we
actually said at the time.
