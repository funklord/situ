# 0018: `ratio_padded(a, b)`, for codes that emit whole groups

Status: accepted
Date: 2026-07-28
Phase: 26.15

## Context

Section 13.2 gave `expansion` five forms. Decision 0016 established that a
composed signature may carry a ratio and an addend together, because
`rs |> manchester` is affine and none of the five said so.

base32 and base64 are the next thing none of them says. Their ratios are exact
at the bit level -- 8 output bits per 5 input bits, 8 per 6 -- but the output is
always a whole number of groups, and a group is five input bytes or three. One
byte of input produces four characters of base64, not the two an exact ratio
predicts, because the last group is filled out with `=` rather than truncated.

Every representable form is wrong for it:

- `ratio_exact(4, 3)` predicts 2 bytes for an input of 1. The answer is 4.
- `ratio_bounded` is true but throws away an output length that is a closed
  form of the input length, costing every member after the region its static
  offset for no reason.
- `+N` cannot express a ratio at all.

The failure is not in the ratio. It is that the ratio applies at group
granularity and the vocabulary had no way to say where a group ends.

## Decision

A sixth form:

```
output = ceil(input / group_in) * group_out
```

where the group follows from the ratio rather than being declared. A group is
the smallest run of input that is both a whole number of bytes and a whole
number of symbols -- `lcm(8, b)` bits -- which for base64's six-bit symbols is
24 bits, three bytes, and for base32's five-bit ones is 40 bits, five bytes.

Deriving the group rather than declaring it matters: an author who could write
the group size could write one that disagrees with the ratio, and there is no
sensible reading of that. The two numbers are not independent.

**No propagation row changes.** Every row that consults `expansion` asks whether
the form is `PRESERVING`, and `ratio_padded` is not, exactly as `ratio_exact`
is not. Interior positions survive it for the same reason they survive an exact
ratio: a given input byte is in a computable group at a computable offset. Only
the final group's *extent* depends on how much input there was, and extent is
what `_expand` computes.

## Consequences

`granularity` becomes `block(group)` rather than `symbol(bits)`. That is the
useful statement: the unit a reader has to align to is the group, not the
character, and a decoder handed half a group has nothing to do with it.

The generated decoder refuses input whose length is not a whole number of
groups, and input carrying a byte outside the alphabet. Both are the kind a
decoder actually sees, because the input to a decoder is by definition
something somebody else produced.

## Alternatives considered

**A `group` argument on the kernel.** Explicit, and it would let a code declare
a group the ratio contradicts. Rejected: the group is a function of the symbol
width, so offering to state it separately offers to state it wrongly.

**Round the exact ratio up to a multiple of the output group.** Arithmetically
equivalent -- `roundup(ceil(input * a / b), a)` gives the same answers -- and it
would need no new form, only a flag. Rejected because it describes the
consequence rather than the cause: the code emits whole groups, and rounding
the output is what that looks like from outside.

**Treat base64 as a stuffing code.** It does have `ratio_bounded`'s shape from
a distance. Rejected because stuffing's bound is data-dependent -- COBS's
overhead depends on where the zeros are -- and base64's is not. Two codes with
genuinely different predictability should not share a form.
