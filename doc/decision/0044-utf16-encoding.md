# 0044: `[encoding = utf16]` states code units and endianness

Status: proposed
Date: 2026-08-26
Phase: after 0043

## Context

`suggestion/hydra.md` writes `base::Pickle`'s UTF-16 strings as

    u16 data[length];

which is the honest layout and loses that the bytes are text -- a reader of
the schema cannot tell it from a `u16` run that happens to be even. Situ
already treats encoding as a property: `smtp.situ` carries `[encoding =
ascii]`, and 8.6 names `ascii` and `utf8`, each with a generated validity
check. `[encoding = utf16]` is refused because the set is closed.

The ask is sharper than "let it through", and the sharpness is the reason to
build it. The consumer's hand-written reader carries an overflow guard
precisely because *a length field in this format sometimes counts characters
and sometimes bytes, and nothing distinguishes them*. A schema that states
`[encoding = utf16]` on a `u16[length]` run is stating that `length` counts
code units, which is the distinction the wire format itself omits and the trap
the reader was written around.

## The fork

UTF-16 has a byte order and UTF-8 does not, so `[encoding = utf16]` alone does
not say what the two bytes of a code unit mean. Three readings:

- The code unit's endianness follows the field's declared `endian`/`bit_order`
  scope, like every other multi-byte value.
- `utf16` is underspecified and refused; only `utf16le` and `utf16be` are
  accepted, each naming its order outright.
- A byte-order mark in the data decides it, as `endian_marker` already does for
  a struct.

And a validity question that is not optional: a lone surrogate -- a high or low
surrogate code unit with no partner -- is a code unit sequence that decodes to
no character, the UTF-16 analogue of utf8's overlong form, and the utf8 check
already rejects its analogue. A permissive utf16 check would be a weaker
promise than the utf8 one beside it.

## Decision

**`utf16le` and `utf16be` are the encodings; bare `utf16` is refused, naming
the two.** Endianness is part of the encoding rather than inherited from the
field's scope, and a validity check rejects an unpaired surrogate.

- **Named order, not inherited.** A text encoding is a property of the text,
  and inheriting the code unit's order from the surrounding `endian` directive
  would make `[encoding = utf16]` mean UTF-16LE or UTF-16BE depending on a
  directive three lines up -- the same silent-dependency trap 0043 refuses for
  `pad_to`. `utf16le` and `utf16be` say it where the reader is looking.
  `base::Pickle` is UTF-16LE on the platforms it ships on, and the schema says
  so.
- **No BOM reading.** A byte-order mark in the string data is content, and a
  format that uses one to choose order is choosing per string what the schema
  should state per field; `endian_marker` exists for a struct-level marker and
  is the construct to reach for if a format genuinely does this. Not this
  record's to add.
- **The check rejects a lone surrogate**, matching the strictness of the utf8
  check for the reason 8.8 gives: two byte sequences that decode to the same
  text, or to no text, are a malleability surface. `situ_utf16le_valid` and
  `situ_utf16be_valid` join `situ_utf8_valid` in the runtime.
- **The element is a `u16` run, and the encoding implies nothing about the
  element width** -- the layout stays exactly `u16 data[length]`, which is
  already what an author writes. The encoding is a claim checked over those
  bytes, not a shorthand that changes them.

## Alternatives considered

**Inherit endianness from the field's scope.** One fewer keyword, and the trap
above: the encoding's meaning moves with a distant directive, and a schema
copied between two files with different `endian` directives silently changes
what it validates.

**Accept bare `utf16` and default to one order.** A default here is a guess
about somebody's wire format written into the language, and the wrong guess is
a validity check that passes on byte-swapped text. There is no safe default;
the two named forms cost one word and remove the question.

**A permissive check that accepts lone surrogates.** Cheaper, and weaker than
the utf8 check it sits beside -- a schema would get less from declaring utf16
than from declaring utf8, which inverts the point of declaring at all. The
whole value of an encoding attribute is that the code checks it (26.60's rule
for `[size]`, met one attribute over).

**A `[count = units]` attribute instead, decoupled from encoding.** Names the
character-vs-byte distinction directly without committing to an encoding. But
the distinction the consumer hit is *which encoding*, and "these are UTF-16
code units" says both "count is units" and "the bytes are text" in one word
the map can render as "text". Two attributes for one fact is the duplication a
schema language removes.

## Consequences

- `TEXT_ENCODINGS` gains `utf16le` and `utf16be`; the encoding check emits the
  matching validator, which the four runtimes gain beside `situ_utf8_valid`.
- The encoding check currently guards on an 8-bit element; utf16 guards on a
  16-bit one, so the width check inverts for these two names.
- `situc doc` and the map say "text (utf16le)" where they said a `u16` run, and
  the wire signature carries the encoding as it does `encoding=utf8`.
- `example/` gains a UTF-16 case -- a Pickle-shaped string is the worked
  example, and it is the format the ask came from.
- The character-vs-byte trap becomes statable: `[encoding = utf16le]` on a
  `u16[length]` run documents that `length` is code units, which no other
  construct says.
