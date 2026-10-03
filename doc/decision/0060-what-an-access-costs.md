# 0060: what an access costs, and where that belongs

Status: accepted
Date: 2026-10-03
Phase: raised by `raidcfgd`, from a decode that was correct and unusable

## Context

**`raidcfgd` asked for the cost and pointed at the axis that carries it.**
Their words: *say in the generated header which access pattern costs what.
The map already says `access=Sequential`; it does not say "exponential in
k".*

The exponential half was 26.555 and is fixed. What remains is the question,
and it turns out to have been aimed correctly: `access=Sequential` IS where
the cost they hit lives, and the map understates its consequence by a
factor that grows with the data.

**Measured here, C at `-O2`, a run of four-byte elements:**

    n       _at(i) for every i      a cursor      ratio
    16            0.0022 ms        0.0001 ms        18
    64            0.0256 ms        0.0003 ms        73
    256           0.4017 ms        0.0014 ms       288
    1024          6.5589 ms        0.0056 ms      1177

**6.56 ms to read a four-kilobyte message**, because `_at(index)` walks
from the start every time: reaching element N is O(N), so a loop over every
element is O(N^2). `access=Sequential` says *cannot reach element N
directly* and a reader takes that for a constant factor.

**The member chain is a different axis with the same shape**, and this is
worth separating because the two get confused. Reaching member k of a
struct whose preceding members are variable-length is `offset=Dynamic`, not
`access=Sequential`. Measured on a struct of k `str` members, reading every
member once:

    k      every member via _offset    one pass via _extent_from    ratio
    8             0.00036 ms                  0.00005 ms              7
    16            0.00142 ms                  0.00010 ms             14
    32            0.00656 ms                  0.00019 ms             35
    64            0.02415 ms                  0.00037 ms             65

The totals computed are identical at every k, which is how the two routes
were shown to agree rather than merely to differ in speed.

## Decision

**Four things, and the first two are the answer to the question as asked.**

**1. The generated header says what an access costs, beside the accessor
that has the property.** A walked element accessor carries O(index), the
O(n^2) consequence of looping over it, and the cursor to write instead. All
four backends; the indexed case says nothing, because for a fixed-size
element the sentence would be noise.

**2. The map states the cost CLASS and not the caller's loop.**
`Sequential` is honest: it is a property of the format that element N
cannot be reached directly. That a loop over all N is quadratic is a
property of the loop the consumer writes, and a capability vector does not
describe the caller. So the number belongs in the header and the class
belongs in the map, which is why this record does not add an axis for it.

**3. Where the number IS static, the map may carry it, and that is a
separate pass.** The offset chain's length is a layout fact:
`offset=Dynamic` under-specifies it exactly as `Sequential` under-specifies
the run. `Dynamic(k)` is the spelling, and the cost is measured: about 120
lines across the committed maps, a numeric-strength rule whose direction is
inverted from `Aligned(n)` -- fewer terms is stronger -- and the normative
table in project.md 11.1 and `doc/capability-axes.md`. **Not taken here.**
It is worth taking: 18.1 promises that a capability regression is *a
reviewable diff at the moment of editing rather than a performance surprise
months later*, and inserting a variable member today lengthens every later
member's chain with no diff at all.

**4. No capability states whether the generated code ACHIEVES the bound,
and nothing should.** 26.555's 3^k chain and the linear one carry the same
vector on every axis. That half is a gate --
`test/unit/test_offset_chains_are_linear.py` -- and a reader must not be
able to conclude from a map that the code is as good as the format allows.
Stated here because the question invites exactly that conclusion.

## What is still owed, and it is a feature rather than a note

**A run has no cursor, and that is the real gap behind
`access=Sequential`.** The member chain has one: `_extent_from(at)` is
public, documented per function, and makes a full read linear. A run has
`_at(index)` and `_span_from(start)` and nothing in between, so a consumer
who wants one pass assembles it from a sub-view and the element's own
`extent` -- which `raidcfgd` did, by hand, and which the header now tells
them to do.

**The shape would be an element cursor** -- `next(view, at, &element,
&next_at)`, or an `_at_from(view, index, known_index, known_at, &out)` --
in four backends, and it is a new public API rather than a correction.
**The copyright holder's.** The cost of not having it is in the table
above; the cost of having it is four backends, four sets of tests, and a
decision about what the signature promises when the walk stops early.

## Alternatives considered

**An `access=Sequential(n)` parameter.** Refused on measurement rather than
taste: the run's length is a count the message carries, so there is no
static n. The axis cannot say what the format does not fix.

**A new `reach` axis.** It would duplicate `offset`, which already reads
*position knowledge, and what it costs to get*. Two axes answering one
question is how they drift.

**Leaving it in `project.md` only.** The reader who needs this is holding a
generated header and reading an accessor's doc comment, which is exactly
where `raidcfgd` was not told.

## Consequences

Every generated walked-element accessor gains four lines of documentation
in four languages. No generated CODE changes, no capability vector changes,
and no committed `.map` or `.wire` file moves -- checked by regenerating
the corpus rather than by reading the diff.
