# 0065: A value size is an expression over the tag's own parts

Status: proposed
Date: 2026-10-20
Phase: 26.663

## Context

A `tlv` region's `value_size` dispatched to one of four rules: a literal
byte count, `self_delimiting`, `prefixed(T)` and `error`. None of them
can read a count out of the tag just decoded, which is what CoAP's
option header is -- a four-bit delta and a four-bit length in one byte.

26.142 recorded that as one of two independent reasons CoAP could not be
described, the other being a running option number across items. It
named both as needing a language decision and said they should be
answered together with 8b10b's running disparity, since "adding them
separately is how one language grows two spellings for one idea". The
open register carried that as item 13.

## The measurement

**The design document had already answered this one, before any code
existed.** Section 9.5 has said since the scaffolding commit of
2026-07-26:

> `self_delimiting` is only legal for value types that carry their own
> extent (varints, nul-terminated byte runs). Everything else must be
> sized by a literal, a `prefixed(...)` form, **or an expression**.

Three forms for "everything else", and the parser implemented two. So
this was not an undecided question but a contradiction between the
document and the code -- and `code-style.md`'s precedence makes that
resolution automatic: the document wins, which leaves implementing it
the only available reading rather than a decision for anybody to take.

**Nothing pointed at the sentence**, which is the part worth keeping.
The item was written from the refusal the parser produces, and a
refusal says what the code does. Three records cite 9.5 while naming
this gap and not one quotes the sentence that settles it.

**The mechanism was already there too.** The dispatch's selector is a
tag part, so the parts are in scope at exactly the point a value's
extent is decided -- `ValueSize`'s own docstring says so. A member's
extent has always been an arbitrary expression over what was read:
`u8 body[n + 13]` parses and generates. The two spellings were one
decision taken in one place.

## The decision

**`value_rule` gains an expression, last among the alternatives.** The
three keywords are matched first, because they are idents too; what
remains is parsed as an expression, and a constant folds to the literal
rule it already was -- `4` and `2 * 2` both arrive as `FixedValue`, so
every pass that reads one goes on reading one.

**The leaves are the tag's parts and nothing else.** `wellformed`
refuses any other name, with the parts it does have, for the reason the
selector is held to the same rule: a size naming a member would name
something the item has not reached.

**Bounded like every other size (14.2b).** A part is whatever the tag
said, so `length + 13` on a lying tag would wrap a 32-bit size and read
a value small enough to fit. The four backends render it through the
same leaf-bounding renderer a member's size expression uses: signed 64
bits throughout, one clamp at the end.

**The image carries it as a program over `tag` with the parts inlined**,
which both walkers evaluate -- the Python one and `walker/c`'s, each
holding its own copy of the kind numbering. A part is itself an
expression over `tag` alone, so `length + 13` compiles as
`(tag & 0x0F) + 13`: neither VM needs part slots or a new opcode, and
the arithmetic stays one implementation across six readers. The rule
row grew a program offset and is read by stride, which is how the image
already tolerates a row that has gained a field.

## Alternatives rejected

**A bare part name rather than an expression** -- `default: length` and
nothing more. Smaller, and it would have needed a second form the day
somebody met CoAP's `13` and `14` escapes, which are biased lengths.
`n + 13` is already legal for a member's extent, and refusing it here
would have been two spellings for one idea in the same document that
warns against them.

**`prefixed(length)`, reusing the existing keyword.** It reads well and
means something else: `prefixed(T)` is a length that sits BEFORE the
value on the wire, which is protobuf's shape. A tag-sized value's count
has already been read as part of the tag.

**A new keyword, `tag_sized(length)`.** A keyword for what an
expression already says, and it could not carry the bias.

## Consequences

CoAP's option *length* is expressible. Its option *number* is not: that
is a running total across items, which is the other half of 26.142 and
is untouched here -- so this does not make CoAP a worked example and
does not pretend to.

A computed size of zero is legal, where a literal `0` is refused. The
refusal is about a schema sizing every item of a wire type at nothing;
a message whose option carries no value is one item, and the walk still
advances because the tag moved first.

The wire signature prints the arithmetic rather than the kind word.
`computed` alone would have called a change from `length` to
`length + 13` unchanged, which is the one thing that file exists to
refuse; with the expression in it, `situc wire --check` reports the
change as breaking.
