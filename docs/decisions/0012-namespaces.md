# 0012: namespaces are blocks, one level deep, qualified with `::`

Status: accepted
Date: 2026-07-27
Phase: 8

## Context

Three problems arrived together.

A schema cannot declare two types of the same name, so a file describing an
inner and an outer protocol layer cannot call both headers `header`. The
repository has this today: `tests/schemas/header.situ`, `examples/message/` and
`examples/packet/` each declare a `Header`, all three generate into one
directory, and `tests/generated/Makefile` works around the clash by never
linking them into the same binary -- with a comment saying exactly that.

The C namespace of a schema lived on the command line (`--prefix`, default
`situ`), so `situc build x.situ` produced different output depending on who
invoked it. That contradicts the single-source-of-truth rule of section 25 and
is precisely the sort of thing that drifts between a Makefile, a CI job and
somebody's shell history.

And project.md says only "Prefix configurable" (20.2), with `--prefix=NAME` in
the CLI list. Nothing about the schema.

## Decision

**A block, not a directive.**

```situ
namespace outer {
	struct header { ... }
}
```

Scope is lexical and visible. The alternative -- a repeatable positional
`namespace foo;` -- would have to answer what a second one does to the
declarations before it, and `endian` already answers that question the other
way: `_file_scope` keeps the *last* directive and applies it file-wide, so
`endian native;` on line 3 retroactively rewrites the struct on line 2. Shipping
a second top-level directive kind with opposite semantics would be the actual
dirt. Braces make the question not arise.

**One level, for now.** Nesting is rejected with the phase-gate diagnostic the
language already uses for a construct it does not yet accept, so the boundary is
documented rather than discovered. A file-level namespace is the block form with
the braces around the whole file, so adding nesting later changes no existing
schema's meaning.

**Qualified with `::`, not `.`.** A path is already `Type.field.sub` and the dot
already carries two meanings there. A third would make the head of a path
ambiguous, and six places in the compiler split a path on its first dot to find
the type it starts from. With `::` the head of `outer::header.seq` is still one
name, and those six places are untouched.

**Unqualified names resolve in the current namespace and nowhere else.** No
fallback to the enclosing file. A fallback is a guess, and a schema that
silently picked the wrong `header` would produce a layout that looks right and
is not.

**`--prefix` stacks outside whatever the file declares**, rather than replacing
it. It is called a prefix, so it prefixes. That is also the better semantics for
the case the flag exists for: an integrator vendoring two copies of a schema
wants to tell them apart while keeping the author's structure intact.

**Flattened away immediately after parsing.** Every declaration comes out with a
qualified name and every reference inside the namespace is rewritten to match,
so no later pass learns that namespaces exist. The unparser reconstructs the
blocks from the qualified names, which is the only place the original shape
matters.

## Why not a struct

A struct is a layout. Wrapping declarations in one changes the wire format, so
it can never be used for organisation alone -- which is exactly why C gets away
without namespaces for data and situ does not. Nesting also gives an *instance*
at an offset rather than a *type*: `outer.hdr` is eight bytes at position zero,
where `outer::header` is something usable in ten places.

The nearer alternative is C++'s member types -- letting a struct body hold a
declaration that contributes no bytes. Rejected because it breaks the strongest
reading invariant the language has: every line in a struct body is bytes, in
declaration order. Under member types a reader would have to check each line for
whether it contributes to the layout, and the two forms differ by one word.

## Alternatives considered

**Call it `prefix` and keep it C-specific.** Honest about what it does today.
Rejected: phase 11 adds a Rust backend, which wants a module rather than an
identifier prefix, and we would be explaining for the rest of the project why
the Rust backend ignores a directive called `prefix`.

**Dotted, file-level, one per file, as protobuf's `package a.b;`.** Reads
correctly to the audience situ is aimed at, and needs no indentation. Rejected
on the `endian` precedent above: it is positional, and situ's other positional
directive is not.

**Full C++ semantics now** -- nesting, reopening, unqualified lookup through
enclosing scopes. Rejected as a phase 8 side quest: it changes the path language
that `require`, `covers`, variant discriminants and every size expression all
consume, and none of that is in 26.8's acceptance criteria.

## Consequence

`import` still parses without resolving, so multi-file schemas are ahead of us.
When they land, "is a namespace per file or per compilation unit" becomes a real
question -- and the block form answers it better than a file-level directive
would, because the braces already say where it ends.
