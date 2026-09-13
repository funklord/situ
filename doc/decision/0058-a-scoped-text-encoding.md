# 0058: a text encoding that a scope states

Status: accepted 2026-09-13; both halves built 2026-09-13 -- the struct
scope, then `[encoding = from(f)]` in all four backends and both walkers
(26.348, 26.351), and over a fixed-count member too (26.352)
Date: 2026-09-13
Phase: raised by the copyright holder, 26.330

## Context

**Two requests, both about where an encoding applies rather than what the
encodings are.** A region should be able to say what it is encoded in, and
a format should be able to name its encoding in its own data -- MIME's
`charset=`, an XML declaration, an HTML `<meta charset>`.

**Encoding is the odd one out among three cross-cutting properties, and
that is the whole of the first question.** `endian` and `bit_order` are
already "a file-level directive, overridable per struct, overridable per
field" (section 8.3, `layout.Scope`). Measured rather than assumed:

    struct little_one [endian = little] {
            u16  a;
            u16  b [endian = big];
    }

compiles today and gives `a` little and `b` big. Encoding has a file
directive and a per-field attribute and nothing between them, so a struct
cannot say what its text is. Making it follow the shape its two siblings
already have is the smallest language that answers the request, and the
mechanism is `Scope`, which exists.

**The word is overloaded, and the two meanings have different arities.**
This is the constraint that decides the first question's shape:

- `encoding ascii | utf8 | latin1;` is *what a character literal in this
  schema means*. It is a LIST on purpose -- "the strongest thing a schema
  can say about bytes whose encoding the data does not carry" -- and it is
  decidable because a literal is accepted only where every listed encoding
  gives it the same code unit.
- `[encoding = utf8]` is *what the message's bytes are*. Exactly one.

So a scope for the second cannot inherit a default from the first: there
is nothing to inherit, since the first names several by design. A
file-level data encoding would be a third meaning for one word.

**And the existing refusal is about the first, not the second.**
`wellformed.check_encoding_place` refuses a directive below a struct
because "a character literal is resolved where it is written, so a
directive here would give the literals above it a different meaning from
the ones below". That reasoning is untouched by anything here and the
refusal stays. Note that `endian`'s directive IS positional by 8.3 --
"applies to the declarations that follow it and to nothing before it" --
and encoding's deliberately is not. The two directives differ for a stated
reason; the two *attributes* need not.

## Decision

**`[encoding = ...]` becomes a struct attribute as well as a field one.**
Two levels, not three:

    struct header [encoding = utf8] {
            u8  name[32];
            u8  raw[16] [encoding = ascii];
    }

Every text member in the struct is checked as that encoding unless it
states its own. No file-level default, for the arity reason above.

**It resolves onto the member and changes nothing downstream.** The scope
puts an ordinary `[encoding = X]` on each member that has none, which is
the shape `pad_to` already uses for `must_be_zero` -- "the policy rides on
the placement as an ordinary reserved attribute, so the existing
validation needs no pad special case". Everything that reads an encoding
reads `placement.attrs`: the packer's `ENCODED_AS` constraint, both
walkers, all four backends. So this costs no image change, no backend
change and no walker change, which is why it is worth doing on its own.

**The data-named half is specified here and not built.**
`[endian = from(marker)]` is exact precedent -- a property the data names,
with `endian_marker` declaring the value-to-meaning mapping -- so the
shape is `[encoding = from(charset)]` beside a declaration mapping the
field's values to encodings. What stops it being the same change twice is
one sentence in `EndianMarkerDecl`:

> A marker travels with the data, so exactly one encoding is valid once
> the marker is known -- and **endianness never changes extent**, so this
> costs nothing on the offset or size axes.

An encoding does change extent. 0044 settled that `[encoding = utf16]` on
a `u16[length]` run states that `length` counts CODE UNITS, "which is the
distinction the wire format itself omits and the trap the reader was
written around". So a data-named endianness can be resolved after the
layout is fixed and a data-named encoding cannot: the layout may depend on
the answer.

**What that implies, and what a later record has to settle.** A data-named
encoding is admissible only where the extent does not depend on it --
which is decidable, because the compiler already knows whether a run's
length is in bytes or in code units. The rule is therefore not "refuse the
circular case", which is what 26.330 recorded from the length precedent;
it is two rules, and only the first was written down:

1. The field naming the encoding is declared before the bytes it governs
   -- the rule expressions already have, so the circular case is a
   compile-time refusal rather than a surprise.
2. No member the encoding governs may have an extent that depends on it.
   Otherwise the size of the thing being read is a function of a value
   inside it.

**The chicken and egg is not resolved and is not ours to resolve.** The
copyright holder named its shape exactly: "it usually comes out to be the
same sequence, it is formally broken". A declaration is read before its
own answer is known, so it is read under an assumption; for the
ASCII-compatible encodings the assumption holds and that is luck rather
than proof. What situ can offer is the assumption written down and
checked: a bootstrap encoding for the region the declaration lives in, the
declared encoding for what follows, and a refusal when the two disagree.
XML's own answer has the same shape -- detect a family from the first
bytes, then confirm the declaration against it.

## Consequences

**The corpus schema lands with the construct**, per 0052, 0055, 0056 and
0057's experience: a construct with no corpus schema poses no case to the
four-way differential.

**`[encoding]` on a struct does not imply the struct is text.** It says
what its text members are encoded in. A struct with no text member and an
encoding attribute is not an error and states nothing -- the same way
`[endian = little]` on a struct of `u8` says nothing.

**The second half is refused until its two rules are written, and the
refusal names the rule rather than the construct.** A schema reaching for
`[encoding = from(f)]` should be told that the extent question is
unsettled, not that the language does not have the idea.
