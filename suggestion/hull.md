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

## 10. `--layer edit` emits a C decode that does not compile for a `decimal` member

Added 2026-10-09, measured at `f2fbba3` and again at `ab2d760` with identical
output. hull's `sexpr/canonical.situ` -- Rivest's atom, `decimal u32 length
until ":" max 10 [minimal]` then `u8 bytes[length]` -- built with `situc
build --target c --layer edit --prefix hull_sexpr_c` writes
`canonical_edit.h`, and including it fails:

    canonical_edit.h:75:23: error: too few arguments to function
      'hull_sexpr_c_atom_length_get'
       out->length = hull_sexpr_c_atom_length_get(view);

The ordinary header's getter for a TextConverted member is
`situ_err_t _get(situ_view_t, uint32_t *out)`, since a decimal parse can
fail; `edit.py`'s decode calls it as if it returned the value, as it would
for a binary scalar. `_length_value(view)` exists beside it and is what the
decode reads elsewhere. Reproduce: build the schema above with those flags
and compile any file that includes the edit header.

## 11. No rung builds a message, though `edit` is described as building one

README's ladder and decision 0032 give `edit` as "build or resize a message
whose extent is not fixed". What `--layer edit` emits in C is the owned
decode of 0031's cases C and D, and `edit.py` refuses a variant as "a shape
rather than a length". For canonical s-expressions that leaves nothing to
write with: the view layer says "No setter: mutate is Shifting" for both the
atom's length and its bytes, and the remedies it offers -- `[must_eq]`,
`[max]` -- would change the format rather than describe it. `grep` over
`situc/codegen` finds no builder, appender or resize in any backend. hull
asked for its canonical writer to come from the edit layer and it cannot
yet; whether a builder belongs at `edit`, or the ladder's sentence is the
thing to change, is situ's to decide, and I have not assumed either.

## 12. The frame layer's `next` neither applies `[max]` nor validates what it hands out

Added 2026-10-09, measured at `9d66b6d` with the generated C. hull's
agent protocol frames `struct message { u32 length [max = MAX_MESSAGE];
u8 body[length]; }` with `MAX_MESSAGE = 1048576`, at `--layer frame`.

    reader buffer 1048580 (SIZE_MAX):    length 1048577 -> next = SITU_ERR_BOUNDS
    reader buffer 1048644 (SIZE_MAX+64): length 1048577 -> next = SITU_ERR_TRUNCATED
    same, whole 1048577-byte body pushed -> next = SITU_OK, then validate = SITU_ERR_CONSTRAINT

So a length the schema forbids is refused only when the caller's buffer
happens to be exactly `SIZE_MAX`, and then as a capacity error rather than
a malformed one. With any larger buffer the reader waits for the body, and
once it arrives `next` returns a view of a message its own schema rejects.
`_required` reads the length without consulting `[max]`, and
`_reader_next` never calls `_validate`. The point of a length is that a
receiver can refuse an oversized message before reading it, and that needs
`[max]` asked the moment the length is whole. hull does this itself now
(`agent/framing.c`) and validates every message `next` returns, so it does
not depend on a fix. A caller following the header's own comment -- "call
`next` until it answers SITU_ERR_TRUNCATED" -- gets the forbidden message.

## 13. A `decimal` run whose width a field gives is measured in elements

Added 2026-10-09, measured at `9d66b6d` and again at `73d4bee`, with
`situc verify`. 8.6.2 says a width is digits, not elements, and a literal
width obeys it; a width taken from a field does not:

    struct g { u8 n; decimal u16 code[n]; }
    g x 03 30 30 37          -> BoundsError: g.code: declared length does not fit
    g b 01 37 37             -> conforms (one u16 is two bytes, "77")

    struct h { u8 n; decimal u8 code[n]; }
    h a 03 30 30 37          -> conforms

So `decimal u32 value[length]` reads four bytes per digit. hull met it
describing canonical s-expression numbers (`3:493`, a length then that many
digits) in `manifest/manifest.situ` and `agent/protocol.situ`, and frames
them as untyped atoms meanwhile; the comments there cite this entry.

## 14. A peek at the end of a nested struct cannot see the parent's next byte

Added 2026-10-09, measured at `9d66b6d`. A trailing `peek u8` after a
member of varying length, where the byte peeked belongs to the enclosing
struct:

    struct word  { u8 n; u8 bytes[n]; }
    struct inner { peek u8 k; variant w switch (k) { case 2: word has;
                   default: absent no; }
                   peek u8 next; variant more switch (next) {
                   case 'm': u8 extra; default: absent none; } }
    struct outer { peek u8 first; variant body switch (first) {
                   case 2: inner in; default: absent none; }
                   u8 close [must_eq = ')']; }

    outer w  02 61 62 29     -> BoundsError: inner.next: outside the frame
    outer wm 02 61 62 6D 29  -> conforms

Without `word` -- fixed-width members only -- the same peek passes, and
passes even past the end of the buffer (`inner` alone, one byte). If the
struct closes itself (`u8 close` inside `inner`) it passes too. So the
frame a nested struct's peek is checked against seems to end at the
struct's own extent, which the peek is in the middle of deciding. hull
keeps every trailing peek in the struct that owns the next byte
(`manifest/manifest.situ`, `entry`).

## 15. `verify` spends about three seconds on each element of a run it does not validate

Added 2026-10-09, measured at `9d66b6d`. `manifest/manifest.situ` in hull,
one manifest holding n copies of the same 219-byte entry:

    entry alone             ~0.3 s beyond start-up
    manifest, 6 entries     18.5 s
    manifest, 12 entries    36.2 s
    the 1200-byte manifest with six varied entries    118.6 s

Linear, at roughly ten times the cost of the same entry verified alone --
and the report then says the run's elements were not read ("an array's
elements are validated by the caller"). hull cut its vector to two entries
to keep `make test` near half a minute. Not a correctness fault; recorded
because a corpus of real manifests would hold thousands of entries.

## 16. A peek inside an arm not taken, or a run element not present, raises the minimum

Added 2026-10-09, measured at `9f3274a`; not present at `9d66b6d`. The
fix that made a peek count toward its struct's minimum counts every
static peek inside it, including peeks in a variant arm the data may not
choose and in the elements of a run that may hold none:

    struct big   { u8 x[40]; peek u8 k2; variant w switch (k2) {
                   case 1: u8 one; default: u8 other; } }
    struct small { u8 y; }
    struct t     { peek u8 k; variant v switch (k) {
                   case 1: big b; default: small s; } }

    map:    struct t size=41            (was 1..41)
    t short 02 -> BoundsError: t needs 41 bytes at offset 0; the message is 1 bytes

And for a run: `struct e { u8 a; peek u32 k; variant ... }` with
`struct r { u8 o; e items[] until ")"; }` makes `r` need 5 bytes, so the
empty `()` is refused; the same element without the peek leaves `r` at 2.

hull met it moving its pin to `9f3274a` for the variant builders: in
`manifest/manifest.situ` the empty `(7:entries)` and every entry not
carrying its last optional key are refused, because the arm that would
carry it peeks further on. hull's reader and framing schemas peek only at
offset 0, and their maps did not move.
