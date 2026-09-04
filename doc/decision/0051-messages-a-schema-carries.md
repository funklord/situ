# 0051: messages a schema carries, and who renders them

Status: proposed
Date: 2026-09-04
Phase: raised by the copyright holder, after 0050

## Context

situ has seven runtime answers and none of them is a sentence:

    SITU_OK, SITU_ERR_BOUNDS, SITU_ERR_CONSTRAINT, SITU_ERR_VERSION,
    SITU_ERR_TAG, SITU_ERR_STAGE, SITU_ERR_STALE

`[must_eq]`, `[min]`, `[max]`, `[must_be_zero]`, `enum_known` and
`fits_frame` all collapse into `ERR_CONSTRAINT`, and generated code carries
no strings at all -- measured, zero in `udp.h`. A caller that wants to tell
somebody *what* went wrong writes that sentence itself, in a language situ
does not describe.

**Three things are missing and they are not the same thing.**

*A predicate over several members of one message has no spelling.* `require`
and `assert` are compile-time. `must` lives only in a `relation`, which is a
predicate over *two* messages. And the obvious route was closed on purpose:
`invariant` refuses comparison operators because "an invariant states that a
field *equals* something, and the moment the right side can be a predicate
the construct has quietly become a second `require` with a worse syntax".

*A failed check has no identity.* The information exists --
`image_constraint` carries `{placement, value, check}` and `image_check`
names ten kinds -- so the walker knows exactly which check failed and throws
it away, because "a walker that reported *which* constraint failed would be
answering a question the generated code cannot". That reason is about the
generated code, not about the walker, and it is liftable.

*And a message is not always a failure.* The ask that prompted this record
is wider than errors: a schema should be able to say "these two fields
together mean legacy framing" so that `situ-edit` can show it **without
format-specific callback code**. The editor is generic on purpose (0034),
and today the only way to make it helpful about a particular format is to
write code that knows that format -- which is a sixth description of the
layout, in a place none of the five can check.

**The channel already exists and only the schema cannot reach it.**
`editor/document.py`'s `Field` carries `note: str`, and its docstring says
why: "`value` is None where the walk could not read it, and `note` says why.
An editor that silently omitted such a field would show a message missing
something it actually has." Notes reach the frontends today; they are all
walker-generated.

## The construct

At file scope, beside `invariant` and `require`, naming a struct's members
the way those do:

    when packet.mode == 3 && packet.flags & 0x8 != 0
        note legacy_framing
        "the length field counts the header, as it did before v4";

    when frame.count > frame.capacity
        refuse count_over_capacity
        "more entries than the table can hold";

Three severities, and no fourth:

| severity | `validate` | meaning |
|----------|------------|---------|
| `refuse` | `ERR_CONSTRAINT` | the message does not conform |
| `warn`   | unchanged | conforms, and something is worth saying |
| `note`   | unchanged | conforms, and something is worth showing |

## When it runs, which is the whole of the model

**Once, in `validate`, and nowhere else.** No new execution point is
invented, because situ already has exactly one and it is the right one:
`validate` is "one line about the whole struct", the caller invokes it, and
it recurses into nested structs -- so validating a file's top-level struct is
"the end of processing the buffer" already spelled.

Everything that would otherwise turn into a schedule falls out of that:

- **No `when` is evaluated on an accessor call.** A getter stays arithmetic,
  which is the promise 20.2 makes and the reason a view is acquired once.
- **No `when` may read another `when`.** They are independent predicates
  over the same immutable bytes, so there is no order to define, no cascade
  to bound and no fixed point to reach.
- **No `on_read` or `on_write` form.** That is the `effect` axis, it means
  something else, and a message is not an effect.
- **Nothing fires during a partial read**, because the struct has to be
  placed before `validate` has anything to say.

**One wrinkle, and it decides the API.** `validate` short-circuits: "order
matters because the first failure is the answer". That is right for a
verdict and wrong for collecting messages, since a caller wants all of them
rather than the first. So the messages come from a sibling --
`situ_X_messages(view, ids, cap, *count)` -- which runs the same predicates
without stopping, and which a caller who wants none never links. `validate`
keeps its signature, its cost and its short circuit.

Evaluating the predicates twice, where a caller wants both, is a cost and
not a correctness question: they are pure over bytes nobody is writing
during the call, which is the same reason `require` can be checked once at
compile time.

