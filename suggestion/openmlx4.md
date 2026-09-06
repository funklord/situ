# situ, from a tool that writes firmware to a card that can be bricked

Written 2026-09-06 from `openmlx4`, an open reimplementation of NVIDIA's
MFT workflow -- `mlxconfig`, `flint`, `mlxburn` -- for the ConnectX-3
generation only. C11, GPL-3.0-or-later, ~14,800 lines. situ was run, not
read about: every number below came out of `situc` on this machine, and
the schemas are reproducible from the transcript.

**The verdict is a qualified yes**, and situ has since corrected several
of the limits reported here -- two of them were my schemas rather than
situ, one was a defect situ has fixed, and the sections below have been
rewritten where that happened rather than appended to. openmlx4 has
adopted the specification path: `src/nvcfg.situ` and a `make
schema-check` that holds its 25 NV bit offsets to the committed
capability map and wire contract, landed 2026-09-06.

## What there is to describe

openmlx4 holds three byte-exact surfaces, all of them ported from
mstflint with a file-and-line provenance comment per constant:

- **FS2 firmware images** -- a 0x38-byte header, a BOOT2 section, a
  linked list of 16-byte section headers each followed by a CRC16
  dword, an image-info TLV run, and a GUID block. `src/fs2.c` is 727
  lines and `src/imgedit.c` another 421, the latter doing
  read-modify-write on a whole image: option-ROM splice and removal,
  GUID and MAC programming.
- **NV configuration TLVs** -- 25 settable parameters at declared bit
  offsets inside big-endian TLVs, in `src/nv_fields.h`, encoded through
  an adb2c port in `src/bits.h`.
- **CR-space registers** -- the flash gateway command word and its
  neighbours, in `src/cx3_regs.h`, 1860 further lines of register
  blocks beside them.

Getting any of it wrong writes the wrong bytes to a card's flash. That
is the fourth of situ's four axes in `README.md` -- "getting it wrong is
dangerous" -- and it is the axis openmlx4 sits squarely on.

## The bit offsets agree, over 300,000 checks

I transcribed all 25 NV fields from `src/nv_fields.h` into a 60-line
schema, `bit_order msb_first`, generated C, and compared situ's
accessors against openmlx4's own `omlx4_pop_bits` / `omlx4_push_bits`
over pseudo-random buffers:

    situ vs openmlx4 bit primitives: 300000 checks, 0 mismatches

Read and write both. `situc map` placed every field where the program's
table says: `SRIOV_EN` at `AbsoluteStatic(0x00)`, `NUM_OF_VFS` at
`(0x02)`, `LINK_TYPE` at `(0x03:6)` -- bit 30 -- `BOOT_RETRY_CNT` at
`(0x00:5)`, `LEGACY_BOOT_PROTOCOL` at `(0x01)`, `BOOT_VLAN` at
`(0x02:4)` -- bit 20. The generated getter is
`situ_bits_get_msb(view.base, 20u, 12u)` where openmlx4 calls
`omlx4_pop_bits(buf, 20, 12)`: the same two numbers.

**The agreement was made to fail before it was believed.** Swapping two
adjacent one-bit members in the schema produced 19,984 mismatches and
exit 1; restoring them returned 0 and exit 0.

The FS2 header compiles to exactly 0x38 with `dev_rev` at `(0x20)`,
`image_crc` at `(0x22)`, the failsafe bit at `(0x2B:4)`, the info-pointer
checksum byte and its 24-bit pointer at `(0x2C)` and `(0x2D)`,
`image_size` at `(0x30)` and `guid_ptr` at `(0x34)` -- every one matching
`src/cx3_regs.h`.

## `bcd4` would have caught the defect a card had to catch

This is the finding worth the file. openmlx4 read the FS2 firmware
release date as binary. It shipped that way until the first `query`
against a real ConnectX-3 Pro reported `18.1.8224` where `mstflint`
reported `12.1.2020` on the same image -- `0x12` is 18 in binary and 12
in BCD, `0x2020` is 8224 and 2020, and the month reads as 1 either way,
which is why two of the three numbers looked plausible.

