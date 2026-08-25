# 0013: identifier casing is the author's, and collisions are the compiler's

Status: accepted
Date: 2026-07-27
Phase: 8

## Context

Nothing in the compiler has ever keyed off identifier casing -- no `isupper`
anywhere in `situc/` -- but the examples were written PascalCase for types and
snake_case for everything else, taken from project.md's own examples
(`Header`, `Packet`, `MsgType`) rather than chosen. Section 25 says nothing
about it, and the style gate cannot check it: `tool/style_gate.py` is a text
checker with no parser, so it cannot tell a type name from a field name. (The
gate was `tool/lint_conventions.py` when this was written; it was replaced in
26.68, and the limitation is the same one -- casing is a question about the
AST, and neither tool reads one.)

The argument for PascalCase types was that generated C stays unambiguous:

```c
situ_IcmpEcho_sequence_get     /* type | field, legible */
situ_icmp_echo_sequence_get    /* struct icmp, field echo_sequence? */
```

That argument turned out to be worth less than it looked. The ambiguity is
inherent to flattening a path into an identifier, not to the casing, and it was
already reachable:

```situ
struct A_b { u8 c;   }      /* situ_A_b_c_get */
struct A   { u8 b_c; }      /* situ_A_b_c_get */
```

Both were emitted, and the only thing that noticed was the C compiler, with a
redefinition error naming a function nobody wrote and pointing at no schema at
all. Phase 8 widened the same hole into the type namespace: an `enum a_b` and
the sealed region `b` of a struct `a` both reach `situ_a_b_t`.

So PascalCase was not making the scheme sound. It was making a hole unlikely.

## Decision

**Casing is not prescribed.** A schema may use snake_case, PascalCase or a mix,
and the compiler has no opinion. Both are first-class.

**Collisions are an error**, checked in `situc/codegen/c/names.py` before
anything is emitted, with both constructs named and their spans pointed at. It
fires on two constructs that generate the same identifier stem regardless of how
either is spelled.

**Names differing only in case are a warning**, not an error. They are distinct
functions in C but meet in the macro namespace, which is uppercased, so a size
constant or an array count derived from either collides while the accessors do
not. Worth saying out loud; not worth enforcing.

**Types keep the `_t` suffix.** Every generated type already carried it --
`situ_MsgType_t`, `situ_Packet_sealed_t`, and the runtime's own `situ_view_t` --
inherited from the phase 0 runtime and applied consistently without being
written down. It is worth keeping because it partitions the type namespace from
the function namespace: an accessor ends in `_get`, `_set`, `_ptr` or another
verb, so a type can never collide with one. It does *not* help with the
flattening ambiguity, which is why the check above exists.

**`ident()` sanitises every part.** Namespace separators and dotted paths
flatten there rather than at the call sites, of which there are dozens; one that
forgot would emit a header no compiler accepts, which is how the first namespace
build failed.

## Alternatives considered

**Reserve a separator, `situ_icmp_echo__sequence__get`.** Sound by construction
with no analysis and no constraint on naming, and legal C -- C11 7.1.3 reserves
only identifiers *beginning* with an underscore plus an uppercase letter or
another underscore, unlike C++. Rejected as noticeably uglier for a hazard that
is rare and now diagnosed properly.

**Enforce a convention.** Rejected on the author's instruction, and it was the
weaker option anyway: the collision check is what actually prevents the failure,
and a style rule would prevent nothing while forbidding schemas that are fine.

**Keep PascalCase as the house style for the examples.** Rejected: every schema
in the tree is snake_case, including the worked examples in project.md sections
5.1, 5.2, 5.3 and 9.7 that three of them mirror. One convention in the tree is
worth more than the churn, and the conversion needed no compiler change at all
-- which is the evidence that casing was already free.

## Note

POSIX reserves identifiers ending in `_t`, so `situ_view_t` technically
encroaches on the implementation namespace. Universal practice, and the
configurable prefix is what keeps generated code clear of `time_t` and friends
in any real program.
