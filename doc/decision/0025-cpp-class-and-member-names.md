# 0025: a C++ class a member has named is renamed, not refused

Status: accepted
Date: 2026-07-31
Phase: after 26.32

## Context

C++ declares a class's own name inside the class, so no member may take it.
Two shapes in this repository reach that rule.

The first is a schema name landing on a method every view gets:

```situ
struct framed {
    u8  magic[4];
    u8  version[] until "\r\n" max 16;
    u16 count;
}
```

Every generated class has a `framed()` -- "is what I already hold a complete
message?" -- so `class framed` declares `err framed(uint32_t &)` and g++ reads
it as a constructor with a return type. `test/schema/edges.situ` has held
this struct since delimited members landed, and the header it generated has
not compiled since. Nothing noticed for weeks, because `test_every_example_
compiles` globbed `example/` and `edges.situ` lives in `test/schema/` --
the directory that exists to carry the constructs the worked examples do not
have (26.27). The schema written to catch awkward cases was the one the
compile check skipped.

The second needs no unlucky name at all:

```situ
struct option { u8 option; u8 length; }
```

which is `class option` with an accessor `option()`. A TLV-shaped protocol
produces that without trying.

Neither is a problem in the other three backends. C flattens the whole path
into `situ_option_option`; Rust's `impl option { fn option }` and Python's
`class option: def option` are both fine. This is the only place in the
project where a schema three backends accept is rejected by the fourth's
*compiler* rather than by the compiler this project wrote.

## Decision

**The class moves, and the schema's name becomes an alias for it.** Where a
member would take the class's name, the class is emitted as `option_` and
`using option = option_;` follows it at namespace scope. Every accessor keeps
the name the schema gave it, every other class goes on naming this one the way
the schema does, and the rename is visible only in a debugger.

Inside the class body the impl name is used, because the alias is not
available there: it is declared after the class, and a member of that name
hides it in any case. Five sites need it -- the class head, the register class
head, the `at` factory, the gate's `friend` declaration and the temporary
`required` builds -- and they are the only places the emitter names its own
class.

**Which classes move is decided from the schema, not from the emitted text.**
Section 25 says a pass reads the AST and never its own output, and reading the
class body back would be the only *complete* way to ask this question. So
`situc/codegen/cpp/names.py` over-approximates instead: a class is renamed if
its name is a structural one (`at`, `framed`, `required`, `validate`,
`extent`, `word`, `read`, `write`, ...) or a member's name under any affix the
emitter uses (`set_`, `_len`, `_span`, `_offset`, `_gate`, ...). A false
positive costs one alias nobody reads; a false negative costs a header that
does not compile, so the lists lean long. `test_the_affixes_match_the_emitter`
derives them from `cpp/emit.py` and fails when an accessor shape arrives that
they do not know.

**One underscore, and the case where that is taken is a diagnostic.** A schema
holding both `framed` and `framed_` would have the alias for the first and the
class for the second reach one name. That is a coincidence rather than a
construct, so the backend says so, names both spans, and stops.

## Alternatives considered

**Refuse the schema.** The decision 0013 shape: a collision is the compiler's
to diagnose. Rejected, because the collision here is not between two things
the author wrote -- it is between what the author wrote and what this backend
generates. It would make `framed`, `validate`, `extent` and `at` reserved
words in one backend of four, and outlaw `struct option { u8 option; }` for a
reason that has nothing to do with its bytes. A backend that refuses what
three accept is the disagreement 26.31 spends its whole page on.

**Rename the colliding method instead.** `option_()` for the accessor,
`framed_()` for the framing method. Rejected on both cost and reach: it moves
the name a caller writes constantly rather than one that appears in a
debugger, it splits a member's accessors (`option_()` beside `set_option()`),
and it has to be applied at roughly fifty emission sites plus every
cross-class call of a structural method -- against five for the class name.

**Prefix or suffix every generated class unconditionally.** Sound by
construction and needs no analysis at all. Rejected: every header in the
project would grow a second name for every type, to fix a shape that is rare.

**Read the emitted class body back and rename if a member took the name.**
The only complete rule, and the one that cannot go stale. Rejected on section
25: no pass in this compiler re-reads its own output, and making this the
exception buys completeness for a rule whose incompleteness a test can hold.

## Consequences

`test/schema/edges.situ` compiles in C++ for the first time, and the compile
checks in all four backends now read every schema in the repository rather
than `example/` alone -- one list, in `test/unit/every_schema.py`, which is
also what the two agreement checks of 26.32 already read.
