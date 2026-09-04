# 0049: an operation the backends define differently is refused, not emulated

Status: accepted and built, 2026-09-04
Date: 2026-09-04
Phase: raised by 26.208 and 26.214, settled with the other open items

## Context

situ emits five descriptions of one layout, and its central claim is that
they agree. Four arithmetic operators do not mean the same thing in the
languages those descriptions are written in:

- **`/`** truncates toward zero in C, C++ and Rust, floors in Python and Lua,
  and is *float* division in the last two unless spelled `//`. 26.208 found
  it in a field expression; 26.214 found it again in a relation, where the
  generated Python read `v_1 / v_3` and a relation satisfied by three
  backends was refused by the fourth.
- **`%`** takes the sign of the dividend in C, C++ and Rust and the sign of
  the divisor in Python and Lua: `-7 % 3` is -1 against 2.
- **`<<` and `>>`** by an amount at or above the operand width are undefined
  in C, an `arithmetic_overflow` deny in Rust, a panic in a debug build, and
  an ordinary answer in Python.

For non-negative operands and in-range shift amounts every one of these
agrees, which is why a corpus of well-formed schemas never showed it.

## Decision

**Where the four compiled backends and the two scripted ones do not define
an operation identically, situ refuses the expression rather than emulating
the difference.**

Concretely: `/` and `%` are refused where an operand may be negative, and a
shift is refused where the amount is not provably below the operand's width.
The non-negative and in-range cases -- which is nearly every real schema --
are unaffected, and `/` is spelled `//` in Python so that the *accepted*
cases agree too.

**The refusal is not a preference for strictness. It is the only policy all
five descriptions can hold.** Emulation means emitting a helper --
`trunc_div(a, b)` and its modulo -- in the languages that differ. Python can
have one. **The Lua dissector cannot**, and not for a reason that can be
argued away: decision 0021 keeps `//` out of generated Lua because Wireshark
bundles 5.2 in older builds, where it is a syntax error, "and a dissector
that fails to load is worse than one that divides". `situc/dissector.py`
already refuses `/` outright for this reason, in `_UNSPELLABLE`.

So emulation would buy agreement among four descriptions and leave the fifth
declining -- which is the divergence the whole exercise exists to prevent,
moved one description over. A rule that four of five can follow is not a
rule.

## Alternatives considered

**Emulate everywhere except the dissector, and let it decline.** The
dissector already declines these expressions, so this costs nothing today
and is the tempting answer. It makes the language's guarantee conditional on
which description you are reading, and the guarantee is the product: a
schema would mean one thing to four backends and be unstatable in the fifth,
with nothing in the schema saying which.

**Mask the shift amount.** `a << (b & 63)` is defined everywhere and is what
C programmers assume the hardware does. It silently changes the answer for
`b >= 64` rather than refusing it, which is worse than either honest option:
the schema would state a shift and the code would perform a different one.

**Refuse only in the construct where it was found.** 26.208 refused in a
field expression and 26.214 had to carry the same rule into relations by
hand, having been caught by `bound_widening`'s own lesson -- "a caveat
written in a message is not a guard in the compiler". A rule that lives in
one path and not the next is how both of these got in.

**Accept the narrowing without recording it.** 26.208 flagged that
`(n - 10) / 3 + 5` stops being writeable even though C computes it
correctly. That is a real loss and it is the price; what makes it payable is
that the alternative loses the dissector, and what makes it honest is
writing it down here rather than leaving it a diagnostic somebody meets.

## Consequences

- The remedy a refused schema gets is to order the operands so the result
  cannot go negative, or to bound the shift with `[max]`, both of which the
  diagnostics already suggest.
- `/` and `%` on signed operands are refused in field expressions (26.208)
  and in relations (26.214).
- The shift rule is built in both, at a width of 64: the generated C widens
  with `situ_leaf_u64` before it shifts, and a relation widens every operand
  to a signed 64-bit value, so the bound is the same in both and is not the
  field's own width. A literal amount, a `u5`, or a `u8` carrying
  `[max = 63]` all pass; an unbounded `u8` does not, and neither does 64
  itself. `layout.constrain` already folds `[max]` into the interval a field
  expression is checked against, so the remedy the diagnostic names is one
  the solver acts on -- checked, because a refusal suggesting a fix nothing
  accepts sends the reader in a circle.
- A relation refuses an *expression* as the amount rather than analysing it.
  `a.hdr.index + 1` is bounded in fact, and proving it needs interval
  machinery a relation is checked without. Unknown is treated as unprovable,
  which is the direction where being wrong refuses a legal schema instead of
  emitting three programs.
- An invariant is unaffected: every term it may use -- `offset`, `size`,
  `count` -- is non-negative by construction, and `negative_value` refuses
  the one operator that can leave that range.
- If Wireshark's floor ever moves past 5.2, this record is the one to
  revisit: the constraint that decides it is 0021's, not arithmetic.
