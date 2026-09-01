# 0047: a `file` target, and which assumptions it may carry

Status: accepted, and built
Date: 2026-09-01
Phase: raised by the copyright holder while reading `situ-edit`;
implemented the same day (26.175)

## Context

The proposal is a third target beside `buffer` and `mmio`:

    target file;

so that a schema describing data situ works on *in a file* can say so once
rather than restating the consequences, with `append` and sparse-file support
as further shorthands. The motivation is `situ-edit`, which reads a message
and shows its fields, and which could edit one in place -- replacing a class
of hand-written tools that each rediscover a format's rules.

**A target is already a bundle of assumptions, not only a capability
distinction.** The compiler says so in its own diagnostic: "`target mmio`
makes `volatile` implicit and `access_width` ...". So "a shorthand that brings
in assumptions" is what a target is for, and the question is not whether a
target may do that but which assumptions are true of the medium.

Section 9.8 contains the phrase "no `target file` for it to hang off", and it
is worth saying what that argument was and was not. It concerns `at expr`:
BMP's `pixel_offset`, TIFF's `ifd_offset` and DNS name compression all wanted
a member placed where the data says, and two of those are file formats while
one is a protocol -- so *that construct* does not follow the file line. It is
not a finding that the medium has no consequences.

## The measurement

Seven file-format examples, with five wire formats as a control. Counts are
of resolved members.

| schema   | memb | in place | shifting | stable | extent    |
|----------|-----:|---------:|---------:|-------:|-----------|
| bmp      |   35 |       34 |        1 |     34 | fixed     |
| cpio     |   33 |       29 |        4 |     30 | fixed     |
| dnsname  |   28 |       17 |       11 |     26 | bounded   |
| keystore |   20 |       16 |        3 |     15 | fixed     |
| pickle   |    2 |        1 |        1 |      2 | bounded   |
| sqlite   |    9 |        7 |        2 |      7 | unbounded |
| tiff     |    3 |        3 |        0 |      3 | fixed     |
| **file** |**130**| **107** |   **22** |**117** |           |
| udp      |   11 |        8 |        2 |     11 | bounded   |
| tcp      |   25 |       21 |        3 |     24 | unbounded |
| dns      |   13 |       13 |        0 |     13 | fixed     |
| icmp     |   21 |       20 |        0 |     21 | fixed     |
| mqtt     |  139 |       81 |       58 |     96 | fixed     |
| **wire** |**209**|         |          |**165** |           |

Two members across the twelve are `Immutable`. Four things fall out of it.

**1. `effect` is `Pure` for every member of every example, on both sides.**
Across the whole tree it is 1075 `Pure`, 25 `EffectOnRead` and one
`EffectOnWrite` -- and those 26 are the `mmio` register example. Outside a
register, *nothing in situ has ever had an effect*. A write to a mapped file
is durable and visible to other processes, which is exactly `EffectOnWrite`,
and it is the one assumption that is true of files as such and cannot be said
today. It would move 130 of 130 file members and none of the 209 wire ones.

**2. The editor does not need the target.** The 130 members already partition
into 107 that may be written in place, 22 whose write moves the bytes after
them, and one the schema refuses to let anyone write -- and `auth=Covered(t)`
already marks the writes that invalidate a tag. Everything `situ-edit` must
decide per field, the capability map decides now.

**3. `append` is not a default.** It would flip 117 of 130 addresses from
`Stable`/`FrameStable` to `Unstable`, because a resize invalidates every
outstanding pointer -- a real lattice consequence, which is the argument for
it being a flag rather than an implication. And it is *needed* by one of the
seven: only `sqlite` has an unbounded top-level extent. A default that is
wrong for six cases in seven is not a shorthand.

**4. Sparse has nowhere to land.** A hole reads as zeros cheaply and the first
write into it allocates and may fail. That makes a write's cost and its
fallibility depend on *where within a member* it happens, and no axis varies a
property by position. This is a lattice question before it is a target
question.

A fifth, incidental: `at expr` is used by exactly one committed schema
(`bmp`). Section 9.8 names three formats that wanted it and the tree exercises
one, which does not weaken the argument -- the point was that the three do not
share a medium -- but the record should say which of the three is real.

