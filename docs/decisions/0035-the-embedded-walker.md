# 0035: the walker that matters is embedded, and it is C

Status: accepted, and largely built. `walker/c/` reads an image, evaluates a
section 10 program, places a fixed or located member, decodes a varint, scans
for a delimiter, parses a text number, walks a counted or `while` run,
measures a variant and answers `validate` -- held to the Python walker by a
differential test. Regions and the remaining probes are what is left; the
table below is the work and each row this build does not render is refused
by name rather than guessed.
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
| `while` runs | however many elements pass the predicate | **done** |
| variants | the extent of the arm the discriminant selects | **done** |
| regions | a gate's interior, read through `authenticated` or `sealed` | `report.py` |
| `validate` | constraints, enums, nested structs, the span checks | **done** |
| the rest of the probes | tags, markers, versioned members, gated regions | `report.py` |

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


## Amendment, 2026-08-10: the `while` run, and the depth a device pays for

The loop lands, and 0026's bound argument needs extending to cover it. The
VM's totality is what makes an *expression* safe to ship to a device; a run
walk is not an expression, so its termination is argued separately and in
the source:

  - `count` reaches the cap, which is the schema's `max` or 0xFFFF -- a
    ceiling that does not depend on the bytes;
  - `at` reaches the frame, and every iteration advances by at least one
    byte because a zero extent breaks, so the message length alone bounds
    the loop;
  - an element does not fit or measures zero;
  - the predicate goes false, which is the construct's own reason to stop.

Two of the four are independent of the message, which is the property worth
having: an adversary choosing every byte cannot make the loop run longer
than the frame.

**Recursion is the part that needed a decision rather than a guard.** A
`while` run's extent is the sum of its elements' extents, and an element is a
struct that may hold another such run -- mutual recursion through
`size_bits`, the walk and `struct_extent`, bounded in principle by the
schema's nesting and in practice by nothing, the schema arriving in an image
at run time. On a device the stack is the arena's neighbour. So the depth is
a constant here, `WALK_DEPTH_MAX` of eight against a corpus whose deepest
nesting is three, and it is threaded through *every* path that descends --
including the expression evaluator's load callback, which reads a field,
which may sum members, one of which may be a run. A bound with a public
entry point that restarts the count is not a bound.

**Reaching that bound is a refusal and not a short answer.** The first
version absorbed it into "the run ends here", which is the shape of mistake
this component exists not to make: an extent that stops early reads exactly
like one that is right. `SITU_WALK_UNSUPPORTED` propagates out of the walk;
a frame that runs out still counts the elements that fit.


## Amendment, 2026-08-10: variants, and a fifth instance of the pattern

A variant's extent is a switch: read the discriminant, take the arm it names,
answer that arm's size. Not the minimum -- reading the minimum instead made a
dnsname label one byte long and walked thirty-nine of them through a
thirty-eight byte buffer -- and not the worst case. An unrecognised
discriminant is nought bytes rather than a refusal, matching the generated
C's own `: 0u`: a discriminant naming no arm is a malformed message and
saying so is `validate`'s job, not the extent's.

The arms table is one row per arm, so a variant owns several contiguous rows.
The search lands on one of them and walks back to the first, which is the
first table here that is not one row per placement.

**The differential found the pattern's fifth instance, and Python had it.**
Both walkers agreed about every extent immediately -- two bytes for the small
arm, eight for the large, nought for a discriminant matching nothing, with
`tail` correctly placed in each -- and disagreed about the *value*:
`read_scalar` read the selected arm's bytes as an integer, so a two-byte arm
came back as 43707 and an eight-byte one as 4822678189205111.

That is the byte run again. A variant is a shape the discriminant chooses,
not a number; the arm is what holds a value and has its own placement to be
read through. Refused now, which makes the tally five constructs and three
answers of "this has no single value" against two of "it has one, decode it".

**The test schema omits `[equalize]` deliberately.** Padding every arm to the
largest is what buys back the offset axis for everything after a variant --
and it would also hide a walk that picked the wrong arm, since every answer
would then be right by construction. The arms here are two bytes and eight,
so a walker taking either the smallest or the largest is caught.


## Amendment, 2026-08-10: `validate`, whole or nothing

This is the answer a device wanted and could not get. The walker could say
where every member of a message is and what it holds, and not whether the
message is a legal instance of the schema -- so anything asking that still
needed Python, which is the thing 0026 exists to remove.

**Whole or nothing is what had to be built rather than ported.** Every other
probe renders per member and skips what it cannot do, because each is a
separate line. `validate` is one verdict about a whole struct, so a partial
one reports OK for the rules it happened to be given -- and that is the one
wrong answer indistinguishable from a right one. Two things follow, and
both are refusals of the *struct*:

  - the image carries a bit per struct saying it holds every check, and a
    walker that ignored it would answer for rules it was never given;
  - a kind of check this build does not render refuses the struct rather
    than skipping the member.

**The return value and the verdict are different questions**, and the API
keeps them apart: the verdict is about the message, the return is about the
walker. Folding them together is how "this build cannot say" becomes
"well-formed".

Two things the differential settled on the way. A **nested struct** is
`validate` called through, with the inner verdict returned as it stands
rather than folded into CONSTRAINT -- the first version refused nested
structs wholesale, which would have made most real schemas unanswerable. And
the **frame check belongs at the top**: section 20.2's check, the one every
constant-offset access below it depends on, which the Python walk does when
it acquires a view. Without it the two agreed about every well-formed
message and disagreed about short ones, C answering for the members that
happened to fit.

Sixteen cases across four shapes -- constraints, enum membership, a nested
struct's own rules, a text number's range -- each with a well-formed
message, a rule broken and a frame too short. `[since]` members, the span
checks and gated regions are refused by name and are what remains.


## Amendment, 2026-08-10: the span checks, and `remaining` from the wrong end

The checks that read a member's *bytes* rather than its value: the delimiter
is there, a nul terminator is somewhere in the run, a reserved run is all
zero, and the bytes are the encoding the schema declared. They share a code
path because they share a question -- which bytes did the schema call text --
and a delimited member's answer is its content, not its span: the delimiter
is where the next member starts and is not part of what was named.

**UTF-8 is a second implementation of that state machine in this repository,
and it is deliberate.** `runtime/c/situ.h` has the first, and generated code
links it; the walker links nothing, which is the whole of what makes it
droppable into an arena. Depending on the runtime would be the walker
depending on the thing it exists to be independent of. It is held to the
first by `edges.situ`'s own cases and by the differential -- a bad
continuation byte and a surrogate half are refused by both, which are the two
that look like text rather than like corruption.

**And the differential found `remaining` measured from the wrong end.** The C
walker evaluated a size program with `remaining` as the frame's whole length
rather than what is left of it from the member's own offset, so every
`[remaining]` run came out as long as the buffer. `walk.py` carries a comment
about this exact bug -- "sqlite and ipv6ext both caught: 44 against C's 37,
and 38 against 46" -- and the C walker made it again from a clean start,
which is what a second implementation is for. It was invisible until a schema
with a `[remaining]` tail met the validator, because nothing else asks a size
program for a length it can be wrong about by exactly the offset.
