# 0023: which part of a decoded tag names an item

Status: accepted
Date: 2026-07-31
Phase: after 26.31; needed before any backend can walk a tlv region

## Context

Section 9.5's general form decodes a raw tag into named parts:

```situ
tag_decode = { field = tag >> 3, wire = tag & 0x7 },
known      = { 1 : { name = user_id, wire = 0, type = pb_varint } },
```

and the `known` map keys those parts' values. Which part is the key matching?

For protobuf the answer is `field`: tag 1 with wire type 0 is the raw tag 8, so
the key is neither the raw tag nor `wire`. Nothing in the schema said so. The
question never came up while the item grammar was verbatim source text nobody
read, and it arrived the moment a generated accessor had to compare something
to something.

The stakes are not "the compiler cannot tell". Every candidate part yields a
walk that runs, finds an item, and returns it. Matching a wire type where a
field number was meant returns the wrong item, and nothing about the message
or the generated code says a substitution happened. That is precisely the case
invariant 9 names: situ never takes a silent default where the wrong choice is
undetectable at runtime.

## Alternatives considered

**The part the `value_size` dispatch does not select on.** `switch (wire)`
selects on `wire`, so the identity is `field` -- the part left over. Needs no
new syntax and gets protobuf right. Rejected because it only decides the
two-part case: three parts leave two candidates, and a region with one part
that *is* the selector leaves none. A rule that decides the example and not the
construct is a rule that will be revisited under pressure, which is the worst
time.

**The first part written.** Always decides, needs no syntax, and reads
plausibly -- the identity is usually written first. Rejected because it makes
the order of a `{ ... }` map significant without saying so anywhere, and
reordering two lines that look like declarations would silently change which
part every accessor matches. A positional convention is a silent default, and
this is the kind whose wrong choice cannot be observed.

**Requiring it always.** Consistent, and noise in the simple form, where a tag
that decodes into nothing or into one part has nothing to be ambiguous about.
Situ asks for a declaration where a reader could be wrong, not everywhere.

## Decision

A tlv region names the identifying part with `tag_identity = <part>`, and it is
required exactly where more than one answer is possible.

| `tag_decode` produces | `tag_identity` | a `known` key matches |
|---|---|---|
| nothing (the simple form) | not written | the raw tag |
| one part | optional | that part |
| two or more, and there is a `known` map | **required** | the part it names |

Omitting it where it is required is an error naming every candidate, with the
remedy in the note. Naming a part the tag does not decode is an error too.

The `known` map is what makes it required: a region with no named tags has
nothing keyed by identity, and a caller walking it reads whichever parts it
likes. Requiring a declaration there would be asking for an answer nothing uses.

## Consequences

`example/protobuf/protobuf.situ` gains `tag_identity = field`, which makes the
description of the protobuf wire format more precise rather than less: the
field number's role was carried by the name `field` and by nothing else, and a
compiler that keyed on the name of a part would be reading protobuf's
vocabulary into the language.

Section 9.5 and the grammar in section 7 gain the argument.
