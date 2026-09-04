# 0052: a byte run as a value

Status: proposed
Date: 2026-09-04
Phase: raised by the copyright holder, from three schemas in one week

## Context

**situ cannot say that four bytes are `WOZ2`.** Not in any spelling. Every
route was tried against the front end and every one is refused:

    u8 sig[4] [must_eq = "WOZ2"]       `[must_eq]` means nothing here
    u8 sig[2] [must_eq = 0x424D]       `[must_eq]` means nothing here
    enum m : u8[2] { bmp = "BM" }      expected `{`, found `[`
    enum m : u16 { bmp = "BM" }        a string is not an integer expression
    u8 sig[4] = "WOZ2"                 expected `;`, found `=`
    magic u8 sig[4] = "WOZ2"           expected `;`, found `sig`

`[must_eq]` is a bound on a *scalar*. An array member is a span, and the
attribute that compares one number to another has nothing to compare.

**So the only way to write a magic is one field per byte**, and three
independent schemas arrived at it separately -- a WOZ2 header, a BMP header,
and a header written outside this project by a reader who had the reference
open. That last one is the useful witness: it is what the language teaches a
competent author to write when the right spelling does not exist.

Here is what it costs, generated rather than argued:

    situ_err_t situ_woz_check(situ_view_t view, uint32_t *which)
    {
            /* woz.high_bit [must_eq = 141] */
            if (situ_woz_high_bit_get(view) != 141) { ... }
            /* woz.w [must_eq = 87] */
            if (situ_woz_w_get(view) != 87) { ... }
            /* woz.o [must_eq = 79] */
            ...
    }

Six accessor calls and six branches where one span comparison would do; six
check ids for what is one fact; and six invented member names -- `high_bit`,
`w`, `o`, `z`, `lf`, `cr` -- for bytes that are not fields and that no
caller will ever read individually.

**The comments are the worse half.** They print `141`, `87`, `79`, `90`,
`10`, `13`, because a scalar bound is a number and a number renders in
decimal. Nothing in the generated code says `WOZ2`. A reader holding the
format reference beside the header cannot see that these are the same
thing, which is the readability failure 14.5 is about pointed at a comment
instead of a check.

**And a preamble has no spelling at all.** Fixed bytes that a caller should
never read -- a sync run, a leader, a framing byte -- are describable only
as ordinary fields, which means they get accessors, appear in the walker's
member list and in the dissector's tree, and are offered to the editor.
`reserved` is the construct for "not yours", and it takes a *policy*
(`[must_be_zero]`, `[must_be_one]`, `[preserve]`, `[unknown]`) rather than a
value.

**This record follows a refusal that made the gap sharper.** `reserved
[must_eq = N]` used to be accepted, and compiled to `!= 0` under a comment
reading `[must_be_zero]` -- the default policy, enforcing zero, on a member
whose author had written N (26.233). That is now refused. The refusal is
correct and it closes the only door an author was reaching through: they
wanted inaccessible-and-fixed, found the inaccessible construct, and
attached a value to it. Refusing the miscompile without providing the
spelling leaves the author with nothing, so the two belong in one motion.

## Decision

**Three constructs, because these are three facts and not one.**

*A byte run is comparable to a byte run.* `[must_eq]` on a member whose type
is `uN[k]` takes a string or byte literal of exactly `k` elements, and
compiles to one span comparison:

    u8 sig[4] [must_eq = "WOZ2"];

The length is checked at compile time against the declared extent, and a
mismatch is a diagnostic naming both numbers. It generates `SITU_ERR_
CONSTRAINT` like every other bound, and it takes one check id, because it is
one fact.

*An enum may be tagged by a byte run.* `enum m : u8[k]` is an enum whose
underlying type is a span, and its arms are literals of that length:

    enum format : u8[2] { bmp = "BM", pe = "MZ" }

This is the construct the copyright holder asked for by name, and the reason
it is not just sugar for a `u16` is 0024's lesson about bases: `"BM"` as a
`u16` is `0x424D` or `0x4D42` depending on endianness, and an author writing
a *signature* is not thinking about byte order at all. A span has no
endianness, so the arm means the same thing under both, which is the whole
reason signatures are written as text in every format reference there is.

*A preamble is fixed and not exposed.* A new member kind, spelled to be read
as "these bytes are here and they are not yours":

    preamble u8 sync[4] = "\x8d\x57\x4f\x5a";

It is checked on validate exactly as `[must_eq]` is, and it generates no
getter, no setter, no walker member, no dissector row and no editor field.
It is `reserved`'s sibling: `reserved` says "content governed by a policy",
`preamble` says "content is these bytes".

**The comment renders the literal as written.** Where an author wrote
`"WOZ2"` the generated comment says `"WOZ2"`, not a decimal run. This is not
cosmetic -- it is the only thing that lets a reader check generated code
against a format reference, and the per-byte route's failure to do it is
half of what makes it bad.

## Alternatives considered

**Leave it, and let authors write a byte per field.** This is the status
quo, and it is what three schemas already did, so the cost is measured
rather than predicted: six branches, six invented names, six ids, and
comments that do not name the thing they check. It also scales with the
magic -- a 16-byte signature is 16 fields.

**Sugar: expand `[must_eq = "WOZ2"]` into per-byte checks at compile time.**
Cheaper to build and it fixes the *schema*'s ugliness while leaving the
generated code exactly as it is. Rejected because the generated code is
where the cost was measured, and because it would give one fact six check
ids -- which 0051 just spent a record making meaningful. A caller told
"check 3 failed" for a four-byte magic learns nothing a caller told "the
signature is wrong" does not.

**A `magic` member kind, combining the run and the inaccessibility.** One
construct instead of two, and it matches how format references talk. Rejected
because the two facts are genuinely separable: a version signature is a fixed
run that callers *do* read, and a sync leader is inaccessible without being
fixed to a single value in every dialect. Fusing them would make the common
case easy and the other two unsayable, which is the shape of the problem this
record is fixing.

**Reuse `reserved` with a value.** This is what an author already tried, and
it is what 26.233 refuses. `reserved`'s vocabulary is policies, and the moment
its right-hand side can be a value it is two constructs sharing a keyword.

## Consequences

**The differential gets a case it cannot currently pose.** A byte-run bound
is a per-member fact deciding what bytes *mean*, which is the exact shape of
all four bugs found in 26.22x -- each invisible because no corpus schema
could pose it. Adding the construct without adding a corpus schema that uses
it would reproduce that, so a `[must_eq]` span and a `u8[k]` enum belong in
the differential corpus in the same change as the construct.

**`preamble` adds an axis question rather than answering one.** A member
with no accessor is `access` at its bottom, and whether that is an existing
lattice value or a new one is not decided here. It is the one part of this
record that needs the lattice consulted before it is built.

**Nothing here is built.** This record is the spelling and the argument for
it, written while three schemas that needed it are still fresh.
