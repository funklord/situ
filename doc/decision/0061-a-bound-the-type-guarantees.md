# 0061: a bound the type already guarantees

Status: accepted
Date: 2026-10-03
Phase: raised by 26.558, which met it and set it aside

## Context

**`u8 a [max = 255]` generated a comparison the compiler proves false.**

    error: comparison is always false due to limited range of data type
           [-Werror=type-limits]

`-Wextra` turns that on and this project's own flags make it an error, so
situc emitted C that situc's own gate would not compile. Six spellings do
it: `[max]` at an unsigned ceiling, `[min = 0]` on an unsigned, and either
limit of a signed type.

**All four backends emitted the check and all four agreed on the ids.**
That is what ruled out fixing it in the one backend that complains:
omitting it in C alone would renumber C's check ids against the other
three, which is 26.560's defect introduced on purpose.

## Decision

**The comparison is omitted, in all four backends, where the member's own
type already guarantees the bound.** A check that cannot fire is worse than
none wherever it sits -- 26.558's words -- and the bound remains true,
remains in the capability map, and remains enforced: by the type rather
than by a comparison. That is what separates this from 26.558's complaint
about a bound nothing enforces.

**It is NOT a refusal, and that was measured rather than argued.** A bound
may be a `const`:

    const CAP = 255;
    struct k { u8 x [max = CAP]; }

    situc build --define CAP=100    the bound bites
    situc build --define CAP=255    the bound is the type's own ceiling

Refusing the second would refuse a schema that is correct for every other
value of its own constant. The compiler therefore says nothing and emits
nothing, which is the only answer that serves both.

**And there is NO warning, which `example/usb` decides.** The corpus holds
exactly one instance, and it is deliberate:

    u7  address   [max = 127];     // usb.h:1980, and devmap is 128 wide

That bound records what the format's own source says -- the schema's header
cites `usb.h:1980` for the seven bits and `usb.h:471` for the 128-wide
device map. It is documentation of a cited fact, and a warning calling it
redundant would be wrong about the only real case there is. 14.5's rule
that an attribute must sit where something reads it is answered here by the
type reading it.

**Conservative outside the plain integers.** A BCD field's bound is
compared against the decoded value rather than the packed nibbles, and a
fixed-point one is in units this does not establish, so neither is answered
and both keep their checks. All six spellings that broke the build are
`uint` or `sint`, so nothing is lost by declining to guess.

## Alternatives considered

**Refuse the schema.** 0059 refuses a `max` on a run something already
bounds, and the parallel is real -- but that cap has another spelling to
redirect the author to, and a type's own ceiling has none. The `const` case
above is what settles it.

**Keep the comparison and silence the compiler with a cast.** Deforming the
source so a checker stops being right, which `evidence.md` names: *a gate
that can be satisfied by deforming the source is worse than one that simply
reports wrongly.* Here the compiler is not wrong.

**Warn in the front end.** There is no warning channel in `wellformed`; the
three places that emit warnings are the LSP, the C backend's name
collisions and `requirements`. Building one for this would put a
language-independent schema observation behind one backend, and
`example/usb` says the observation should not be made at all.

**A `situc advise` row.** The natural home for "your schema could say this
better", and its own contract is a row carrying what a change costs and
what it buys, both computed. A redundant bound costs nothing and buys
nothing, so a row would have an empty cost column -- and the USB case is
advice nobody should take. Named here so the next reader does not have to
re-derive why it is absent.

## Consequences

**One corpus schema changes and nothing renumbers.** `usb.h` loses
`SITU_TOKEN_ADDRESS_CHECK` and `usb.c` loses the five dead lines behind it.
It was the last id of the two `token` declares, so `SITU_TOKEN_PID_CHECK`
keeps 0 and no consumer's number moves. The committed `usb.situ.map` and
`usb.situ.wire` are unchanged, the bound being a schema fact rather than a
generated one, and no test referenced the id.

**A generated API loses a symbol**, which is worth stating plainly rather
than leaving in a diff: a caller keying on `SITU_TOKEN_ADDRESS_CHECK` will
not compile. It named a refusal that could not happen.
