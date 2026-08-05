# situ, evaluated against fuzzypickles

Written 2026-08-04, from reading rather than a trial -- see *How this was
evaluated* at the end for why, and for what would need trying if the blocker
below is removed. Per `build-and-commit.md`'s standing instruction that
projects which could adopt situ should say whether they would.

## The domain match is real

fuzzypickles is an encrypted peer-to-peer messaging platform, and it
hand-writes a great deal of byte-exact code:

- `core/src/control_codec.c` -- **4,127 lines, 225 encode/decode functions**
  for the daemon's control protocol.
- Peer-wire pack/parse pairs across `group.c` (49), `messaging.c` (35),
  `manifest.c` (28), `blob.c` (17), `peer_pair.c` (17), `peer_wire.c` (12),
  `log_relay.c` (12).
- A hand-written CBOR subset in `sticker.c` for the sticker pack manifest.
- `core/src/wire.c`, 101 lines of reader/writer primitives underneath all of
  it.

The shapes are ones the schema language covers: a leading subcommand byte
discriminating a payload (`variant ... switch`), length-prefixed strings
(`u8 value[length]`), fixed 32-byte keys, counted arrays of records
(`record recs[hdr.rec_count]`).

More than that, the *discipline* matches. This codebase repeatedly enforces by
hand exactly what `require canonical(...)` and the capability map assert
mechanically. From a payload parser written this week:

> Exact length, not a minimum: a fixed-shape payload that accepts a longer one
> is a payload with an unread tail, and the next field added would be silently
> ignored by every host built before it.

That rule is currently a comment and a hand-written `if`. In situ it would be
a compile-time `require` and a committed capability map that `--check` diffs.
A wire contract that fails the build when it changes is strictly better than a
convention maintained by whoever is paying attention.

## The blocker is the data model, not a gap

**situ emits zero-copy views. This codebase decodes into owned structs.**

Generated situ accessors read through a view -- a base, a limit, a generation
counter -- and every field is arithmetic against the buffer. fuzzypickles
decodes into fixed-size C structs that callers hold *by value* and outlive the
buffer entirely:

```c
fzp_group_show_resp_t v;                       /* on the caller's stack */
if (fzp_ctrl_decode_group_show_resp(payload, hdr.payload_len, &v) != FZP_OK)
        return 1;
/* payload is gone from here on; v is the data */
```

There are 225 such call sites. Adopting situ is therefore not swapping a
codec: it changes how every caller reads, and introduces buffer-lifetime rules
the code does not currently have. Several call sites keep the decoded struct
across an event loop turn, which a view cannot survive.

I checked whether `--materialize` bridges this. It does not -- section 26.30 is
explicit that what the second family materializes is **the walk, not the
data**, emitting an index of element offsets for capped runs. That is the
right answer for the problem it solves and not the one needed here.

## A risk/volume inversion worth naming

Even setting the model aside, the value is not where the volume is:

- **The volume** is the control protocol -- 4,127 lines. It is a *local* unix
  socket between a daemon and clients from the same build. No version skew, no
  hostile input, no cross-host compatibility. Lowest risk in the system.
- **The risk** is the peer wire, where bytes cross hosts and versions. But its
  plaintext is tiny by design: a 3-byte frame header (version, command,
  sub-type) and fixed payloads -- the group asset-key payload is exactly 96
  bytes, chat_id + root + content_key. Everything else is AEAD-sealed opaque
  ciphertext, which no layout description can help with.

So situ would generate most code where it matters least, and least where it
matters most.

## Verdict

**Not materially better today**, on the bar `build-and-commit.md` sets. The
schema language fits, the guarantees are ones this project wants and currently
hand-maintains, and the volume is large enough to justify real work -- but the
accessor model is wrong for a codebase built entirely on owned structs, and
changing that is a rewrite of the callers rather than of the codec.

**What would flip it, in order:**

1. **A decode-to-owned-struct backend.** A `--target c --owned` that emits, per
   struct, a fixed-size C type and a `decode(buf, len, out)` returning an error
   code. That is what this codebase already has 225 of, hand-written. With it,
   adoption becomes mechanical and the answer is probably yes.
