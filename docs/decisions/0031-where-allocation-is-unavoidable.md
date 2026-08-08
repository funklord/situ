# 0031: where allocation is unavoidable

Status: the enumeration is accepted; the mechanism is superseded in part.
Its proposed schema directive was withdrawn the same day by decision 0032,
which puts the choice at `situc build --layer` instead. The five cases below
are what that layer boundary is drawn around.
Date: 2026-08-08
Phase: unscheduled

## Context

Section 2 says: "No dynamic allocation in generated code. Ever. No `malloc`,
no hidden arena, no growable buffers. Callers supply memory." It is cited as
invariant 4 in at least four places that decide API shape, and decision 0026
leans on it for why an offset is a constant and an operation is *absent*
rather than refused.

The question is whether that rule should be relaxed for accessor families
other than the zero-copy views -- `fuzzypickles` cannot adopt situ because its
225 call sites hold decoded structs that outlive the buffer -- and the
governing constraint is that situ is meant to be generic. A protocol that
exists and needs a growable buffer is a protocol situ should be able to
describe.

So the question is not "can we avoid allocating". It is **which cases
genuinely cannot be served without it**, so that the ones that can are not
paying for the ones that cannot.

## The enumeration

The size axis already computes this per member. Sorting by it:

| | size axis | worked example | storage that serves it | situ allocates? |
|---|---|---|---|---|
| **A** | `Fixed` | a fixed header | caller's stack | no |
| **B** | `Bounded`, small | `u8 name[len]`, `len: u8` | inline array at `SITU_X_SIZE_MAX` | no |
| **C** | `Bounded`, large | `len: u32`; sqlite's varint rowid at 2^64-1 | measure, then caller-supplied buffer | no |
| **D** | `Unbounded`, measurable | `u8 name[] until "\0"` with no `max`; `remaining`; an uncapped `tlv` run | measure, then caller-supplied buffer | no |
| **E** | `Unbounded`, not measurable | a `coded` region with `expansion = unbounded` | nothing else works | **yes** |

**A and B need nothing.** `fuzzypickles`' own example puts the decoded struct
on the caller's stack, which is case A. Case B is how `--materialize` already
works: 26.30 emits `start[CAP + 1]` and the cap comes from the schema's `max`.

**C is bounded in theory and not in practice.** The constant exists but cannot
be inlined -- `gen-fuzz` already met this, declaring `uint8_t
buf[SITU_X_SIZE_MAX]` for a struct whose maximum is 2^64-1, "a constant too
large for its type before it is an allocation nobody can make", and capping at
4096. An arena serves it because the *actual* extent is knowable by measuring
first.

**D is where the two-pass primitive already exists and is not being counted.**
Every generated header carries

```c
static inline situ_err_t situ_X_required(const uint8_t *data, uint32_t have,
                                         uint32_t *need);
```

which answers "how many bytes does this need" from the bytes themselves. That
is precisely the primitive that lets a caller allocate exactly without situ
allocating at all. Measure, allocate, decode. It is already generated for
every struct, including the fixed ones, on the stated reasoning that a caller
framing a stream should not write one loop for the fixed messages and another
for the rest.

**E is the only case that defeats measuring, and it defeats it in principle.**
A decompressing codec cannot report its output extent without performing the
decompression. There is no measure pass, because the measure pass *is* the
work. This is not exotic: deflate under HTTP content-encoding, PNG `IDAT`, any
compressed payload inside a described frame.

So: **one case out of five genuinely requires allocation, and it is real
enough to build for.** The other four are served by `_required()` plus
caller-supplied storage, which is what Section 2 already says.

## The Python exception, which was misread before it was checked

**Section 2's "Ever" was already false, and the exception was undocumented.**
26.30 records what the four backends emit for a materialized run:

| | `max` needed? |
|---|---|
| C, C++ | yes |
| Rust | yes -- `no_std` has no allocator either |
| Python | **no** -- `x_all()` returns the elements as a list |

The cap "is not part of the idea. It is C's refusal to allocate, arriving in
three languages that share it and absent from the one that does not."

