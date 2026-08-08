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