2. **Failing that, a checking-only adoption.** Describe the peer wire in situ
   without generating accessors, and use `situc wire --check` and `situc map
   --check` in CI purely as a wire contract that fails the build when a layout
   changes. Keep the hand-written codec. This is much less than situ is for,
   but it is the part with the highest value here and it has no model
   conflict -- the schema would be a specification, checked against golden
   vectors, rather than the source of the code. If you want a first user for
   that mode, this project is a good one.
3. `gen-fuzz` per parseable struct is attractive independently. This project
   has `wire_fuzz_test.c` and `peer_wire_fuzz_test.c` hand-written and nothing
   for the control protocol's 225 decoders.

## How this was evaluated

By reading `README.md`, the examples, and section 26.30 of `project.md` -- not by
writing a schema and generating code. The blocker identified is structural and
a trial would not have changed it: the mismatch is between what situ emits and
how 225 call sites consume data, which is visible from the accessor signature
alone.

If a decode-to-owned-struct backend appears, the trial worth doing is
`core/src/control.h`'s group sub-protocol -- about 15 request/response pairs,
self-contained, with tagged variants, length-prefixed names and counted
arrays, and an existing hand-written implementation to diff against.

---

## Two formats this project needed that situ may not describe

Offered as domain input rather than complaints -- both are shapes an encrypted
messaging protocol runs into immediately, and neither appears in `examples/`.

### 1. A sealed payload is a two-stage layout

Nearly every frame on this project's peer wire has the same shape: a small
plaintext header, then one AEAD-sealed run whose *inner* structure is another
schema entirely, visible only after decryption. A group frame is

    version u8 | command u8 | sub_type u8 | sealed[...]

and inside the sealed run, once opened, is a fixed 96-byte payload --
`chat_id[32] | root[32] | content_key[32]` -- with its own exactness rules.

Today situ can describe the header and must call the rest opaque, which means
the half with the real invariants gets no schema, no `require canonical`, no
capability map and no `gen-fuzz`. That is the wrong half to lose, and it is
not specific to us: it is the shape of Signal, MLS, Noise and every protocol
built on sealed frames.

A way to say "this run is opaque *here*, and is `struct X` once a transform is
applied" would put the inner layout back under the schema without situ knowing
anything about cryptography. The transform is the user's; what situ would own
is that the plaintext has a described layout and that the two are declared
together in one file rather than in a comment.

Related, and cheaper: even without the transform, being able to declare "these
N bytes are `struct X` in some other context" would let the inner payload get
a schema, a map and a fuzz harness of its own, decoded from a buffer the
caller produced. That may already be expressible by describing the payload as
a top-level struct -- if so, it is worth an example, because the pattern is
extremely common and the README's examples are all single-stage.

### 2. Self-describing formats: CBOR

`core/src/sticker.c` hand-writes a CBOR subset for a sticker pack's manifest --
definite-length only, integer keys, major types 0/3/4/5, with unknown *fields*
skipped and unknown *flags* fatal. That last distinction is a real design
decision and exactly the kind of thing a schema should carry.

CBOR is not a byte-exact layout in situ's sense: the type is in the data, and
an integer's width varies with its value. So this may simply be out of scope,
and saying so plainly in the README would be useful -- "situ describes layouts
that are fixed by the format, not formats that describe themselves" is a
one-line answer to a question people will keep asking, since CBOR, msgpack and
protobuf all sit in that space. `examples/protobuf/` suggests the boundary is
not where I would have guessed, which is itself a reason to state it.

## On `explain`, and why it is the feature worth advertising

This project's `project.md` section 14 is largely a catalogue of diagnostics that
named the wrong cause: a timer table reporting "could not register the location
sampling timer" when the real problem was the sweep added four lines earlier; a
size enquiry returning `BUFFER_TOO_SMALL` so every sticker reported "its art is
missing from the pack".

`situc explain` is the opposite discipline, mechanised:

    mutate := Shifting
      a member that ends at a delimiter
      remedy: `until D max N` bounds the scan, which makes the member
      statically allocatable

A blame chain plus a remedy, derived rather than written. Whatever happens to
the adoption question, that is the part of situ worth leading with -- it is a
harder thing to build than the code generation and a rarer thing to find.
