# 0015: what a register adds to a struct, and what it does not

Status: accepted
Date: 2026-07-27
Phase: 10

## Context

Section 15 describes the MMIO target as "the other half of the thesis": one
capability lattice answering both wire protocols and hardware registers. It
specifies the vocabulary (SystemRDL's access modes, side-effect declarations,
scoped defaults) and the three interactions of 15.3, and leaves the shape of the
implementation open.

Three questions came up that the section does not answer.

## Decision

### A register is a struct

`register` lowers to a `StructDecl` carrying a `RegisterInfo`, at parse time.
The layout solver places it, the propagation table costs it, the capability map
renders it and `situc explain` explains it, all unchanged. Only the C backend
branches, because only the C backend is emitting something different.

That is the unification section 15 claims, made structural rather than
asserted: if a register needed its own solver, the claim that one lattice
answers both would be a slogan.

### A field getter takes a word, not the register

```c
uint32_t word = situ_ctrl_read(block);
if (situ_ctrl_busy_get(word)) { ... }
```

rather than `situ_ctrl_busy_get(block)`. A read is an event: `on_read = pop`
returns something different each time, so an API that read once per field would
drain a FIFO to decode a status word. Read once, decode many.

This falls out of the `effect` axis rather than being imposed on it, and it
makes the cheap thing and the correct thing the same thing. The write side is
symmetric: `_with()` composes a word purely, and one `_write()` issues the
transaction, which is what 15.3 means by "only `write(builder)`".

### The byte is not the unit inside a register

The alignment rule of 8.4 and the straddle rule of 8.2 are about buffers, where
a field crossing a byte boundary silently costs a multi-byte read-modify-write.
A register is one access of `access_width` bits, so every field in it is a bit
range within a single word: starting mid-byte costs nothing and crossing a byte
boundary costs nothing. Both checks are skipped under a register.

The consequence in the other direction is a new row: a field narrower than the
access width is `ValueConverted` and `NonAtomic` whatever its declared width,
because it is shifted and masked out of a bus word. A `u8` at bit 3 of a 32-bit
register is exactly as converted as a `u3` is, which the buffer rules would not
have said.

### An access mode is a construct property, not a fourteenth axis

`wo` generating no getter and `ro` generating no setter are facts about which
operations exist. The lattice's job is to cost the operations that do exist:
`ro` reaches it as `mutate = Immutable`, `no_rmw` plus a partial field as
`mutate = RewriteRequired`, `on_read`/`on_write` as the `effect` axis. What is
left over -- readability, and whether a write means "store this value" -- rides
on the placement and is read by the backend.

A `readable` axis would be a boolean wearing a lattice's clothes: two values, no
interesting meet, and every propagation row would have to carry it. And
`is_assignment` is not an ordering at all -- a `w1c` write is not a weaker store,
it is a different operation, which is why the generated function is
`clear_error()` and not `set_error(false)`.

## Alternatives considered

**A separate register solver.** Registers have no dynamic sizes, no frames and
no variable-length anything, so a purpose-built pass would be simpler in
isolation. Rejected: it would make section 15's central claim untrue, and every
capability rule would then exist twice and drift.

**Getters that read the register.** Convenient, familiar from vendor headers,
and wrong for exactly the registers that need this language most -- the ones
with side effects on read. Rejected on the grounds that an API should not make
the dangerous thing the default spelling.

**Model `w1c` as a `mutate` value.** Tempting, since it is about writing.
Rejected: `mutate` orders operations by cost, and "the write means something
else" is not a point on that order. It would also have been unreachable in
practice -- under `no_rmw` the field is already `RewriteRequired`, which is
weaker, so a row claiming `InPlaceFixed` could never apply.

## Consequence

`FUTURE_CONSTRUCTS` is now empty: every construct project.md names is accepted.
The phase-gating machinery is still exercised by the nested-namespace
diagnostic, and the `// STATUS: needs phase N.` convention for examples is still
enforced for any example carrying the marker -- but no example carries one, and
the next construct to be gated will be the first user of both again.