## Decision

**Add the target. Give it one default and a legality gate, and keep `append`
opt-in and sparse out.**

- `target file` sets `effect := EffectOnWrite` on every writable member. This
  is the assumption that is true of a file as such, is currently
  inexpressible, and is what tells a generated API that a setter is not a
  pure store.
- `target file` refuses what cannot live in a file: a `register` struct, and
  the `mmio`-only attributes. The mirror of the refusal `target mmio` already
  makes for a buffer layout.
- An unbounded top-level extent requires `append`, because without it the
  schema describes a message with no end in a medium that has one.
- `append` sets `address := Unstable` throughout and makes the top-level
  extent growable. It is written, not implied.
- **Sparse is not in this decision.** It needs a position-dependent property
  the lattice does not have, and inventing one to serve a target is the wrong
  order.

## Alternatives considered

**Do nothing: a file is a buffer.** This works today and should be said
plainly -- `mmap` the file, hand situ a view, and every accessor is the same
arithmetic. What it cannot do is state that a write is durable, and it cannot
refuse a schema that could never live in a file. Both of those are the gate's
job, and a target is where the gate learns the medium.

**Make it a driver instead (0033).** Transports are the third axis, and a file
backend has the shape of one -- `io_uring`'s rings are `mmap`ed and that is a
driver, not a target. The counter is that a driver is chosen when the code is
built, while these are static properties of the *data* the schema describes:
whether a write is durable is not a function of which event loop is linked.
The two are compatible -- a file driver may still be wanted -- but the
assumptions above belong to the schema.

**Model fallibility instead of the medium.** The sharpest gap is that a mapped
file can fault under a live view: another process truncates it and a load
becomes a `SIGBUS`, which no bounds check prevents, and which dents the
promise that a view is acquired once and every accessor after it is
arithmetic. That is real and it is *not* file-specific -- a shared-memory
segment another process can shrink has it too. It deserves its own record
rather than being smuggled in under a target, and it is the reason this one
does not claim `target file` makes access safe.

**Bundle everything: `file` implies growable and durable and sparse.** This is
the version to refuse. `check_attribute_places` exists on the ground that "a
schema that states what the generated code does not enforce is worse than one
that states nothing", and six of the seven examples are not growable. A
shorthand may carry what is true of the medium and must not carry what is true
of some of its instances.

## What was built

`TargetKind.FILE`, and `append` as a flag on the target directive rather than
a directive of its own, so it sits beside the thing it modifies and is refused
on the two targets that cannot grow.

Two rows in section 11.3's table, which is normative and checked:
`file-durable-write` and `file-append`. The suite refused both until they were
listed there and named as tested, which is the table doing its job.

The refusal for an unbounded top-level extent lives in `resolve` rather than
`wellformed`, because the extent is what `solve` computes and `wellformed`
reads the AST -- beside `_check_host_dependence` and the other
layout-dependent checks.

**One deviation from the decision above, and it is a narrowing of the
reasoning rather than of the effect.** This record says `effect :=
EffectOnWrite` on "every writable member". The row fires for every member,
because there is no predicate for "writable" at that point: `Immutable` is
reached by four separate rows and a row cannot see an axis another row is
deciding. It does not need to. Durability is a property of the medium, and
whether there is a store at all is what `mutate` says -- the same separation
14.2 already makes between mutation being possible and its invalidating a
tag. A tag nobody may write still reports what a write would do.

## Consequences

- The `effect` axis is exercised outside `mmio` for the first time, which is
  worth watching: 26 members in one example is thin coverage for an axis about
  to be set on every member of a whole class of schemas.
- Seven committed examples become candidates to re-declare, and their maps
  would change. Whether they *should* is a separate question: `bmp` describes
  the BMP format, not a particular BMP file, and the target says where the
  bytes are worked on rather than what they are.
- `situ-edit` gains a reason to refuse a write, rather than only a reason to
  show a field. It does not need the target to gain the first one.
- Fallibility and sparse both stay open, and both are lattice questions.
