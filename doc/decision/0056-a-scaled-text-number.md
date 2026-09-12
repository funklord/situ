# 0056: a text number with a point and an exponent

Status: accepted 2026-09-11; built in all four backends and the walker;
amended 2026-09-12 -- json's `number` is a `scaled i64` now (26.339)
Date: 2026-09-11
Phase: raised by the copyright holder, from json's `number`

## Context

**json describes the one field a JSON reader most wants as a byte run:**

    struct number {
    	u8  rest[] before ',' | ']' | '}' [trim];
    }

The schema says where the number stops and nothing about what it is.
`decimal` and `hex` (8.6.2) cover integers, signed and unsigned, delimited
and padded; nothing covers a fraction or an exponent, and the refusal is
explicit rather than absent:

    struct s { decimal f64 value until ","; }

    error: `value` is a text number, so its type must be an integer
      = the type gives the range of values the digits may spell, and situ
        reads digits as an integer
      = a fractional text format needs a point and an exponent, which is a
        grammar rather than a number: frame it as a byte run and let the
        reader parse it

Section 8.6.2 records the same sentence: "A point and an exponent are a
grammar rather than a number, which is the same line drawn below."

## Decision

**The grammar objection does not separate the two cases.** `decimal i32
offset until ","` already reads `-42`. That is an optional sign, which is
alternation, and a run of digits, which is repetition -- two of the three
things 8.6.5 says stay out of a layout. A fraction adds a third optional
part and a second run of digits. If repetition and alternation were the
line, the integer form would be on the wrong side of it.

**What actually differs is what the value maps onto.** An integer text
number maps onto `uN` or `iN` exactly, by accumulating digits, and four
independently written backends get the same answer by construction. A
fraction maps onto a binary float only with correct rounding: `0.1` has no
exact double, and a seventeen-digit significand needs an algorithm rather
than a loop. Four implementations of that are four chances to disagree, in
a project whose four-way differential exists to catch exactly that.

**So the construct is admitted and the float is not.**

    scaled i64  value  until ",";

reads the bytes as an EXACT decimal and reports two integers: a
significand, in the declared type, and a power of ten.

    "12.5e3"   -> significand 125,   exponent 2     (125 * 10^2)
    "-0.004"   -> significand -4,    exponent -3
    "1.50"     -> significand 150,   exponent -2

Three reasons it is the pair and not a double:

- **It is exact, so the backends agree by construction** rather than by
  four separate correct-rounding implementations. The arithmetic is the
  integer parse the runtime already has, plus a count of where the point
  was.
- **`strtod` is locale-dependent.** In a locale whose decimal point is a
  comma, `strtod("1.5")` stops at the point. That is the same class of
  fault as `tolower` in 0055: a wire format must not mean different things
  to readers in different environments. The C runtime is header-only and
  freestanding-friendly besides, and `strtod` is not available to it.
- **The rounding belongs to whoever wants a float.** A consumer that wants
  a double can compute one and own the error; a schema that handed one over
  would have chosen for it, silently, in the accessor.

**No normalization: the pair reflects the bytes.** `1.50` reads
`(150, -2)` and `1.5` reads `(15, -1)`, which are the same number and
different spellings. That is deliberate -- situ describes bytes -- and it
makes a scaled number `canonical = NonCanonical`, because two byte
sequences carry one value.

**`[minimal]` is REFUSED on a scaled number, and working out what it
should mean is what made this paragraph honest.** It first said `[minimal]`
buys `Canonical` back by forbidding a leading zero on the integer part, a
trailing zero in the fraction, a `+` in the exponent, a leading zero in
it, and an exponent of zero. Every one of those is a real second spelling
and removing them is a real narrowing -- and it is not canonicality,
because `10` and `1e1` survive all of it and are one value. No LOCAL rule
separates them: each is the natural spelling in some format, and a rule
preferring either would refuse traffic somebody sends.

So the attribute is refused rather than half-implemented, with a
diagnostic saying which part is settled and which is open. Refusing it
also avoids a concrete wrong answer: `situ_digits_minimal` forbids a
leading zero, so run unchanged on a scaled member it refuses `0.5`.

What a canonical decimal spelling is remains open, and it is a question
with published answers to consult -- RFC 8785 canonicalises JSON numbers
through a float, which this construct deliberately does not have. It is
not one to settle in the record that introduces the construct.

