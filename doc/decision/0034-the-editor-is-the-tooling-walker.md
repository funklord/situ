# 0034: the editor is the tooling walker, in three frontends over one core

Status: accepted; the read-only half and the in-place write are built for
the two frontends that
are Python -- the core, `situ-edit` and `situ-edit-tui`. The GUI is not, and
the reason recorded here first was wrong: it said no Qt binding was
installed, from a check that piped a successful import through `head -1` and
read the silence as a failure. PyQt6 is installed and Qt6's C++ headers are
too. The real blocker is that the GUI is to be C++ (and the whole point of a
walker is an embedded program), so it wants a C walker that does not exist --
see `doc/decision/0035-the-embedded-walker.md`.
Date: 2026-08-08
Phase: unscheduled; depends on 26.99 (rung 2)

## Context

`situ-walk` reads a packed image over live bytes and prints what it can. It
was justified from a device -- decision 0026's radio, whose framing has to
change without a firmware rebuild -- and it has stayed that shape.

26.33 recorded that the format's consumers pull opposite ways: "an embedded
walker in a fixed arena wants a small byte-addressable table, a tooling walker
wants names and capability vectors." That is why the image splits into a core
and a `--metadata` tail. **The tail has been carrying names for a reader that
does not exist.** An editor -- open a capture or a file, open a `.situ`, see
the fields, change one -- is that reader.

It is worth building for a reason no template-driven hex editor can copy. 010
Editor, Kaitai's IDE and Wireshark all do "open bytes, open a description, see
fields". None carries **capability reasoning**, so none can grey out a setter
that does not exist, say that the field just edited cannot be written in place
and what that costs, or show the blame chain for why. situ computes all of
that already and currently only prints it. An editor is the first surface
where the capability map stops being a document and becomes the interface's
behaviour.

## Decision

**One core, three frontends, and the core is where the editing model lives.**

A CLI, a TUI and a GUI, as the other projects here have. What matters is that
none of them contains an editing rule: opening an image, placing a field,
changing a value, recomputing what that invalidated and re-answering
`validate` all belong to one module the three drive. Three frontends each with
their own idea of what a write costs is the failure `traverse.py` and
`situc/relation.py` both exist to prevent, arriving a third time.

**The CLI is the reference frontend and the test harness.** Nothing the TUI or
GUI can do may be absent from it. That is not tidiness: an interactive
frontend is hard to test and a scriptable one is not, so the rule is what
keeps the other two testable -- drive the core through the CLI and the
coverage transfers. It also keeps the editor usable from CI, which is where a
"does this capture still conform" check belongs.

**Three binaries, not one with flags, and the reason is the dependency.**
`situc` needs Python and nothing else, by policy, so that the toolchain
vendors into an embedded build environment as a directory copy. A GUI needs
Qt. Putting all three in one binary would put Qt in the path of somebody who
wanted to set a byte in CI. So the CLI and the TUI keep the no-dependency
property -- `curses` is in the standard library -- and only the GUI takes a
third-party dependency, in its own binary and its own package.

**Qt Widgets for the GUI**, per `harmonization.md`, which settles this
workspace-wide and is not reopened here. It makes situ the fourth Qt project,
which turns that document's open question about sharing Qt code from
hypothetical into concrete. Raise it there rather than answering it here.

## Opening a `.situ` does not link the compiler

The request is to open a schema, and 0026 keeps the compiler and the
interpreter apart -- a boundary that is load-bearing rather than tidy, because
it is what keeps "an offset is a constant" and "an operation is absent, not
refused" true of what `situc` generates.

Both hold at once: **the editor reads images, and opening a `.situ` runs
`situc pack` first.** The separation becomes a process boundary rather than a
link-time one, exactly as 0026 made it a binary boundary. A machine with no
compiler opens a pre-packed image and loses nothing but the convenience.

## Writing is rung 2, interpreted

This is the part that is not a small addition to `walker/walk.py`, which today
has no write path at all -- no setter, no mutation, nothing.

Writing a field is where every axis the lattice describes stops being a
description:

| what is written | what it drags in |
|---|---|
| a fixed scalar in place | the easy reverse of `read_scalar` |
| anything that shifts layout | the invalidation model (12.3), which in generated code is a generation counter and in Rust the borrow checker |
| a tag-covered field | the tag is stale (14.2); recompute or refuse, and the image already carries coverage |
| a field an invariant reads | the invariant has to be *maintained*, not merely checked |

So the honest framing: **an editing walk is `--layer edit`, interpreted rather
than compiled.** The layer ladder of 0032 applies to the walker as much as to
the backends, and this is the ladder projected onto the interpreter rather
than a new axis. Today's `situ-walk` is `view`.

That makes the dependency real and unskippable: the editor's write path needs
26.99, and 0031's five storage cases acquire a sixth consumer. An editor built
before that would invent its own answer to what a shifting write costs, which
is precisely the answer the ladder exists to give once.

**A read-only editor is worth shipping first**, and is not blocked: open, see
the tree, see offsets and capability vectors, see `validate` and every probe
`walker.report.SUPPORTED` names, follow a relation between two messages
(26.95). That is a real tool, it exercises the metadata tail nothing has
exercised, and it is how the three frontends get built and tested before the
hard half arrives.

## Alternatives considered

**A `situc` subcommand.** Rejected: it links the compiler into the walker,
which 0026 forbids for reasons that are about the generated code rather than
about packaging.

**One binary, three modes.** Rejected on the dependency, above.

**Frontends over the schema rather than the image.** Rejected: it makes the
editor a second implementation of the layout solver, and the image exists so
that there is exactly one.

**Editing without the ladder** -- letting the editor decide for itself what a
shifting write costs. Rejected, and this is the one worth restating: it would
be a second answer to a question 0031 and 26.99 answer, in the component whose
input is least trusted.

## Consequences

- A new core module, and three frontends over it. None of the three holds an
  editing rule. **Built: `editor/` plus `situ-edit` and `situ-edit-tui`.**
  The shared opening and rendering live in the core rather than in the CLI --
  the first attempt put them in the CLI and had the TUI import that script by
  path, which is a worse answer wearing the shape of a better one. "The CLI
  is the reference" is a statement about what it can do, not about where the
  code lives.
- The CLI is complete by construction, because the other two are specified
  not to exceed it.
- The GUI is the first thing in this repository with a third-party
  dependency, in its own binary and its own package.
- `harmonization.md`'s question about sharing Qt across projects becomes
  concrete. It is raised there, not answered here.
- The write path is blocked on 26.99. The read-only editor is not, and should
  come first.

  **Amended 2026-09-01 (26.179): the first row of the table above is built.**
  A fixed scalar written in place is "the easy reverse of `read_scalar`" and
  it is `--set name=value` now, refusing what the schema forbids, what does
  not fit the member's width, and what the image was never told. The other
  three rows stand: a shifting write is still blocked on 26.99 and is refused
  by measuring rather than by analysis -- the write goes into a copy, the copy
  is walked, and every member's offset and size are compared, so a write whose
  *consequence* shifts the layout is caught however it arrives. udp's `length`
  is that case: a fixed scalar in place whose value decides the payload's
  extent. A tag-covered write happens and is reported stale, since 14.1 puts
  computing a checksum with the caller and this tool is not the exception.
- The metadata tail finally has a consumer, which means `--metadata` stops
  being a flag nothing reads and starts being one something depends on.
