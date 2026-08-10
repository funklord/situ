# 0035: the walker that matters is embedded, and it is C

Status: accepted, and begun. `walker/c/` reads an image, evaluates a section
10 program, places a fixed or located member, decodes a varint, scans for a
delimiter and parses a text number, held to the Python walker by a
differential test. Runs, variants, regions and the probes are not written;
the table below is the remaining work and each row this build does not render
is refused by name rather than guessed.
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
| located members | `at expr`, which joins no offset chain | **done** |
| delimiter scan | where a delimited member stops, with quoting and escapes | **done** |
| varint decode | a width that is in its own bytes, and the value | **done** |
| text numbers | digits rather than bits, both forms and both radices | **done** |
| run walking | counted and message-sized runs | **done** |
| `while` runs | however many elements pass the predicate | `walk.py` |
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


## Amendment, 2026-08-10: one disagreement the differential found, settled

The two walkers do not agree about a byte *run*. Python's `read_scalar` will
answer one -- `u8 name[n]` over "hello" comes back as 448378203247, the five
bytes as an integer -- and the C walker refuses it, because a member whose
width the data decides has no single value to give.

**Settled the same day: `read_scalar` refuses a run.** A run is not a scalar
however few bytes it happens to hold, and the old answer came about because
the width fitted and nothing asked whether the result meant anything.
`read_bytes` is the reader for those and every probe that wants one already
called it -- which is why the whole suite passed unchanged when the refusal
was added. Nothing depended on the behaviour; it was accidental rather than
relied upon, and a suite that does not move is the evidence for saying so.

The differential is correspondingly wider: it now covers a schema whose
member lengths the data decides, and the two walkers agree on the run's
refusal *and* on placing the member after it. That is the offset chain
holding in two independent implementations, which is the claim the fifth
column makes about the four backends.


## Amendment, 2026-08-10: the same shape again, in varints

Adding varints to the C walker surfaced a second divergence of exactly the
family the first one was.

Both walkers agree on a varint's *width* -- `11 96 01 22` places `after` at
offset 3 in each, so Python decodes the length correctly. They disagree on
its *value*: the C walker answers 150, which is what `96 01` means in
leb128, and Python's `read_scalar` answers 38401, which is those two bytes
read as a big-endian integer.

This is the byte-run question again with the opposite answer. A run is not a
scalar and `read_scalar` was made to refuse it; a varint *is* a scalar -- it
has a value, and every compiled backend's getter decodes one -- so refusing
would be wrong and reading the bytes raw already is. **`read_scalar` should
decode a varint**, and until it does the two walkers disagree about every
schema holding one.

Recorded rather than fixed in the same commit because the fix belongs to the
Python walker and wants the full suite run behind it, as the byte-run change
did -- that change passed 3431 tests untouched, which was the evidence that
its old behaviour was accidental, and this one deserves the same test.

The pattern is worth naming now it has happened twice: `read_scalar` reads
*bits* where the walk's public answer should read *values*. Delimited
members and text numbers are the remaining constructs where the distinction
bites, and whoever ports them should expect a third instance.


## Amendment, 2026-08-10: the amendment above was wrong about which walker

**Neither walker decoded the varint.** The entry above has the C side down as
answering correctly and Python as the one to fix, and that reading came from a
single pair of bytes on which the two readings coincide. `96 01` is 150 in
leb128, and `0x96` is 150 -- so a walker decoding two bytes and a walker
reading the first one produce the same number, and the difference between
them is invisible. `situ_walk_read` had no varint case at all: it took
`size_bits` from the record, which for a varint is the one-byte lower bound.

Measured on `ac 02`, where no two readings agree: leb128 says 300, the two
bytes read big-endian are 44034, and the first byte alone is 172. Python
answered 44034 and C answered 172. Both are fixed here, and the test uses
`ac 02` for that reason -- a differential over a value two readings can
produce is a differential that passes without comparing anything, which is
what the first write-up was reading.

**A third divergence, found by the test asserting the truncated case.** They
disagreed about a varint whose last byte never arrived: Python gives it a
width of zero and places the member after it, C refused the width and so
dropped everything downstream. The compiled backends settle it -- the
generated `_len` returns zero and only `_get` refuses, deliberately, because
"the length arithmetic downstream of this field is not fallible". The C
walker follows them now. Two readers again, and it is the same split the
first amendment describes rather than a new one: a width and a value are
different questions and a truncated encoding answers them differently.

What this costs the earlier claim, and it is the point worth keeping: the
divergence had been *recorded* rather than fixed, and recording it is what
made it look settled. A written-up disagreement with one side named correct
is a conclusion, and this one was drawn from a coincidence that a second
input would have destroyed.


