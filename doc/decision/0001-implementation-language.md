# 0001: situc is written in Python 3.11+, standard library only

Status: accepted
Date: 2026-07-26
Phase: 0

## Context

`situc` is a symbolic solver over layout expressions plus a text generator. It
has no hot loop: the work is proportional to the size of a schema, which is
measured in hundreds of declarations, not millions.

The toolchain has to vendor into embedded build environments where adding a
package manager is a political problem, not a technical one.

## Decision

Python 3.11+, standard library only. Full type annotations, checked by mypy in
strict mode. Adding a third-party dependency requires its own decision record.

`pytest` and `mypy` are development tooling, not dependencies of `situc`: the
compiler must run from a bare interpreter with nothing installed.

## Alternatives considered

**Rust.** Better fit for the eventual semantics, and the capability lattice
would benefit from sum types. Rejected for now because the semantics are not
settled, and the cost of changing a design in Rust is higher than in Python at
exactly the stage where the design changes most. Rewriting later is a known and
bounded option once the lattice stops moving.

**C.** Would make the compiler self-hosting against its own runtime. Rejected:
symbolic interval arithmetic and diagnostic rendering in C is a great deal of
work for no benefit to the user.

**OCaml or Haskell.** The best fit on paper for a compiler with a lattice at its
core. Rejected on availability: neither vendors into an embedded build
environment without argument.

## Consequences

- Performance is not a design constraint at this scale, and should not be
  treated as one. Clarity wins every time.
- mypy strict is load-bearing. The capability vector has a dozen axes with
  distinct domains, and an untyped implementation would confuse them.
- No dependency means no `attrs`, no `lark`, no parser generator. The lexer and
  parser are hand-written, which is the normal choice for a language that wants
  good diagnostics anyway.
