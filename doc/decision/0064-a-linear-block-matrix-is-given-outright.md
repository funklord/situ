# 0064: A linear block matrix is given outright, one `parity` per check bit

Status: proposed
Date: 2026-10-20
Phase: 26.662

## Context

`linear_block` knew one code. `hamming_7_4` was a name the three
emitters resolved to rows they kept themselves, and any other `(n, k)`
derived a correct `ratio_exact` and generated nothing. The open register
carried that as item 11 and said what it would take:

> `linear_block` knows one code. `hamming_7_4`, and going further needs
> the generator matrix expressible in a schema, which is a language
> addition rather than a table entry.

It had stood since 26.218 on that premise, and the premise was the
reason nobody took it: a language addition is the holder's, so an item
that names one is an item nothing moves.

## The measurement

**It needed no language addition.** `attr = ident [ "=" expr ]` is what
a kernel argument already is, and an argument may already repeat --
`table` has been taking its map outright as one `symbol` per input
symbol for as long as `_symbol_map` has existed, read by filtering
`kernel.args` rather than by `argument("symbol")`. Measured before
anything was written: a kernel with three `parity` arguments parses
today, keeps them in order, and round-trips through `unparse` with each
radix intact. The grammar is untouched by this change and
`test/unit/ebnf.py` stayed green through it.

So the question was never what the language can express. It was which
of two forms a matrix should arrive in, and the sibling family had
answered that already.

## The decision

**One `parity` argument per check bit, each a mask over the `k` data
bits saying which of them that row covers.** Hamming(7, 4) is

    kernel = linear_block(n = 7, k = 4, parity = 0b1101,
                          parity = 0b1011, parity = 0b0111);

and `code = hamming_7_4` goes on meaning the same rows by name, exactly
as `code = base64` and the `symbol` form coexist for tables.

**`systematic` is derived from the rows rather than declared.** They are
the P of a standard-form generator [I | P], so the data bits sit
verbatim at computable positions by construction -- which is what
`_linear_block`'s own docstring had called the property worth deriving
rather than trusting, while taking it from a `standard_form` flag. The
flag stays for a code described without its matrix.

**Wrong is refused with the count in front of it**, not taken as a
signature-only codec: a row that is not a literal, a set that is not
`n - k` rows, a row naming a data bit the message does not have, a zero
row, two identical rows. That follows 26.161 rather than `_symbol_map`,
whose silence is the fault that record exists for -- an author who wrote
one `parity` row meant to write them all.

**Three things bound what generates**, each with its own sentence in the
advisory rather than the catalogue's "not one of them":

- a named code supplies its matrix at its own dimensions and nowhere
  else;
- a codeword has to fit the byte the generated `encode` and `decode`
  take, so `n <= 8`;
- the rows have to locate a single error, because the generated decoder
  corrects: a matrix whose check columns repeat or vanish detects
  without locating, and a corrector generated for it would flip a good
  bit.

## Alternatives rejected

**A list literal in `expr`** -- `parity = [0b1101, 0b1011, 0b0111]`.
This is the language addition the item assumed, and it buys nothing the
repeated argument does not: it would be a second multi-value form in a
language that already has one, and `[` opens an `array_spec` elsewhere.

**One packed integer** -- `parity = 0xDB7`, rows of `k` bits most
significant first. Needs nothing new either and reads like `taps` or
`poly`, which was the argument for it. Refused because a CRC polynomial
is written as a hex number by every catalogue in the field and a parity
matrix is written as a matrix: the packing order would be a convention
nobody can see in the schema, and `0xDB7` tells a reader nothing about
three rows.

**A `matrix` declaration naming the rows, referenced by `code =`.** The
root fix, and it moves the question rather than answering it: `table`
and `stuffing` resolve `code` to compiler-side data too, so a new
declaration form would set a pattern for three families at once. That is
a bigger decision than this one and is not blocked by it.

## Consequences

The arithmetic moved to `codegen/kernel_math.py`, where the CRC tables
and the GF logs already live, because three emitters had a copy each of
the syndrome and codeword computation with the dimensions written into
it -- `3`, `4`, `7`, `0x0F`, seven times per backend. That is what had
bounded the family to one code, and it is the fifth-copy hazard that
module's docstring is about.

**It found the dishonest case 13.4 says does not happen.** The emitters
gated on the name and read the dimensions from nowhere, so
`linear_block(n = 15, k = 11, code = hamming_7_4)` generated Hamming(7,
4)'s tables under a signature saying `ratio_exact(15, 11)`: a consumer
sizing from the map and calling the pair would encode something else.
Named codes carry their dimensions now.

`test/schema/edges.situ` gained extended Hamming(8, 4) with its matrix
stated, which is a second `(n, k)` and a four-bit syndrome. The four
compile and type-check gates read it, and that is what separates a
generalisation that compiles from one that works.
