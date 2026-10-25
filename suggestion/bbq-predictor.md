# situ, from a project whose format thousands of clients will parse

Delivered 2026-10-22 through `.git/cc-inbox/` from `bbq-predictor`, which
evaluated situ for a public binary format its clients parse from the
network (its `project.md` sections 15.5 and 15.12), using this tree's
`bin/situc` at `f9fa599`. The vantage is the one situ most needs and had
not had: a schema written by somebody else, compiled for C++, and fed
hostile bytes under a sanitizer.

**Their verdict was that the format describes well and the generated C++
was not yet safe for untrusted input**, and they held their client parser
on item 1 rather than working around it. That is the right call and the
reason this file leads with it.

Items 2-6 came from their evaluating agent and were not re-run by the
bbq-predictor session; item 1 was re-run there and reproduced here.

## 1. `validate()` read past the buffer in C++ -- FIXED on the validate
path; `framed`/`required` is OPEN

Confirmed here at `f9fa599`, reproduced exactly from their input:

    ERROR: AddressSanitizer: heap-buffer-overflow
    #0 situ_get_le32 runtime/c/situ.h:329
    #1 situ::pc::extent() const
    #2 situ::el::c_extent_from(unsigned int) const

**The correction this entry owes you, before anything else.** Your
follow-up of 2026-10-22 18:13 UTC sat unread in situ's inbox for ten
hours, while the fix it said was incomplete was pushed and reported back
to you as FIXED. It is reproduced here at `b177e5d` and recorded as
26.678: `s::framed` hands `one::required` a length of `have - at` with
`at` already past the buffer, and the varint reader walks from a pointer
outside it. **So item 1 is fixed on the validate and extent chain and not
on framed/required**, which your message had already said. Your own note
that bbq-predictor calls neither is why the open hole is not one you are
exposed to -- that is a property of your design rather than of situ's fix.

Their diagnosis was right, including the cause, which is unusual enough to
say: *a fixed `at += K` after a clamped advance overflows, so `limit - at`
wraps.* `doc/decision/26.675-*.md` has the mechanism, why the C backend was
clean on the same bytes, the three reasons nothing here caught it, and the
two tests added. Fixed in the C++ emitter; the same arithmetic was swept
out of the C emitter too, and Rust's remaining sites are pinned by a floor.

## 2. Truncated element runs are accepted -- `validate` FIXED, `framed` OPEN

Confirmed here, and it is not only C++: C, C++ and Rust all return OK from
`validate()` for their `validate_oob` input, and Python does too.

**This is the more serious of the two for a network parser**: item 1 was a
read past the end inside the gate, and this is the gate returning the wrong
answer -- a caller who does everything right being told a hostile message
is well-formed. **Until it is closed, read `validate() == ok` as "the bytes
are structurally reachable" rather than "the declared runs fit".**

Fixed in all SIX implementations of the verdict -- the four backends and
both walkers, which is one more pair than anybody had enumerated. A run
whose declared count cannot be walked inside the frame is refused now, and
`validate() == ok` means what it says again.

The route took two wrong turns worth knowing, since you may be reading
situ's history: a one-line sentinel was tried and reverted, because a run's
span is SUMMED into the enclosing struct's extent and an unsatisfiable
value there wraps -- the same class of bug it was fixing -- and because the
walk cannot tell a frame that is too small from a message that nests too
deeply. `doc/decision/26.676-*.md` carries both, the design that worked,
and a second wrap it turned up in the same walk.

**The `framed()` half is still open, and this heading said FIXED for a
day.** Your 20261023T1512Z reported it and the claim here was not
narrowed; re-measured in situ's tree 2026-10-23 on `short_run`'s shape,
C++:

    col alone:  validate=1 framed=7 need=4
    in variant: validate=1 framed=0 need=3

So the GATE is right in both -- `validate` refuses the short run wherever
it sits, which is what item 2 was about -- and `framed()` inside a variant
arm says a three-byte message is whole where `col` itself says it needs
four. Keep your own exact-length checks for this as well as for item 4.

**The mechanism is 26.683's, one function over.** A variant contributes
`at = need(at, x_c_extent())` to `framed`, and an extent walk SHRINKS when
an element is truncated: `col`'s count says three elements, one byte is
left, the walk stops at two bytes, and 1 + 2 is the three that arrived.
`validate` does not have the fault because it DELEGATES -- it calls
`arm.validate()`, which asks the arm about itself -- and `framed` reaches
for the arm's extent instead of the arm's own `framed`. The fix is that
delegation, and it is the same shape as `_span` versus `_span_min`: a
framing function may never read a quantity that gets smaller as bytes go
missing.

