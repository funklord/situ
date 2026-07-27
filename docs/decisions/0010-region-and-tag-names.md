# 0010: regions and tags may be named, and default to their keyword

Status: accepted
Date: 2026-07-27
Phase: 8

## Context

Section 7's grammar gives the cryptographic constructs no names:

```ebnf
tag_field = "tag" scalar_type array_spec [ "covers" "(" ref_list ")" ]
            [ attrs ] ";" ;
block     = "authenticated" "{" { member } "}"
          | "sealed" "(" codec_args ")" "{" { member } "}" ;
```

Two things in section 14 need names anyway.

A `covers` clause names regions -- `covers(a, b)` -- so a struct with two
`authenticated` blocks has no way to say which one it means. Section 14.1
permits several tags per struct with disjoint or nested coverage, which is
exactly the case that needs the distinction.

And example 5.3 writes `require verify_gated(Packet.sealed)`, which is a path
naming a region that was declared without a name. So the language already
assumed an implicit one.

## Decision

Every region and tag carries a name. Where the schema does not give one, the
name is the keyword that introduced it: `sealed`, `authenticated`, `tag`,
`checksum`. That is what makes `Packet.sealed` resolve in 5.3 with no change to
the source there.

A name may be written before the argument list or the brace:

```situ
authenticated head { ... }
sealed body(aes_gcm_128, nonce = n) { ... }
tag u8 outer[16] covers(head, body);
checksum u16 crc[1] covers(head);
```

The parse stays unambiguous: what follows an unnamed region is `(`, `{` or `[`,
never an identifier. The one identifier that could follow a tag's array spec is
`covers`, which is checked for by name.

Two regions in a struct may not share a name, because `covers` could not then
name either of them. That is an error rather than a silent first-match.

## Alternatives considered

**Positional references in `covers`.** `covers(0, 1)` needs no names at all.
Rejected: it is the field-number design situ rejects everywhere else (section
4), and inserting a region silently repoints every clause after it.

**Require a name always.** Consistent, and it would have made the grammar's
silence a plain omission rather than an implicit default. Rejected because 5.3
is written without one and reads better for it: a struct with a single sealed
region gains nothing from being made to name it, and `Packet.sealed` is exactly
what a reader would guess.

**Name only where ambiguity forces it.** The parser could demand a name only in
a struct with two regions of the same kind. Rejected: the diagnostic would
arrive when a second region is added, which is the worst moment to learn the
first one needed a name, and every path naming it would change at once.

## Consequence

`covers` resolution, the `Placement.regions` stamp and the map all key off the
name, so the default has to be stable. It is the keyword, and the keyword does
not change.
