# 0041: every wire-signature line is enforced, and enforced contract is a line

Status: accepted
Date: 2026-08-25
Phase: after 0040

## Context

The wire signature's charter is in its own header: "every line here is a
promise to a receiver that is already deployed and cannot be recompiled".
26.117 found a line that breaks it -- `[trim]` on a plain `u8` adds `trim`
to the signature while nothing trims anything -- and held four attributes
(`trim`, `case_insensitive`, `nul_terminated`, `must_be_zero`) out of the
placement table on the grounds that they are "read into the map" even where
they change no code. The question it left open, and three records have
since cited, was whether being recorded counts as being placed.

Meanwhile the same question's mirror accumulated on the other side: 0038's
codec widths and 0040's `nonce =` and `key =` are enforced by checks and
recorded nowhere, on the grounds that the recording question was open.

**The framing dissolves the question.** A signature line a receiver relies
on is a claim about the format; a claim nothing enforces is a false one,
and worse than absence -- the reader of `@0x0000 1 u8 a trim` writes a
peer that trims, against a schema whose generated code does not. There is
nothing to adjudicate about whether recording is placement: recording an
unenforced fact is the defect, and it stops being reachable the moment the
four attributes have placement rules like the other thirty-two.

## Decision

**Two rules, two directions, one principle: the artifacts record exactly
what is enforced.**

**1. The four held attributes get placement rules, settled by 26.60's
method -- reading what consumes them.** Measured in 26.117's five-position
sweep and re-read in the emitters:

- `[trim]` and `[case_insensitive]` are read where text is scanned and
  compared: a delimited member. Everywhere else they are inert in every
  backend and still enter the signature as `trim` and `fold-case`.
- `[nul_terminated]` is read on a counted byte array (cpio's `name`), and
  already refused on a delimited member; on a scalar it is inert and still
  a `WIRE_ATTRS` pass-through.
- `[must_be_zero]` is `_reserved_policy`'s vocabulary and stays legal on a
  `reserved` member, where writing the default out loud is not meaningless
  (26.60's argument, unchanged). On an ordinary field it is inert in all
  five positions and still enters the signature.

With these four placed, `UNPLACED_ATTRS` shrinks to `secret` and
`non_canonical`, both genuinely read in any position -- the table closes at
"every attribute placed or read everywhere".

**2. `nonce = field` and `key = field` enter the sealed region's signature
line.** They pass the charter's test the way a field width does: which
field seeds the nonce and which selects the key is precisely "what a byte
means", a receiver that disagrees about either cannot interoperate, and
both are enforced by 0040's checks. They do not enter the capability map,
which prices API capability rather than stating contract; the region's
stage and coverage already do.

Codec widths (0038) stay off the signature: the tag and nonce *fields*
already carry their widths as ordinary lines, and a codec-side declaration
adds no byte a peer could observe beyond them.

## Alternatives considered

**Decide that recording is a kind of placement.** The reading 26.60 used to
hold the four out of the table. It leaves the false claim in the artifact:
the signature would keep asserting `trim` over bytes nothing trims, and the
question would return with every new attribute.

**Strip the facts from the signature instead of placing the attributes.**
Makes the signature honest and the schema author no wiser: `[trim]` on a
`u8` would go back to compiling silently, which is the pre-26.117 state
14.5 refuses.

**Put the selector and nonce in the capability map as well.** The map's
axes price what generated code can do; "which field is the nonce" changes
no axis. A fact in two artifacts is two chances to disagree.

## Consequences

- Four placement rules in `_attribute_place`, with refusing tests and
  accepting controls. The committed schemas conform with one exception,
  which is the record's own case found in the tree:
  `tcp_pseudo_header.zero [must_be_zero]` has no check in the generated
  `validate` while its wire line claims `must_be_zero` to every peer. On
  an ordinary field the enforced spelling is `[must_eq = 0]`, and the
  correction changes the signature from an unenforced fact to an enforced
  one -- a wire diff a reviewer should want to see.
- `wire.py` renders `nonce=` and `key=` on sealed region lines; every
  committed `.wire` regenerates; the diff is additions on region lines
  only.
- The signature's `FORMAT_VERSION` stays 0: new facts on existing lines
  are what the fact list is for, and a comparator that keys on facts it
  knows ignores ones it does not.
- 26.117's open question closes, and with it the standing exception the
  placement table has carried since 26.60.
