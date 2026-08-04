# Suggestions from evaluating situ against hydra

Written while evaluating situ for `hydra`, a Qt Widgets browser that
hand-writes four binary readers. The standing directive is to test viability
against the project actually in front of you rather than in the abstract, so
this was done by writing schemas for hydra's real formats and compiling them,
not by reading the documentation and reasoning.

Everything below is a suggestion for situ. The verdict for hydra lives in
hydra's own `project.md`.

## What was tested

Two schemas, both written from `hydra/src/session_import.cpp`:

- **`snss.situ`** -- Chromium's session file: a signature and version, then a
  stream of length-prefixed command records to the end of the buffer.
- **`pickle.situ`** -- `base::Pickle` as it appears inside those records: a
  UTF-8 string as a byte count plus bytes plus padding to four, a UTF-16
  string whose count is in *characters*, and the body of an
  update-tab-navigation command built from them.

Both compiled with `situc build --target c`, and `situc map` and
`situc explain` were read against what the hand-written reader does.

## The thing that made the case

`pickle_string16` carries `u8 data[length * 2]` where `length` is a `u32`. The
map came back with

    nav_entry.title.data   size=Bounded(0, 8589934590)

which is 2 x 4294967295. The hand-written reader guards exactly this with

    if (!m_ok || n < 0 || (m_end - m_p) / 2 < n) { m_ok = false; return {}; }

-- a division rather than a multiplication, specifically so `2 * n` cannot
overflow before the comparison. That subtlety took care to write and is
invisible on inspection; situ derived it from the schema and printed it in the
map. **That is the argument for situ in one line**, and it is worth putting
somewhere prominent: the tool did not merely accept a description of a format,
it computed a bound the author of the equivalent C had to reason out.

## Suggestions

### 1. A padding idiom, because everyone writing a Pickle will write this twice

Four-byte padding after a variable run is spelled:

    reserved u8 [align_up(length, 4) - length];

and for the UTF-16 case:

    reserved u8 [align_up(length * 2, 4) - (length * 2)];

It is correct and it says *what the bytes are*, which is the stated intent of
the `reserved` form and a good one. But the expression repeats its own
subexpression, and the second version repeats it twice. `base::Pickle`, cpio,
TLV formats generally and most RPC framings all pad this way.

An attribute -- `[pad_to = 4]` on the run, or a `pad_to 4;` member -- would
carry the intent, and the map could then say *padding* rather than
`<reserved0>`. The current spelling is not wrong; it is the one place in the
two schemas where I had to re-read what I had written to be sure it was right.

### 2. A UTF-16 element type, or a way to say the count is not bytes

`u8 data[length * 2]` is the honest layout and loses the fact that this is
text. A reader of the schema cannot tell it from a byte run that happens to be
even. Situ already treats encoding as a property elsewhere -- `smtp.situ` has
`[encoding = ascii]` -- so `u16 data[length] [encoding = utf16]`, or an
`encoding` attribute that implies the element width, would keep the layout
exact and let `situc doc` say "text" where it currently says bytes.

This matters more than it looks: the whole reason hydra's reader has the
overflow guard is that a length field in this format sometimes counts
characters and sometimes counts bytes, and nothing in the format distinguishes
them. A schema that can state which is which is documenting the trap.

### 3. Say that encoded payloads are out of scope, next to the other exclusions

`README.md` lists what situ will not do, and the list is good. It does not say
that a payload requiring *transformation* -- compression, encryption, escaping
-- is outside the model, and that is the first thing a reader with a real
format wants to know.

Concretely: of hydra's four readers, the LZ4 block decompressor is the largest
and the riskiest, and it is not a layout at all. It reads a token, a
variable-length literal run, a back-reference offset and a match length, and
writes into a *different* buffer with overlapping copies. No schema describes
it. That is entirely reasonable and it is not currently written down, so a
reader spends a while looking for the feature before concluding it is absent.

The existing exclusions are phrased as principles ("no wire format of its own",
"no allocation in generated code"). This one follows from the second: a
decompressor must allocate or be handed an output buffer, and its output is
not a view over its input.

### 4. The sized-run syntax is the one thing a newcomer gets wrong

My first schema said `u8 body[] count (size - 1);`, on the assumption that a
run with an explicit length was spelled like the `while`/`until` forms. The
error was precise about *where*:

    error: expected `;` after the field declaration, found `count`

It could also be precise about *what*: a suggestion of `name[expr]` when the
unexpected token is one of the plausible guesses (`count`, `len`, `length`,
`size`) would have saved a trip to the examples. The examples do show it --
`u8 options[(data_offset - 5) * 4]` in tcp, `u8 data[header.filesize]` in cpio
-- but the README's own snippets are all fixed-size structs, so the first place
a reader looks does not contain the form they need.

### 5. What adoption actually costs a CMake/Qt project, and what would lower it

This is the part that decides whether hydra adopts situ, and none of it is
about the schema language:

- **`situc` is Python 3.11+ at build time.** Hydra's build is CMake plus Qt's
  own `moc`, with no other code generation and no Python. Adding a generator
  to the build means adding a build dependency to the `.deb` as well, and the
  packaging path was built this week. The alternative -- commit the generated
  `.h`/`.c` and regenerate by hand -- is viable but wants a documented,
  blessed workflow, including how `situc map --check` fits into CI when the
  generator is not run at build time. A short section on "generated files
  committed, generator not required to build" would remove the largest
  objection.
- **The C surface meets Qt at a conversion layer.** Generated accessors hand
  back offsets and lengths; hydra needs `QString` from UTF-8 and UTF-16 and
  `QByteArray` views. That layer is small and unavoidable, but it is where a
  `--target cpp` emitting spans (`std::string_view`, or a pointer+length pair
  documented as convertible) is worth more than one emitting raw pointers.
  Worth saying which the C++ backend does, in the README table.
- **Which struct does the caller loop over?** `cpio.situ` is admirably explicit
  that the boundary is at the archive and an archive is a caller's loop over
  `cpio_entry`. `netlink.situ` puts the loop *inside* the schema with
  `attrs[] while (...)`. Both are right for their format, and a reader with a
  new format has no rule for choosing. A paragraph on when to model repetition
  and when to hand it to the caller would be the single most useful addition
  to the examples' prose.

## What was not evaluated

The generated code was compiled by `situc` but not compiled by a C compiler
here, not linked into hydra, and not run against a real Chromium session file.
The schemas were checked against the map and against the hand-written reader by
reading, not by differential testing. So this is evidence that situ *can
express* these formats and that its analysis is sharper than the hand-written
equivalent -- not evidence that the generated accessors agree with hydra's
current output on real bytes.

The obvious next step, if hydra adopts any of this, is to run both readers over
the same captured session file and diff, which is a thing hydra already has the
harness shape for.
