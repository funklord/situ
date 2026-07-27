# 0016: a composed expansion may carry both a ratio and an addend

Status: accepted
Date: 2026-07-27
Phase: 12

## Context

Section 13.2 gives `expansion` five forms, as alternatives:

```
length_preserving | +N | ratio_exact(a, b) | ratio_bounded(a, b) | unbounded
```

Section 13.4's own pipeline example does not fit any of them:

```situ
codec framed = rs_255_223 |> interleave(16) |> manchester;
```

Reed-Solomon appends 32 bytes of parity (`+32`). Manchester doubles everything
(`ratio_exact(2, 1)`). The composition takes `n` bytes to `(n + 32) x 2`, which
is affine rather than linear and rather than constant. Every representable form
is wrong for it:

- `+64` loses the doubling of the data.
- `ratio_exact(2, 1)` loses the parity.
- `ratio_bounded` is false for small inputs and gives up exactness the code
  actually has.
- `unbounded` is catastrophically conservative: it would make every member
  after the region Dynamic and Unbounded, for a code whose output length is a
  closed form of its input length.

The spec's own example is the counterexample to its own vocabulary.

## Decision

`expansion_add` may be non-zero alongside a ratio. The form stays the ratio and
the addend rides along in the arithmetic:

```
output = ceil(input * a / b) + N
```

Composition scales the addend by the ratios that follow it, because that is
what physically happens: parity appended before a doubling gets doubled.
`rs |> interleave |> manchester` therefore composes to `ratio_exact(2, 1)` with
`expansion_add = 64`.

**The form stays the ratio because the form is what the lattice reads.** Every
propagation row that consults `expansion` is asking whether interior positions
survive -- and they do: appended parity does not move the data in front of it,
so a field's offset is still a linear function of its offset in the input. The
addend changes the region's extent and nothing about its interior addressing,
which is exactly the split between `_expand` and the rows.

That is also why this does not violate section 26.12's "no propagation rule
changes in this phase". No row's condition changes; the arithmetic in
`_expand` gains a term.

## Alternatives considered

**Refuse to compose a fixed addition with a ratio.** Consistent with situ's
habit of erroring rather than approximating, and it was the first thing tried.
Rejected because the pipeline it refuses is the one section 13.4 uses as its
worked example, and a language that cannot describe RS-then-Manchester has
missed the point of having pipelines.

**Report `ratio_bounded`.** Representable today, and wrong: it claims the
output length is data-dependent when it is a closed form, which would cost the
schema its exact interior offsets for no reason.

**Add an `affine(a, b, N)` form.** Honest, and it makes the vocabulary six
forms where one of them subsumes three others. Rejected as more surface than
the problem needs: a ratio with an addend of zero is the ratio, and the
existing five forms keep their meaning unchanged.

## Consequence

Section 13.2's table should say that `+N` and a ratio may appear together in a
*composed* signature, even though a hand-written one gives one or the other. A
tier-1 author still writes one form; the combination only arises from
composition, where the compiler computes it.