## Amendment, 2026-08-10: the delimiter scan, and the third instance

The prediction above was right about where it bit next and wrong about who
had it. **Both walkers answered a delimited member as a number**, in the two
different ways their implementations made available: Python read the whole
span, so `"GET "` came back as 1195725856, and the C walker had no scan at
all, so it took the record's `size_bits` -- which for a delimited member is
its *delimiter's* width, the one number invariant 25 names as not being the
answer -- and gave 0x47. The C one then placed the `u16` after it at offset
1, where it belongs at 4.

Settled the same way the byte run was: **a delimited member has no scalar
value**, because its end is the data's to decide, and `read_bytes` is its
reader. That is two of the three constructs in the pattern settled by
refusing and one by decoding, and the question each time is whether the
construct has a value rather than whether this layer can produce one.

The scan itself is now in C: quoting, escapes, the `max` cap, and the
truncated case where the delimiter never arrives and the member reaches as
far as it got. The differential compares *widths* as well as values for it,
because a walker can agree about every value in a struct and still disagree
about the struct -- and the last member's extent is not observable through
the offset of anything after it.

**The owned decode needed the other half of the same answer.** `read_scalar`
refusing would have failed the whole message under the whole-or-nothing
rule, for every schema with a delimited member -- so `owned.decode` routes
one to its content instead, which is what a backend's `_ptr` and `_len` hand
back. Content and not span: the span carries the delimiter because that is
what places the member after it, and the two numbers differ by exactly the
delimiter. `[trim]` has one derivation feeding both readers now, rather than
a length for the probe and a second copy for the value.


## Amendment, 2026-08-10: text numbers, and the pattern closed

The last of the four constructs the first amendment's pattern named. Both
forms parse in the C walker now -- fixed-width and delimited, both radices --
and the pattern's four instances are settled: a byte run and a delimited
member have no value and are refused, a varint and a text number have one and
are decoded.

**The refusal it replaces was true for a false reason.** `decimal u32 n[4]`
is one number in four digits, so the run refusal declined it on the grounds
that it looked like four numbers -- the answer a caller wanted, from a
premise about the construct that is wrong. Invariant 18 is the rule: a real
gap must be attributed to whatever actually causes it, and this one was
attributed to a count that is not a count.

**Writing it found a width bug the values could not show.** `size_code` is
set on a fixed-width text number, so the sized-run branch read `[4]` as four
32-bit elements and answered sixteen bytes for a four-byte member -- the same
arithmetic that once put `edges`' `text_driver` tail twelve bytes past every
backend, in the Python walker. It was invisible through a value differential
because the layout solver hands a member *after* a fixed-width text number a
constant offset, so the struct was measured four times too long and every
member in it still read correctly. The width differential of the previous
amendment is what caught it, which is the argument for having added it.

**One divergence remains, stated rather than left to be found.** The C parse
accumulates into `uint64_t` and refuses on overflow; Python's has arbitrary
precision and does not. No schema here writes a text number long enough to
reach it -- cpio's widest is eight digits -- so the two agree everywhere the
tree can ask, and this says where they would stop.


## Amendment, 2026-08-10: runs, and the flag a caller could not ask for

`situ_walk_count` and `situ_walk_element` are what a run needs, and their
absence was a real gap rather than a missing convenience: **a device could be
told a run is fourteen bytes long and had no way to read any of them.**
`situ_walk_read` refuses a run because a run has no single value, which says
nothing about the values it does have, and until now nothing else answered.
`situ_walk_bytes` is the same for a byte run and a delimited member, handing
back a pointer into the caller's buffer because an embedded walker in a fixed
arena has nowhere to copy to.

The element read is the scalar read at a different offset, extracted into one
function rather than written twice -- which is what `walk.py` says about its
own split, having watched a backend and its own run accessor disagree about
exactly that.

**Writing the differential found an API gap the walker could not see from
inside.** A value comes back sign-extended through a `uint64_t`, so `-2` and
18446744073709551614 are the same answer and the *caller* decides which it is
looking at -- with no way to ask. The harness printed a signed element
unsigned and reported a disagreement that was not there. `SITU_WALK_SIGNED`
is in the header now. The lesson is the shape of the finding: a walker that
hands back a number and withholds how to read it has an incomplete API, and
the first consumer outside the module is what shows it.

`while` runs stay refused and are their own row. Their extent is a loop --
bounded by `repeat_cap`, by the frame, and by refusing to advance on a
zero-length element -- and a loop in an arena is worth its own commit.