**Overflow is refused rather than rounded.** A significand with more
digits than the declared type can hold is a number the schema said it
could read and cannot, which is what the integer form already does. Note
that this is a property of the SPELLING and not of the value:
`1.5000000000000000000000` overflows an `i64` significand where `1.5` does
not. Refusing is the honest answer, and `[minimal]` refuses that spelling
one step earlier for a better reason.

## Alternatives considered

**A float accessor, parsed with each language's own correctly-rounded
reader.** `std::from_chars`, Rust's `str::parse`, Python's `float()` are
all correctly rounded, so three of the four are free. Rejected on the
fourth: C has `strtod`, which is locale-dependent and absent from a
freestanding runtime, so the one backend that cannot do it is the one
every other reading is checked against. A construct whose correctness
depends on a locale is not one this project should ship.

**Reuse `decimal` with a float type.** `decimal f64 x` is what an author
tries first -- it is what the refusal above is written against. It reads
well and it hides the whole question: the scalar would stop naming the
value's domain and start naming a conversion, and the rounding would be
invisible at the point where somebody chose it.

**Normalize the pair, so `1.50` and `1.5` both read `(15, -1)`.**
Friendlier for equality, and it would make the significand overflow above
mostly disappear. Rejected because the pair would then no longer be a
reading of the bytes, and a schema language that quietly rewrites what it
read has stopped describing the format. The same information is available
from the honest pair; comparing values is `a.sig * 10^a.exp == b.sig *
10^b.exp`, which is the consumer's arithmetic to do.

**Leave it, and let the reader parse the byte run.** The status quo, and
its cost is measured rather than predicted: json's `number` is the
schema's largest silence, and every consumer of it writes the same parser
with the same rounding decisions unreviewed.

## Consequences

**A corpus schema lands with the construct**, per 0052's consequence and
0055's experience of it: a construct with no corpus schema poses no case
to the four-way differential, and the differential is the only thing that
can catch three backends agreeing wrongly.

**And a corpus schema poses no case either, unless the draws can reach
it.** The differential draws from fixed alphabets. The first version of
this construct's corpus struct framed on `","`, which appears in none of
them, so the member never terminated, all four agreed the frame stopped
early, and the parse was compared by nobody. The struct frames on `":"`
and `" "` now, and the digit alphabet gained `eE+` -- without those an
exponent cannot be drawn at all. The control is a sabotage aimed at the
fraction alone, which leaves every plain integer right and only `12.5`
wrong; the differential goes red on it.

~~**Its only asker cannot use it, and that is a fact about a different
missing feature.**~~ **Amended 2026-09-12 (26.339): json uses it.** What
follows is why it could not, kept because it is the measurement.

json's `number` is the schema this record was written from, and
`number.rest` was not a number: `value` switches on `kind`, a
discriminant occupied its byte, and the arm began after it. `{"a":12.5}`
handed the `number` struct `2.5`. A `scaled i64` there would have
reported `(25, -1)` confidently for a document whose number is 12.5 -- a
wrong value where there was an honest byte run.

26.253 recorded the blocker, from an argv evaluation: "dispatch
consumes; a text grammar needs it to keep". `before` was named there as
the shape of the answer, and **0057 is that answer** -- `peek` being
`before` for dispatch. The language obstacle went on 2026-09-12 and the
conversion still waited on the WALKER, three faults deep (26.337): a
peeked member's span, a permissive `default:` the walk never validated
(26.338), and a bounds policy that refused where four backends answered
(26.339). With those closed, `example/json` carries the worked example:

    {"a":12.5}      value  125   power -1
    {"a":-3.25e2}   value -325   power  0

against the generated C.

**`[minimal]` is open and so is `canonical`.** A scaled number is
NonCanonical today with no way to say otherwise, which is a real cost:
a format whose numbers ARE canonically spelled cannot say so. Settling
it needs the canonical-form question above answered first.

**The exponent's type is not the schema's to declare.** It is a power of
ten and it is `i32` everywhere. A schema that could name it would be
naming the number of digits a format may carry, which no format states.

**`decimal`, `hex` and `scaled` are now three keywords in one slot, and
the trio is input to the vocabulary pass rather than settled here.**
Arguably `decimal` should name THIS construct and the integer form
something narrower -- a decimal number in every other context may carry a
point. That is exactly the kind of question 26.327 defers until the
featureset is known, and renaming a keyword now would be doing it twice.
