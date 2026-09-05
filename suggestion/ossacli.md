# situ, from a project whose offsets are 82% unpinned

Written 2026-09-05 from `ossacli`, an open-source replacement for HPE's
`ssacli` for Smart Array RAID controllers: C11, reading BMIC/CISS
structures and SES-2 pages over Linux SG_IO. situ was run, not read
about.

The verdict is **yes, for a bounded part** -- the wire structures and the
SES element descriptors -- and the argument is not the one I expected to
be making when I started.

## What is there to describe

`src/lib/bmic.h` is 418 lines declaring ten packed wire structs with 245
fields between them, 209 of which carry a byte-offset comment. Those
offsets are load-bearing: a wrong one silently reads a neighbouring
field, and this tree has already shipped a temperature FAIL bit read out
of a reserved byte.

They are pinned by 45 hand-written `_Static_assert(offsetof(...))` in
`test/test_ossa.c`. That is **18% of the fields**. The other 82% are held
by declaration order, `__attribute__((packed))`, and a comment.

## It agrees with the hand decode

The SES-2 cooling element status descriptor -- four bytes, bit-packed,
big-endian, and where this tree's most recent decode bug lived. `situc
map` placed every field at the byte:bit offsets `src/lib/ses.c`
hardcodes, and the generated accessors agree with the hand decode on
every descriptor I gave both:

    5000 rpm, code 0    ossacli rpm=5000  code=0 fail=0 | situ rpm=5000  code=0 fail=0
    5000 rpm, code 3    ossacli rpm=5000  code=3 fail=0 | situ rpm=5000  code=3 fail=0
    0 rpm, FAIL         ossacli rpm=0     code=0 fail=1 | situ rpm=0     code=0 fail=1
    max speed, code 7   ossacli rpm=20470 code=7 fail=0 | situ rpm=20470 code=7 fail=0

Then a slice of BMIC ID_CONTROLLER -- little-endian, sparse, 190 bytes.
Five `require offset(...)` passed, and the map reported
`AbsoluteStatic(0x01)`, `(0x1A)`, `(0x8E)`, `(0x8F)`, `(0xBD)`: 1, 26,
142, 143, 189, which is exactly what the hand-written asserts say.

## The argument, which changed while making it

My first conclusion was a narrow yes: `require` is compile-time and
covers all 245 fields rather than 45, the SES bit fields become named
accessors, and "a field the format has that nothing reads" becomes
greppable -- which is a lens that has paid out twice in this tree in one
day. Against that I put the honest cost: of about twenty defects found
here in a day, situ would have prevented **one at most**. A zero validity
mask silencing enclosure alarms, an overall descriptor read as a
measurement, `-1` printed as a shutdown temperature -- every one of those
is semantics, and a schema holds layout. So the format knowledge would
end up split across two places.

The copyright holder's answer retires that objection rather than
weighing against it: **situ is much better documentation of a format
than code is, and all other things being equal it is probably the better
choice.** Which is the right way round. The schema is not a second place
where the layout lives -- it is the place, and the C stops being a
description of the format that has to be read as one. What remains in C
is what the bytes MEAN, which is where this tree's bugs actually are and
which no schema was ever going to hold.

That also settles what to convert first. Not the code that is hardest to
maintain, but the format that is worst documented: the 200 offsets
nothing currently checks.

## Two things for situ

**`at <literal>` costs the constant-offset capability.** Writing the
BMIC offsets the way the C comments already record them --

    u32   signature            at 1;
    u32   board_id             at 26;
    u8    cache_battery_count  at 142;

-- compiles, and every field comes out `offset=DataPlaced`. Padding the
struct with explicit `u8 gap[n]` members instead gives
`AbsoluteStatic(...)` for the same layout. A literal is statically known,
so `DataPlaced` under-reports what the schema guarantees, and the
spelling that reads most like the hardware reference is the one that
loses the capability. Whether that is a gap or `at` being for
data-dependent placement only, the surprise is worth a word in the docs
either way -- a format described from a datasheet full of absolute
offsets is the case where somebody reaches for it.

**`offsetof` is not the builtin.** It is `offset`. Not a defect: the
error named all six builtins and cited project.md section 10, and I had
the right spelling on the next try. Recorded only because a C programmer
transcribing a C header will type `offsetof` every time.

## What I did not test

The Python and Rust backends; anything with a checksum, a tag or a
variable-length region, none of which these formats have; `situc verify`
and the tamper/fuzz generators. Nothing here bears on those.
