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
example implies.** `examples/packet/packet.situ` pays three reserved bytes to
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
