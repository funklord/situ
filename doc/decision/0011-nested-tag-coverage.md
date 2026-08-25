# 0011: nested tag coverage recomputes innermost first

Status: accepted
Date: 2026-07-27
Phase: 8

Resolves open question 2 of project.md section 27, which section 27 marks
"decide before phase 8".

## Context

Section 14.1 permits several tags in one struct, each covering "a disjoint or
nested set of regions", and makes overlapping non-nested coverage an error.
Open question 2 accepts nesting as coherent and leaves the recomputation order
open:

> Nested (an inner tag over a subrange of an outer tag's range) is coherent but
> the recomputation order matters.

It matters because a tag is bytes in the message. An outer tag covering a
region that contains an inner tag covers the inner tag's own bytes. Recompute
the outer one first and the inner one is then written, changing bytes the outer
tag has already authenticated -- and the outer tag is stale again, silently.

## Decision

**Innermost first.** A tag whose coverage is a subset of another's is
recomputed before it, and `Placement.covered_by` is ordered accordingly, so the
generated code and the capability map both read in the order the work must be
done.

Innermost is narrowest: coverage is disjoint or nested by the rule above, so
comparing the size of two coverage sets is enough to order them, and two tags
covering the same number of regions cannot be nested in each other. Declaration
order breaks the remaining ties, which are all disjoint and therefore free.

Only this order terminates. Under it an inner tag is final before the outer one
reads its bytes, and nothing an outer tag writes lies inside an inner tag's
coverage -- a tag sits outside the regions it covers, which is enforced
separately.

## Alternatives considered

**Reject nesting in phase 8 and defer it.** The conservative reading, and
tempting: disjoint coverage is what real protocols mostly use. Rejected because
section 14.1 already promises nesting works, and because the question is not
actually hard once stated -- there is exactly one order that terminates, so
deferring would be deferring a decision that makes itself.

**Let the schema state the order.** A `recompute_after` clause, or ordering by
declaration. Rejected: it is a choice with one correct answer, and offering it
as a knob means offering the wrong answer. Section 17.0's rule is that where a
default exists, the safe option is the silent one.

**Recompute outermost first and iterate to a fixed point.** Correct in the
limit and obviously wasteful; it also has no fixed point when a tag's own bytes
are inside its coverage, which is a case the compiler rejects rather than
iterates over.

## Consequence

The capability map lists a covered field's tags innermost first, and generated
code that clears dirty bits in the order they are listed is correct by
construction. A future construct that made coverage sets incomparable would
break the size-ordering argument and needs this revisited.
