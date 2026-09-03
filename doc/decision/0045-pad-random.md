# 0045: `pad_random` is bounds and a name, and not a content policy

Status: accepted 2026-09-04; not yet built
Date: 2026-08-27
Phase: after 0043, which built `pad_to` and deferred this

## Context

14.7 names two padding constructs and one policy: `pad_to(n)` and
`pad_random(min, max)` for traffic-analysis resistance, with padding content
explicitly `zero`, `random` or `preserve`, and `zero` required for
`require canonical`. 0043 built `pad_to(n)` and deferred the other as "a
different thing (a variable pad to hide length, not a fixed pad to align)",
which is true and is not a design.

The question a design has to answer is what the compiler *does* with it, and
that is sharper than it looks, because **situ generates readers.** A random
pad length is a sender's choice made at send time, and situ writes no code
that chooses one -- it has no random source and 14.6 puts the caller's
primitives outside the compiler on purpose. So `pad_random` cannot mean
"emit a padder". Whatever it means, it is a claim checked on parse.

**Most of it is already expressible, and that has to be established before
adding syntax.** Measured, this compiles today and emits a real check:

    struct hidden {
        u16      length;
        u8       body[length];
        reserved u8[remaining] [must_be_zero];
    }

The generated `validate` walks the pad and returns `CONSTRAINT` on any
non-zero byte. So the run-to-the-end shape, the zero content policy and the
enforcement all exist. 14.7 half-says this itself: the canonicity item it
contributes "is already covered by `reserved [unknown]`, which is the same
freedom under a name that exists."

## Decision

**`pad_random(min, max)` is a bounded reserved run with a name, and the
`random` content policy is not built.** Three parts, and the third is the
one worth arguing.

**It adds length bounds, which nothing expresses today.** A pad that hides
length must still be bounded, or a peer can claim a megabyte of padding
inside a frame and a reader that trusts it has no ceiling to check against.
`[max = N]` is refused on an array today -- "an array has no single value to
bound" -- and there is no minimum at all. `pad_random(min, max)` states both,
and `validate` checks the pad's length against them, exactly as `pad_to`'s
`must_be_zero` is checked over its span.

**It adds a name, for the reason 0043 gave.** The map renders the member as
`<reserved0>` and the wire signature says nothing about intent. `pad_to`
earned `<pad>` and `pad-to=n` because a peer padding to a different multiple
disagrees about where the next field starts; a peer padding to different
*bounds* disagrees about what lengths are legal, which is the same class of
wire-visible fact and belongs in the signature as `pad-random=min..max`.

**It does not add a `random` content policy, and 14.7 should lose it.**
Randomness is not a property of a message. A pad of random bytes and a pad
of any other bytes are the same bytes, and no reader can tell them apart --
so a schema declaring `random` would be stating something the generated code
cannot test. That is exactly what 17.0 refuses and what `UNIMPLEMENTED_ATTRS`
exists to prevent: "a schema that states what the generated code does not
enforce is worse than one that states nothing." The two policies that
survive are the two that are checkable: `zero`, which `must_be_zero` already
spells, and unconstrained, which `[unknown]` already spells.

The sender's obligation to *use* a random source is real and is the caller's,
in the same place as the AEAD primitive and for the same reason.

## Alternatives considered

**Build `random` as a documented-but-unchecked policy.** It would read as
intent and cost nothing to emit. It costs something to *have*: every
unchecked declaration in this language has been removed on discovery, and
one added deliberately teaches the reader that some attributes are decorative
without saying which.

**A statistical check on the pad's content.** A chi-squared test over a
few hundred bytes, refusing an obviously non-random pad. It would run on a
single message, where the sample is far too small to distinguish a weak
source from a good one, and it would refuse the legitimate case of a sender
whose random pad happened to come out flat. A check that cannot be right is
worse than none.

**Leave it to `reserved u8[remaining]` entirely and delete `pad_random` from
14.7.** Tempting, because that shape exists and is checked. It loses the
bounds, which are the part a reader genuinely needs and cannot state, and it
leaves the map calling a deliberate privacy measure `<reserved0>`. The name
is not decoration here: a reviewer who cannot see that a field is padding
cannot see that removing it changes what an observer learns.

**Infer the bounds from the frame.** A pad running to the end is bounded by
the frame already, so `max` is redundant where the frame is capped. It is
not redundant where the frame is not, and `min` -- the part that says padding
is mandatory rather than optional -- cannot be inferred at all.

## Consequences

- `pad_random(min, max)` parses as a member, like `pad_to(n)`, and is
  refused where `min > max` or either is negative.
- Its length comes from where a reserved run's length already comes from:
  the remainder of the frame, or a field. The construct adds the bounds, not
  a new way to size a run.
- `validate` gains a length check against the bounds. The content check is
  whatever the attributes already say -- `must_be_zero` or nothing.
- The map renders `<pad>` with its bounds and the wire signature carries
  `pad-random=min..max`, both for 0043's reason.
- 14.7 loses `random` from the content policies, and says why: it is a
  property of the sender, not of the message.
- `require canonical` continues to want `zero`, which is unchanged --
  a pad whose content is unconstrained admits many encodings of one value,
  which is 8.8's malleability rule and is already enforced.