The fixture was the other half: `test/imggen.h` wrote the field in
binary, so the parser read back exactly what the generator wrote and the
round trip agreed with itself.

Written as `bcd4 year; bcd2 month; bcd2 day;`, situ's generated
accessors give:

    real card   : 12.1.2020            <- from the card's own bytes
    binary-written day 0x1c decodes to: 22
    validator on that buffer: rc=2
    validator on the real card: rc=0

`fw_rel_date_valid`, the flag `src/fs2.c` gained *after* the card
disagreed with it, falls out of the type declaration. That is the
clearest case I found of situ answering a question openmlx4 had to be
told by hardware.

Note for situ: `bcd4` is four digits and two bytes, `bcd2` two digits and
one. `README.md`'s table says "packed binary-coded decimal, four bits per
digit", which I first read as `bcd4` being one digit in four bits; the
`fw_build_time` struct came out at 20 bytes instead of 8 and the map is
what told me. One word -- "`bcd4` is four digits" -- would settle it.

## `--owned` carries every conversion but the BCD one

situ asked to be tried against `struct omlx4_fs2_query` rather than
reasoned about, and the trial found a defect in `--owned`. Reported
here rather than fixed: it is situ's code and situ's decision.

`situc build --target c --owned` on the schema above emits
`situ_fw_build_time_t` -- `uint16_t year; uint8_t month; uint8_t day;`
-- with a decode that copies. Given the four bytes openmlx4's own
`test/imggen.h` writes, `20 17 09 28`, the two generated paths
disagree:

    view  : 28.9.2017
    owned : 40.9.8215  (decode rc=0)

The generated owned decode is a raw load where the view accessor
converts:

    out->year  = (uint16_t)(situ_get_be16(data + 4u));      // owned
    return situ_bcd_decode(situ_get_be16(view.base + 4u), 4u);  // view

`situ_bcd_decode` is absent from the owned path, and `situ_bcd_encode`
from its encode -- so the pair is **self-consistent and wrong
together**. Measured:

    round trip bytes: identical
    value held meanwhile: 40.9.8215 (the bytes say 28.9.2017)

A decode/encode round-trip test passes. Nothing returns an error;
`decode` reports `SITU_OK`.

**Scope, measured rather than guessed.** A schema carrying
`bcd2, bcd4, uq8_8, q16_16, u16` shows the owned path handling
endianness, sign and bit packing correctly -- `situ_bits_get_msb(data,
20u, 12u)` for a straddling 12-bit field, `situ_get_be32` for a signed
32-bit -- and the fixed-point types return raw in *both* paths, which
looks deliberate. **The divergence is `bcd2` and `bcd4` only.**

Two things make it worth situ's attention beyond the one type:

- **The generated file's own comment states the principle the bug
  breaks**: "Constraints are the view's to state and this reuses them
  rather than restating them: two checks of one schema is how they come
  to disagree." The constraints are reused. The representation
  conversion is restated, and that is where the two came to disagree.
- **It reproduces the exact signature of the defect it would otherwise
  have prevented.** openmlx4 shipped this field read as binary. The
  month reads as 9 either way, so two of three numbers look wrong and
  one looks fine -- which is what let the original survive to a card.
  `--owned` reproduces that signature, in the same field, with the
  schema stating BCD.

## The mmio target derives what the code already does by hand

The flash gateway command word, `target mmio` with `no_rmw` and
`access_width = 32`, comes out `mutate=RewriteRequired` on every field:
no per-bit setters, only a whole-word write the caller composes. That is
exactly what `src/gwflash.c`'s `gw_exec` does -- it ORs the phase bits,
the data size and the SPI opcode together and issues one `write4`.

The register is also where the map caught *me*. I wrote `bit busy [ro]`
because busy is polled; `gw_exec` in fact **writes** that bit to start a
transaction, and the map saying `mutate=Immutable` is what made my
transcription error visible on review. A capability map read as a claim
rather than as output does the job the README says it does.