## The identity is the contract; the text is a default

Every `when` is named, and **the name is what a consumer keys on**. The
string is a default rendering.

That split is the one situ already makes for fixed point, where C emits a
`_SCALE` macro and no conversion: "the scale is exact and belongs in the
header, while the conversion needs a type situ cannot choose for an embedded
target." Text needs a locale, a log format and a flash budget situ cannot
choose either. So the id is always emitted and the text is opt-in --
`--messages`, in the shape `--owned` and `--single-file` already have.

It also answers the localisation question without inventing i18n: a consumer
with its own catalogue keys on the name and ignores the default, and one
without it renders what the schema said.

## The dissector wants this one

0049 and 0050 both turned on the Lua dissector being the description that
could not follow. **Here it is the description that gains most.**
Wireshark's expert info is this construct exactly -- a severity and a
message attached to a packet, filterable -- and `situc gen-dissector` emits
none today. `note`, `warn` and `refuse` map onto its chat/note/warn/error
scale without a fourth level being invented for it.

## Decision

**Add `when`, name every one of them, and emit the identity always and the
text on request.** Concretely:

- `when <predicate> <severity> <name> "<text>";` at file scope. The
  predicate is the relation body's expression language over one view, which
  the bytecode VM already runs and 0049 already bounds.
- **Evaluated once, in `validate`, and at no other point.** The model is
  flat by construction rather than by rule: there is one place, the caller
  chooses when to call it, and no `when` can see another.
- `refuse` contributes to `validate`; `warn` and `note` do not, and all
  three are reported by the `messages` sibling that does not short-circuit.
- **Existing per-member constraints gain the same identity**, which costs
  nothing new: `image_constraint` already carries placement and check kind,
  so `validate` can report which check failed rather than only that one did.
  This is the half that makes the walker's discarded knowledge usable.
- Five consumers, no callbacks: `validate` and `situ verify` report the id;
  the editor puts the text in the `note` it already has; the dissector emits
  expert info; `situc doc` lists them per struct.
- **A `refuse` is part of the wire contract and a `note` is not.** A
  `refuse` changes which messages are legal, so the wire signature names it
  for 0048's reason -- two peers that disagree about it disagree about the
  bytes. A `note` changes nothing about legality and stays out, or the
  signature would churn on an editorial change.

## Alternatives considered

**Text with no identity.** Simplest, and what a hand-written reader does.
Rejected: it makes localisation impossible, forces every embedded target to
carry the strings, and gives a consumer nothing stable to match on when the
wording is improved.

**Identity with no text.** Clean, cheap, and leaves the sentence in
hand-written code -- which is the sixth description this record exists to
remove. The ask was explicitly to improve the editor "without bringing in
any extra callback code", and an id with no default text does not.

**Extend `invariant` to take a predicate.** The construct is already the
right shape and already refuses this, in writing, on the ground that it
would become "a second `require` with a worse syntax". That reasoning holds
and this record does not reopen it: `when` is the second `require` said
plainly, at run time, which is the part `require` cannot do.

**Evaluate a `when` wherever its members are read.** The version that
would make messages feel live in an editor, and the one that quietly adds a
schedule: an accessor stops being arithmetic, two `when`s over the same
member need an order, and "when did this run" becomes a question the schema
cannot answer. Refused for the reason the flat model exists.

**A callback or hook the caller registers.** What the ask exists to avoid,
and it puts the format's knowledge back in code no description can check.

**A comment convention.** Inert. A comment cannot be shown by a dissector,
keyed on by an editor, or reported by `verify`.

## Consequences

- **situ cannot check prose.** A `note` that says something false about the
  format is shown with the compiler's authority behind it, and nothing in
  the tree can catch that. It is the first construct where the schema
  asserts something no check can hold it to, which is 14.5's rule pointed
  the other way -- worth stating in the record rather than discovering.
- Generated size grows only with `--messages`; the id is a small integer.
- `situc doc` gains a section per struct, which is the cheapest consumer and
  probably the first to build.
- The severity vocabulary has to stay at three. Wireshark has four and the
  editor has three; adding a fourth to match one consumer would put situ in
  the business of somebody else's presentation model.
- A `when` naming members of two structs is not a `when` -- it is a
  relation, and 0030 already owns that.
