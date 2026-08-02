# 0027: a packed-decimal field may declare a width narrower than its digits

Status: accepted
Date: 2026-08-02
Phase: after 26.35

## Context

`bcd<d>` is a nibble a digit (section 8.1), so `bcd2` is eight bits and holds
0 to 99. That is what a seven-segment display wants and what a real-time clock
register very often is not.

A DS1307's register file, from `drivers/rtc/ds1307.c` in U-Boot:

| register | 7 | 6 | 5 4 | 3 2 1 0 |
|---|---|---|---|---|
| 0x00 seconds | Clock Halt | tens of seconds | | units |
| 0x02 hours | 0 | 12/24 | AM/PM or tens | units |

The seconds register spends its top bit on Clock Halt, leaving *seven* bits of
packed decimal: three of tens above four of units. The hours register spends
two, leaving six. Every driver in `drivers/rtc/` masks the control bits off
before decoding -- `bcd2bin(sec & 0x7F)`, `bcd2bin(hour & 0x3F)` -- and several
parts put a century bit in the month register besides.

Situ could not describe either register. `bcd2` is eight bits, and a field of
`u1` above it made the byte nine bits wide; splitting it into `u1` plus `u7`
described the placement and threw away the decimal encoding, so the getter
would hand back 86 for the byte that says 56. Masking would be the caller's,
which is the work a description exists to remove -- and it is the work that
makes reading a device register error-prone in the first place.

This was found by writing the vectors for `examples/rtc` against U-Boot's
`bin2bcd` (26.35): the encoding matched, and the register file could not be
written down.

## Decision

A packed-decimal field may declare a width:

```situ
struct ds1307_clock {
	u1    clock_halt;
	bcd2  seconds  [bits = 7, max = 59];

	u2    hour_mode;
	bcd2  hours    [bits = 6, max = 23];
}
```

`[bits = N]` narrows the *top* digit and leaves every digit below it a whole
nibble, which is what the hardware does. `bcd2 [bits = 7]` is three bits of
tens over four of units and spans 0..79; `[max = 59]` is what says which of
those the field means, and it is the schema's to state as it already was.

The rules:

- `[bits]` applies to a `bcd` type and to nothing else. Every other type in
  situ carries its width in its name -- `u7` is seven bits -- and `bcd<d>`
  names digits, which is why it is the one that can be narrowed. `[bits]` on
  anything else is an error rather than a silent no-op.
- `N` may not exceed the type's own width. Padding a field out is a different
  type, not a narrowing.
- `N` must leave the top digit at least one bit: `4 * (d - 1) < N <= 4 * d`.
- Everything else is unchanged. The field is an `N`-bit field and section 8.2's
  packing rules apply to it as written -- it straddles a byte or it does not,
  and `[allow_straddle]` says which is acceptable. `repr` stays
  `ValueConverted`, and the nibble-above-nine validator (8.1) still runs, over
  the narrowed value.

## Alternatives considered

**A `[decimal]` attribute on a plain bit field**, so `u7 seconds [decimal]`
would be seven bits read as packed decimal. It needs no new width rule and
reads as what it is -- an encoding claim about a field the schema already
places. Rejected because it splits packed decimal across two spellings: `bcd2`
for a byte and `u7 [decimal]` for seven bits, with the digit count implicit in
the width in one and explicit in the other. A reader would have to know both,
and `bcd3` -- twelve bits, already bit-packed -- would be spellable two ways.

**A type name carrying the width**, `bcd2:7` or `bcd2_7`. Consistent with `u7`
and `q8_8`, and rejected for the same reason it looks consistent: it puts two
numbers in the name that mean different things, and `bcd2_7` reads like a
fixed-point type. The width is a property of the *placement* here, not of the
encoding.

**Leave it, and let the caller mask.** What the RTC schema said before this
change, and honest as far as it went: the schema described the encoding rather
than the register file, and said so. Rejected because section 15 makes
memory-mapped registers a target of this language, and a control bit above a
decimal field is what a register is. A description that cannot describe the
part is the case 26.32 keeps making.

## Consequences

- `examples/rtc` describes the DS1307's register file, control bits included,
  rather than an encoding that resembles it.
- One more attribute the layout pass reads, and one more error it can raise.
  The narrowing happens in `Layout.narrowed` and reaches every backend through
  the `ScalarType` they already read, so no backend needed a change.
- A schema that wants the old shape writes `bcd2` and gets it. Nothing about
  an unnarrowed field moves.
