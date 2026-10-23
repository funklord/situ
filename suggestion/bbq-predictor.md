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
- **Minor:** the README shows `require max_size(x) <= N` where the compiler
  takes `max_size(x, N)`; an enum member in `when` is refused as naming two
  messages; every diagnostic prints twice; a fractional expansion ratio
  prints sizes in bits where `explain` gives bytes; `explain` calls a member
  Dynamic that the generator then cannot place.

## What this report is worth beyond its items

**A consumer's schema reached a composition the corpus does not have.**
That is the finding behind the finding, and 26.675 carries it: every
dynamic check here is driven by the corpus, so a shape no example contains
is a shape nothing executes -- including `make fuzz`, which does build C++
under libFuzzer and ASan and could not have found this either.

Their libFuzzer harness came from situ's own `gen-fuzz --target cpp` and hit
the bug in seconds. The tool was right; it had never been pointed at a
schema with this shape.