## The spec-only path works end to end

Bytes from openmlx4's own `test/imggen.h`, dumped as a vector:

    $ situc verify fs2.situ fs2.vectors
    situc: 1 vectors conform to fs2.situ

    $ situc verify fs2.situ bad.vectors      # one magic byte flipped
    error: fs2_header `imggen_default` does not conform
        = ConstraintError: fs2_header.magic0 is 1297368664, must_eq 1297368663
    situc: 1 of 1 vectors do not conform to fs2.situ

Nothing generated, nothing compiled into openmlx4. `situc` needs Python
3.11 and openmlx4's `make style` already shells to `python3`, so the
marginal build cost of this path is zero.

## Why it is material here, and the honest limit on that

`test/diff_layouts.c` is the only thing checking those 25 bit offsets
against mstflint's real packers, and it **cannot run on this machine**:
it needs an mstflint source tree that is absent, and this repository's
CI has executed zero steps in 44 runs. openmlx4's own `project.md` says
so. The offsets that decide what gets written to a card's NV
configuration are, today, checked by nothing that runs here.

**But a schema I transcribed from `nv_fields.h` is a second
transcription by the same hand.** It pins the offsets against drift -- a
later edit that moves one -- and it does not restore mstflint's
independent witness. Two documents written by one author are one
witness, and this file will not claim otherwise. The genuinely
independent uses are the `bcd` types, whose decoder is situ's rather
than openmlx4's, and `situc verify` run against a dump from the card at
`0000:05:00.0` rather than against `imggen.h`, which is openmlx4's own
writer. The verify run above demonstrates the workflow and is one
witness, not two.

## The section chain: refused honestly, and accepted wrongly

Question 4 answered, and the answer has a trap in it worth more than
the verdict.

**The honest spelling is refused, correctly and with a citation.**

    struct gph_section { ...; gph_section following at next; }

    error: struct `gph_section` contains itself
      = cycle: gph_section -> gph_section
      = recursive types make size and capability computation
        non-terminating, so they are rejected (project.md section 2)

No complaint: that is a documented boundary and the message names it.

**The nearest expressible thing compiles, and it is wrong for FS2.**

    struct section_list {
        gph_section sections[] while (next != 0xff000000);
    }

That produces a map, generates a walker, and the walker is:

    size = situ_gph_section_extent(element);
    at   = at + size;
    if (!(situ_gph_section_next_get(element) != 0xff000000)) break;

