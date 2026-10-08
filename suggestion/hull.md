# situ, from hull: an s-expression reader generated from two schemas

Written 2026-10-09 from `hull`, a rootless machine runtime whose files are
s-expressions (`hull/project.md` section 4.1). hull is adopting situ for its
reader: a readable schema (lists, strings with escapes, symbols, `;` line
comments, `#hex#` octets) and a canonical one (Rivest's length-prefixed
form, which hull hashes and signs). situ was run, not read about. Every
finding below was measured at `871b90e` with `~/src/situ/bin/situc`, and
the ones marked *upstream* reproduce on situ's own `example/sexpr` with no
hull schema involved. hull vendors situ at `f2fbba3`; the two commits
between change no compiler or backend file.

**The verdict is yes.** Canonical s-expressions came out cleanly:
`decimal u32 length until ":" max 10 [minimal]` then `u8 bytes[length]`
describes an atom, and `verify` refused a leading zero (`04:root`), a short
atom (`5:root`) and a missing colon (`4root`) with good messages. The
problems are in framing a run of forms, and in what reports success.

Observations and mechanisms are kept apart below: where I name a cause I
read it from the generated code, and say so.

## 1. A list closed after whitespace gains a phantom element (*upstream*)

    (a b)   items=2  [a] [ b]
    (a )    items=2  [a] [ ]

Generated C from `example/sexpr/sexpr.situ`, `hull_sx_list_items_count` and
`_items_at`. The second item of `(a )` is a zero-length symbol whose extent
is the one space. Hand-written files close a paren on its own line
constantly, so in hull's case this is every multi-line form. My reading of
the generated loop: the space is not `)`, so an element is attempted; the
lead skips the space, the symbol ends at once at `before ... ')'`, and the
element's extent is 1, not 0, so the zero-extent guard does not stop it.

## 2. `example/sexpr` mis-frames an escaped quote (*upstream*)

    (a "x\"y")   items=3  [a] ["x\"] [y"]

Two items expected. `text.chars` has no `[escape = "\\"]`, which
`example/json` has and whose header explains. The example's own comment
says escapes are framed, which this shows they are not. Adding the
attribute fixed it in hull's schema.

## 3. A struct run's terminator is never checked (*upstream*)

    (a b      validate=SITU_OK  extent=4
    (a (b)    validate=SITU_OK  extent=6

`hull_sx_list_check` returns `SITU_OK` without the closing paren. Its
comment says this is deliberate -- termination is `items_span`'s answer and
validating each element is the caller's choice -- and that division is
workable. But nothing at the view layer *reports* termination for a run of
structs the way `_chars_terminated` does for a run of bytes, so a caller
has to re-derive it by comparing a byte to the delimiter. A
`_items_terminated` beside `_items_span` would close that.

## 4. `items_count` and `items_at` cannot tell malformed from finished

Read from the generated code rather than measured: both loops `break` on a
refused sub-view, a zero extent, or an element running past the limit, and
then return the count so far or `SITU_ERR_BOUNDS`. A list whose third
element is broken reads as a two-element list. hull walks elements itself
for this reason; a distinct error for "an element was malformed" would let
a caller use the generated walk.

## 5. `situc verify` says "conforms" about bytes it did not validate (*upstream*)

    situc: 5 vectors conform to .../example/sexpr/sexpr.situ

The five included `(a b`, `(a (b)`, `(a "b)` and the escaped-quote case
above. Against hull's canonical schema `(zzz)`, `(04:root)` and
`(4:root 3:abc)` also conform, though each holds an element the schema
refuses on its own. `verify` validates the top struct, and by 3 that
stops at the run, so its verdict covers less than its sentence claims.
This is the finding that would cost an adopter most, because `verify` is
what section *Adopting it* tells them to trust. Either walk runs of
structs or say that elements were not validated.

## 6. A struct named `bytes` breaks the Python backend instead of being refused

    error: situc failed while checking form `list`
      = TypeError: View.__init__() missing 2 required positional arguments: 'at' and 'length'

Every vector of a schema with `struct bytes { ... }` failed this way;
renaming the struct `octets` and nothing else made all of them run. A
struct named `list` was fine, so it is not every builtin -- presumably the
generated module shadows the `bytes` the runtime calls. `situc build
--target c` did not complain. The name should be refused or mangled at
compile time, as the message itself suggests a construct no backend
generates for should be.

## 7. `hex` as a struct name gives a diagnostic that does not name the cause

    error: expected a field name, found `;`
    12 |   case '#':  hex      as_hex;

`hex` is a radix prefix, so it is reserved, which is fair. The message
points at the semicolon after the arm and does not say the word is
reserved.

## 8. A line comment cannot end at the end of the buffer

hull's files have `;` comments. `whitespace` and `skip` take single bytes,
so a comment cannot be a lead, and hull models it as a form arm instead:
`case ';': comment as_comment;` with `u8 chars[] until "\n"`. That works,
except that a final comment with no newline is refused (`comment.chars has
no b'\n': the frame stops first`). hull will require a final newline. Either
an `until "\n" | end` or a word in the docs about this pattern would help
the next format with line comments; the `example/sexpr` header lists `;`
comments as out of scope as reader macros, and for a configuration format
they are a layout question.

## 9. The packaged situc and the source tree both print `situc 1.0`

`/usr/bin/situc` is the Debian package of 2026-08-05 and `bin/situc` is
HEAD; `--version` gives the same line for both, and `VERSION` reads `1.0`.
An adopter who has run the wrong one cannot find out from the tool.
