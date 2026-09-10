# 0055: a token set

Status: accepted 2026-09-10; front end built
Date: 2026-09-10
Phase: raised by the copyright holder, from two schemas already in the tree

## Context

**Two schemas here describe a field whose value is one of a known set of
words, and neither can say so.** Both were written before this record and
neither was written to motivate it:

    example/smtp/smtp.situ:19
    u8  verb[]    until " "  max 16  [case_insensitive, encoding = ascii];

    example/http/http.situ:62
    u8  method[]  until " "  max 16  [encoding = ascii];

Each is a delimited byte run. The reader receives a span and does the
comparison itself, so the set of legal verbs lives in the reader rather than
in the description of the format -- which is the thing this project exists to
stop. `example/http/http.situ:32` is the same shape a third time, on header
names.

**0052 built the fixed-width form of exactly this.** `enum format : u8[6] {
newc = "070701", newc_crc = "070702" }` is cpio's magic, and it replaced a
`decimal u32 magic[6] [min = 70701, max = 70702]` whose comment said "there
is no enum of text numbers, and the alternative is six bytes nothing
constrains". That comment is now history rather than a live gap: the byte-run
enum closed it.

**What is still refused is the variable-width form.** Four routes tried
against the front end, in the manner of 0052:

    enum method : u8[]  { get = "GET", post = "POST" }
        error: expected an expression, found `]`

    enum method : u8[4] { get = "GET", post = "POST" }
        error: `get` is 3 byte(s) of a 4-byte enum
        = an enum whose arms differ in length is a grammar, not a value

    method verb until " " max 8;        (* a u8[3] enum on a delimited run *)
        error: `verb` says twice where it stops

    u8 verb[] until " " max 8 [must_eq = "GET" | "POST"];
        error: `[must_eq]` means nothing here
        = belongs on a scalar field -- an array has no single value to bound

The second is the one that matters, because its refusal is principled rather
than incidental.

## Decision

**0052's line is about who decides the extent, and not about the widths
being equal.** That is the whole of this record; everything else follows.

An `enum : u8[k]` *decides* its member's extent: reading one means consuming
k bytes, and the arms are consulted afterwards. Let the arms differ in length
and a reader can no longer know where the member ends without trying them in
turn, with the longest match winning. That is parsing, the member's extent
becomes a function of the data through a search, and 0052 is right to refuse
it.

**A token set never decides an extent.** `until " "` has already ended the
member before the set is consulted. What remains is a comparison of one known
span against a list of literals -- no trial, no backtracking, no longest
match, and the same relationship to its member that `[must_eq]` has, over a
set rather than a single value.

So the construct is admitted exactly where something else fixes the extent,
and refused where the extent would have to come from the match. 0052's rule
is kept rather than relaxed: the grammar case is still a grammar and is still
refused, and it is refused by a check that names the reason.

    tokens verb [case_insensitive] {
    	helo = "HELO",
    	ehlo = "EHLO",
    	mail = "MAIL",
    	rcpt = "RCPT",
    	data = "DATA",
    	quit = "QUIT",
    	default = error,
    }

    struct command {
    	verb  keyword  until " "  max 16;
    }

**A separate keyword rather than a mode of `enum`.** Two reasons, and the
second is the one that would survive alone. The copyright holder asked for
distance between the mostly-static binary vocabulary and the very dynamic
text vocabulary, and this is where that distance is cheapest to keep. And an
`enum` is width-bearing -- its backing type is mandatory and fixes how wide
one value is -- while a token set has no width at all and takes its extent
from the member. Spelling two different relationships to extent with one
keyword is how a construct acquires a mode nobody can read off the page.

**`default` means what it does for an enum.** `error` refuses an unknown
token, `pass` accepts it and marks the value non-canonical. HTTP's method is
an extension point and wants `pass`; SMTP's verb list is closed by the
grammar in RFC 5321 and wants `error`.

**Case-insensitivity belongs on the set, and the two askers prove the axis
is real rather than a convenience.** SMTP's verbs are case-insensitive by
RFC 5321 section 2.4 and its schema already says so. HTTP's *method* is
case-sensitive by RFC 9110 section 9.1 and its schema already does not say
so, while HTTP's *header names* are case-insensitive and its schema says so
on that member. Three fields, two answers, all three spelled correctly before
this record existed. A flag on the set is therefore not a knob: it is a
property of the protocol's own vocabulary.

## Alternatives considered

**Relax the enum width rule and let arms differ in length.** One construct
instead of two, and it is the first thing anybody tries -- it is route two
above. Rejected because it admits the grammar case by construction: an enum
with arms of differing length and no delimiter has to trial-match, and
nothing in the spelling would distinguish that from the delimited case the
schemas actually want. The width rule is what makes an enum's extent legible,
and it would be traded for a diagnostic somewhere further down.

**`[must_eq]` alternation over an array.** The alternation already exists --
cpio's generated check reads `[must_eq = "070701" | "070702"]` -- so this is
the smallest possible change. Rejected because it names no arms. A caller
told the field is wrong learns less than a caller told which of six verbs it
holds, and the accessor a token set generates is the point of the exercise
rather than the refusal.

**Leave it to callers, which is the status quo.** The cost is measured
rather than predicted, because both schemas are written: the comparison sits
in the reader, the schema documents a byte run and the format reference
documents a vocabulary, and nothing connects them. It is also the state 0052
was written to end for fixed-width runs, one form away.

## Consequences

**A corpus schema lands with the construct, not after it.** 0052's own
consequence, and the reason is unchanged: a per-member fact deciding what
bytes *mean* is exactly the shape the four-way differential exists to catch,
and a construct with no corpus schema poses no case. `test/schema/edges.situ`
carries the tree's instance and the differential reaches it.

**The extent question becomes a wellformed check.** A member typed by a token
set and given neither a delimiter nor a fixed width is the grammar case, and
it is refused there by name rather than falling through to a layout error
about a member of unknown size.

**The grammar case stays open and stays named.** json spells `true`, `false`
and `null` as three structs of `[must_eq = "rue"]`, `"alse"` and `"ull"`
behind a first-byte variant, which is the shape a reader writes when the
token has to decide its own extent. That is not admitted here. Whether it
ever should be is a separate question with a separate answer, and this record
deliberately does not prejudge it -- the sentence "an enum whose arms differ
in length is a grammar" is still the reason.