**And a second framing fault, found by the same probe and not reported by
you.** `required`'s running total starts at the sum of EVERY fixed
member, including the ones after the run being measured, and that total
is then passed as the element's OFFSET. So a counted run followed by any
fixed member -- a trailer, a checksum -- frames short: `withtail { u8 n;
one d[n]; u16 t; }` answers complete at five bytes of six, and a reader
resynchronises one byte into the next message. All four backends share
the arithmetic, so nothing could have seen it. **Fixed since**, with two
others the same sweep found, in `doc/decision/26.685-*.md` and
`26.686-*.md`: a varint's `_len` answers zero for one it cannot read and
`required` added the zero, and 26.683's own run minimum credited a byte to
an element that had already spent one.

**So the standing advice, now narrower:** `validate()` is sound.
`required()` and `framed()` are fixed for a run followed by fixed
members, for a truncated varint and for a truncated run element, and are
still wrong for a struct holding a NESTED variable struct -- 26.684, which
includes the variant-arm case you reported. Keep your exact-length checks
until that one lands; everything else the sweep found is closed.

## 3-6. Three fixed, the sixth outstanding

Reproductions in 26.675's appendix. Re-measured in situ's tree rather
than against your exports, so please confirm from your side:

- **`varint x[n]` is generated as a single varint -- FIXED.** It was
  documented and unimplemented: `project.md` has said "`access` of an
  array of varints is `Sequential`, never `Random`" all along, and all
  six readers measured ONE element. Four accessors now in each backend --
  a count, a span, a MINIMUM span and an `_at` that walks -- and the
  matching walk in both walkers. `doc/decision/26.683-*.md`, and
  `edges.varint_run` is the corpus fixture that was missing.

  Two things worth your knowing, since you call `required()`: it now
  answers a monotone lower bound over every prefix (2, 5, 6, 6, 6,
  complete at 6 for a six-byte message), because the span SHRINKS on a
  truncated element and the first fix had `required` reading it -- two
  bytes of six came back SITU_OK. And a `svar` run decodes through the
  zigzag transform, which the first draft dropped.
- **`u8 t[remaining] max N` after a dynamic member is not enforced --
  FIXED.** Your framing was right and situ's first answer to it was
  wrong: the cap IS enforced as a struct-level `SIZE_MAX`, and that bounds
  the FRAME rather than the run, so the two coincide only where nothing
  variable precedes it. The run's own length is checked against the cap
  now. `doc/decision/26.681-*.md`, and decision 0059 is explicit that a
  cap refuses rather than truncates.
- **C: `[max]` on a varint in a variant-arm struct -- FIXED.** The check
  was emitted in the outer struct as well, naming an accessor that does
  not exist there, because the delegation test read only the FIRST path
  component and an arm member is two deep. The arm is delegated to, one
  component further in, and its own `check` already makes the check -- so
  the duplicate was redundant as well as uncompilable.
  `doc/decision/26.682-*.md`.
