# 0070: a registry of keyed cells, compiled and walked

Status: proposed -- raised by the copyright holder 2026-11-18, with the
requirements below. Being built: the gaps 26.1075, 26.1077, 26.1079; the
declaration 26.1080; C 26.1081; walked 26.1084; written at runtime 26.1085
Date: 2026-11-18

## Context

The holder, 2026-11-18: "fuzznet has a schema language for some of its
datatypes and it bothers me that situ isn't a good fit for this", and then:
"Fuzznet would also need both a walker and the compiled versions. As it
would need to be written dynamically at runtime in some cases. As there
might be a schema editor in GUI."

What fuzznet hand-writes is its settings registry: `state/schema.h` (the
entry type and its parsers) and `node/schema.c` (its own table, 32 entries: 13 COUNT, 10 TEXT, 6 FLAG,
2 ADDRESS, 1 DURATION; 9 estate cells, 23 host cells), read at fuzznet
8b15ad8b. An entry declares a **cell** in a keyspace:

- a key, or a **family** -- a key ending in `/`, whose instances add one
  word of `a-z0-9` (`stream/grant/<hash>`);
- a value **type** spelled as text: FLAG (`on`/`off`, `1`/`0` read alike),
  COUNT (decimal, `min` to `max`), DURATION (a count and one of `d h m s`,
  bounded in seconds), ADDRESS (`HOST PORT`), TEXT (printable, bounded, with
  an optional grammar -- `archive/erasure` is `4+2`);
- a **default** as it would be written, a **unit** shown and never parsed, a
  **summary**;
- a **scope** (estate cell, host cell, or either);
- a **safe direction** (sec 981 there): of two values written apart, the
  lower or the higher stands -- an order on the value, used to merge;
- what a client shows (restart, sensitive, advanced, written by a verb) and
  what peers observe (advertised);
- a **window**: written no oftener than once in so many milliseconds.

Its header states the property situ exists for: "the parsers are the
readers' too". And fuzznet sec 1099 measured the cost of not having one
description: fuzzypickles' typed client re-modelled the schema and lost
what did not fit -- durations and addresses read as plain strings, three
flags dropped, `on`/`off` turned into `true`/`false`.

fuzznet's sec 1268 carries the holder's next step: storage that is
"schema-driven, and not purpose-built" (mail folders, places on a map),
"the schema language situ or a future extension of it". A registry is the
first half of that: declared shapes under keys.

**No non-goal is crossed.** situ owns no encoding (section 2): fuzznet's
cells already have one, text spellings in its own records, and situ would
describe it as it describes any format that exists.

## The decision proposed

