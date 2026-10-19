# 0063: `canonical` stays refused on a scaled number, and this is why

Status: proposed
Date: 2026-10-19
Phase: 26.659

## Context

0056 introduced `scaled` and left two things open: what a canonical
decimal spelling is, and whether `[minimal]` can buy `Canonical` back.
It said the question *"has published answers to consult -- RFC 8785
canonicalises JSON numbers through a float, which this construct
deliberately does not have"*, and that it was not one to settle in the
record introducing the construct. This is that record.

A scaled number reads an exact decimal as a significand and a power of
ten: `1.50` is `(150, -2)` and `1.5` is `(15, -1)`. Two byte sequences,
one value, so the member is `canonical = NonCanonical`. 0056 refused
`[minimal]` on the ground that the narrowing it would apply -- no
leading zero, no trailing fraction zero, no `+` or leading zero in the
exponent, no zero exponent -- leaves `10` and `1e1` as two spellings of
one value, and that **no local rule separates them**: each is natural in
some format, and preferring either refuses traffic somebody sends.

## The measurement

**The construct has exactly one asker**, and it is the format whose
canonicalisation is published: `example/json/json.situ`'s `number`
struct, `scaled i64 value before ',' | ']' | '}' [trim]`. `std/image.situ`
names 0056 in a comment and `example/rtc` scales by a power of two,
which is a different construct.

So the question is not abstract. It is: can situ declare JSON's number
field `Canonical`? RFC 8785 answers, and the answer is consulted here
rather than recalled:

- **Section 3.2.2.3**: *"ECMAScript builds on the IEEE 754 [IEEE754]
  double-precision standard for representing JSON number data."* Numbers
  MUST be serialised per ECMA-262 section 7.1.12.1 including its Note 2.
- **Appendix D**: numbers outside that exactly-representable range MUST
  be wrapped using the JSON string type, integers ideally staying within
  -9007199254740991 to 9007199254740991.

## The decision

**`canonical` stays refused on a scaled number, and `[minimal]` with
it.** Not as an open question deferred, but because consulting the one
published answer closes it:

1. **The only canonicalisation its asker has is defined through a double,
   which this construct deliberately does not have.** situ reads an
   exact decimal precisely so that `-3.25e2` is -325 and 0 rather than a
   `double` that is nearly -325. Implementing JCS would mean acquiring
   the float the construct exists to avoid.

2. **For most of the construct's own range, the published answer is that
   the value must not be a JSON number at all.** A `scaled i64`
   significand spans +-9,223,372,036,854,775,807, which is 1,024 times
   JCS's exactly-representable integer range -- **99 per cent of the
   significand's range has no canonical JSON number form**, Appendix D
   requiring a string instead. A member declared `Canonical` would be
   making a claim that is wrong for almost all of what it can read.

3. **So an invented normal form would be worse than refusing.** situ
   could define one -- strip trailing zeros from the significand, prefer
   no exponent -- and it would be self-consistent, and it would declare
   a JSON field canonical while JCS disagreed with it above 2^53. A map
   that says `Canonical` where the protocol's own specification says
   otherwise is the plausible-wrong-answer this repository rates worst.

**What `NonCanonical` costs is nothing a consumer needs.** It is the
honest reading: two byte sequences carry one value, and a reader that
wants to compare values compares the pair rather than the bytes.

## Alternatives rejected

- **Define situ's own exact-decimal normal form and declare it
  canonical.** Rejected per 3: self-consistent and wrong for the one
  protocol that has published an answer.
- **Implement JCS, acquiring a double for the comparison only.** The
  comparison would then disagree with the accessor, which hands back an
  exact pair; and a freestanding C runtime has no `strtod` (0056).
- **Make `canonical` take the convention as an argument**, so a schema
  declares which spelling its format canonicalises to. Not rejected on
  merit -- it is the shape that could work -- but it is a language
  change with one asker, and that asker's convention cannot be expressed
  without a float. Revisit if a second format asks with a rule situ can
  state exactly.

## Consequences

`[minimal]` keeps refusing on a scaled member with the diagnostic 0056
describes. The capability map keeps saying `NonCanonical`, and
`example/json/json.situ`'s comment already says why at the member.

**This record is what makes the refusal a decision rather than a
deferral.** 0056's own text invited the consultation; the consultation
is above, with the sections quoted and the range measured.
