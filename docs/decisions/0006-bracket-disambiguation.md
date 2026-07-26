# 0006: `[` is disambiguated by the closed attribute vocabulary

Status: accepted
Date: 2026-07-26
Phase: 1

## Context

The grammar in project.md section 7 gives `[` two jobs on the same production:

```ebnf
field       = type_ref ident [ array_spec ] [ pin ] [ attrs ] ";" ;
reserved    = "reserved" scalar_type [ array_spec ] [ attrs ] ";" ;
array_spec  = "[" [ size_expr ] "]" ;
attrs       = "[" attr { "," attr } "]" ;
attr        = ident [ "=" expr ] ;
```

An array spec and an attribute list both open with `[`, and both may contain a
single identifier. `u8 x [foo];` is therefore two readings: an array of `foo`
elements, or a scalar carrying the flag `foo`.

Most instances resolve on content. `[12]`, `[hdr.length]` and `[remaining]` are
sizes; `[must_eq = 1]` and `[wo, on_write = trigger]` are attributes, because
`=` and `,` cannot appear at the top level of a size expression -- `==` is a
distinct token and an array has exactly one size.

What is left is the bare `[IDENT]` form, and it is common on both sides:
`[must_be_zero]`, `[preserve]`, `[rw]`, `[secret]` are attributes, while
`u8 buf[MAX_PAYLOAD]` is an array sized by a constant.

Section 17.0 says ambiguity is an error and the schema must resolve it
explicitly. That principle cannot be applied literally here, because the
ambiguity is in the language rather than in any schema: the author of
`[must_be_zero]` has nothing to disambiguate.

## Decision

The attribute vocabulary is closed and fixed by the language, so use it.

A bracket group is an attribute list when:

1. it contains `=` or `,` at bracket depth 1, or
2. it contains exactly one token, an identifier that is a known attribute name.

Otherwise it is an array spec.

`parser.ATTRIBUTE_NAMES` holds the full vocabulary, including names belonging to
phases not yet implemented. Listing them early is deliberate: the parse of
`bit enable [rw];` must not change meaning when phase 10 arrives.

Every bare-identifier bracket appearing in project.md was checked against this
rule. All of them -- `[unknown]`, `[secret]`, `[allow_host_dependent]`, `[rw]`,
`[ro]`, `[require_aligned]`, `[preserve]`, `[must_be_zero]`, `[allow_straddle]`,
`[w1c]`, `[nul_terminated]`, `[equalize]`, `[allow_unverified_read]` -- are
attributes and are in the table. The only bare-identifier size form in the
document is `[remaining]`, which is a keyword rather than an identifier.

## The residual collision -- closed

A constant whose name collides with an attribute name would make
`u8 buf[secret];` mean the wrong thing silently, with no way for the author to
spell the other reading. That is the one case where section 17.0 applies as
written.

This was first deferred to phase 2 on the assumption that it needed constant
resolution. It does not: rejecting the *declaration* needs only its name. A
`const` whose name is in `ATTRIBUTE_NAMES` is therefore an error at the point of
declaration, implemented in `wellformed.check_const_names_do_not_shadow_attributes`.

Killing the collision at its source rather than at each use is also the better
diagnostic: the author is told to rename one constant, instead of being told
that one of their array declarations parsed as something else.

A test asserts that *every* name in `ATTRIBUTE_NAMES` is refused as a constant,
so adding a name to the vocabulary cannot silently reopen the ambiguity.

## Alternatives considered

**Whitespace-significant.** `x[N]` is a size, `x [N]` is an attribute list. This
matches every example in project.md exactly, since sizes are always written
tight against the name and attributes always spaced. Rejected: whitespace
changing the meaning of a declaration is a bad property in any language, it
contradicts the spirit of section 6, and it is invisible in review.

**Positional: first bracket group is the array, second is attributes.**
Rejected: it misparses `u8 version [must_eq = 1];`, which has no array spec at
all, and that form is in example 5.1.

**A sigil on attributes**, such as `#[...]`. Unambiguous and needs no
vocabulary. Rejected: attributes are not marked OPEN in project.md, so their
surface syntax is not the implementer's to change, and every example in the
document would have to be rewritten.

**Resolve against declared constants.** If the identifier names a const it is a
size, otherwise an attribute. Rejected: it makes parsing depend on name
resolution, so a schema's parse tree would change when an unrelated `const` is
added or removed.

## Note for review

This resolves an ambiguity in project.md section 7 rather than in any schema. If
the intended reading was different, the grammar should say so explicitly and
this decision be revisited.
