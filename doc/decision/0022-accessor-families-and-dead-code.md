# 0022: accessor families, and what actually purges the unused ones

Status: accepted
Date: 2026-07-30
Phase: after 26.27; decides the shape of work not yet done

## Context

Any consumer of a protocol uses a small fraction of the accessors generated for
it. A schema of fifty fields produces on the order of 150 functions and a
reader touches three. Adding a second *accessor family* -- a materialized parse
beside the zero-copy views -- multiplies that rather than adding to it.

Before generating twice as much, it is worth knowing what actually removes the
unused half. The assumption was "C optimises out uncalled functions". That is
true of some of them.

## Measured

One schema (`example/message`), linked into a program that calls nothing:

| | unused accessor survives? |
|---|---|
| C, `static inline` in the header | no -- never emitted. 40 of 44 functions here |
| C, external in the `.c` | **yes**. All four `validate` symbols were in the binary; `-ffunction-sections -fdata-sections -Wl,--gc-sections` removed them, 16296 -> 15648 bytes |
| C++ | same split; nearly everything situ emits is header-inline |
| Rust, in a binary crate | no -- zero symbols survived |
| Rust, in a **lib** crate | **yes**, 33 symbols. `pub` is the public API; dead-code elimination may not touch it |
| Python | **yes, always.** Every method and property exists at import. There is no flag |

Across four examples the ratio is stable: 31--40 `static inline` against 1--4
external, the external ones being `validate` (large, and out-of-line for that
reason).

## Decisions

**1. The purge is per-language, and one of them cannot purge at all.**

- C and C++ are nearly free already, because nearly everything is header-inline.
  What is left is `validate`, and the answer there is documentation:
  `-ffunction-sections -fdata-sections -Wl,--gc-sections`, or LTO. situ has
  never said so and must.
- Rust is free in a binary and not in a library. A generated module consumed as
  `mod unit;` inside a user's crate is eliminated; one published as a crate is
  not. Feature gates are the mechanism there, not hope.
- **Python cannot.** So for Python the family split has to be separate modules
  -- a consumer imports the one they want -- because nothing else has any
  effect at all.

Generating twice as much and trusting the toolchain is therefore not a strategy
that works in four languages. It works in two and a half.

**2. Which families to emit is decided by the lattice, not by a flag.**

The observation that made this tractable: *both families are not useful for
every construct*. Materializing a field that is already `Random`,
`InPlaceFixed` and `MemoryIdentical` buys nothing -- the zero-copy accessor is
already a pointer at the bytes, and a copy is pure cost. Materializing is worth
its RAM exactly where the zero-copy vector is weak:

| zero-copy vector | why materializing helps |
|---|---|
| `access = Sequential` | a walk becomes an index |
| `mutate = Shifting` | editing stops moving everything after it |
| `mutate = RewriteRequired` | the region is decoded once instead of per write |
| `repr = TextConverted` | the parse happens once |
| `stage = TransformTime` | the codec runs once |

So the generator does not emit two of everything. It emits the second family
for the members whose vector says the first one is expensive, and that decision
is derived from the map rather than declared. This is the same rule that
already suppresses a setter where `mutate` forbids one.

**3. The consumer chooses the family, not the schema.**

An embedded receiver and a desktop inspector read the same wire format and want
opposite trade-offs. A schema that picks has put a deployment decision into the
file that defines the byte contract, which is exactly the separation section
19.3 exists to maintain: wire compatibility, API compatibility and cost are
three different questions. So the family is a codegen flag.

**4. Streaming is not a third family.**

`access = Sequential` already means "element N is reached by reading the N-1
before it", which is a streaming parse and cannot be improved on. A `Random`
struct gains nothing from one: re-entrant state, to reach bytes that are
already indexable. Whether a struct can be parsed incrementally is therefore a
question the map already answers, and `situc map` already prints it -- HTTP has
twenty `Sequential` fields, TCP has none.

What *is* missing, and is not a family, is the **framing** question: given a
partial buffer, is there a whole message here, and if not how many more bytes?
Every network consumer writes that by hand and gets the truncated-length case
wrong. situ can answer it exactly -- it knows `SIZE_MIN`, which fields carry
lengths, and where they sit. One generated function per parseable struct,
independent of any family.

## What this costs if we are wrong

The lattice-driven rule (2) is the load-bearing one. If it turns out consumers
want materialized accessors for fields whose vector is already strong -- for
uniformity of API, say, rather than for cost -- then the rule prunes something
people wanted and the flag has to grow a "materialize everything" mode. That is
a cheap thing to add later and an expensive thing to undo, which is the reason
for starting narrow.