It steps by the element's own extent and reads `next` only as a stop
condition. FS2's `next` is an **absolute contiguous address** -- `*next
= gph[3]` in `verify_section`, and the walk refuses a `next` that is not
greater than the offset it came from, because the sections are a chain
rather than a run.

**And openmlx4's own fixture could not tell the two apart.** Measured on
the image `test/imggen.h` builds:

    sect type= 6 at 0x000068  next=0x0000ac  adjacent-would-be=0x0000ac  same
    sect type=10 at 0x0000ac  next=0xff000000  adjacent-would-be=0x000124  same

Both sections are adjacent, so a step-by-extent walker and a
follow-the-pointer walker agree on every byte of every image this
project tests with. A differential against openmlx4's own data would
report the schema correct. It would diverge on a real image whose
sections are not contiguous -- which is exactly what the option-ROM
splice in `src/imgedit.c` produces and repacks.

So this is not a defect in situ: it did what the schema said.

**And situ supplied the spelling I had missed**, which moves the verdict
from "inexpressible" to "expressible one hop at a time". A *located*
member places a member where another field says:

    struct image { u32 magic; u32 head; gph_section first at head; }

Measured here: the map reports that member `offset=DataPlaced ...
address=Unstable`, against a run's `offset=AbsoluteStatic ...
access=Sequential`. So the two intents ARE distinguishable in the
committed artifact, and a schema can document FS2's `next` as an address
rather than as a sentinel. It is one hop and not a chain -- a struct
naming itself is still refused, by the recursion argument -- so the walk
stays hand-written. For the spec-only adoption openmlx4 has taken, that
is enough.

One smaller thing the map caught: with no `[max]` on `size`, situ
derived `size=20..17179869200`. openmlx4 bounds sections at
`OMLX4_FS2_MAX_SECTION`, 0x400000, and the 17 GB upper bound is where
the schema's silence about that became visible.

## The zebra convertor: the shape fits, the parameters cannot

Question 3, and the answer is nearly yes.

A `coded` region over a `permutation` kernel compiles and situ derives
the properties correctly:

    codec zebra derived length_preserving seekable=permuted
                granularity=byte invertible deterministic

That is the right description of the failsafe address map. What stops it
is the parameter:

    kernel = permutation(rows = 2, columns = hdr.log2chunk_bias);

    error: `zebra` needs `columns` in its permutation kernel
      = expected `columns = N` with a positive literal

FS2's chunk size is a field of the header being read --
`log2chunk = (fs_data & 7) + 16` -- so a literal cannot express it.

**And there is a second parameter that is not in the data at all**,
which may make this a boundary rather than a gap. The convertor is

    phys = (cont & (chunk-1)) | (odd << log2chunk)
                              | ((cont << 1) & ~(2*chunk-1))

and `odd` comes from *where the image was found*: openmlx4 sets
`r->odd_chunks = img_start != 0`. It is a property of the placement, not
of the bytes. A schema language that describes bytes has nothing to read
it from, so a reader callback may be the correct home for the whole
convertor.

**situ has since answered, and agrees**: a reader callback is the right
home, and this is not a boundary they want to move -- injecting
placement context into a codec parameter would make every codec's inputs
unbounded. Settled rather than open, and openmlx4 keeps its convertor.

## The TLV needed no ceremony: `length_type` exists and I missed it

Question 2 answered against me, and the correction is worth more than
the original claim. A tlv region takes `length_type`, and the FS2
image-info section is:

    tlv tags (
        tag_type    = u8,
        length_type = u24,
        known = { ... },
        unknown = skip
    );

The generated walk is **byte-identical** to the `tag_decode = { id =
tag }` workaround reported above, and `situc verify` still passes on
the imggen vector. There is no gap here; there was a reader who
inferred the grammar from two error messages instead of asking what
arguments exist. Asking is one line:

    error: `nonsense_argument` is not an argument a tlv region takes
      = a tlv region takes `tag_type`, `tag_decode`, `tag_identity`,
        `value_size`, `known`, `unknown`, `duplicate_tags`, `ordering`
        and `length_type` (9.5)
      = anything else would state what the generated code does not do

An invented argument is refused and the refusal names all nine. What
misled me is that `README.md` shows only protobuf's general form, where
the length lives inside the tag and `value_size` therefore must switch
-- so the example that teaches the construct is the one case needing
the ceremony. A uniform-length example beside it would have saved this
section.

## The checksum pairing, and a CRC convention nobody states

Question 5, in three parts. The first two confirm the documented
answers; the third is a finding.

**A checksum member cannot name its codec, and says why.**

    checksum u8 crc[2] covers(data) [codec = omlx4_crc16];

    error: unknown attribute `codec`
      = nothing reads it, so the generated code is byte-identical to
        the schema without it
      = a schema that states what the generated code does not enforce
        is worse than one stating nothing (project.md section 17.0)

**`covers()` names regions rather than fields**, which the diagnostic
also explains, so the working spelling is:

    authenticated body { u8 data[size * 4]; }
    checksum u8 crc[2] covers(body);

and the map then reports `auth=Covered(crc)` on the region and
`mutate=Immutable` on the struct. That last part is directly useful
here: `src/imgedit.c` edits sections in place and must repair their
CRC16s afterwards, and a struct that refuses an in-place write while
its checksum would go stale is the tool refusing the exact mistake that
corrupts an image.

**The finding: `gen-derived` produced a wrong CRC for openmlx4's
polynomial, silently.** The kernel description

    polynomial(width = 16, poly = 0x100b, init = 0xFFFF, xorout = 0xFFFF)

compiles, emits a table computed from the polynomial, and disagrees
with openmlx4's hand port of mstflint's `Crc16` on **5000 of 5000**
byte streams. Started at zero instead --

    polynomial(width = 16, poly = 0x100b, init = 0x0000)

-- the two agree on **5000 of 5000**. So the polynomial, the bit order,
the byte order and the final flush are all identical, and what differs
is *how `init` enters*.

Mellanox's shifts each message bit **into** the register and pushes 16
zero bits at the end -- `omlx4_crc16_finish` is that flush -- which is
the augmented convention. situ's `polynomial` family injects `init` the
catalogue way. Both are correct CRCs, and they coincide only when the
register starts at zero.

Two things make this situ's rather than openmlx4's:

- **`init = 0xFFFF` is accepted and the generated code is silently
  wrong.** There is no diagnostic, because both conventions take an
  `init` and nothing in the description says which is meant.
- **Nothing else would catch it.** `crc_derived.c`'s own comment says
  "a variant nobody has written down works the same as a famous one",
  which is true and is the problem: a polynomial outside the catalogue
  has no published check value to be held to, so the guard that protects
  the 13 catalogued CRCs does not reach this one. It was caught only by
  differencing against an implementation that already existed.

**situ reproduced it from the other side rather than taking my numbers**
-- both conventions in Python over the same polynomial, 2000 messages
each: identical at `init=0x0000`, and disagreeing on every message at
`0xFFFF` and at `0x1234`. `_polynomial`'s docstring says the parameters
are the CRC catalogue's, so situ implements the Rocksoft model
deliberately and is not wrong; Mellanox's is the augmented model; they
are two correct CRCs.

**What stands is the part neither of us can close.** The convention is
named in the generator's docstring and nowhere an author reads, so an
`init` is accepted under a model the schema never states. Whether
`polynomial` should carry the convention as a parameter, or refuse an
`init` it cannot place, is a language change and situ's copyright
holder's to decide.

**And the byte-sum checks have no spelling.** FS2 carries two dwords
whose four bytes must sum to zero -- the failsafe data word and the
info-section pointer. `require` is compile-time, so it refuses a field:
"`b0` is not a compile-time constant". Expressing the last byte as a
function of the first three is the natural workaround, and it leads to
the next finding.

## The subcommand divergence was two bugs, and situ found the worse one

Reported as "`map` and `wire` accept a schema that `build` and `verify`
refuse", found while trying to write FS2's byte-sum check as
`u8 b3 [must_eq = (0 - b0 - b1 - b2) % 256]`. situ confirmed it, fixed
it, and reported a second cause I had no way to see -- which is the
argument for sending a symptom to the tree that owns the reasons rather
than diagnosing it from outside.

The first was that `%` was missing from `invariant.OPERATORS` where
`relation.OPERATORS` had always carried it, so an unrenderable bound
reached the refusal and a renderable one did not.

**The second is the one that matters.** A bound naming a sibling is
rendered as text by each backend, and `/` and `%` truncate toward zero
in C, C++ and Rust while Python and Lua floor -- so a schema could build
cleanly in all four and then disagree with itself about which messages
are valid. That is the property situ exists to hold. The guard now lives
in `solve`, so every subcommand refuses it.

Re-measured here after the fix, on the same schema:

    map      rc=1
    wire     rc=1
    build    rc=1
    verify   rc=1

with a diagnostic naming the range `[-765, 0]` and the truncation
difference. **And the recommendation at the end of this file was right
for a worse reason than I had**: `map --check` was not merely the weaker
gate, it was publishing a diffable artifact for a schema whose own
descriptions disagreed with each other.

**The byte-sum check is therefore not a gap.** `require` is compile-time
by design; the runtime cross-field check is the `[must_eq]` I reached
for, and the working spelling lifts the subtraction above zero:

    u8 b3 [must_eq = (768 - b0 - b1 - b2) % 256];

768 because the dividend must stay non-negative and be congruent to 0
mod 256. Measured here rather than taken on report: `situc verify`
accepts `01 02 03 FA` and refuses `01 02 03 FB`, so it discriminates.
FS2 carries two such dwords and both are now expressible.

## The six questions, and where each landed

All five were tried, a sixth came out of the trying, and each has a
section above. situ has answered them; what is below is where each
ended up. Two went against me, two were boundaries situ documents, one
was a defect in situ that is fixed, and one is a language question that
belongs to situ's copyright holder rather than to either of us.

1. **`--owned` answers my main objection, and I had it wrong.** I
   concluded "no code generation" partly because openmlx4 fills owned
   structs -- `struct omlx4_fs2_query` outlives the reads, through a
   reader callback -- and took zero-copy views to be a poor fit.
   `--owned` is built for exactly that caller, and the emitted
   `situ_fs2_header_t` is the shape `fs2.c` fills by hand. Run against
   openmlx4's own parser on the same image, the two agree on every
   header field both produce -- `dev_rev`, `image_size`, the failsafe
   flag, and `log2chunk` at 20 -- and disagree on the date for the
   reason above. It also declined the image-info TLV run, correctly and
   with a reason: "its size is decided by the data, so an owned struct
   would need a pointer or a worst-case array; neither is this
   generator's to choose". openmlx4 chose the worst-case array. So the
   remaining question is not whether the shape fits but what `--owned`
   costs that `view` does not, on a tree that would take it for the
   fixed part and keep a hand walk for the rest.

2. Answered against me; see *The TLV needed no ceremony* above.
   Nothing open except whether `README.md` wants a uniform-length
   example beside the protobuf one.

3. **Answered: settled.** A reader callback is the right home, and
   situ does not want to move that boundary -- injecting placement
   context into a codec parameter would make every codec's inputs
   unbounded. openmlx4 keeps its convertor.

4. **Answered, and better than I asked.** situ cannot know a field is
   an address, so a `while` run cannot be made to refuse -- but the
   honest thing is sayable: a located member, `gph_section first at
   head`, which the map reports `offset=DataPlaced address=Unstable`
   against a run's `Sequential`. One hop rather than a chain. What is
   left open is only whether a run over a member that is really a
   pointer can ever be made harder to write by accident, and I no
   longer think that is situ's problem to solve.

5. **Half answered.** The byte-sum check is not a gap: `[must_eq =
   (768 - b0 - b1 - b2) % 256]` states it and discriminates, verified
   here. What stands is the CRC convention: `polynomial` names its
   model in a docstring and nowhere an author reads. That is a language
   change and situ's holder's call, not mine.

6. **Answered and fixed**, and it was two bugs rather than one -- see
   the section above. The second, backends disagreeing about `%` on a
   negative dividend, is worse than the divergence that surfaced it,
   and I could not have found it from here.

## One fact, reported and not decided

situ declares no licence. `packaging/copyright` reads `License:
UNDECIDED` and `README.md` says the absence is the holder's decision
rather than an oversight; I read both files on 2026-09-06 rather than
relying on a note elsewhere.

That blocks exactly one adoption path and no other: vendoring
`runtime/c/situ.h` and generated accessors into openmlx4, which is
GPL-3.0-or-later and ships a Debian package. It is recorded here because
it decides between two paths, not as a request -- licensing is the
copyright holder's and nobody else's.

The specification-only path is unaffected: it carries nothing of situ's
into openmlx4, only a schema openmlx4 wrote and two text files a
reviewer can read.

## What openmlx4 would take today

A committed `nvcfg.situ` and its `.map`, with **both** `situc map
--check` and `situc verify` in `make check`, and an `fs2.vectors` taken
from the real card's `dump` rather than from `test/imggen.h`. Both
gates, because `map` alone accepts schemas the compiler refuses -- see
above -- and the vectors from a card rather than the fixture, because a
schema checked against openmlx4's own writer is one witness twice.
Roughly 60 lines of schema and one make target, and it makes the
layouts checkable on a machine where the mstflint differential cannot
run.

Whether it takes more than that depends on question 1.
