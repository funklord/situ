# 0035: the walker that matters is embedded, and it is C

Status: accepted, and begun. `walker/c/` reads an image, evaluates a section
10 program and places a fixed member, held to the Python walker by a
differential test. Delimited members, varints, runs, variants, regions and
the probes are not written; the table below is the remaining work and each
row this build does not render is refused by name rather than guessed.
Date: 2026-08-10
Phase: unscheduled

## Context

Decision 0026 was argued from a device: "a radio whose framing must change
without a firmware rebuild. Ship a description of the new format, load it,
parse." That is the walker's reason for existing. 26.33 said the same thing
from the format's side -- "an embedded walker in a fixed arena wants a small
byte-addressable table" -- and split the image into a core and a `--metadata`
tail so the arena would not have to carry names it never prints.

**What was built instead is a Python walker**, and it has been useful for a
reason that is not the stated one: it is the fifth column of the differential
check, a table walk answering the same questions as four compiled backends
over the same hostile bytes. That is a real role and it has found real
disagreements. It is not the product.

This record exists because the distinction had gone quiet. Several phases
say "and the walker" meaning the Python one, and a reader would reasonably
conclude the embedded case was served. It is not served at all: **no C or
C++ program in this repository can read a packed image over live bytes.**
`build/host/tests/gen/image.c` is the generated accessors for the image
*format*, which read the image's own records and walk no message.

## Decision

**The embedded walker is C, and it is the walker `situ pack` exists for.**
The Python one keeps its job as the fifth column and gains no other; it is
not a prototype to be ported, and nothing should be added to it on the
grounds that the C one will need it.

**It is a separate component**, not a subcommand and not linked into
`situc`. That is 0026's boundary and its reasoning is unchanged: what keeps
"an offset is a constant" and "an operation is absent, not refused" true of
generated code is that the compiler never learns an interpreter exists.

**It reads the image through generated accessors**, as `std/image.situ`
already provides. 0026 chose that deliberately -- "the only artifact in the
project nothing checks, in the component whose input is least trusted" was
the alternative -- and it means the C walker starts with a parser it does not
have to write or trust.

## What it actually takes

Named so the size is not discovered later. The Python walk is not a thin
layer over the image; placing one field needs most of this:

| | what it does | state |
|---|---|---|
| image and directory | header, sections, structs, placements, all bounds-checked | **done** |
| the expression VM | section 10's bytecode, about twenty opcodes | **done** |
| fixed placement | a member at a constant offset and width, either byte order, sign-extended | **done** |
| offset plan | where a member starts, summed through the members before it | **done** |
| sized runs | a `size_code` program, evaluated for the element count | **done** |
| located members | `at expr`, which joins no offset chain | `walk.py` |
| delimiter scan | where a delimited member stops, with quoting and escapes | `walk.py` |
| varint decode | a width that is in its own bytes | `walk.py` |
| run walking | counted, capped and `while` runs | `walk.py` |
| the probes | `validate`, tags, markers, gated regions | `report.py` |

**The bound is the argument.** 0026's case for shipping an evaluator to a
device is that section 10's language is total -- no calls, no recursion, no
iteration -- so a program's length is its own bound and the VM needs no step
limit. That property is what makes a C walker safe in an arena, and it is
the thing to preserve rather than rediscover.

**No allocation.** Rung 1's invariant applies with more force here than
anywhere: an embedded walker in a fixed arena is the caller 0031's caller
buffers were described for.

## Consequences

- 0026 gains an amendment: it says the interpreter is a separate binary *in
  this repository*, which is true of the Python one and needs to say which
  walker is which.
- Every phase that says "and the walker" means the Python fifth column. That
  is now written down here rather than inferred.
- **0034's GUI depends on this.** A C++ window with no Python needs a walker
  it can call, and driving `situ-edit` in a process was the alternative --
  rejected, because the point of a walker is a program that reads situ
  binary without one.
- The Python walker is frozen in scope: fifth column, and nothing else.


## Amendment, 2026-08-10: one disagreement the differential found

The two walkers do not agree about a byte *run*. Python's `read_scalar` will
answer one -- `u8 name[n]` over "hello" comes back as 448378203247, the five
bytes as an integer -- and the C walker refuses it, because a member whose
width the data decides has no single value to give.

Neither is obviously wrong and the difference is real, so it is recorded
rather than smoothed over. `read_scalar` in Python is a low-level primitive
that reads `size_bits` and does not ask whether the result means anything;
`situ_walk_read` is the walker's public answer and declines what it cannot
call a value. **Which of those `read_scalar` should be is the open question**,
and it is worth settling before more of the walk is ported, because every
later probe rests on it.

Until it is settled the differential test compares schemas whose members are
scalars, which is what `examples/udp` is. That is a narrower claim than the
one the fifth column makes about the four backends, and saying so is the
point of this paragraph.