Read from that table alone, the Python backend looks like it allocates freely.
It does not, and the code is the witness: `situc/codegen/python/emit.py`
emits `and len(starts) <= {repeat_cap}` into the walk's loop condition
wherever the schema declares a `max`, so Python enforces the schema's bound
exactly as C does. Absent a `max`, the walk is still bounded by the buffer and
by refusing to advance on a zero-extent element.

What Python allocates is therefore proportional to the elements actually
present -- the same quantity C's index array holds. The real difference is
that C requires that quantity be a compile-time constant and Python does not.
That is the language, not a defect, and the only available "fix" would be to
make Python refuse `x_all()` on an uncapped run so that all four backends
demand `max`. 26.30 rejected that, correctly: crippling a backend that does
the thing properly, to match another backend's limitation, is bending the code
around a model that does not fit it.

Section 2 and invariant 4 now say this. Both were changed together, because
two statements of one rule is how they come to disagree.

## Proposed mechanism

Everything in this section is a proposal, not a settled decision.

**An allocator the caller supplies, never `malloc`.**

```c
typedef struct {
	void *(*alloc)(void *ctx, uint32_t size);
	void  (*free) (void *ctx, void *ptr, uint32_t size);
	void   *ctx;
} situ_alloc_t;
```

This is a stronger reading of "callers supply memory", not a retreat from it:
the caller supplies the policy rather than one buffer. A freestanding target
passes a pool; an audit reads one struct to find every allocation; a `NULL`
allocator is a refusal with a failure class rather than a crash.

**~~A directive~~ -- withdrawn 2026-08-08 by decision 0032.** This record
proposed `allocation none | caller | dynamic` as a schema directive alongside
`target` and `strictness`. That was the wrong place for it. The choice is a
consumer's deployment decision, and putting it in the `.situ` file makes one
consumer's deployment decision into every consumer's wire contract -- a schema
describes bytes.

0032 replaces it: `situc build --layer view` guarantees invariant 4 and
`--layer edit` is where storage becomes available, chosen at invocation. The
allocator struct above stands as layer 2's mechanism; the directive is gone.

The enumeration in this record is unaffected and is the reason layer 2 exists:
cases A through D are what `--layer edit` must serve without allocating, and
case E is the one that leaves it no choice.

**The trap is a predicate that already exists and is currently empty.**
Section 16 lists `no_alloc(X)`, and Section 16's own table records why it does
nothing: "generated code never allocates (invariant 4), so it always holds;
the predicate would be a lint, not a requirement". Under this proposal it
becomes decidable, and `require no_alloc(payload);` becomes a real build-time
gate with the existing blame chain behind it. The slot was left open; this
fills it.

**A schema at `allocation none` that contains case E is refused at compile
time**, naming the construct and the remedies the advisor already knows --
Section 18 lists "pin an unbounded region's max" as a remedy today.

**A new lattice axis, `alloc: None < Caller < Dynamic`,** computed per member
like every other axis. That is what makes an incorrectly-written schema
*visible* rather than merely refused: allocation appearing where there was
none becomes a diff in the committed capability map, which is the mechanism
`situc diff` already provides for every other axis.

That axis survives 0032 and is the per-member form of what 0032 calls
`layer_floor`: a member whose `alloc` is above `None` is a member that cannot
be emitted at rung 1, and the schema's floor is the highest such value in it.
Both are properties of constructs rather than of an invocation, which is why
the capability map carrying them stays identical at every rung.

## What this does not decide

- Whether Section 2 changes, and how it states the Python exception it
  already has.
- Whether the owned-decode accessor family `fuzzypickles` needs is worth
  building at all. It is mostly cases A and B, so it is largely independent of
  this record -- which is the point of the enumeration.
- The eighth failure class an exhausted allocator would need, and whether it
  is worth the cost of arriving in four runtimes. `SITU_ERR_TRUNCATED` was
  added by hand to all four, and nothing would have caught a fourth omission
  until `test_the_failure_classes_match_the_runtimes` was written.