**A cell's value is a message, and its type is a situ struct over the
cell's bytes.** `4+2` is `decimal u8 data until "+"; decimal u8
parity[remaining];` -- situ reads that today, through four backends and
both walkers that agree on it. So the value grammars are not a second
language: FLAG, COUNT, DURATION, ADDRESS and TEXT become structs in a std
schema (`std/cells.situ`), and a consumer's own grammar is a struct of its
own where fuzznet now writes a C function pointer.

**A registry maps keys to those structs and adds the policy a layout does
not carry**: family, default, unit, summary, scope, safe direction, the
display and advertised flags, the window. Policy words a consumer defines
(fuzznet's `scope`) are declared by the consumer, not built into situ.

**Two readers, as everywhere else in situ:**

- **Compiled.** `situc build` emits the registry as each backend's table
  plus one `check` and one `parse` per cell type, the same functions for
  the reader and the writer, and the safe-direction comparison. C first,
  in the shape fuzznet's `fzn_schema_entry_t` table has, so swapping it in
  is a mechanical change; then C++, Rust and Python, so fuzzypickles' client
  is generated rather than re-modelled.
- **Walked.** `situc pack` puts the registry into the image as a section of
  ROWS, and both walkers answer the same questions from it: which entry a
  key is, whether a value is one, what it parses to, which of two is safer.
  The differential holds the walker to the compiled answer over the same
  draws the four-way uses.

**Written at runtime.** The rows are declarative: a key, a reference to a
cell struct, the policy fields. Nothing in a row is a solved offset, so a
program can write one without the compiler -- through the generated writer
accessors of `std/image.situ`, which is itself a situ schema (26.33), or a
small builder in the C walker runtime. A GUI schema editor composes rows
over the std cell types and the consumer's packed ones, and the walker
reads them as it reads a packed image. The compiled half is for the
registry a program ships with; the walked half is for the one a person
edits, and both answer alike because both are held to the same tests.

**What a runtime editor cannot do in the first cut: invent a new value
grammar.** A new cell struct needs the layout solver, which is `situc`.
The rows can reference any cell struct already packed, and the std set
covers fuzznet's five types. Whether a solver for text-only structs (no
static offsets to solve; delimiters and `remaining` only) belongs in the C
runtime is a question for when an editor asks for one.

## What situ cannot say yet, measured

fuzznet's five types written as structs over a cell's bytes, through `situc
build --target c` at 2b8d0cc4:

- **COUNT and `4+2` compile as they stand**: `decimal u64 value[remaining]
  [minimal]`, and `decimal u8 data until "+" max 3; decimal u8
  parity[remaining];`.
- **A token set cannot end where the cell does.** `flag_word word[remaining]`
  is refused: a token set takes its extent from a delimiter, and a cell has
  none -- its end is the end of the bytes. FLAG, `log/level` and every other
  vocabulary cell needs this.
- **Nothing names which delimiter ended a member.** DURATION compiles as
  `decimal u64 count until 'd' | 'h' | 'm' | 's'`, and no accessor says
  which of the four it was -- which is the unit, so the value cannot be
  computed.
- **The expression language has no conditional and refuses a length that
  may be negative**, so "all but the last byte" (`remaining - 1`) is not
  writable either.
- **A bound on a derived value** -- DURATION's `min` to `max` SECONDS, the
  count times the unit -- has no spelling: `[min, max]` is per member.
- **A character class** (ADDRESS's host: letters, digits and `.-:`) has
  none either; `[encoding = ascii]` is wider.

Each of these is a language gap a byte format can meet too (a token ending a
datagram, a unit suffix in a text protocol), so each is built as a situ
feature in its own right, before the registry needs it.

## The syntax

As built in 26.1080 (a cell's value is its type's `let value`, or its one
member):

```situ
import "std/cells.situ";

policy scope   { estate, host }
policy display { restart, sensitive, advanced, verb, advertised }

registry node_settings {
	"revocation/k"    : count [min = 1, max = 255, scope = estate,
	                           safe = higher, unit = "root signatures"]
	                    "the estate's k: root signatures a revocation needs";
	"archive/gone"    : duration [min = 1, max = 315360000, default = "14d",
	                              scope = estate, display = advanced];
	"stream/grant/"   : count [max = 3, scope = estate, display = verb];
	"archive/erasure" : erasure [default = "4+2", scope = estate,
	                             display = advanced];
}
```

`policy` declares the words a consumer's registry uses, so situ checks them
without owning their meaning; `safe` and `window` are situ's, because the
generated code acts on them (the comparison, the rate).

## Steps

1. This record, and the gaps writing fuzznet's types as structs found --
   26.1075, 26.1077, 26.1079; a character class is still open. Nothing is
   written in fuzznet's tree.
2. Parse and check it, dump and round-trip it (26.1080). The map and
   `situc explain` describing it are still to do.
3. C: the table, `find`, `check`, `value`, `safer` (26.1081). The
   four-way holds it once the other three backends emit it (step 6).
4. The image section and both walkers (26.1084), after the walkers
   learned a token test (26.1082) and a 64-bit ceiling (26.1083).
5. The writer path: rows written without `situc`, read back by both
   walkers (26.1085).
6. Python (26.1087), C++, Rust.

## Open, and whose

- The syntax -- this tree's, settled in step 1 against the table.
- Whether fuzznet adopts it, and when -- fuzznet's, told when step 3 lands.
- Storage beyond a registry (records of a declared shape kept under keys,
  sec 1268 there) -- the holder's, after this.