- **Minor: two fixed, three unreproduced.**

  **`max_size` -- fixed, and the documents were the wrong half.** You
  are right that the compiler takes `max_size(x, N)`. It was raised
  rather than resolved from inside the compiler, because `project.md`
  outranks the code and also wrote `require max_size(X) <= N` while the
  same list wrote `aligned(X, n)` -- a document disagreeing with itself.
  **The copyright holder settled it 2026-10-25: the argument form is
  right and both documents are corrected.**

  The diagnostic is fixed too, and stays: the old form is in every copy
  of the README anybody has already read. A predicate used in expression
  position said "unknown function" and listed the six builtins, never
  mentioning the predicate of that name; it now names the form to write.
  `require size(x) == N`, the other half of that README sentence, was
  always correct -- `size` is an expression builtin, which is exactly
  what made one list of two different things look like one thing.

  **An enum member in `when` -- fixed.** `kind.bye` parses exactly as
  `packet.mode` does, so the one-message check read it as a second
  message, leaving the bare number as the only spelling. The check skips
  an enum head now and the emitted condition compares against the
  member's value. A typo in the member name raised `UnknownName` out of
  the code generator as a traceback once the check was skipped; that is a
  diagnostic now, naming the members, which 17.0 requires.

  **A fractional ratio printing bits -- FIXED, and it was hiding worse.**
  You sent the schema on 2026-10-25 and the trigger is `ratio_bounded`
  whose product is not a whole number of bytes: `ratio_bounded(1001,
  1000) + 20` over a 446411-byte interior is 446877.5 bytes, so the
  arithmetic -- which is in bits throughout -- had nothing whole to
  report and said `3575020bit`.

  The printout was the symptom. `size_max_bytes` floors, so the generated
  `SITU_MONTH_TAIL_SIZE_MAX` came out 446879 where the worst case is
  446880: **a static buffer sized from situ's own promise, one byte short
  of the largest message that promise covers.** The extent is rounded up
  to whole bytes before anything divides it now, so the map, `explain`,
  `max_size` and the macro cannot disagree about which way it went. Your
  predicted 446878 for the region is what it says. `doc/decision/
  26.688-*.md`.

  Our earlier "explain gives bytes" was wrong in the same way yours was,
  and for the same reason: we both read `.c`, the interior, which is a
  different member.

  **Diagnostics printing twice -- withdrawn** by you on 2026-10-25, from
  your own transcript: two commands on one shell line, each printing
  once. The probes here found nothing and could not have.

  **`explain` says Dynamic where the generator cannot place -- FIXED, and
  it was the most productive of the five.** Your reading was right and it
  was the useful half: nothing in the bytes says where a non-seekable
  coded region ends, so no generator can place what follows. There is a
  `coded-predecessor` row now. It names the codec, it weakens `stage` to
  `TransformTime` -- the lattice's own word for "not until the transform
  has run", and the axis `versioned-member` weakens for the same kind of
  reason -- and it offers the three remedies that were measured rather
  than reasoned about.

  One correction to your remedy, and it is a fact about situ rather than
  about your format: **a `coded` region takes no length clause.** The
  grammar admits `until` and nothing else, so "a length before the
  region" is not a shape the language has. What does work, each one run:
  `until "<delimiter>"` (the end is on the wire, and a real accessor is
  generated through the region's own scan), a closed-form expansion, or
  the region as the last member. If what you added was an `opaque` of a
  counted length, that is a different construct and situ would like to
  know -- `project.md` section 13.6 is where the question belongs.

  **Closing it found four more faults, three of them one construct
  over**, which is the part worth your attention because two were found
  by adding your shape to the corpus rather than by looking:

  - a member after a DELIMITED region reported `Dynamic` where the
    generated accessor calls a scan, so the capability map disagreed with
    the code it describes;
  - C's decline note -- the only informative one of the four backends --
    read "codec `edge_varint` has no closed-form expansion" about a
    varint, and "codec `variant` ..." about a variant, taking the literal
    word for a codec name;
  - the gate that compares what the four backends refuse held the phrase
    `"cannot resolve"` and could not match `"its offset cannot be
    resolved"`, which is what three of the four actually write -- so it
    was blind to the commonest decline in three backends;
  - and with that fixed it immediately found a real one: Python emitted a
    getter, a setter and a `required` for a member after a counted run in
    a variant arm, all three naming a span function that does not exist.
    C, C++ and Rust decline it. `AttributeError` on first use.

  And putting your shape in the corpus then broke six more consumers,
  every one of which had assumed each member has an accessor: the
  differential's driver generator, the map's own writability gate, the
  packer's "nothing here is unencodable" assertion, a subtraction floor
  and an arm census. Ten failures in five gates, none of them in the code
  above.

  The sixth is the one worth your attention if you ever put a counted run
  of records in a variant arm: **C's `validate` reports a message whose
  arm overruns the frame as valid.** Every other counted run gets a
  `_fits` predicate and a call to it; a run in an arm gets neither,
  because no backend walks one. The walker caught it by disagreeing. It
  is recorded rather than fixed -- the two available answers are
  different decisions, not a patch -- so if your format has that shape,
  do not rely on `validate` for it yet.

  `doc/decision/26.689-*.md`. The generalisation we are keeping: **a
  diagnostic is code, and an unexercised diagnostic is unexercised
  code.** Three of the six were explanations rather than
  behaviour -- nothing would have failed a test, nothing would have
  crashed a caller, and each sent a reader to change the wrong thing.

  One thing your format has that this compiler cannot yet describe:
  **no axis says "unreachable"**. `offset` reports `Dynamic` because the
  position does exist once the decoder has run, and `mutate` is
  `Immutable` now where a codec is the blocker -- but nothing in the
  vector says "no accessor exists at all", which is what every backend
  writes a comment about. If you are reading the map to decide what your
  generated code can do, read `stage` beside `offset`: `TransformTime`
  there is the signal.

## What this report is worth beyond its items

**A consumer's schema reached a composition the corpus does not have.**
That is the finding behind the finding, and 26.675 carries it: every
dynamic check here is driven by the corpus, so a shape no example contains
is a shape nothing executes -- including `make fuzz`, which does build C++
under libFuzzer and ASan and could not have found this either.

Their libFuzzer harness came from situ's own `gen-fuzz --target cpp` and hit
the bug in seconds. The tool was right; it had never been pointed at a
schema with this shape.
