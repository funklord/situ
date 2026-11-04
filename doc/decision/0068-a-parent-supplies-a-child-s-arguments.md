# 0068: A parent supplies a child's arguments, by name, at the member

Status: accepted 2026-11-02 -- designed at the copyright holder's request
("design element arguments for runs") and accepted by the same ("build
0068"); built in the steps below, each recorded from 26.797 on
Date: 2026-11-01
Supersedes: nothing. Settles the half 0050 left open ("lifting the refusal
on a struct that takes an argument being a member of another, which needs
a decision about how a parent's arguments compose with a child's").

## The problem

0050 lets a struct take an argument, `parameter u8 block [stream];`, and
lets a caller supply it when acquiring a view. What it does not let is
nesting. A struct that takes an argument cannot be a member of another,
whether nested, an arm, or an element of a run, because the parent's
accessor builds the child's view and has nothing to pass. Since 26.794 that
includes a struct naming itself.

So a run cannot hold elements whose layout or meaning depends on something
the run's struct knows, and real formats have that shape:

- An MMC response carries records whose width follows a card class read
  once at the top.
- A volume holds directory entries whose size follows a block size from
  its superblock.
- A TLS handshake holds extensions read under the negotiated version.
- A struct naming itself (a tree) passes its own argument to each child.

The element-validation work (26.789 to 26.792) made the gap concrete: a
run's elements are validated through their own `check`, and an element
taking an argument has no value to call it with.

## Decision

**The parent names each argument at the member, in its own terms:**

    struct entry  { parameter u16 block [stream]; u8 name[8]; u8 data[block - 8]; }
    struct volume {
        u16    block;
        u8     n;
        entry(block = block)   files[n];        // a run: every element gets it
        entry(block = 512)     boot;            // a nested member, a constant
        variant body switch (kind) {
            case 1: entry(block = block) only;  // an arm, the same rule
        }
    }
    struct node [depth = 8] { parameter u8 b; u8 n; node(b = b) kids[n]; }

1. **Syntax: `Type(name = expr, ...)` in a member's type position.** Named
   rather than positional, as `indexed(count = ..., base = ...)` already is:
   a parameter is a member with a name, the call survives the child's
   parameters being reordered, and "every parameter exactly once" can be
   checked with a diagnostic that names the missing one. One spelling only.
   A user type is followed today by the member's name, never by `(`. The
   words that do open a member with `(` (`pad_to`, `pad_random`,
   `indexed`) are keywords, read before any type. So `Type(` is new grammar
   rather than a second reading of old grammar. (A struct may be DECLARED
   with a keyword's name, `struct indexed { ... }`, but it can never be
   used as a member, because the keyword wins. That is true today and 0068
   does not change it.)
2. **Every parameter is supplied exactly once.** A missing, unknown or
   repeated name is refused, naming it. There are no defaults: a default is
   a value nobody states, which is what 0050 exists to prevent.
3. **An argument is an expression in the PARENT's scope, at the member.**
   It may read what a size expression there may read: a parent field that
   precedes the member, a parent parameter, a `const`, an enum value. The
   reason is the same one that makes a length field precede what it sizes,
   since every reader has to know the value before it can place the child.
   A field after the member is refused, as it is for a size.
4. **For a run, the argument is evaluated once and every element gets the
   same value.** An argument that varies per element, such as the element's
   index or something read out of the previous element, is not a construct
   any format in front of us needs, and it would make each element's extent
   depend on its neighbours. It is rejected here and can be a later record
   if a format asks.
5. **Provenance follows 0050's one refused cell.** A child parameter marked
   `[stream]` may move bytes, so its value has to be one every reader can
   know. That means a constant, a parent field (in the message, which a
   dissector reads like any other), or a parent `[stream]` parameter. A
   parent parameter WITHOUT `[stream]` is a per-message fact from outside,
   and binding it to a byte-moving child parameter is refused, as 0050
   refuses it on a size directly. A child parameter without `[stream]`
   (meaning only) may take any of them.
6. **A struct naming itself writes its own arguments out.**
   `node(b = b) kids[n]` passes the child what the parent was given, and
   nothing is inferred from matching names.

## What each description does with it

- **Layout and capability map.** The member's row names its arguments
  (`args block = block`), as the map names a parameter (26.409). A parent
  field bound to a byte-moving child parameter becomes a DRIVER of the
  child's extent, exactly as a field named in `[len]` is, so it gets that
  field's mutate and access rows. That is the one place the lattice
  changes, and it reuses a rule rather than adding one.
- **The four backends.** The child's view constructor already takes the
  argument (0050). The parent's accessor renders each argument through
  `_over_fields` in the parent and passes it. The same rendered tail goes
  wherever the child's `extent`, `check`, `required` or element walk is
  called: the parent's offset chain, `validate`'s nested and element
  checks (26.789 to 26.792), and framing. 26.408's rule holds: a call site
  and the definition it calls ask one function which arguments travel.
- **The packed image and both walkers.** A new section, ARGUMENTS, with one
  row per (member, child parameter): the parameter's placement and the
  bytecode of the expression, compiled against the parent the way a size
  program is. A walker evaluates the row when it descends and hands the
  value to the child view, which `acquire` already takes (26.403).
- **The wire signature** prints the member's arguments. A change to one is
  a change to the bytes and is reported as such.
- **The dissector** passes the value into the child's subtree function. A
  parent-field source is read from the packet, and a `[stream]` source is
  the preference it already has. Rule 5 is what guarantees there is never
  a source it cannot reach.
- **`situc verify` and the generated checks** need nothing new beyond the
  top-level `--arg` they have. A nested argument comes from the bytes or a
  constant.

## Alternatives rejected

- **Positional arguments, `entry(block) files[n]`.** Shorter. Rejected
  because it binds by order, so reordering a child's parameters silently
  rebinds every call, and because it is a second spelling beside
  `indexed`'s named one.
- **Implicit binding by name**, where a parent field or parameter called
  `block` feeds a child parameter called `block`. Rejected as action at a
  distance: renaming a parent field would silently disconnect the child.
  26.794's accepted-and-uncompilable schema is that failure in miniature.
- **Arguments as an attribute, `files[n] [with block = block]`.**
  Rejected because attributes describe the member, and these are inputs to
  its type. `Type(...)` keeps them on the type, where `interleave(16)` and
  `indexed(...)` put theirs.
- **Inheriting all of the parent's parameters.** Free for a tree and
  wrong for everything else: a parent rarely has the child's parameters,
  and where it does they need not mean the same thing.

## Building it

In this order, each step leaving all six descriptions agreeing and the
corpus generating, as 26.409 found the only safe order to be:

1. Grammar, AST and well-formedness: rules 1 to 3, 5 and 6, with the
   member refusal still in place in every generator, so nothing half-built
   is reachable. A corpus struct goes into `edges.situ` at the same time,
   so the sweeping generators fail if they mishandle it (26.402).
2. A single nested member, in all four backends and both walkers, plus the
   map and the wire signature. The ARGUMENTS image section lands here.
3. Runs: counted, `[remaining]`, `while`, delimited and `indexed`. Element
   accessors, extents and element validation all pass the tail.
4. Variant arms.
5. A struct naming itself, which needs the depth-carrying forms of every
   function in step 3.
6. The dissector and framing.

Each step lifts the refusal for exactly the shapes it builds and no more:
"a construct that is accepted and cannot be built" is the failure 26.794
just closed.

## What is open

The syntax was accepted on 2026-11-02, and steps 1, 2, 3, 4 and 6 are
built (26.797, 26.799, 26.801, 26.802, 26.804, 26.809). What is not:

- **Step 5 for a SINGLE nested member is built** (26.812): an argument
  that hands on one of the parent's own parameters, where the member is
  one nested struct rather than a run. C++, Rust and Python needed
  nothing -- the parent's argument is already on its view -- and C needed
  the `_extent_from`, `_extent`/`_extent_at` and `_view` trio to take the
  parent's tail, with every call to them passing it.

  **What made it a defect rather than a gap is worth keeping**: the
  signature and the body were decided from different text.
  `_member_reads_arguments` asked the member's base and length
  expressions, while the bodies it decides the signature for are built
  from those AND `_child_args` -- so for `relay { parameter u8 block;
  negotiated(block = block) one; }` the base is `0u`, the length is a
  call into the helper, and the argument appeared only inside that
  helper. The predicate said no, the signature took no tail, and the body
  embedded `arg_block` anyway. Widening that predicate was the wrong fix
  and is recorded because it looked right: it is shared with the member's
  other accessors, and a run's `_count` reads the count field rather than
  the argument, so widening made its call sites pass a tail it does not
  take. The narrow, cycle-free `_hands_own_argument` answers only what the
  three helpers need. Cycle-free matters: asking the wider predicate there
  recursed without end, because it renders the length expression and a
  nested member's length expression renders a call whose tail that
  predicate decides.

- **Step 5 for a RUN of them is built too** (26.815). `_count`, `_at`,
  `_span_from`, `_span` and `_fits` all take the parent's tail now, and
  the refusal for a run is gone from `check_parameter_nesting`.

  `_count` was the work. It has four branches -- a literal count is a
  `#define` with no function, `[remaining]` over elements of one size is
  a division, `[remaining]` over elements of no single size is a walk,
  and a counted run is an expression over the count field -- and each is
  a different function, so one predicate cannot answer for all four. The
  branch that emits the definition now records what to pass and the call
  site reads that, rather than asking the member a wider question whose
  answer happens to differ.

  Two things found by the compiler rather than by reading: `check`'s call
  to a run's `_fits` passed a bare `view` while the line below it passed
  the argument to `_span`, two adjacent lines of one emitted function
  disagreeing; and the division branch was broken for a run whose
  element takes no argument at all, which is 26.814 and is not part of
  this step.

  `edges.situ` carries three of the four branches (`relays_on_runs`,
  `relays_on_fixed`, `relays_on_rest`) and 26.814 carries the fourth.
  The bytes were read back through C and through Python independently.

- **A struct naming itself** is still refused, as is one that can contain
  itself through any chain: a recursive child is measured with a depth,
  and handing arguments down that path is the other half of step 5.
- **An `indexed` region's element is built** (26.859), except for a
  parent's own parameter handed on.
- **A member inside a sealed or coded region** that takes arguments is
  refused. One inside a `positional`
  or `authenticated` block is built (26.811).

  **How far from built each is was measured rather than guessed**
  (26.819), by lifting the refusal in a throwaway export the obvious way
  -- admitting the interior to `wellformed._transparent_members`, which
  is the route 26.811 took. The three answers differ:

  - **`sealed` compiles and reads the wrong byte.** The interior's
    accessors sum the extents in front of a member as though it were the
    struct's own, so `k.mark` inside `kid(b = b) k` lands at `2b + 1`
    where it sits at `b` -- inside the authentication tag, with
    `situ_in_bounds` passing. That is the refusal earning its keep, and
    `_transparent_members`' own docstring had already said why: a
    region's interior is a transform's input and is placed against the
    nested member's view, not the parent's.
  - **`indexed` is one missing argument**: the `_at` emitter calls the
    element's extent without `_child_args`.
  - **`coded` emits no interior accessors at all**, so the lifting
    produced no evidence either way.

  A refusal lifted that compiles is the dangerous outcome rather than the
  encouraging one: the compiler cannot see a wrong offset.
- Rule 4 rejects per-element arguments. If a format needs the element's
  index, that is a later record.
