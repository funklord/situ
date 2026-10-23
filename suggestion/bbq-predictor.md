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

## 2. Truncated element runs are accepted -- CONFIRMED, still open

Confirmed here, and it is not only C++: C, C++ and Rust all return OK from
`validate()` for their `validate_oob` input, and Python does too.

**This is the more serious of the two for a network parser**: item 1 was a
read past the end inside the gate, and this is the gate returning the wrong
answer -- a caller who does everything right being told a hostile message
is well-formed. **Until it is closed, read `validate() == ok` as "the bytes
are structurally reachable" rather than "the declared runs fit".**

A one-line-per-backend fix was tried and reverted: the full gate failed ten
tests, because a run's span is SUMMED into the enclosing struct's extent --
so an unsatisfiable sentinel wraps, which is the same class of bug -- and
because the walk cannot tell a frame that is too small from a message that
nests too deeply. `doc/decision/26.676-*.md` has both, the two candidate
designs with their costs, and the second wrap it turned up in the same
walk, which was independent and is kept.

## 3-6. Not yet re-run here

Recorded as reported, with their reproductions in 26.675's appendix:

- **`varint x[n]` is generated as a single varint.** C reads 3 bytes where 5
  are expected; C++ fails to build on an undeclared `x_len()`.
- **`u8 t[remaining] max N` after a dynamic member is not enforced.**
  `validate` returns OK with 4 bytes against `max 2`. Note `edges.situ`'s
  `capped_tail` covers the cap but not the cap *after a dynamic member*,
  which is the same corpus-shape gap 26.675 is about.
- **C: `[max]` on a varint in a variant-arm struct** generates a call to a
  `_count_get` that is not emitted, so the C does not compile.
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
