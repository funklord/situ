# Situ

A schema language and compiler for byte-exact data layouts, in which
*capability properties* -- in-place mutability, addressability, canonical
encoding, authentication coverage -- are first-class, statically inferred,
assertable, and reported back to the schema author as actionable design
feedback.

Artifacts:

| Thing               | Name        |
|---------------------|-------------|
| Schema file         | `*.situ`    |
| Compiler            | `situc`     |
| C runtime (minimal) | `libsitu`   |
| Capability map file | `*.situ.map` |
| Wire signature file | `*.situ.wire` |

---

## 0. How to use this document

This is a design specification, not a tutorial and not a finished grammar. It
is the authoritative source of intent, and it is kept current: when the
implementation learns something the specification does not say, the
specification gains it.

Rules for the implementer:

1. **Section 11 (the capability lattice) is the core of the project.** Every
   other part of the compiler exists to feed it or to report its results. If
   a design decision elsewhere conflicts with keeping the lattice sound and
   decidable, the lattice wins.
2. **Do not implement ahead of the phase plan in Section 26.** Each phase has
   acceptance criteria. A phase is done when its criteria pass, not when the
   code looks complete.
3. **Syntax in this document is a v0 proposal.** Where a construct's surface
   syntax is marked OPEN, choose something consistent with the rest and record
   the decision in `doc/decision/`. Do not silently invent semantics.
4. **When something in this document is ambiguous or contradictory, stop and
   ask.** Do not resolve ambiguity by guessing; a wrong guess about capability
   propagation is expensive to unwind later.
5. Everything in `doc/decision/` is append-only. One file per decision,
   numbered, with the alternatives considered.

   **Two numbering schemes live there, and the second is deliberate.** A
   DECISION is `NNNN-slug.md` in one sequence, 0001 upward. A FINDING
   moved out of section 26 keeps its own number: `26.NNN-slug.md`. Set by
   the copyright holder 2026-10-17, and the reason is the citations --
   section 26 is cited about 4,000 times, **2,652 of them from source**,
   `codegen/c/emit.py` alone citing 109. Renumbering into the `NNNN`
   sequence would invalidate every one of those sites, in code comments
   nobody would think to grep.

   `harmonization.md`'s settled inventory spells a record `NNNN-slug.md`,
   so this diverges from it, with the measurement above as the technical
   reason. Signalled to `claude-guidelines`' signal list rather than left
   as one tree's habit, because the next tree to move findings into
   records meets the same question.

   **The append-only rule above is the DECISION's, and findings are
   exempt.** Set by the copyright holder 2026-10-17. A decision is a
   position taken, so amending one rewrites history somebody may have
   relied on and a later record supersedes it instead. A finding is a
   running record of what the code does: it gets struck through when it
   is overtaken and amended when it is wrong, which is how section 26
   was written and is why these moved at all.

   **The `Status:` line says which rule applies**, so a reader need not
   remember this paragraph: a finding reads `Status: recorded` and a
   decision `Status: accepted`. Held by
   `test_a_record_says_which_rule_governs_it`, because a distinction
   between two kinds of file that lives only in prose is one that drifts
   -- 512 and 62 today.
6. **Where this document can be checked against the code, it is.** Five things
   here are held to the implementation by a test that fails when they drift:

   | Section | Checked by |
   |---|---|
   | 7, via `doc/grammar.ebnf` | `test_grammar_sync` |
   | 11.3, the propagation table | `test_the_spec_table_matches_the_rows` |
   | 21, the CLI surface | `test_the_cli_section_lists_every_command` |
   | 23, the repository layout | `test_the_layout_section_lists_every_compiler_module` |
   | 20.2, the failure classes | `test_the_failure_classes_match_the_runtimes` |

   Two of 26.31's claims are checked now as well --
   `test_no_construct_falls_through`, that no construct in this repository
   reaches a backend's fallthrough note, and
   `test_backends_refuse_the_same_members`, that what one backend declines the
   other three decline too. Both read generated output rather than running it,
   which invariant 22 is the standing warning about: they hold classification
   and agreement, never behaviour. Every construct they cover was separately
   run against real bytes in each backend.

   The rest of 26.31 is still a survey, and a reader picking this up should
   re-derive it from the generated output rather than trust it to have aged
   well. It has been materially wrong four times. A list that cannot be
   checked should say so.

   Every one of them was added *after* finding the drift, and every one found
   more of it than expected: 11.3 was about twenty rows behind, 21 named a
   `--strict` that never existed and called three per-subcommand flags global,
   23 was missing a module, and 20.2 listed five of the seven failure classes
   -- and the check that found that also found two runtimes short of one.
   Prefer adding a check to adding a promise -- a normative table nobody
   verifies is a comment, and a list a reader types from is worse than that.
7. **Read 0 through 25 and 27 through 28; look 26 up.** The log is around
   six sevenths of this file and the whole of it is past what one reader
   holds at once. So an instruction to "read project.md" is not one
   instruction: the specification is read through, and the log is reached by
   number, which is what the cross-references are for, or by `grep`.

   **No count of this document appears in this rule, and that is the
   remedy rather than an omission.** A present-tense count of the tree's
   own shape is falsified by the commit that records it -- the first
   version of this rule quoted four, and every one was wrong before the
   day was out. The proportion is stable and the figures are not, so what
   is kept here is the method:

       wc -lc project.md                 the whole
       grep -n '^## 2[6-7]\. '           where the log starts and ends
       awk 'NR>=a && NR<=z' | wc -lc     the log, between those two

   Where the reader counts tokens rather than bytes, derive the ratio
   rather than quoting one: divide a range's byte count by the token count
   that reader reports for the same range. Measured that way on
   2026-09-24 it fell between 2.765 and 3.053 bytes per token over four
   disjoint ranges, which put the file near 700,000 -- readable once, by
   a reader carrying nothing else. That ratio belongs to whichever reader
   is asking and is re-derived, never quoted forward.

   26.100 and 26.113 are the same rule met from the other end: each exists so
   that a reader need not walk a run of phase entries to find what they
   settled. That is the shape to add to when the next run has a rule in it.

---

## 1. What Situ is

Situ describes the exact byte layout of data that already has, or is being
designed to have, a specific binary representation: wire protocols, packet
formats, on-disk records, and memory-mapped hardware registers.

From a `.situ` schema, `situc` generates:

- accessor code for reading and writing fields, in C, C++, Rust and Python
- a **capability map**: for every field and region, which operations are
  possible and, when an operation is not possible, precisely why
- design advice: concrete, costed schema changes that would restore lost
  capabilities
- test scaffolding: golden-vector tests and a fuzz harness per schema

How much of a schema becomes code is the consumer's choice, made at
invocation: `situc build --layer` runs from `view` -- zero-copy accessors over
a buffer the caller owns, and the default -- up to `drive`, a send and receive
loop carrying the schema's own correlation and timing contract. A schema may
describe more of a protocol than a given consumer wants generated, and none of
the upper rungs is emitted unless asked for. See
`doc/decision/0032-the-layer-ladder.md`.

Three properties distinguish it from everything in Section 3:

**Capability inference.** The schema declares structure; the compiler derives
what the generated API is permitted to expose. A field that cannot be mutated
in place does not get an in-place setter. The absence is deliberate, explained,
and assertable.

**Mixed paradigms in one schema.** Positional (fixed-offset), indexed
(offset-table), and schema-free (TLV/opaque) regions coexist. Each region
contributes different capabilities, and the compiler tracks the consequences of
mixing them rather than forcing the author to pick one model for the whole
message.

**Design feedback as a product.** The compiler is not only a code generator; it
is an advisor that tells the protocol designer which of their choices cost them
static addressability, what that cost is in bytes, and what to change. Making a
protocol *less dynamic* is a supported workflow with tooling behind it.

## 2. Non-goals

- **Not a wire format.** Situ owns no encoding. It describes formats that exist
  or are being designed independently. There is no "Situ encoding". Note that
  generating *codec implementations* (Section 13.4) does not violate this: those
  implement someone else's standard code, chosen by the schema author.
- **Not a serialization library for language-native objects.** No reflection,
  no object graphs, no cyclic references, no interning.
- **No automatic schema evolution.** Compatibility across versions is explicit
  (Section 19), not implicit. Situ will never silently preserve unknown fields;
  see Section 14.5 for why that is a security position, not an oversight.
- **No dynamic allocation in the freestanding backends.** C, C++ and Rust
  generated code does not allocate: no `malloc`, no hidden arena, no growable
  buffers, callers supply memory. This is invariant 4, and it is what makes an
  offset a constant and an operation *absent* rather than refused.

  **Python is the exception, and it is stated rather than tacit.** Its data
  model has no non-allocating spelling -- a list is the language's, not
  situ's. It is bounded rather than free: the materialized walk stops at the
  schema's `max` where one is declared, and at the buffer otherwise, which is
  the same quantity C's index array holds. What C requires and Python does not
  is that the quantity be a *compile-time constant*, which is why
  `--materialize` needs `max` in three backends and not the fourth (26.30).
  This paragraph exists because "ever" was written before that backend did,
  and was read as covering it.

  Open, and enumerated in
  `doc/decision/0031-where-allocation-is-unavoidable.md`: a `coded` region
  whose `expansion = unbounded` cannot report its output extent without
  decompressing, so it is the one case no measure-then-allocate pass can
  serve. Whether it earns a caller-supplied allocator is undecided.
- **~~No recursive types in v0.~~ Superseded: recursive types are permitted
  where the schema states a depth** (`doc/decision/0054-bounded-recursion.md`,
  the copyright holder's decision 2026-09-06). The reasoning that produced the
  refusal was sound and none of it needed a prohibition: size computation,
  capability computation, `--owned` and the walkers all need a *bound*, and a
  bound is what a declared depth is. Recursion with no declared depth is still
  refused, with the same diagnostic and a remedy attached -- a format whose
  nesting is unbounded is one whose worst case nobody has considered, which is
  a finding rather than a gap in the language.

  Two depths, because they answer different questions and situ has already
  drawn this line twice. `depth = N` is the format's own limit, so a deeper
  message is malformed and it is what makes `size_max` a number. `limit = N`
  is this build's cap, so a deeper message is well formed and refused anyway,
  under its own error class -- the same separation `SITU_ERR_TRUNCATED` keeps
  from `SITU_ERR_BOUNDS`, and that `SITU_WALK_UNSUPPORTED` states outright as
  "a statement about this build rather than about the bytes". The walker has
  carried `WALK_DEPTH_MAX` since it was written; this is that mechanism
  reaching the compiler, not a new one.

  **Implemented, in all four backends**, for a struct that names itself and
  for a cycle of them. Each extent carries a depth and the run's span passes
  it on, so the recursion is bounded by the schema rather than by the
  message's own length. Measured identical across C, C++, Rust and Python
  (26.283) -- and across all three shapes a recursion takes, which took
  another pass: `[depth]` shipped exercised on a counted run alone, and the
  `while` run and the record run are separate emitters that had never been
  compiled (26.288).

  **`validate` refuses a too-deep message in all four**, under two verdicts:
  past the format's `[depth]` it is malformed and gets a constraint error,
  past this build's `[limit]` it is well formed and refused anyway under
  `SITU_ERR_DEPTH` (26.284). The extent could not host that refusal, and a
  *length* may not be capped by `[limit]` at all -- a cap on a length can
  only truncate.

  **Both walkers read the schema's depth** from a side table in the packed
  image, at every site rather than one (26.286) -- and spend it at the same
  rate, which they did not at first: C counted hops through its own file
  rather than levels of struct, so `[limit = 8]` bought four levels there
  and eight in Python (26.287). Both run shapes are walked, counted and
  `while`, and a message past the ceiling is `cannot-say` in both rather
  than a verdict -- the reader's refusal is not a judgement on the bytes.

  **Mutual cycles are described** (26.288, amending 0054). Every struct in
  a cycle declares the same `[depth]`, which counts nested structs rather
  than turns; a cycle that disagrees, or that states `[limit]` on some of
  its structs and not the rest, is refused. All six descriptions stop on
  the same message, which they did not before: the walkers counted structs
  where the generated code counts edges, and no test spanned the two
  layers.

  **A cycle through a variant arm is one too** (26.289), which is how a
  tagged tree is written -- `example/json` is the worked case, and the
  refusal it replaced rested on a proxy: a variant with no maximum reads
  as unbounded, and a recursive arm has none for a different reason. The
  arm carries `opaque` now and the check asks it.

  Still open, and openmlx4's finding rather than a design gap: a format
  that ends its list with a sentinel and declares no maximum has a `limit`
  and no `depth`, and `[limit]` alone is refused. That was definitional
  until `depth` acquired a behavioural meaning; their `[depth = 64]` now
  claims a 65-section FS2 image is malformed, which is false and blames the
  image rather than the reader.
- **Not a parser combinator library.** The schema is declarative and the layout
  solver is a compiler pass, not a runtime interpreter. That is a statement
  about `situc` rather than about everything that may read its output: a packed
  layout image and a separate program that walks one is a late-stage feature
  (26.33), decided in `doc/decision/0026-runtime-image-and-interpreter.md`
  and kept out of this compiler for the reason recorded there -- these
  invariants keep exactly one master.
- **No behaviour the schema did not state.** A `.situ` file describes the
  protocol, and that includes its conversation: which message answers which
  (0030), and at the upper rungs of the layer ladder the retransmission and
  timing contract too. Both endpoints must agree on those, which is why they
  are wire contract rather than deployment settings, and why they live in the
  one file `situc diff` already holds to its predecessor.

  What situ never does is supply a fact nobody declared. There is no default
  timeout, no implicit retransmission, no inferred correlation. Where a schema
  states no policy the generated code has none -- absent, not guessed, exactly
  as a missing setter is absent rather than silently unsafe. A deployment may
  override a declared **value** at `situc` invocation; it can never introduce
  a **shape**. See `doc/decision/0032-the-layer-ladder.md`.

  How much of that description becomes code is the consumer's, not the
  schema's: `situc build --layer` defaults to `view`, which emits none of the
  conversation machinery even where a schema states it.

  This bullet exists because the position had to be established by grep
  before it was written down, and a reader who tried got it wrong -- claiming
  situ "does not run a protocol" -- and propagated the error into two other
  projects. It has since been wrong in the other direction here, reading the
  layer choice's absence from the schema as behaviour's absence from it.
- **Not a replacement for protobuf semantics.** Situ can *describe* the
  protobuf wire format (Section 9.7) and can import `.proto` files to produce
  such a description (Section 19.2). It does not adopt protobuf's identity model,
  its unknown-field retention, or its evolution story. The importer is one-way
  and reports every construct it could not represent faithfully.

## 3. Positioning

| System | Describes arbitrary formats | Mutation | Fixed layout | Bit fields | Side effects | Capability inference |
|---|---|---|---|---|---|---|
| Protobuf | no | rebuild | no | no | no | no |
| FlatBuffers | no | limited in-place | yes | no | no | no |
| Cap'n Proto | no | builder | yes | no | no | no |
| SBE | no | in-place | yes | partial | no | no |
| Kaitai Struct | yes | read-only | n/a | yes | no | no |
| Construct (Py) | yes | parse/build | n/a | yes | no | no |
| ASN.1 (DER) | yes | rebuild | no | no | no | no |
| SystemRDL / SVD | registers only | in-place | yes | yes | yes | no |
| **Situ** | **yes** | **inferred** | **where possible** | **yes** | **yes** | **yes** |

Two specific lessons from prior art that shape the design:

**Canonical encoding cannot be retrofitted.** Protobuf's own documentation
states that its serialization is not and cannot be canonical, and that hashes
of serialized messages are unstable across schema changes, build flags, and
library versions. The primary obstacle is unknown-field retention: bytes fields
and nested messages share a wire type, so a parser cannot know whether to
recurse. ASN.1 needed DER as a separate encoding for the same reason. Situ's
positional layout makes canonical encoding a structural property rather than a
convention -- there is exactly one valid encoding of a given value. This is
free, and it is a headline feature for any signing or MAC use case.

**The eventual scope includes generating the codecs themselves.** Once tier 2
(Section 13.4) exists, Situ overlaps territory currently held by SDR and FEC
toolchains (liquid-dsp, libfec, ISA-L) and by CRC generators (pycrc, crcany).
Nothing in that space carries capability reasoning, and nothing in the schema
space generates line codes or FEC. The property signature is what lets both live
in one tool without the layout solver ever learning what a convolutional code is.

**Describing protobuf is the language's hardest conformance test.** Protobuf is
the worst case on nearly every capability axis: unbounded size, dynamic offsets,
sequential access, no stable addressing, and non-canonical for at least five
independent reasons. If Situ can describe it faithfully and then report exactly
why each capability is weak, the lattice is sound. Section 9.7 makes this an
acceptance test, not merely a feature.

**Register description languages solved the hardware half already.** SystemRDL
and CMSIS-SVD have a mature vocabulary for access modes and side effects.
Borrow it (Section 15) rather than reinventing it. What they never did was
unify that vocabulary with wire-protocol description, which is the novel part.

## 4. Core thesis

> A layout is a set of capability claims. The schema states structure; the
> compiler derives which claims hold; the generated API exposes exactly the
> operations the claims support; the author can assert claims and be told, in
> blame-propagating detail, which construct broke one.

Corollaries that drive concrete design decisions:

- **No field numbers.** In protobuf the tag *is* the field identity because the
  wire format is tag-based. In a layout language, position carries identity.
  Numbering adds a second, silently-breakable identity scheme. Names are the
  identity; renames are visible in diffs.
- **Offset pins are assertions, not declarations.** `@ 0x14` does not place a
  field. The solver computes the offset; the pin asserts the computed value.
  This catches layout drift from an inserted field, which is the bug class
  field numbers were incidentally papering over.
- **Refusals must explain themselves.** A missing setter with no explanation is
  hostile. A missing setter plus "no in-place mutation: varint `len` at offset 4
  precedes this field; move `len` after `payload`, or pin it to u16" is the
  product.
- **Static and dynamic are not a binary.** Most real protocols are a dynamic
  outer frame containing static inner records. Frame-relative staticness is the
  common case, not an exception (Section 12.2).

---

## 5. Language overview by example

Three examples establishing the intended feel. C-struct derived; keywords
carry meaning; brackets carry less-common attributes.

### 5.1 Static header

```situ
target buffer;
endian big;
bit_order msb_first;

const MAX_PAYLOAD = 1500;

enum msg_type : u8 {
    hello = 0x01,
    data  = 0x02,
    close = 0x03,
}

struct flags {
    bit  urgent;
    bit  ack;
    u3   priority;
    reserved u3 [must_be_zero];
}

struct header {
    u8        version  [must_eq = 1];
    msg_type  type;
    flags     flags;
    u16       length   [max = MAX_PAYLOAD];
    u32       seq      @ 0x05;         // pin: assert solver agrees
}

require size(header) == 9;
require absolute_static(header);
require in_place(header.seq);
```

### 5.2 Dynamic frame with static islands

```situ
struct record {
    u32  id;
    u16  kind;
    u16  value;
}

// The header of 5.1 plus the element count this message needs, which makes it
// 11 bytes with `seq` at 0x07.
struct header {
    u8        version  [must_eq = 1];
    msg_type  type;
    flags     flags;
    u16       length   [max = MAX_PAYLOAD];
    u16       rec_count;
    u32       seq      @ 0x07;
}

struct message {
    header  hdr;
    u8      opts[hdr.length];      // dynamic: shifts everything after
    record  recs[hdr.rec_count];   // frame-relative static elements
    u8      trailer[remaining];
}

// A record is fully static once its base is resolved. Assert that:
require frame_static(message.recs[]);
require in_place(message.recs[].value);
```

### 5.3 Sealed packet

```situ
codec aes_gcm_128 {
    granularity = byte;
    length_preserving;          // ciphertext core; tag declared separately
    seekable;                   // CTR-mode core
    authenticated;
    invertible;
    deterministic;
}

struct packet {
    authenticated {
        header  hdr;
        u8      nonce[12];
    }

    sealed(aes_gcm_128, nonce = nonce) {
        u16  inner_kind;
        u32  inner_seq;
        u8   body[hdr.length];  // not `[remaining]`: something follows this
                                // region, and `remaining` runs to the end of
                                // the frame, so it would swallow the tag
    }

    tag u8[16];                 // coverage inferred: all authenticated
                                // and sealed regions in this struct
}

require canonical(packet);
require in_place(packet.hdr.seq);              // outside tag coverage? no --
                                               // will fail; see 14.2
require verify_gated(packet.sealed);           // no parse before verify
```

The regions and the tag are unnamed here, which means they take their keyword
as their name -- which is what lets `require verify_gated(packet.sealed)`
resolve. A struct with two regions of a kind names them:
`doc/decision/0010-region-and-tag-names.md`.

---

## 6. Lexical structure

- ASCII source only. Non-ASCII bytes outside string literals are an error.
- Line comments `//`, block comments `/* */`, nestable.
- Identifiers: `[A-Za-z_][A-Za-z0-9_]*`. Case-sensitive.
- Integer literals: decimal, `0x` hex, `0b` binary, `_` as a digit separator.
- Statements terminated by `;`. Blocks by `{}`.
- Attribute lists in `[ ... ]`, comma-separated, `key = value` or bare flag.
- Indentation is not significant. Tabs for structural indent, spaces for
  alignment (Section 25).

## 7. Grammar (v0, EBNF)

```ebnf
schema        = { directive | decl } ;

directive     = "target"     target_kind [ "append" ] ";"
              | "endian"     endian ";"
              | "bit_order"  bitorder ";"
              | "strictness" "=" strictness ";"
              | "encoding"   ident { "|" ident } ";"     (* section 8.6.7 *)
              | "whitespace" byte_set ";"                (* section 8.6.8 *)
              | "import"     string ";" ;

target_kind   = "buffer" | "mmio" | "file" ;
endian        = "big" | "little" | "native" ;
bitorder      = "msb_first" | "lsb_first" ;
strictness    = "strict" | "lenient" ;

decl          = namespace_decl | const_decl | enum_decl | tokens_decl
              | struct_decl | endian_marker_decl | codec_decl
              | register_decl | register_block_decl | requirement
              | invariant | when_decl | relation_decl
              | impl_decl | varint_decl | pipeline_decl ;

(* One level; nesting is rejected naming its phase. A declaration inside is
   named `outer::header`, and an unqualified reference within the same
   namespace resolves there and nowhere else.
   doc/decision/0012-namespaces.md *)
namespace_decl = "namespace" ident "{" { decl } "}" ;

(* Section 15.2. A register lowers to a struct carrying its bus facts; a
   register_block declares the settings once and disappears. *)
register_decl  = "register" ident [ "@" number ] "{" { reg_setting | member } "}" ;
register_block_decl
               = "register_block" ident "{" { reg_setting | register_decl } "}" ;
reg_setting    = ( "width" | "access_width" ) "=" number ";"
               | ( "volatile" | "no_rmw" ) ";" ;

qualified     = [ ident "::" ] ident ;

const_decl    = "const" ident "=" expr ";" ;

enum_decl     = "enum" ident ":" scalar_type [ "[" expr "]" ]
                "{" enum_body "}" ;
enum_body     = { ident "=" expr "," } [ "default" "=" ( "error" | "pass" ) ] ;

struct_decl   = "struct" ident [ attrs ] "{" { member } "}" ;

member        = field | parameter | reserved | preamble | marker_field
              | block | variant | tag_field | pad ;
pad           = "pad_to" "(" digits ")" [ attrs ] ";"   (* section 8.4 *)
              | "pad_random" "(" digits "," digits ")" scalar_type
                [ array_spec ] [ attrs ] ";" ;    (* 14.7, 0045 *)

parameter     = "parameter" [ radix ] scalar_type ident [ attrs ] ";" ;
                                                 (* decision 0050 *)

field         = [ "peek" ] [ radix ] type_ref ident [ array_spec ] [ skip ]
                [ until | repeat ] [ located ] [ pin ] [ attrs ] ";" ;
located       = "at" expr ;                  (* decision 0042 *)
repeat        = "while" "(" expr ")" [ "max" expr ] ;   (* section 8.6.6 *)
radix         = "decimal" | "hex" | "scaled" ;   (* section 8.6.2, 0056 *)
reserved      = "reserved" scalar_type [ array_spec ] [ attrs ] ";" ;
preamble      = "preamble" "u8" [ array_spec ] "=" string   (* 0052 *)
                [ attrs ] ";" ;
tag_field     = ( "tag" | "checksum" ) type_ref [ ident ] [ array_spec ]
                [ "covers" "(" ref_list ")" ] [ "prefix" "(" ident ")" ]
                [ "is" ident ] [ attrs ] ";" ;

block         = "positional"    "{" { member } "}"
              | "authenticated" [ ident ] [ attrs ] "{" { member } "}"
              | "sealed" [ ident ] "(" codec_args ")" [ until ] [ attrs ] "{" { member } "}"
              | "coded"  ident   "(" codec_args ")" [ until ]
                [ "covers" "(" ref_list ")" ] [ attrs ]
                "{" { member } "}"
              | "indexed" "(" index_args ")" "{" { member } "}"
              | "opaque" ident "[" size_expr "]" [ attrs ] ";"
              | "tlv" ident "(" tlv_args ")" [ attrs ] ";" ;

variant       = "variant" ident "switch" "(" expr ")" [ attrs ] "{"
                { variant_arm } "}" ;
variant_arm   = ( "case" expr | "default" ) ":" arm_body ;
arm_body      = "error" ";" | "opaque" ";" | member ;

(* `max` after the brackets bounds a run whose length comes from the frame:
 * `u8 tail[remaining] max 470` (8.6.1, decision 0059). It counts ELEMENTS, as
 * the brackets do, where `until D max N` counts BYTES, as a scan does. The cap
 * is accepted here after any size and refused by the layout on a run something
 * already bounds -- a well-formedness rule rather than a grammar one. *)
array_spec    = "[" [ size_expr ] "]" [ "max" expr ] ;
size_expr     = expr | "remaining" ;

(* Section 8.6.1. Where a member ends when no length says so. The
   delimiter is a string literal rather than an expression: it would
   otherwise have to be evaluated against the data it is being looked
   for in. `max` bounds the scan. *)
until         = ( "until" | "before" ) delimiter { "|" delimiter }
                [ "max" expr ] ;
delimiter     = string | char ;

(* Section 8.6.8. A run of bytes that may precede a member and belongs to
   it. Single BYTES rather than sequences: a lead runs while the next byte
   is in the set, where a scan matches a sequence. Bare means the file's
   `whitespace` set, and is refused where there is none. *)
skip          = "skip" [ byte_set ] ;
byte_set      = char { "|" char } ;
pin           = "@" expr ;

attrs         = "[" attr { "," attr } "]" ;
attr          = ident [ "=" expr ] ;

(* Section 9.5. Three of a tlv region's arguments carry its item grammar --
   how a raw tag decodes into named parts, how each part sizes a value, which
   tags are named -- and the rest are ordinary attributes. They were kept as
   verbatim source text for a long time, which is why this was `attr { ","
   attr }` and the only thing anything read out of a `switch` was its labels. *)
tlv_args      = tlv_arg { "," tlv_arg } ;
tlv_arg       = "tag_decode" "=" tag_decode
              | "tag_identity" "=" ident    (* decision 0023 *)
              | "value_size" "=" value_size
              | "known"      "=" known_tags
              | attr ;
tag_decode    = "{" tag_part { "," tag_part } [ "," ] "}" ;
tag_part      = ident "=" expr ;      (* over `tag`, the raw tag, alone *)
value_size    = "switch" "(" ident ")" "{" value_case { "," value_case }
                [ "," ] "}" ;
value_case    = ( "case" digits | "default" ) ":" value_rule ;
value_rule    = digits                (* a literal byte count *)
              | "self_delimiting"     (* the value carries its own extent *)
              | "prefixed" "(" ident ")"
              | "error" ;             (* a wire type this schema refuses *)
known_tags    = "{" known_tag { "," known_tag } [ "," ] "}" ;
known_tag     = digits ":" ( ident | "{" known_attr { "," known_attr } "}" ) ;
known_attr    = "name" "=" ident
              | "wire" "=" digits
              | "type" "=" ident [ "[" "]" ] ;

(* Section 9.3. `base` says what an offset in the table is measured from.
   Absent means the region itself, which is the only choice bounded by the
   region's own extent -- see doc/decision/0024-index-offset-base.md. *)
index_args    = index_arg { "," index_arg } ;
index_arg     = "base" "=" ( "region" | "message" | ident )
              | attr ;

(* `array_spec` and `attrs` both open with "[", so this grammar is ambiguous as
   written: `u8 x [foo];` is either an array of `foo` elements or a scalar
   carrying the flag `foo`. Resolved by the attribute vocabulary, which is
   closed and fixed by the language:

     a bracket group is an attribute list when it holds "=" or "," at bracket
     depth 1, or when it holds exactly one identifier and that identifier is a
     known attribute name. Otherwise it is an array spec.

   "=" and "," cannot occur at the top level of a size expression -- "==" is a
   distinct token and an array has exactly one size -- so only the lone
   identifier is genuinely ambiguous, and the vocabulary decides it. Every
   bare-identifier bracket in this document resolves correctly under the rule.

   A `const` whose name collides with an attribute name is rejected at its
   declaration, which kills the residual ambiguity at the source rather than at
   each use. See doc/decision/0006-bracket-disambiguation.md. *)

type_ref      = scalar_type | ident ;
scalar_type   = uint | sint | float | bitfield | "bool" | "byte" ;
uint          = "u" digits ;          (* u1..u64, non-power-of-2 allowed *)
sint          = "i" digits ;
float         = "f16" | "f32" | "f64" ;
bitfield      = "bit" ;               (* single bit; uN < 8 also bit-packed *)

codec_decl    = "codec" ident "{" { codec_prop ";" } "}" ;
codec_prop    = "length_preserving"
              | "expansion" "=" ( added | "unbounded" | ratio )
              | "granularity" "=" granularity
              | [ "not" ] "seekable" [ "=" seekable ]
              | "authenticated" | "invertible" | "deterministic"
              | "systematic" | "error_propagating"
              | "tag_bytes" "=" digits | "nonce_bytes" "=" digits
              | "kernel" "=" kernel ;
granularity   = "byte" | "stream" | "bit" "(" size_or_any ")"
              | "symbol" "(" size_or_any ")"
              | "block" "(" size_or_any ")" ;
size_or_any   = digits | "any" ;
seekable      = "linear" | "permuted"
              | "blockwise" "(" size_or_any ")" ;
added         = "+" digits [ "bits" ] ;        (* 0046 *)
ratio         = ratio_form "(" digits "," digits ")" [ added ] ;
ratio_form    = "ratio_exact" | "ratio_padded" | "ratio_bounded" ;

requirement   = ( "require" | "assert" ) capability_expr ";" ;

(* One field, and what it derives from. The left side is a path rather
   than an expression: an invariant says which field situ maintains, and
   an equality nobody maintains is what `require` already is. *)
invariant     = "invariant" path "==" expr ";" ;
path          = ident { "." ident } ;
(* Section N, 26.95. A pure predicate over two messages, in temporal order --
   the first parameter is the message seen first, which is what lets a
   dissector say "response to frame N" without a second declaration. `must`
   and not `require`: section 16 gives `require` to the build-time gate and
   this is checked at run time against two views.
   The attribute list is the exchange's retransmission and timing contract
   (26.98), on the relation because the relation already identifies the
   exchange. Stated whole or not at all.
   doc/decision/0030-cross-message-relations.md *)
relation_decl = "relation" ident "(" param "," param ")" [ attrs ]
                "{" { must } "}" ;
param         = ident ":" qualified ;
must          = "must" expr ";" ;

(* A predicate over ONE message, a severity, an identity and a default
   rendering. Evaluated once, in `validate`, and at no other point -- so no
   `when` can read another and there is no order to define. The NAME is the
   contract and the text is a default: a consumer with its own catalogue
   keys on the name. A predicate over two messages is a relation, not a
   `when`. See doc/decision/0051-messages-a-schema-carries.md *)
when_decl     = "when" expr severity ident string ";" ;
severity      = "refuse" | "warn" | "note" ;
```

`register_decl` is defined in Section 15.

## 8. Type system

### 8.1 Scalars

- `u1` .. `u64`, `i1` .. `i64`. Non-power-of-two widths are legal.
- `f16`, `f32`, `f64` (IEEE 754).
- `bool` is `u1` with value constraint; `byte` is an alias for `u8` with
  `endian` irrelevant.
- **A width divisible by 8 is a byte-aligned scalar; every other width
  participates in bit packing with the surrounding fields.** So `u24` and `u48`
  are ordinary byte-aligned scalars, and `u3`, `u12` and `u20` are bit packed.
  What decides it is whether the width is a whole number of bytes, not whether
  it is a power of two.
- A bit-packed width above 8 bits cannot fit inside one byte at any offset, so
  it always straddles and therefore always needs `[allow_straddle]`
  (Section 8.2). That is the intended gate: the cost is reported rather than the
  width being refused. See `doc/decision/0005-integer-widths.md`.
- **Fixed point**: `q<int>_<frac>` signed, `uq<int>_<frac>` unsigned. `q16_16`
  is 32 bits, sixteen of them fractional; `q1_15` is the audio convention. The
  width is the sum, so the bit-packing rule above applies unchanged -- `q4_4`
  is a byte and `q2_3` packs.
- **BCD**: `bcd<digits>`, a nibble to a digit, most significant first. `bcd2`
  is one byte and holds 0 to 99, which is what an RTC puts in a seconds
  register. Twelve bits for `bcd3`, so an odd digit count packs.
- A packed-decimal field may declare a width: `bcd2 [bits = 7]` is three bits
  of tens over four of units, which is what a register holding a control bit
  above the decimal is -- a DS1307 spends the top bit of its seconds register
  on Clock Halt and two bits of its hours register on 12/24 and PM. The
  narrowing takes bits from the *top* digit and leaves the rest whole, the
  bound is `4 * (d - 1) < N <= 4 * d`, and `[bits]` on any other type is an
  error: every other type carries its width in its name, and `bcd<d>` names
  digits, which is why it is the one that can be narrowed. Section 8.2's
  packing rules apply to the result unchanged
  (`doc/decision/0027-narrowed-packed-decimal.md`).
- Both cost `repr = ValueConverted`, for different reasons the map states: a
  fixed-point field's stored integer is the value scaled by a power of two, and
  a BCD field's nibbles are digits. Neither generates floating point -- the
  target may have none, and the scale is exact, so the header carries
  `_SCALE` and `_FRAC_BITS` and the caller does the arithmetic in whatever type
  it has.
- A BCD field can hold a bit pattern that is not a number: a nibble above nine.
  The getter cannot report that, so the generated validator checks it, which is
  the only type where reading and validating disagree about what is possible.
- **A `[min]` or `[max]` on either is stated in the domain the getter
  returns** -- the raw stored integer for fixed point, the decoded number for
  BCD -- and never in the other one. So `[max = 100]` on a `q8_8 trim_ppm` is
  100 raw units, a little under 0.4 ppm, and a schema meaning 100 ppm writes
  `[max = 25600]`. That is the same number `validate` compares and the same
  number `VALUE_MIN`/`VALUE_MAX` export, so the three cannot drift apart; the
  caller converts with `_SCALE` where it wants the scaled figure, which is
  what those constants are for.

  Stated here because it was not stated anywhere, and the absence had already
  produced a wrong sentence in the compiler: bounds on these two types were
  withheld from the exported constants on the grounds that "the getter's value
  is scaled or decoded", which is false of fixed point in every backend and
  contradicts the bullet above. Settled by the copyright holder 2026-09-06.
  Note that `rtc.situ`'s own comment describes `trim_ppm` in the scaled domain
  -- "-128 to just under +128 parts per million" -- which is the reading a
  person naturally has and the reason this needs writing down rather than
  inferring.

### 8.1.1 Variable-length integers

Required, not optional: describing protobuf (9.7) is impossible without them,
and they are common in compact protocols generally.

```situ
varint_type leb128 {
    encoding  = leb128;         // base-128, low group first, continuation bit
    max_bits  = 64;
    minimal;                    // shortest encoding only; omit to allow padding
}

varint_type zigzag {
    encoding  = leb128;
    transform = zigzag;         // signed mapping, protobuf sint32/sint64
    max_bits  = 64;
    minimal;
}
```

Capability consequences, which are severe and must be reported clearly because
this is exactly the construct users reach for without understanding the cost:

- `size := Bounded(1, ceil(max_bits / 7))`
- **every following member in the frame** gets `offset := Dynamic`,
  `address := Unstable`
- `mutate := InPlaceSlack` when the new value encodes to the same length,
  `Shifting` otherwise
- `minimal` is required for `canonical`. Omitting it (which protobuf's wire
  format does permit) sets `canonical := NonCanonical` and must be reported with
  the reason "non-minimal varint encodings accepted".
- `access` of an array of varints is `Sequential`, never `Random`

**Two encodings, and what a declared `max_bytes` means.** `be128` is the same
shape with the high group first: ASN.1's identifier octets, MIDI's delta times,
SQLite's record varints. A format may also cap the encoded length below what
seven-bit groups would need, and `max_bytes` says so:

```situ
varint_type sqlite_varint {
    encoding  = be128;
    max_bits  = 64;
    max_bytes = 9;      // the ninth byte carries eight bits and ends the value
}
```

The bytes before the last carry seven bits each and the last carries what is
left -- here 64 - 7x8 = 8. Eight bits leaves no spare bit for a continuation
flag, so that byte is read whole and ends the value whatever its high bit says.
That is SQLite's ninth byte, and it falls out of the arithmetic rather than
being a second thing to declare. Too few bytes for `max_bits` is an error, and
so is a byte the range can never reach.

The advisor's varint suggestion (18.2) applies here: if a varint field carries a
`max` constraint, report the fixed-width alternative and its true cost. For
`max = 1500` a varint costs two bytes across most of its range anyway, so `u16`
is free and restores static offsets for everything after it.

**Reading one, and the bug that hid behind not reading one.** C emits two
accessors per varint field, because a varint answers two questions and a caller
usually wants one of them: `_get` decodes the value, and `_len` says how many
bytes it occupied. Everything after it in the frame is measured through `_len`,
which is what `offset := Dynamic` costs here -- a read, not a scan. `_get`
refuses a truncated value with `SITU_ERR_BOUNDS`, and where the type declares
`minimal`, a padded encoding with `SITU_ERR_CONSTRAINT`: a canonicality claim
nothing enforced would be a comment.

There was no accessor at all before, and the note said so on a defensible
argument -- emitting one would pretend the width is fixed. The note was the
smaller half of the problem. Three separate places computed a length for a
varint and all three returned **zero**:

| | consequence |
|---|---|
| `_length_expression` | every member after a varint was placed as though it occupied nothing, and read the varint's own bytes |
| `_count_expression` | `u8 payload[n]` for a varint `n` was a zero-length field |
| the field scope | a varint never entered it, so `u8 payload[n]` was refused with "no fields are in scope at this point" |

The first is the one that matters. `struct s { u8 kind; v n; u16 after; }`
generated an `after` accessor that looked like every other one and returned the
varint's bytes -- no diagnostic, no refusal, a plausible number. Situ's whole
argument is that an operation it cannot support is absent rather than wrong,
and here the absence of a varint accessor was read as "this construct is not
finished" while the members *around* it went on being generated confidently.

A gap that is declared in one place and silently wrong in another is worse than
either. What kept it invisible is that no example used a varint field: protobuf
declares `pb_varint` as a tlv `tag_type`, which the walk reads without ever
placing a member. `example/sqlite` is what asked for it (9.3).

**The other three backends had the same hole and a different bottom.** A varint
classified as `Member.NOTHING` -- it has no `scalar`, so it fell past every
branch -- and C++, Rust and Python emitted *nothing at all* for the member: not
an accessor, and not the note saying why. A member that simply vanishes is the
one shape a reader cannot ask about. But the member *after* it fared better
than C's: all three said "its offset cannot be resolved" and emitted nothing,
which is the safe half of the same gap. C, which does not ask the shared
classifier, placed it at zero instead.

So the assumption that three backends shared C's bug was wrong, and the reason
they did not is invariant 20 pointing the right way for once: they ask
`traverse.classify`, which had no answer for a varint and said so, while C
answered on its own and answered wrongly. There is a `Member.VARINT` now.

Each reads one in its own idiom: an `err` and an out-parameter in C++, a
`Result<u64>` in Rust, a property that raises in Python. All four refuse a
truncated value, refuse a padded one where `minimal` is declared, and decode
`zigzag` as signed. Checked against the cells in `example/sqlite/vectors.txt`,
whose varints are one byte each -- where `leb128` and SQLite's encoding agree,
and nowhere above 127.

### 8.2 Bit packing

- Fields narrower than 8 bits, and any `bit`, pack into the current byte
  according to the active `bit_order`.
- `bit_order msb_first` fills from the most significant bit of each byte.
- A field that would straddle a byte boundary is legal only if the enclosing
  struct declares `[allow_straddle]`; otherwise it is an error, because
  straddling silently forces multi-byte read-modify-write.
- Bit packing always sets `repr = ValueConverted` and `atomic = NonAtomic`
  for the affected fields (Section 11.1).

### 8.3 Endianness and bit order

- Both are scoped, and the scope is **positional**: a directive applies to the
  declarations that follow it and to nothing before it. A second directive
  changes the scope from that point on rather than rewriting what came before,
  which is what lets one file describe a protocol whose layers disagree about
  byte order. Overridable per struct via `[endian = little]`, and per field.
- Endianness applies to multi-byte scalars only.
There are three distinct endianness situations, and conflating them was an
error in an earlier draft of this document. They are separate constructs:

**1. Fixed endianness.** `endian big` / `endian little`. Compile-time known.
`repr = MemoryIdentical` when it matches the host, `ValueConverted` otherwise.
Fully canonical.

**2. Host endianness with no marker.** `endian native`. For in-memory and
same-machine IPC formats where host order is the point. Sets
`canonical = NonCanonical` for the containing struct, because the encoding
depends on the host. Requires `[allow_host_dependent]` on the struct so it
cannot be reached by accident.

*Whose* host is the question this construct invites and does not answer well.
`native` means the machine the generated code is compiled for, resolved by the
C compiler through `SITU_HOST_BIG` and never by situc -- those are different
machines on every cross build, and a generator that baked in its own order
would emit code that reads the wrong bytes on the target while compiling
cleanly. But that still leaves `native` describing the *writer's* order, and a
writer is not always the machine that matters: a server producing frames for a
weaker client wants the client's order, which is not its own.

So `native` is the wrong tool whenever the bytes leave the machine. Name the
order outright when it is known -- a schema that says `endian little` because
the client is little-endian is describing the format rather than the builder --
and use a marker when it is not. That is what the requirement for
`[allow_host_dependent]` is for: it makes reaching for `native` deliberate.

**3. Runtime-resolved endianness via a byte-order marker.** This is the TIFF
`II`/`MM` pattern, and it is a first-class construct:

```situ
endian_marker byte_order : u16 {
    little = 0x4949,        // "II"
    big    = 0x4D4D,        // "MM"
}

struct tiff_header [endian = from(byte_order)] {
    endian_marker byte_order;
    u16 magic  [must_eq = 42];
    u32 ifd_offset;                 // interpreted per byte_order
}
```

The compiler emits, per marker type, a host-order constant and accessor so a
writer can populate the field:

```c
#define SITU_TIFFHEADER_BYTE_ORDER_HOST  0x4949   /* on a LE build */
uint16_t situ_TiffHeader_byte_order_host(void);
```

This is what "endian native produces a variable that populates a header field"
means concretely: the marker value is derived from the build's host order, and
writers use it rather than hardcoding.

Capability consequences, which are mercifully narrow:

- `repr := ConditionallyConverted(byte_order)` for every field in scope. The
  swap decision is a parse-time branch on one already-read field.
- **`offset` and `size` are untouched.** Endianness never changes extent, so
  runtime-resolved endianness does not cost static addressability. This is the
  saving grace and the reason the construct is cheap to support.
- `canonical := CanonicalGiven(byte_order)`. Two byte sequences encode the same
  value at the format level, but exactly one does given the marker. See 11.1.
- Generated getters for these fields return by value only, never by pointer.
- A `must_eq` constraint on a field in marker scope is validated after the
  marker resolves, not before.

### 8.4 Alignment

- Situ does **not** insert implicit padding. Layout is exactly as declared.
  Use `reserved` or `pad_to` for explicit padding.
- The solver tracks each field's alignment relative to the message base and
  records it on the `align` axis. Unaligned multi-byte access is permitted but
  flagged, because it faults on some targets and is slow on others.
- `[require_aligned]` on a field turns a misalignment into an error.
- `pad_to(n)` inserts explicit padding to the next multiple of `n`.

### 8.5 Arrays

Five forms, with sharply different capability consequences:

| Form | Size | Consequence |
|---|---|---|
| `T x[N]` | compile-time constant | fully static |
| `T x[expr]` | parse-time from a prior field | frame-relative static elements; everything after becomes dynamic |
| `T x[remaining]` | to end of enclosing frame | must be last in its frame |
| `T x[remaining] max N` | as above, but at most N elements | `Bounded`; a longer frame is refused (0059) |
| `T x[] until "D"` | ends where the terminator would be an element | sequential access only (8.6.3) |
| `T x[] while (cond)` | ends after the element failing `cond` | sequential access only (8.6.6) |

Element type must have a fixed size for indexed access to be O(1). If the
element size is dynamic, the array is sequential-access only and the compiler
says so.

The last two rows read `T x[] until (cond)` here, marked OPEN and deferred to
phase 6, until long after phase 6 delivered them -- as two constructs rather
than one, and with `while` for the predicate because `until` had been given to
the terminator. A row nobody checks is a comment (section 0), and this one was
a comment claiming an open question that had been settled twice over.

### 8.6 Strings and bytes

There is no string type. Text is `u8 name[N]` with an optional
`[encoding = ascii | utf8]` attribute that is validated on parse if
strictness is enabled, and a `[nul_terminated]` flag. Length-prefixed text is
just a `u8[]` with a size expression. This avoids inventing string semantics
the underlying protocol may not have.

**Both attributes are implemented.** `[encoding = ascii | utf8]` is validated
by `validate`, strictly in the sense RFC 3629 requires -- an overlong form or
a surrogate half is refused, because either is a second spelling of a
character that already has one. An encoding situ cannot check is an error
rather than a silent nod, and the compiler refuses an encoding it does not
know by name.

**It is checked wherever the member has a span, which is everywhere.** A
fixed-width field is scanned from its offset for its declared count; a
delimited one is scanned over the content the scan found, not including the
delimiter. The second case was accepted and never checked for a long time,
because the gate asked whether situ can validate this *encoding* -- and
`ascii` always answers yes -- while what decided whether a check was emitted
was the *placement* (26.94). A rule enforced on one axis is silent on every
other, and the silence looks exactly like compliance.

**`[nul_terminated]` reads the declared size as the capacity.** The field is
its declared width whatever it holds, so nothing after it moves and `size(X)`
is unchanged; the content runs to the first zero byte. Two consequences follow
and both are stated rather than left to be found:

- The generated accessors gain `_len()`, the content length, bounded by the
  capacity. An unterminated field reports the whole width rather than reading
  past it -- a getter is not the place to discover a malformed field.
- `validate` refuses a field with no terminator in it. Without one, nobody
  knows where the content stops.
- The field is **`canonical = NonCanonical`**, because the bytes past the
  terminator do not affect the value and two buffers differing only there mean
  the same thing. The remedy the rule names is to zero the padding on write and
  require it on parse, which buys the single encoding back.

Delimiter-framed text is covered too, and has its own section below.

### 8.6.1 Delimited members

A member may end at a delimiter rather than at a length:

```situ
struct greeting {
	u8  magic[4];                       // binary, placed
	u8  version[] until "\r\n" max 16;  // text, scanned, capped
	u8  name[]    until "\0";           // text, scanned, unbounded
	u32 payload_len;                    // binary again -- but now behind a scan
	u8  payload[payload_len];
}
```

The delimiter is part of the member's extent and not part of its value, for
the reason `nul_terminated` counts its capacity: members partition their
struct's bytes exactly, and a delimiter nobody owned would be a hole.

**A scanned offset is not a dynamic one.** `Dynamic` is arithmetic over values
already read -- one addition, and it cannot fail. `Scanned` is a search:
linear in the distance to the delimiter, and the delimiter may not be there.
The map above reports `payload_len` as `offset=Scanned access=Sequential
address=Unstable` even though it is an ordinary big-endian `u32`, because that
is what putting it after text costs, and the remedy the blame chain names is
declaration order.

**`until D max N` bounds the scan.** With a cap the member is `Bounded` rather
than `Unbounded` and a missing delimiter is an error instead of a read to the
end of the buffer. Without one the member is also `effect = EffectOnRead`,
because the cost of a read then depends on the data rather than the schema --
which an embedded caller has to know before choosing the format.

**`[remaining] max N` bounds the FRAME, because such a run has no scan.**
Its length IS the remainder, so the cap cannot stop a read early -- it says
how long the frame may be, and a longer one is refused rather than read in
part (0059). The number counts ELEMENTS, as the brackets do, where
`until D max N` counts bytes, as a scan does: each spelling counts what its
own form counts. The two coincide for a `u8` run, which is why the
difference is written down here rather than left to be discovered.

**The content cannot contain the delimiter** -- structurally, not by a check.
The scan stops at the first occurrence, so a parsed member's content never
holds one, which is what makes the member `Canonical`. For HTTP that is what
defeats header injection on the read path: a value with a bare CR or LF cannot
be produced by parsing, because the scan would have stopped there.

What `validate` does check is the other half. A member whose delimiter is
absent ran to the end of the buffer rather than to its own end, and that frame
was cut short.

On the write path a delimited member is reached through its pointer, so
nothing situ generates can enforce anything; the header says so where the
covered-pointer note already says the rest.

Where a protocol genuinely admits the delimiter inside a field, say how it is
made inert -- `[quoted = "\""]` or `[escape = "\\"]`. Both relax the scan and
cost `canonical = NonCanonical`, because two byte sequences then encode one
value. Situ cannot tell which case a protocol is in; only the author knows
whether a comma inside a CSV field is possible, so this is stated rather than
inferred and the safe reading is the silent one.

### 8.6.2 Text-encoded numbers

A number may be written as digits rather than stored as bits:

```situ
struct message {
	u8       method[]  until " "     max 8;
	u8       target[]  until "\r\n"  max 256;
	decimal  u32       length until "\r\n" max 12;
	u8       body[length];
}
```

`decimal` and `hex`, and the scalar beside them gives the value's **domain,
not its width**: "7" and "1234567" are the same kind of field at different
widths, so a text number has no width to declare. `u32` says which values are
representable, which is what the range check is written against.

**Two ways to say where the digits stop.** `until "\r\n"` for a delimited
number, and `[3]` for one of declared width -- SMTP's reply code and HTTP's
status are both exactly three digits with nothing after them, and requiring
`until` made those unwriteable. A signed number has only the first, for the
reason below.

The two differ on canonicity, in the direction that surprises. A *padded*
field is `Canonical`: `007` is the only spelling of seven in three digits,
because `7` alone does not fit and the parse refuses a space, so the padding
is forced rather than optional. A delimited number is `NonCanonical` unless
`[minimal]` says otherwise.

A width is digits, not elements. `decimal u16 code[3]` is three bytes:
everywhere else `[n]` counts elements of the declared type, and here the type
is the value's domain rather than its storage. And the range checked is the
*field's* -- three digits hold 0..999 whatever `u16` would allow, so a check
written against the type would accept a value the field cannot represent.

**The getter takes an out-parameter and returns an error.** Every other scalar
getter returns the value, because every other conversion is total -- a byte
swap has an answer for any bit pattern. A decimal parse does not, and a getter
that returned 0 for `12x4` would be handing back a number nobody wrote. That
difference is the whole of `repr = TextConverted`, and it shows up in the
signature rather than in a comment.

Refused, each for a reason a protocol cares about: an empty run, because no
digits is not the number zero; a byte that is not a digit in that base,
including a trailing space; and a value outside the declared type. `validate`
refuses all three, so a frame that parses has a number in it.

**A text number gets no setter.** Writing 4096 where 12 was takes two more
digits than the field holds, so the write moves everything after it -- a
re-encode of the frame rather than a store.

**A length written in digits still drives an array.** `body[length]` works,
and the offset arithmetic that depends on it reads a non-failing helper rather
than the fallible getter: an offset function returning an error would make
every accessor downstream of it fallible, and the shape of this API is that
the checks happen once and the reads trust them. `validate` is that check.

**A text number is `NonCanonical` unless it says otherwise.** `007` and `7`
are one value written two ways, and above base ten so are `FF` and `ff`. That
is exactly what the `canonical` axis reports, and it is the axis decision 0020
argued has more to say about text than about binary -- so a text construct
that did not use it would be the argument left unmade.

`[minimal]` refuses a leading zero and an upper-case hex digit on parse, which
buys one spelling per value back and makes the field `Canonical`. It is the
same word `varint_type` already uses for the same reason. Refusing them
unasked would reject valid data, because most formats do permit `007`, so the
loose reading is the default and the map says what it costs.

**A signed text number is one optional `-` and then digits**, and that is the
whole of the grammar. `decimal i32 offset until ","` reads `-42`; `+42` and
`-0` are refused, each being a second spelling of a value that already has
one, which is the rule `[minimal]` states about leading zeros applied to the
sign.

It is **delimited only**. `decimal i32 n[6]` is refused, because a sign costs
a byte and the two facts cannot both hold: with the sign always written, `+5`
and `5` spell one value; with it written only for negatives, the field is not
a fixed width. situ refuses rather than choosing, since either choice is a
canonicality the schema did not state. That is also why the signed form's
range is the *type's* own rather than a digit count's -- there is no digit
count.

~~Fractional text formats are still refused. A point and an exponent are a
grammar rather than a number, which is the same line drawn below.~~ They
are `scaled`, since 0056, and that sentence was the reason it had to
disagree with: the integer form above already carries an optional sign and
a run of digits, so alternation and repetition cannot be what separates
them. What does is the value -- digits map onto an integer exactly and onto
a float only with correct rounding -- so `scaled i64 v until ","` reads an
EXACT decimal and reports a significand and a power of ten. See 26.332.

```situ
scaled i64  price  until ",";    // "12.5e3" is 125 and 2
```

### 8.6.3 Runs of records

An array of structs may end at a terminator rather than at a count:

```situ
struct field {
	u8  name[]  until ": ";
	u8  value[] until "\r\n";
}

struct block {
	field  fields[] until "\r\n";
	u8     body[remaining];
}
```

**The terminator ends the run, not the content.** This is the one place the
two spellings of `until` mean different things, and confusing them is silent:
for a byte array the delimiter is looked for anywhere, and for a run it is a
terminator only where an element would otherwise start. A CRLF inside the
first header line belongs to that line. Treating a run as a byte array found
that CRLF and stopped there, reporting one field where there were three.

The terminator belongs to the run's extent, as a delimiter belongs to the
member it ends, so `body` starts past the blank line rather than on it.

**Walked, not indexed.** A view is a value and situ never allocates, so there
is nowhere to keep a table of offsets; `access = Sequential` says so and
`indexed` is the construct for a caller who needs O(1). The element type gets
a generated `_extent`, because the next element starts where this one ends and
for a struct whose own members are delimited that is not a constant.

**Every walk is bounded twice.** By the view's limit, and by refusing to
advance on a zero-extent element -- which is not theoretical, since a record
whose members are all delimited and all empty occupies no bytes, and a walk
that took it would not terminate on input somebody chose.

Where the element has no extent situ can compute -- a `[remaining]` member
inside it consumes whatever view it is given, so a second element has nowhere
to begin -- no accessors are emitted and the header says why.

### 8.6.4 Optional whitespace, and tokens compared without case

Two attributes on a delimited member, both saying the same kind of thing: the
bytes carry less information than they appear to.

```situ
struct header {
	u8  name[]  until ":"    [case_insensitive];
	u8  value[] until "\r\n" [trim];
}
```

**`[trim]` separates framing from value.** The whitespace at either end is
still the member's bytes -- members partition their struct exactly, and the
span is unchanged, so nothing after it moves. What changes is the value those
bytes carry. `Content-Length:  5` and `Content-Length:5` are one message
written two ways, which is `canonical = NonCanonical`, and the accessor hands
back `5` for both.

Space and horizontal tab, and nothing else. Not what a locale calls
whitespace, which includes CR and LF -- delimiters in every protocol this is
for, so trimming them would eat the framing. This is HTTP's OWS.

**`[case_insensitive]` says two spellings are one token.** Also
`NonCanonical`, for the same reason: `Content-Length` and `content-length`
carry the same meaning, so the bytes do not follow from it.

**Both generate the comparison, and only the schema decides how.** Every
delimited byte run gets an `_eq`, because "is this field `Content-Length`" is
the question a caller of a text format actually asks; what
`[case_insensitive]` changes is whether ASCII case is folded. Leaving it to
the caller means leaving them to decide something the schema has already
decided -- and to reach for `strncmp` against a literal, which makes a prefix
a match. The generated comparison checks the length first.

ASCII case, and only ASCII. `tolower` is locale dependent -- in a Turkish
locale it maps `I` to a dotless form, so a header name would stop matching
itself -- and Python's `str.lower` is Unicode. A protocol token is ASCII by
definition and the fold is exactly `A`-`Z`.

The two compose with 8.6.2: `decimal u32 n until "\r\n" [trim]` accepts
` 5`, and adding `[minimal]` still refuses `007`.

### 8.6.5 A region that is both delimited and coded

`coded body(dot_stuffing) until "\r\n.\r\n" { ... }`: the extent is found by
scanning, and the bytes found are the transform's output.

**Scan first, decode second, and the order is the protocol's rather than a
convenience.** SMTP's dot-stuffing exists to protect its own terminator: a
body line consisting of one dot is sent as two, so `CRLF . CRLF` is
unambiguous in the encoded bytes and would not be in the decoded ones. A
decoder running first would have to know where to stop, which is what the
scan is for.

The extent comes from the delimiter and not from the codec's expansion. Those
answer different questions -- the expansion says how much the transform could
grow the interior to, and the delimiter says where the encoded bytes actually
stop -- and only the second is on the wire.

Such a region gets the scan accessors and no token comparison. `_eq` over a
transform's output would compare stuffed text, or ciphertext, against a
literal somebody wrote in the clear; the header says instead that the pointer
is the encoded form and the decode is the caller's.

What stays out is a grammar: alternation, repetition and rule references.
A parse tree has no offsets to be static about, and the capability map would
have nothing to say about one. See
`doc/decision/0020-delimited-data.md`.

### 8.6.6 A run that ends on a condition

`T x[] while (cond)`: the run ends **after** the element for which `cond` is
false.

```situ
struct ext_header {
	next_header  next;
	u8           hdr_ext_len;
	u8           data[(hdr_ext_len + 1) * 8 - 2];
}

ext_header chain[] while (next == 0 || next == 43 || next == 44
                          || next == 60) max 8;
```

**The difference from `until` is the quantifier, and it is the whole of it.**
`until` asks about the position *before* each element: is the terminator
standing where an element would start. `while` asks about the element just
read. Neither expresses the other, and two real protocols wanted the second:
an IPv6 extension chain ends after the header naming an upper-layer protocol,
and SMTP's multiline reply ends after the line whose separator is a space.

That pair is why the construct exists. SMTP asked first and was left
unwritten, with the schema saying so, because one protocol is not evidence
that a construct is general -- 26.23 records the wait and what ended it.

**The condition reads the element's own fields and nothing else.** Not the
enclosing struct's: its later members are placed after the run, so asking
about one would be circular, and its earlier members are a temptation worth
refusing, because a condition mixing both scopes reads as though it were
evaluated once and it is evaluated per element.

**A `while` run is never empty.** The first element is parsed before the
condition is evaluated. Whether the run is there at all is a different
question and `variant` is what asks it.

**`max N` bounds the walk**, and for a chain it should. RFC 8200 sets no limit
on the number of extension headers, and a receiver that walks an unbounded
chain on attacker-chosen input is the denial of service that RFC's own
security section warns about. The cap is a deployment decision and the schema
is the right place to record it.

The run is `access = Sequential`: how many elements there are is neither in
the schema nor stated in the data, so element N is reached by reading the N-1
before it. A count field ahead of the run would make it `Random`, at the cost
of a number the format has to carry and keep true.

### 8.7 Enums

- Backing type mandatory: `enum E : u8 { ... }`.
- `default = error` (reject unknown values on parse) or `default = pass`
  (accept and preserve). Default is `error`, deliberately: see 14.5.

  **Enforced in every backend**, which it was not until three of them existed
  to be compared. C, C++ and Rust each emit an `is_known` predicate over
  the declared members and call it from `validate`, so a field declared to
  admit seven protocol numbers no longer accepts all 256. Python emits no
  predicate: it imports `known_enum` from the runtime and `validate` calls
  it -- the same rule reached through a shared helper rather than a
  generated one. `default = pass` still emits the predicate in the three
  that generate one, because a caller may want to ask, and does not call it -- a schema
  that opts out of the rule is not second-guessed.

  It had been a comment in the generated C since phase 4. The Python backend is
  what surfaced it: writing a third implementation of the same schema meant
  comparing three answers, and this was the one place they differed for a
  reason that was nobody's intent.
- Exhaustiveness of a `variant switch` over an enum is checked. A missing case
  without a `default` arm is an error.

### 8.8 Reserved

`reserved` is a first-class declaration, not an annotation on an unnamed field:

```situ
reserved u3  [must_be_zero];
reserved u12 [preserve];
reserved u8  [unknown];
```

Semantics on parse:

- `must_be_zero` / `must_be_one`: validated; violation is a parse error under
  strict policy. This is malleability control, not pedantry.
- `preserve`: read and carried through unchanged on rewrite; excluded from
  canonical comparison.
- `unknown`: not validated, not preserved. Sets `canonical = NonCanonical` on
  the enclosing struct, and the compiler warns.

Default when unspecified is `must_be_zero`.

---

## 9. Structural constructs

### 9.1 struct

The default container. Members laid out in declaration order with no implicit
padding. Structs nest. A nested struct is a **frame** if any member of it has a
dynamic size (Section 12.2).

### 9.2 positional

`positional { ... }` is the default and the keyword is normally redundant. It
exists so that mixed-paradigm structs read symmetrically:

```situ
struct Msg {
    positional { u16 a; u16 b; }
    indexed(...) { ... }
    tlv opts (...);
}
```

Enforcement: inside a `positional` block, any construct that would introduce a
dynamic offset is an error. This makes the block a locally-checked staticness
guarantee, which is useful as a design tool -- wrap the part you want to keep
static and the compiler defends it.

### 9.3 indexed

An offset table followed by elements, FlatBuffers style:

```situ
indexed(offset_type = u16, count = hdr.n, base = table_start) {
    record entries[];
}
```

Capabilities: O(1) random access to elements (one indirection), elements need
not be fixed-size, element mutation is in-place if the element itself is fixed
size and same-size, insertion is not supported (offsets would shift).

**`base` says what an offset is measured from**, and defaults to the region:

| written | measured from |
|---|---|
| nothing, or `base = region` | the start of the region -- the table's first byte |
| `base = message` | the start of the message, as `at expr` means it (9.8) |
| `base = <member>` | the start of that member, declared before the region |

It had been written here as `base = table_start` since before there was a
parser for it, and the parser never read the argument at all -- which went
unnoticed because no backend walked the table, so nothing needed to know where
an offset pointed. Real formats disagree about the answer: a FlatBuffers vtable
measures from the table, a TIFF IFD and a ZIP central directory from the start
of the file, and TrueType's `loca` from the start of a different table. Situ
describes formats it did not design, so all four have to be sayable.

Which one is silent is invariant 9's rule rather than a preference. An offset
measured from the region cannot name a byte outside it, so the region's own
extent bounds it and a malformed table is caught by the check that is there
anyway; the other two can name anything in the message. The safe option is
silent and the unsafe option is loud. See
`doc/decision/0024-index-offset-base.md`.

A member named by `base` must be declared before the region -- the rule a size
expression follows, and for the same reason: the base has to be readable when
the table is walked. Naming a later member, or one that is not there, is an
error listing what is in scope.

**The table is walked.** C emits, per region, a count, the offset entry N
holds, and a view over the element it reaches. Three things fell out of
building it:

1. **An entry has a byte order and the placement did not record one.** The
   solver set `endian = None` on an indexed region, so a backend reading an
   entry had nothing to ask and took its default -- which reads a big-endian
   table little end first and hands back a plausible offset into real bytes.
   The region's scope decides it, as it does for every other scalar.
2. **An element that is not fixed-size has to be measured.** Which is the
   construct's whole purpose, so it is the case that matters: the extent is in
   the element's own bytes, so the sequence is to take the largest view the
   region allows, ask the element how long it is, and narrow to that. Handing
   back the provisional view would give the caller the rest of the region and
   call it an element.
3. **The gate on emitting an extent function at all** knew about runs and
   nested members and not about this. An indexed region asked for an extent
   that was never emitted, and reported that it could not compute one -- a
   declared gap that was not a real one, which is invariant 12's failure mode
   arriving from the other direction. All four backends had their own copy of
   that gate and all four were wrong the same way, which is invariant 13
   again: `traverse.classify` says what kind of member a placement is, and the
   four lists of what needs an element's extent were four places to forget.

**One walk, four shapes**, as with the tlv walk and for the same reasons:

| | element handed back as | offsets from the message |
|---|---|---|
| C | a `situ_view_t` out-parameter | the accessor takes the message too |
| C++ | the element's own class, out-parameter | the same, via `::at` |
| Rust | `Result<Element<'_>>` | not emitted -- a struct borrows the frame it was framed against, not the buffer |
| Python | the element's class, returned | anchored at the buffer rather than the view |

Rust is the one that refuses, and the refusal is the borrow checker being
honest rather than a gap: an element reached from the message outlives the
region's slice, so handing one back from `&self` would be claiming a lifetime
this view does not have.

`base` outside the region also weakens the region's addressing, which the
propagation table now records: `address := Unstable` where an offset can name
any byte in the message, because nothing about the region says an element is
inside it. That is section 9.8's argument for `at expr`, and an index table is
the same shape with a table in front of it.

**The worked example is `example/sqlite`**, and finding one took longer than
building the construct. Bare offset tables are rare: the common shape in the
wild is a table of *records* -- an offset and a length side by side -- which is
an array of structs and not this. A SQLite b-tree page is the real thing. Its
cell pointer array is `cell_count` two-byte offsets measured from the start of
the page, and the pointers are in key order while the cells fill the page
backwards, so element N is nowhere an array could have put it. The vectors come
from sqlite3.

It also settled `base`. The page's offsets are measured from the page, which is
a *member* of the frame rather than the message, and that distinction turned
out to matter: an offset measured from a member is still bounded by the frame,
so the check at the frame boundary still covers it. The propagation row now
weakens `address` for the message base alone. It had been written for both,
which was true of the dangerous one and pessimistic about the other.

**What the example asked for, and got.** It landed with elements of `u8
cells[]` rather than a `cell` struct, and that was not a modelling choice. A
SQLite table leaf cell is `varint payload_size; varint rowid; u8
payload[payload_size]`, and situ could not describe it for two independent
reasons, neither of them about `indexed`:

1. A SQLite varint is big-endian base-128, up to nine bytes, the ninth
   contributing eight bits rather than seven. `varint_type` has one encoding
   and it is `leb128`, which is the other byte order. Declaring it `leb128`
   would be a lie about the wire.
2. **A varint field has no accessor in any backend, and nothing may be sized by
   one.** `u8 payload[payload_size]` is refused with "no fields are in scope at
   this point": a varint member does not enter the field scope at all. The C
   backend's note has said "varint decoding is not generated yet" since phase
   4, and nothing on this page recorded it.

That second one is the larger finding, and it was not on this list. Section 9.7
argues describing protobuf is impossible without varints; situ has them as a
type the lattice reasons about correctly, and no backend can read one. The tlv
walk (9.5) reads a varint to *size* an item and hands the caller an offset and
a length, never a value -- which is why the gap survived the construct that
looks most like it would have found it.

Both are closed in all four backends, so the cell is a struct and `cells_at`
hands one back.
The example's test walks a page sqlite3 wrote through to the strings in its
rows, and a separate case checks the nine-byte varint against rowids sqlite3
produced for 2^56-1 and 2^60-1 -- the boundary where this encoding stops
agreeing with every other base-128.

Building that turned up two more things neither of which was about varints.
`gen-fuzz` declared `uint8_t buf[SITU_X_SIZE_MAX]` for a struct whose maximum
is 2^64-1 bytes, which is a constant too large for its type before it is an
allocation nobody can make; it caps at the same 4096 an unbounded struct gets.
And C emitted structs in the solver's insertion order, on the argument that the
solver resolves dependencies before their dependents -- true of containment,
false of an `indexed` element, whose type the region's extent does not depend
on. The first schema declaring its element after its container produced a
header that called an `extent` defined below it.

That ordering is `traverse.containment_order` now, which C++ and Python had a
copy of each and C had none. Both copies walked own members only, so a
variant's arm types came out in the right place *by alphabet* -- `sorted` roots
happened to put `A` and `B` before `S` -- and would not have for a schema
naming them the other way round.

### 9.4 opaque

A region with a size but no interior schema:

```situ
opaque ciphertext [hdr.length];
```

Capabilities: treat-as-bytes, whole-region replace if same size, no interior
access. Deliberately collapses structural capability in exchange for
flexibility. An opaque region can later gain structure via a stage transition
(Section 12.1) -- this is how sealed payloads work.

### 9.5 tlv

A schema-free region of tag-length-value items:

```situ
tlv options (
    tag_type    = u8,
    length_type = u8,
    known       = { 0x01 : Mtu, 0x02 : Window },
    unknown     = error          // or `skip`, or `preserve`
);
```

Capabilities: sequential iteration, append if slack exists, lookup by tag is
O(n), no stable addressing of contained items across any mutation, item
mutation in place only if same size.

`unknown = preserve` sets `canonical = NonCanonical`. Default is `error`.

The simple form above is insufficient for real TLV formats, where the tag is a
composite value and the value's extent depends on part of it. The general form:

```situ
tlv options (
    tag_type       = leb128,
    tag_decode     = { field = tag >> 3, wire = tag & 0x7 },
    value_size     = switch (wire) {
        case 0: self_delimiting,        // the value encodes its own extent
        case 1: 8,
        case 2: prefixed(leb128),       // varint length, then bytes
        case 5: 4,
        default: error,
    },
    duplicate_tags = error,             // or `allowed` for repeated fields
    known          = { ... },
    unknown        = error
);
```

`self_delimiting` is only legal for value types that carry their own extent
(varints, nul-terminated byte runs). Everything else must be sized by a literal,
a `prefixed(...)` form, or an expression.

`duplicate_tags = allowed` sets `canonical := NonCanonical` unless an ordering
rule is also declared, because the same content then has multiple valid
encodings.

**The item grammar is structure, and is checked.** `tag_decode`, `value_size`
and `known` are what a walk over the region would have to follow, and for a
long time all three were kept as the verbatim source text of their arguments,
"interpreted by the pass that needs them". No such pass existed. The only thing
ever read out of a `value_size` dispatch was its `case` labels -- scanned for by
a function that skipped the bodies it was walking past, because which wire types
a region accepts is a canonicality question and nothing else asked anything.

So a region could declare `switch (wire)` while decoding no `wire`, name a tag
whose wire type the dispatch rejects, or size a value at zero bytes, and all
three parsed. Each is now refused where it is written:

| refused | because |
|---|---|
| a dispatch selecting on a part `tag_decode` does not produce | nothing to select on |
| a tag part reading anything but `tag` | nothing else has been read yet |
| a `known` tag whose wire type the dispatch rejects | an accessor that could never read |
| two arms for one wire type | two answers for where the value ends |
| a value sized at zero bytes | a walk over them does not advance |
| `prefixed(T)` for a `T` that is not a type | no length to read |

The grammar for all of it is in section 7, and `situc dump-ast` prints what
situ made of a dispatch rather than leaving a reader to re-read the schema.

Which decoded part a `known` key matches is declared with `tag_identity`, and
required exactly where more than one answer is possible -- see
`doc/decision/0023-tlv-tag-identity.md`. Every candidate yields a walk that
runs and returns *an* item, so a guess here is wrong in the way invariant 9
exists to refuse: undetectably.

**The walk is generated, and the runtime holds no format.** C emits, per
region, a cursor over one item (`first`, `next`, `count`), a `find` keyed on
the identity part, and one accessor per named tag. The tag decode appears as
the schema wrote it -- `(uint32_t)(tag >> 3)` -- and each wire type is sized by
the arm that sizes it, `default: error` becoming `SITU_ERR_CONSTRAINT` because
a wire type the schema refuses has no extent to guess at.

It went the other way round for a long time, and that is the more useful half
of this section. `runtime/c/situ.h` carried a `situ_tlv_iter_t` with `tag >> 3`,
`tag & 0x7` and protobuf's four wire types written into it: a description of
one format, in the runtime, beside the schema language whose purpose is to
describe formats. Generated code never called it. Its only caller was
`test/generated/test_protobuf.c`, which hand-wrote a dispatch over field
numbers it `#define`d itself -- numbers the `known` map already declares.

Three descriptions of the protobuf wire format in one tree, and the schema's
was the one nobody read. The runtime cursor is gone, the test walks the
generated accessors, and what stays in `situ.h` is what the walk is built out
of: a varint read and the bounds-checked view operations. A runtime that knows
a wire type is a runtime that will disagree with a schema eventually.

**One walk, four shapes.** All four backends emit it, and as with the second
accessor family (26.30) what each emits is its own language's answer:

| | the item | iteration |
|---|---|---|
| C | a `struct` the caller declares | `first`/`next` on a cursor |
| C++ | the same, nested in the view class | the same, `err` returned |
| Rust | a module-scope struct; `_value` hands back a `&[u8]` | `Result<Item>`, returned rather than filled in |
| Python | a `__slots__` class | `first`/`next`, **and** a generator |

Rust returns the item because a cursor is a value there and an out-parameter
would be C's shape wearing Rust's syntax; it can also hand back the value as a
borrowed slice, which is the one thing the other three cannot say. Python
carries both protocols on purpose: `for item in msg.fields()` is what a Python
caller reaches for, and the cursor pair is what makes the four comparable.

Two things the port found. The Rust walk derived an item's start by
subtracting the tag's width, which is wrong for a length-prefixed value --
`used` is shadowed inside the dispatch arms and the cursor has already moved
past the prefix. It is asserted now, in all four, that `at` is where the item
starts and not where its value does. And the Python item was a `@dataclass`,
which resolves its annotations through `sys.modules[cls.__module__]` under
`from __future__ import annotations`: a module loaded by `exec_module` on a
spec -- which is how the example suite loads them -- raised on the class body.
Generated code should not care how it was imported.

The varint read had to be added to the Rust and Python runtimes, neither of
which had one. That is the correct direction for this file: a primitive, not a
format.

### 9.6 variant

Discriminated union selected by an expression over an already-parsed field:

```situ
variant body switch (hdr.type) {
    case msg_type.hello: Hello hello;
    case msg_type.data:  Data  data;
    case msg_type.close: Close close;
    default: error;
}
```

The discriminant must be parsed strictly before the variant in layout order;
forward references are an error. Size of the variant is the size of the
selected arm, so unless all arms are the same size the variant makes everything
after it dynamic -- which the advisor will point out, along with the padding
cost of equalizing them.

Dynamic is not unknown. The size of the selected arm is a switch on the
discriminant, and generated code evaluates it (11.6), so a variant can sit
inside a struct something walks a run of. An `opaque` default arm is the one
shape that cannot: it swallows whatever is left.

`default: error` is a parse-time rejection, not documentation. An unrecognised
discriminant fails `validate` with the "unknown version or variant
discriminant" error, in every backend. Stating this because it was true of the
specification and of nothing else for a long time -- see 11.6.

**The arms' members have accessors, and each asks the discriminant first.** A
variant has none of its own -- there is no one thing to hand back -- and for a
long time that meant its contents were unreachable: situ could measure a
variant and reject a bad discriminant, and a caller could not read the arm
that was there. The members are the enclosing struct's to emit, an arm not
being a type, and each is guarded:

```c
situ_err_t situ_label_body_text_ptr(situ_view_t view,
                                    const uint8_t **out, uint32_t *len);
/* SITU_ERR_VERSION unless `form` selects this arm. */
```

All four backends emit them, each refusing in its own way -- an `err` in C and
C++, a `VersionError` in Python, an `Err(Error::Version)` in Rust -- and the
question "which arm is this member in" is `traverse.arm_of`, asked once rather
than answered four times.

The check is per access, and that is what the construct costs: which arm is
present is a fact about the message, and nothing about the frame records the
answer. Reading another arm's bytes stays inside the view -- the extent bounds
it -- so it is a wrong answer rather than a fault, which is the kind situ
refuses rather than the kind it cannot prevent.

A struct-typed arm -- `case msg_type.hello: Hello hello;`, which is this
section's own example and so the common shape -- gets a guarded sub-view over
it. Its own members belong to its type and are emitted there, which is what
makes the sub-view the whole of the work.

---

### 9.7 Describing protobuf

Situ must be able to describe the protobuf wire format. This is both a real
requirement and the best available conformance test for the language, because
protobuf is close to the worst case on every capability axis.

```situ
varint_type pb_varint {
    encoding = leb128;
    max_bits = 64;
    // no `minimal`: the protobuf wire format accepts non-minimal encodings
}

struct proto_message {
    tlv fields (
        tag_type       = pb_varint,
        tag_decode     = { field = tag >> 3, wire = tag & 0x7 },
        value_size     = switch (wire) {
            case 0: self_delimiting,
            case 1: 8,
            case 2: prefixed(pb_varint),
            case 5: 4,
            default: error,             // groups (3, 4) unsupported
        },
        duplicate_tags = allowed,       // repeated and last-wins scalars
        known = {
            1 : { name = user_id,  wire = 0, type = pb_varint },
            2 : { name = username, wire = 2, type = u8[] },
            3 : { name = score,    wire = 5, type = f32 },
        },
        unknown = preserve              // protobuf semantics
    );
}
```

The expected capability outcome, which the compiler must produce and explain:

```
proto_message           size=Unbounded  offset=AbsoluteStatic(0)
proto_message.fields    access=Sequential  address=Unstable  mutate=InPlaceSlack
proto_message.fields[*] offset=Dynamic
canonical = NonCanonical, five independent causes:
  - non-minimal varint encodings accepted (pb_varint has no `minimal`)
  - duplicate_tags = allowed with no ordering rule
  - unknown = preserve
  - field order is unconstrained
  - packed and unpacked repeated encodings both legal
```

**This is the acceptance test.** `situc explain proto_message` must enumerate all
five causes with source locations. A schema language that can describe protobuf
and then tell you precisely why protobuf cannot be canonically encoded has
demonstrated that its capability reasoning is real rather than decorative.

What Situ deliberately does *not* reproduce: field-number-as-identity for
schema evolution (Section 19.1), and re-serialization that preserves unknown
fields in their original positions. A Situ description of protobuf can parse and
report unknown fields; round-tripping them byte-identically requires
`unknown = preserve` plus an explicit ordering rule, and the compiler will say so.

### 9.8 A member the data positions

```situ
struct bmp {
    bitmap_file_header  hdr;
    bitmap_info_header  info;
    u8  pixels[info.image_size] at hdr.pixel_offset;
}
```

`at expr` places a member where a field says, measured from the start of the
*message*, rather than after the member before it. Three formats asked for it:
BMP's `pixel_offset`, TIFF's `ifd_offset`, and DNS name compression's pointer
-- which is worth noting because two of those are file formats and one is a
protocol, so the capability does not follow the file/protocol line and there is
no `target file` for it to hang off.

**It is a reference, not a member in the ordinary sense.** It joins no offset
chain: it contributes nothing to the enclosing struct's extent, and the member
declared after it sits where it would if the located member were not there. A
variable-length member makes everything after it `Dynamic`; this makes nothing
after it anything. That is the whole reason it is a separate construct rather
than an unusually-sized member.

**A word rather than `@`.** `@ expr` is `pin`, which asserts the offset the
solver computed and places nothing -- the opposite meaning. Overloading the
symbol would make the two indistinguishable at a glance, which is the mistake
decision 0006 exists to avoid.

**On the lattice** it is `offset := DataPlaced`, a value between `Dynamic` and
`Scanned`: reaching it is one read and an addition, so it is not a search, but
the number comes from the message rather than from what precedes it. And
`address := Unstable`, because writing one field moves bytes far away from it.

**The bounds check cannot be amortised.** Section 20.2 amortises at the frame
boundary, and that argument holds for offsets the frame is known to contain
(invariant 41). This one is a number the message chooses and may point
anywhere, so it is checked on every call. `offset = DataPlaced` in the map is
exactly the notice that it costs that.

**The accessor takes the message as well as the view.** `situ_view_t` is
`{ base, limit, generation }` and carries no message origin; only `situ_msg_t`
knows where offset zero is. So:

```c
situ_err_t situ_bmp_pixels_view(const situ_msg_t *msg, situ_view_t view,
                                situ_view_t *out);
```

The alternative was a fourth word in every view, growing the core type by half
for a construct few schemas use. Taking both is the honest signature: the view
reads the offset field, the message says where zero is.

**What it does not do is follow a pointer.** The DNS case needs the offset
resolved *and* the parser re-entered there with a cycle check and a budget,
which is control flow rather than layout (11.5). `at` gives a schema the first
half and says so.

## 10. Expression language

Used for array sizes, variant discriminants, offset pins, constraints, and
capability requirements. It must stay **total and decidable**; the layout
solver evaluates it symbolically.

Permitted:

- integer literals, `const` references
- references to previously-declared fields in the enclosing scope chain
- arithmetic: `+ - * / %` (integer), bitwise `& | ^ ~ << >>`
- comparison and boolean operators (in constraints only)
- `size(X)`, `offset(X)`, `count(X)`, `remaining`
- `min`, `max`, `align_up(x, n)`

Forbidden, by construction:

- function calls, recursion, iteration
- forward references to fields later in layout order
- references to the *result* of a transform (see 13.2 -- this is the
  decidability boundary)
- floating point

Every expression carries a symbolic interval `[lo, hi]` derived from the
declared bounds of its inputs. This interval is what the solver uses to compute
worst-case sizes and to decide whether an offset is statically known. An
expression whose interval is a single point is a compile-time constant even if
it textually references a field -- for example `x[hdr.n]` where
`hdr.n [must_eq = 4]`.

---

## 11. The capability lattice

The core of the compiler. Each field and region carries a **capability vector**:
one value per axis. Axes are independent; each is a lattice with a defined
weakening order. Constructs weaken axes; nothing strengthens them.

### 11.1 Axes

| Axis | Domain (strongest to weakest) | Meaning |
|---|---|---|
| `size` | `Fixed(n)` > `Bounded(lo,hi)` > `Unbounded` | byte extent |
| `offset` | `AbsoluteStatic(n)` > `FrameStatic(n)` > `Dynamic` > `DataPlaced` > `Scanned` | position knowledge, and what it costs to get: `Dynamic` is arithmetic over values already read, `DataPlaced` is one read of an offset the message chose and so cannot be bounds-checked at the frame (9.8), `Scanned` is a search that can fail (8.6.1) |
| `access` | `Random` > `Sequential` | can reach element N directly |
| `mutate` | `InPlaceFixed` > `InPlaceSlack` > `Shifting` > `RewriteRequired` > `Immutable` | write cost |
| `address` | `Stable` > `FrameStable` > `Unstable` | can a pointer be held |
| `align` | `Aligned(n)` > `Unaligned` | relative to message base |
| `repr` | `MemoryIdentical` > `ValueConverted` > `TextConverted` > `ConditionallyConverted(f)` | is the value literally the bytes, and can the conversion fail: a byte swap is total, a decimal parse is not (8.6.2) |
| `atomic` | `AtomicWord` > `NonAtomic` | single-instruction access possible |
| `canonical` | `Canonical` > `CanonicalGiven(f)` > `NonCanonical` | exactly one valid encoding |
| `stage` | `CompileTime` < `ParseTime` < `TransformTime` < `VerifyGated` | when resolvable (later = more gated) |
| `auth` | `Uncovered` / `Covered(obligation)` | which obligations cover these bytes: a tag (14.2), or an invariant that derives a value from them (16.1) |
| `secrecy` | `Public` / `Secret` | affects generated API |
| `effect` | `Pure` > `EffectOnRead` / `EffectOnWrite` / `EffectBoth` | MMIO side effects, and an uncapped scan, whose read cost depends on the data (8.6.1) |

Notes on the less obvious ones:

- **`repr`**: a big-endian `u32` on a little-endian host is `ValueConverted`.
  The value is not the memory. This matters because "in-place mutation" of a
  converted field is a read-swap-write, not a store, and because a caller
  cannot take a pointer to the value. Most systems ignore this distinction;
  Situ makes it explicit. `ConditionallyConverted(f)` means the swap decision is
  a parse-time branch on field `f` (a byte-order marker, Section 8.3); the
  generated accessor carries the branch, and the branch is on a public,
  layout-irrelevant value so it is not a side channel.
- **`canonical`**: `CanonicalGiven(f)` means the format admits more than one
  encoding of a value, but exactly one given the value of field `f`. This is the
  correct classification for a byte-order-marked format. Consequence for
  signing, stated as a rule: **verify over received bytes, never over
  re-encoded bytes.** A writer is deterministic (it always emits host order plus
  the matching marker) even though the format is not, so distinguish
  `deterministic_writer(X)` from `canonical(X)` in requirements.
- **`atomic`**: bit fields are never atomic (read-modify-write of the
  containing byte). Multi-field updates are never atomic in v0. The system
  makes no atomicity promise it cannot keep.
- **`stage`** is the only axis that increases (gets later/more gated) rather
  than weakening; treat it uniformly as "monotone in the direction of less
  usable".
- **`auth`** is not ordered; it is a set-valued tag identity. Mutating bytes
  with `Covered(t)` marks tag `t` dirty (Section 14.2).

### 11.2 Vector ordering

Vector A is at least as strong as B if A is at least as strong as B on every
axis. This is a product lattice, so incomparable vectors exist and that is
fine -- the compiler never needs a total order, only meet (worst case) when
combining constructs.

Meet is computed pointwise. A struct's vector is the meet of its members' plus
whatever the struct construct itself imposes.

**Four axes are not the meet**, and `resolve._struct_vector` forces them after
meeting. `offset` and `align` go back to their strongest values, because a type
is not placed anywhere and a member's position says nothing about the type;
`size` takes the layout's own extent, which it knows directly; and `auth` is
not the meet at all.

**`auth` on any line -- member, region or struct -- names an obligation only if
that obligation covers EVERY byte the line is about.** One rule at every level,
settled with `fuzznet` across 26.519 and 26.522. An obligation never covers its
own bytes, because coverage means *writing these bytes leaves it stale* and a
checksum does not invalidate itself; that exemption is what keeps the rule from
emptying the axis, since a tag inside a struct never literally covers all of
it.

The meet is the other reading of the same relation and stays that way: inside
the lattice `auth` meets toward `Covered` and UNIONS its parameters, because
there the question is *writing these bytes stales which obligations*, and
mutating a byte covered by any of them stales all of them. A union is right
there and over-claims on a rendered line, which is the whole of the
distinction.

### 11.3 Propagation rules

These are normative. Implement them as a table, not as scattered conditionals
-- and the rows below are transcribed from `situc/propagate.py`, which is where
they run. `test_the_spec_table_matches_the_rows` fails if the two drift, which
is the only reason this list can be trusted: it was hand-maintained for a while
and fell about twenty rows behind, all of them things the compiler was already
doing.

The `Rule` column is the name that appears in a blame chain, so an `explain`
output can be read against this table directly.

**Scalars and bit packing**

| Rule | Construct | Effect |
|---|---|---|
| `non-native-endian-scalar` | a multi-byte scalar in a declared byte order | `repr := ValueConverted` |
| `endian-marker-scope` | a field whose byte order comes from an `endian_marker` | computed from the construct |
| `bit-field` | a bit-packed field | `repr := ValueConverted`, `atomic := NonAtomic`, `align := Unaligned` |
| `straddling-bit-field` | a bit field crossing a byte boundary | `atomic := NonAtomic` |
| `unaligned-multi-byte-scalar` | a multi-byte scalar not known to be on its boundary | `atomic := NonAtomic` |
| `odd-width-scalar` | a scalar whose width is not a machine word | `atomic := NonAtomic` |
| `aggregate-or-array` | a struct-typed field or an array | `atomic := NonAtomic` |
| `fixed-point` | a fixed-point field | `repr := ValueConverted` |
| `bcd` | a packed binary-coded decimal field | `repr := ValueConverted` |
| `varint` | a variable-length integer | `size := Bounded`, `mutate := InPlaceSlack`, `align := Unaligned`, `atomic := NonAtomic`, `repr := ValueConverted` |
| `non-minimal-varint` | a varint type without `minimal` | `canonical := NonCanonical` |
| `endian-native` | `endian native` | `canonical := NonCanonical` |

**Size and position**

| Rule | Construct | Effect |
|---|---|---|
| `bounded-size` | a member whose length comes from an earlier field | `size := Bounded`, `mutate := Shifting` |
| `unbounded-size` | a member with no upper bound on its length | computed from the construct |
| `data-placed` | a member the data positions, rather than the members before it | `offset := DataPlaced`, `address := Unstable` |
| `dynamic-predecessor` | a dynamically sized member earlier in the same frame | `offset := Dynamic`, `address := Unstable` |
| `frame-relative` | a member of a frame, addressed from the frame base | `offset := FrameStatic` |
| `dynamic-element-type` | an array whose element type is variable-sized | `access := Sequential` |
| `variant-unequal-arms` | a variant whose arms are not the same size | computed from the construct |

**Text protocols (8.6)**

| Rule | Construct | Effect |
|---|---|---|
| `delimited-member` | a member that ends at a delimiter | `mutate := Shifting` |
| `relaxed-delimiter` | a delimited member whose delimiter may occur in its content | `canonical := NonCanonical` |
| `unbounded-scan` | a delimited member with no cap on the scan | `effect := EffectOnRead` |
| `scanned-predecessor` | a member found by scanning for a delimiter earlier in the frame | `offset := Scanned`, `access := Sequential`, `address := Unstable` |
| `skipped-lead` | a member that begins after a run of whitespace | `offset := Scanned`, `access := Sequential`, `address := Unstable`, `effect := EffectOnRead` |
| `repeat-while` | a run that ends after the element failing a condition | `access := Sequential`, `mutate := Shifting` |
| `text-number` | a number written as digits rather than stored as bits | `repr := TextConverted` |
| `non-minimal-text-number` | a text number that accepts leading zeros | `canonical := NonCanonical` |
| `trimmed-value` | a value with optional whitespace around it | `canonical := NonCanonical` |
| `case-insensitive-token` | a token compared without regard to case | `canonical := NonCanonical` |
| `nul-terminated` | a nul-terminated field | `canonical := NonCanonical` |

**Aggregates**

| Rule | Construct | Effect |
|---|---|---|
| `tlv` | a `tlv` region | `access := Sequential`, `address := Unstable`, `mutate := InPlaceSlack` |
| `tlv-unordered-items` | a `tlv` region with no ordering rule | `canonical := NonCanonical` |
| `tlv-non-minimal-tag` | a `tlv` region whose tag type accepts non-minimal encodings | `canonical := NonCanonical` |
| `tlv-packed-and-unpacked` | a `tlv` region accepting both packed and unpacked encodings of a repeated value | `canonical := NonCanonical` |
| `tlv-unknown-preserve` | a `tlv` region with `unknown = preserve` | `canonical := NonCanonical` |
| `tlv-unordered-duplicates` | a `tlv` region with `duplicate_tags = allowed` and no ordering rule | `canonical := NonCanonical` |
| `opaque` | an `opaque` region | `access := Sequential`, `mutate := RewriteRequired` |
| `indexed` | an `indexed` region | `address := FrameStable` |
| `indexed-outside-the-region` | an `indexed` region whose offsets are measured from the start of the message | `address := Unstable` |

**Codecs (13)**

| Rule | Construct | Effect |
|---|---|---|
| `codec-not-invertible` | a codec with no inverse | `mutate := Immutable` |
| `codec-needs-decode` | a codec that is neither systematic nor length-preserving | `stage := TransformTime`, `access := Sequential` |
| `codec-whole-region-rewrite` | a length-preserving codec that is not seekable | `mutate := RewriteRequired` |
| `codec-block-granularity` | a length-preserving codec with block granularity | `mutate := InPlaceSlack` |
| `codec-permuted` | a codec whose output positions are a permutation | `address := Unstable` |
| `codec-not-deterministic` | a codec that is not `deterministic` | `canonical := NonCanonical` |

**Cryptography (14)**

| Rule | Construct | Effect |
|---|---|---|
| `covered-by-tag` | bytes covered by an authentication tag | computed from the construct |
| `verify-gated` | the interior of a sealed region | `stage := VerifyGated` |
| `allow-unverified-read` | `sealed(...) [allow_unverified_read]` | `stage := TransformTime` |
| `tag-field` | an authentication tag or checksum | `mutate := Immutable` |
| `secret-field` | a `[secret]` field | `secrecy := Secret` |

**Registers (15)**

| Rule | Construct | Effect |
|---|---|---|
| `register-partial-word` | a register field narrower than the bus access | `repr := ValueConverted`, `atomic := NonAtomic` |
| `register-read-only` | a register field the bus does not let you write | `mutate := Immutable` |
| `register-rmw-unsafe` | a partial-width field in a register whose reads are not free | computed from the construct |
| `register-side-effect` | a register field whose access has a side effect | computed from the construct |

**Files (0047)**

| Rule | Construct | Effect |
|---|---|---|
| `file-durable-write` | a member of a schema worked on in a file | `effect := EffectOnWrite` |
| `file-append` | a member of a file the schema may append to | `address := Unstable` |

**Declared, not inferred**

| Rule | Construct | Effect |
|---|---|---|
| `reserved-unknown` | `reserved [unknown]` | `canonical := NonCanonical` |
| `enum-default-pass` | an enum with `default = pass` | `canonical := NonCanonical` |
| `strictness-lenient` | `strictness = lenient` | `canonical := NonCanonical` |
| `declared-non-canonical` | a schema saying its encoding is not canonical | computed from the construct |
| `derived-field` | a field an invariant maintains | `mutate := Immutable` |
| `versioned-member` | a member present only from a given protocol version | `stage := ParseTime` |

**The critical rule, stated once:** a construct with dynamic size weakens the
`offset` and `address` axes of every *subsequent* member of its enclosing
frame, and of nothing else. It does not weaken members of parent frames before
it, and it does not weaken its own interior. This locality is what makes
islands of staticness work.

**Row precedence on the `mutate` axis.** The generic size rows -- the ones that
cost a bounded or unbounded size in the abstract -- must not fire for a
construct that owns a row of its own. Both rows land on `mutate`, the meet
takes the weaker, and the generic reason then wins the axis and buries the
specific one: a varint reports "size is bounded" where it should report the
slack it actually has, and the blame chain leads nowhere useful. Every
construct with its own row is therefore listed as owning `mutate`, and the
generic rows skip it. This has been got wrong four separate times -- varints,
variants, `tlv`, `opaque` -- so it is stated here rather than left to be
rediscovered.

### 11.4 Worked example

For 5.2 `message`:

```
message.hdr              offset=AbsoluteStatic(0)  size=Fixed(11)   mutate=InPlaceFixed
message.hdr.seq          offset=AbsoluteStatic(7)  repr=ValueConverted (big endian)
message.opts             offset=AbsoluteStatic(11) size=Bounded(0,1500) mutate=Shifting
message.recs             offset=Dynamic            access=Random
message.recs[]           offset=FrameStatic(0)     size=Fixed(8)    address=FrameStable
message.recs[].value     offset=FrameStatic(6)     mutate=InPlaceFixed
message.trailer          offset=Dynamic            size=Bounded(0,...)
```

`require in_place(message.recs[].value)` passes. `require absolute_static(message.recs)`
fails with blame on `message.opts`.

### 11.5 Declared weakening

The propagation table (11.3) infers a vector from constructs. It is sound for
what it can see, and there are formats where what it can see is not the whole
argument.

DNS name compression is the case that forced this. A name is a run of labels,
each a length byte and that many text bytes, ended by a zero -- and a label
whose top two bits are `11` is instead a pointer to a name earlier in the same
message. Every construct in `example/dnsname/dnsname.situ` is canonical taken
alone: a `u2`, a `u6`, a byte run, a `while`. And the format still admits many
encodings of one name, because the redundancy is not inside the name at all --
it is between the name and bytes elsewhere in the message that no per-member
rule ever sees together.

So a schema may say so:

```situ
struct name {
    label labels[] while (form == 0 && rest != 0) max 128
        [non_canonical = "a name may be spelled uncompressed, or as a pointer to any earlier occurrence of any suffix of it"];
}
```

The attribute names one axis and one value, and the reason is required and
carried verbatim into the blame chain -- `situc explain` prints *the schema
says so:* followed by the author's words, because a weakening nobody can act on
is worse than none, and the author is the only one who knows why.

Three properties make it safe to add:

1. **It can only weaken.** It enters propagation as another effect, met with
   the rest (invariant 2). Asserting it on a field that is already
   non-canonical for a reason of its own neither restores it nor displaces the
   reason it already had; both appear in the blame.
2. **It touches one axis.** A blanket "this is unusual" would make the vector
   useless for every other question asked of it.
3. **It is visible.** It is a row in the table like any other, it appears in
   the `.situ.map`, and a diff shows it arriving or leaving.

What it is not is a way to silence a diagnostic. Every *inferred* weakening
stands regardless; this can only add. A schema that wants a stronger vector has
to change the layout, which is the whole point of the lattice.

The complementary limit is worth stating plainly, because it bounds situ rather
than the attribute: situ can *describe* a compressed name completely and *walk*
one, and cannot *follow* the pointer. Resolving one means re-entering the parser
at an arbitrary earlier offset, with a cycle check and a budget -- control flow,
not layout. The schema says where the pointer is and what it means; a consumer
follows it. That boundary is much narrower than "situ cannot describe this",
and naming it precisely is the difference between a limit and a shrug.

### 11.6 A variant's extent

Walking the run above needs each label's length, and a label is a `variant` on
its top two bits. For a while the answer was that a variant has no computable
extent: it is whichever arm the discriminant selects, and the arms differ in
length, which is what a variant is for.

That is true of the *constant* and false of the value. Each arm has a length,
and which arm applies is a question generated code can ask. So the extent of a
variant is a conditional chain over the discriminant, in whichever form the
target spells one -- a ternary in C and C++, a conditional expression in
Python, an `if`/`else` expression in Rust:

```c
extent = 1u + (form_get(view) == 0u ? rest_get(view)
             : form_get(view) == 3u ? 1u : 0u);
```

A variant is measurable when every arm is. An `opaque` default arm is the
exception and is refused, because it swallows whatever is left -- `[remaining]`
spelled differently, and the layout already says so by leaving the variant with
no maximum.

The comparison is against the discriminant folded to an integer. `case k.a:`
has to become a comparison in four languages and each spells an enum member
differently, so the compiler resolves it once and the generated comment keeps
the name the author wrote.

Two consequences worth naming, because both were latent until something walked
a variant:

1. **`default: error` had to start being enforced.** Section 14.5 has always
   said an unrecognised discriminant is rejected on parse, and no backend
   rejected it -- `SITU_ERR_VERSION` was defined, commented "unknown version or
   variant discriminant", and returned by nothing. Nothing noticed while a
   variant had no extent, because nothing reached one. It stops being
   ignorable the moment a length depends on the discriminant.
2. **An unmatched discriminant contributes zero, and that is not a claim.** It
   is the value the run walk already refuses to advance by, so the run stops
   rather than looping. It is not a claim that such a message is empty --
   `default: error` says there is no such message, and `validate` is where that
   is said. The two answer different questions and the walk must stay total.

---

## 12. Staging and typestate

### 12.1 Stages

Layout resolution is staged. Each stage is a gate; capabilities behind a gate
are unavailable until the gate is passed.

| Stage | Resolved by | Example |
|---|---|---|
| `CompileTime` | the solver | offsets in a fully static struct |
| `ParseTime` | reading a prior field | `recs[hdr.n]` base offset |
| `TransformTime` | running a codec | interior of a compressed region |
| `VerifyGated` | authenticating a tag | interior of a sealed region |

The generated API encodes the staging directly: you cannot obtain a reference
into a region whose stage has not run. This is typestate. In Rust it is
distinct types; in C it is distinct opaque handle types plus debug-mode checks.

### 12.2 Views and frame-relative staticness

A **view** is a handle carrying a resolved frame base plus a bounds limit. It
is the mechanism that gives dynamically-positioned static structs their static
capabilities back.

Design requirements:

1. **The generated accessor type is identical** whether a struct sits at a
   static or dynamic position. Only *acquisition* differs: a compile-time
   constant base versus one runtime resolution. Do not generate two accessor
   families; that doubles the codegen surface and muddies the capability map.
2. **Bounds checking is amortized at the frame boundary.** Acquiring a view
   performs the check once; the N subsequent field accesses within it are
   constant-offset with no further checks. On a small-memory target this is
   the difference between viable and not.
3. **Field access within a view is a constant offset from the view base.**
   The generated C should compile to `base + K`.

### 12.3 Invalidation

A view is valid only while nothing upstream of its frame shifts. This is the
bug class most likely to bite users, so it must not be implicit.

- **Rust backend**: express with borrows. Mutating a preceding length field
  requires `&mut` on the message, which invalidates outstanding views at
  compile time. This is free and exact.
- **C backend**: cannot be enforced by the type system. Therefore:
  - the message object carries a `generation` counter
  - every view carries the generation it was created at
  - any mutation that can shift layout increments the generation
  - in debug builds (`SITU_CHECKED`), every view access asserts the
    generation matches; in release builds the check compiles out
  - the generated header documents, per view type, exactly which operations
    invalidate it

Do not skip the generation counter as an optimization. It is the only thing
standing between a user and silent memory corruption, and it costs one
comparison in debug builds.

A consequence for the generated C signatures: **a setter that can shift layout
takes the message, not just the view**, because it has to bump the generation.
The cost shows up in the signature, where the caller cannot miss it; a comment
would not be enough. A field that drives a length gets that setter and no
other, so there is no way to write it without paying for the invalidation.

---

## 13. Transforms (codecs)

A transform is an opaque operation over a byte region -- encryption,
compression, encoding -- supplied by the user as an implementation and
described to the compiler by a **property signature**.

### 13.1 Two tiers of codec

A codec enters the compiler in one of two ways, and the distinction matters:

**Tier 1: extern codec.** The user supplies the implementation; the schema
declares a property signature. The compiler trusts the declaration and never
learns what the algorithm does.

**Tier 2: derived codec.** The codec is described declaratively in terms of the
kernel families in 13.4, and `situc` **generates the implementation and derives
the property signature from the description.** The user declares nothing about
seekability or expansion; the compiler proves it.

**The property signature is the interface between both tiers and everything
downstream.** This is the load-bearing architectural decision of the transform
system. The capability lattice consumes property signatures and nothing else, so
adding generated codecs later is purely additive: the lattice never learns what
Manchester encoding is, and no propagation rule changes when tier 2 arrives.

Two consequences worth stating plainly:

- **Tier-1 codecs can lie.** A declaration is trusted and unverified. Mark such
  codecs `trusted` in the capability map so a reviewer can see which properties
  rest on an assertion rather than a proof.
- **Only tier 1 may seal.** A tier-2 implementation is generated, and generated
  code is table driven -- over the plaintext of a sealed region that is a
  cache-timing channel. The trust that makes tier 1 weaker on properties is
  what makes it the right tier for timing: a supplier can state what situ
  cannot derive. See 14.3 and
  `doc/decision/0019-sealing-requires-authentication.md`.
- **Tier-2 codecs cannot lie.** Properties derived from a kernel description are
  sound by construction. Prefer tier 2 wherever a codec is expressible.

Tier 1 is phase 7. Tier 2 is phase 12 and later, but the property algebra below
must be designed now so that tier 2 slots in without disturbing it.

**Signature and implementation are separate declarations.** This is what makes
built-in algorithms replaceable. A `codec` declares the contract; an `impl` binds
an implementation to it:

```situ
codec crc32 {                       // contract: what it does, conceptually
    expansion = +4;
    granularity = block(any);
    systematic;
    seekable = linear;
    deterministic;
    kernel = polynomial(width = 32, poly = 0x04C11DB7, init = 0xFFFFFFFF,
                        xorout = 0xFFFFFFFF, reflect);
}

impl crc32 derived;                 // use situc's generated implementation
impl crc32 extern "my_fast_crc32";  // or replace it with the user's
impl crc32 extern "hw_crc32_unit";  // or with a hardware accelerator
```

Three consequences that matter:

- **A user can swap the implementation without the schema changing at all.**
  Every capability conclusion in the compiler derives from the signature, so
  substituting a hand-tuned assembly CRC, a DMA-driven hardware unit, or a
  vendor library changes nothing about the layout, the capability map, or the
  generated accessors. This is the requirement "the schema parser should
  understand what they do conceptually" satisfied structurally.
- **A signature may exist with no implementation at all.** Declaring the contract
  and leaving `impl` unbound is legal; the compiler does all its reasoning and
  reports an unimplemented-codec error only at code generation. Schemas can be
  designed and analyzed long before any codec is written, which is the normal
  case for a protocol under design.
- **Where a `kernel = ...` clause is present, the compiler derives the properties
  and cross-checks the declared ones.** A mismatch is an error naming both
  values. A signature with a kernel but declared properties that contradict it is
  the single most dangerous kind of mistake in this system, because every
  downstream capability conclusion inherits the lie.

**Standard signature library.** Ship `std/codecs.situ` with correct signatures
for the algorithms users will actually reach for: AES-GCM, AES-CTR, AES-CBC,
ChaCha20-Poly1305, HMAC, the common CRCs, Reed-Solomon at standard parameters,
Manchester, NRZI, 4b5b, 8b10b, COBS, HDLC. Hand-written signatures are where
silent capability lies enter the system, so users should rarely need to write
one.

**Conformance harness.** The compiler cannot verify a tier-1 implementation, but
it can generate the tests that would catch a lying one. `situc gen-codec-tests`
emits a property-based suite from the signature:

| Declared property | Generated test |
|---|---|
| `length_preserving` / `expansion` | output extent matches the declared function across a range of input sizes |
| `deterministic` | repeated encoding of identical input is byte-identical |
| `invertible` | decode(encode(x)) == x over random and edge-case inputs |
| `seekable = linear` | byte N produced from a partial input equals byte N of the full output |
| `seekable = permuted` | the position map is a bijection over a full block, and not the identity: nothing leaves its own block, no position is written twice, and at least one moves. A different permutation of the right shape passes -- this asserts the property the signature states, not the map |
| `systematic` | input bytes appear verbatim at the offsets the compiler computed |
| `granularity = block(N)` | modifying one input block changes only the corresponding output block |

This closes most of the gap left by "tier-1 codecs can lie". The properties
cannot be proven, but each one has a cheap falsifying test, and generating them
from the signature costs the user nothing.

### 13.2 Property signature

```situ
codec aes_ctr_128 extern {
    length_preserving;
    seekable = linear;
    granularity = byte;
    invertible;
    deterministic;
}

codec deflate extern {
    expansion = unbounded;
    granularity = stream;
    not seekable;
    invertible;
}
```

| Property | Domain | Meaning |
|---|---|---|
| length | `length_preserving` \| `expansion = +N` \| `expansion = ratio_exact(a,b)` \| `expansion = ratio_padded(a,b)` \| `expansion = ratio_bounded(a,b)` \| `expansion = unbounded` | output extent as a function of input extent |
| `seekable` | `linear` \| `permuted` \| `blockwise(N)` \| `none` | class of the output-position function |
| `granularity` | `bit(N)` \| `symbol(N)` \| `byte` \| `block(N)` \| `stream` | minimum independently-transformable unit |
| `systematic` | flag | input data appears verbatim in the output at computable positions |
| `authenticated` | flag | produces or consumes a tag over a declared range |
| `invertible` | flag | inverse exists |
| `deterministic` | flag | same input, same output, always |
| `error_propagating` | flag | a corrupted input unit damages more than its own output unit |
| `tag_bytes` | byte count | the authentication tag the primitive produces |
| `nonce_bytes` | byte count | the nonce the primitive requires |

The last two are sizes rather than shape, and they are the only two: decision
0038 adds them because a schema declares a tag and a nonce as *fields*, so
there is something concrete to check a stated size against. A key width is
deliberately absent for the mirror reason -- no construct here names a key, so
the setting would be read by nothing, which is the shape 26.60 and 26.117 spent
two passes removing. Both are optional, and silence claims nothing.

**They are not derivable from `expansion`**, which answers a different
question: how the region's own extent changes. Where a codec appends its
overhead the two coincide; where the tag is a separate field the codec is
length-preserving, expansion is zero, and it says nothing about the tag beside
it. An inference would be right for one spelling and silently wrong for the
other.

Three of these are new relative to the first draft and each was forced by a real
codec class:

**`expansion = ratio_padded(a,b)`** is the exact ratio applied at group
granularity: the output is always a whole number of groups, so a partial final
group is filled out rather than truncated. The group follows from the ratio --
`lcm(8, b)` bits, the smallest run of input that is both whole bytes and whole
symbols -- rather than being declared, because the two numbers are not
independent. base64 and base32 are the cases; base16 is not, because four bits
divide a byte and the question never arises. See
`doc/decision/0018-padded-expansion.md`.

**`expansion = ratio_exact(a,b)`** replaces the earlier blanket `ratio`. An
exact integer ratio -- Manchester at 2:1, 4b5b at 5:4, 8b10b at 10:8 -- means
output position is a linear function of input position, so **interior offsets
remain statically computable**. The earlier draft marked all ratios as
`Unbounded`/`Dynamic`; that was wrong, and Manchester is the counterexample.
`ratio_bounded(a,b)` is the compression case: worst-case bounded, actual
data-dependent, offsets dynamic.

**`seekable = permuted`** covers block interleavers. The position function is a
total computable bijection but not monotone. Random access survives; sequential
prefetch does not. Distinguishing this from `linear` matters for whether the
generated accessor can hand out a contiguous span.

**`systematic`** is the FEC analogue of CTR-mode seekability, and it is the
single highest-value property in this section. A systematic block code -- RS(255,223),
Hamming(7,4), any CRC-appended frame -- leaves the data bytes unchanged at
computable offsets. That means **a field can be read without decoding at all**,
and a write only needs to recompute parity for the containing block. A
non-systematic code (convolutional with Viterbi, turbo, most LDPC constructions)
destroys interior addressing completely.

### 13.2a The tier-1 ABI

A tier-1 codec's implementation is the user's, and `impl x extern "my_x"`
names it. These are the two functions situ calls, and the two
`gen-codec-tests` attacks:

```c
situ_err_t my_x_encode(const uint8_t *in, uint32_t in_len,
                       uint8_t *out, uint32_t out_cap, uint32_t *out_len);
situ_err_t my_x_decode(const uint8_t *in, uint32_t in_len,
                       uint8_t *out, uint32_t out_cap, uint32_t *out_len);
```

One shape whatever the algorithm, because that is the whole of what the
compiler knows: the signature says what the transform *does to extents and
positions*, and the ABI is how it is reached. A capacity rather than an
allocation, for invariant 4's reason; `*out_len` written only on success; a
short buffer refused rather than half-filled, because half a decode is not a
shorter message.

`situ_err_t` and not a byte count, because a tier-1 codec can fail where a
table lookup cannot -- a truncated ciphertext, an odd number of nibbles, a tag
that does not verify. `SITU_ERR_BOUNDS` for a buffer too small,
`SITU_ERR_CONSTRAINT` for input the codec does not admit; a code this build
does not name reads as `constraint` in the Rust binding, because an
implementation that refused the input has refused it whatever it called the
refusal.

A coded region whose codec is tier-1 gets its decode accessor through this
(13.6a): C, C++ and Rust call it, and Python names it in the note that says
why it does not. A tier-2 region reaches its generated kernel instead, whose
shape is the kernel family's rather than this one.

**This was written down after the fact**, and the gap it closed is the
argument for writing it down at all. `gen-codec-tests` had been emitting
`situ_codec_<codec>_encode` since phase 7 -- a name the accessors did not
call, the spec did not name, and no `impl` bound. So the harness could not be
linked against any implementation this repository produces, and had never once
been run (26.35, `doc/decision/0028-the-tier-one-codec-abi.md`).

### 13.2b The scattered form, for a transform that covers spans

13.2a takes one pointer and one length, which cannot express a transform over
bytes that are not adjacent. QUIC's header protection is the case: a mask
derived from a sample of the ciphertext, applied to the first byte and to the
packet number, with the connection id sitting between them.

A `coded` region carrying a `covers` clause (14.1a) is reached through a second
pair instead:

```c
situ_err_t my_x_encode_spans(const situ_span_t *spans, uint32_t count);
situ_err_t my_x_decode_spans(const situ_span_t *spans, uint32_t count);
```

```c
typedef struct situ_span {
	uint8_t  *base;
	uint32_t  len;
} situ_span_t;
```

**In place, so there is no output buffer.** That is the whole reason the form
exists: gathering the spans into a temporary would copy exactly what a
zero-copy accessor is for, and a codec writing its answer somewhere else would
have copied them after all. It is confined to length-preserving codecs, which
is what 14.1a already requires of a `covers` clause -- in place is meaningful
only where the answer is the same size as the question.

**Every run in one call, never one call per run.** A mask derived per call is a
different transform from one derived once and spread across the spans, and the
schema does not say which was meant. Handing the codec the whole list is the
only reading that does not guess.

**An addition, not a replacement.** A region with no `covers` clause keeps
13.2a exactly, so every implementation written against it goes on working. The
two shapes answer different questions and neither subsumes the other: 13.2a is
out-of-place and may change length, which is what `stuffing` and `base64` need
and what an in-place pair cannot express; this one is in-place and scattered,
which is what a mask needs and what a single pointer cannot express.

**The clause decides, not the layout.** A contiguous coverage comes here too,
as a list of one span, rather than being sent through 13.2a because it happens
to be adjacent. Otherwise inserting a field between two covered spans would
silently change which symbol a schema demands -- a link error at best, and at
worst a second implementation to keep in step. One clause, one contract,
whatever the layout does.

Adjacent covered members are merged, so `count` is the number of *separated*
pieces rather than the number of names written down. The generated span list is
the thing to read when checking that an uncovered field in the gap is really
left alone.

The one shape still refused is a covered member whose offset the layout cannot
fix. A span is an address and a length, and the list is built before the codec
is called, so an offset nobody can compute is not a span.

Situ never sequences this against a tag covering the same bytes. Whether a mask
goes on before or after authentication is the protocol's decision -- QUIC
authenticates the unmasked header and masks afterwards -- and 13.1's rule is
that situ describes a transform's properties and never its algorithm or its
order. See 14.8.

A length-preserving kernel reaches this too, and did not for a long time. Two
gates stood in the way and both looked reasonable:

- `decodes_here` admitted the `table` family and named `stuffing` codes only.
  Its docstring gave the reason as Hamming's interface -- a codeword is a
  nibble in and a byte out with a correction flag, so a generic region decode
  over it would be guessing -- and then generalised that to "the other
  families". `permutation` and `shift_register` emit exactly the
  `(in, len, out) -> count` shape `table` does, so the reason did not reach
  them. This is the same over-generalisation the function's own history
  records one step earlier, when `stuffing` sat where these two did and four
  comments said the other families were "described and not yet generated".
- Behind it, all four backends read `codec.ratio` and returned nothing when it
  was absent. A length-preserving kernel computes no ratio because there is
  nothing for one to say, and the buffer size was never the unknown: it is the
  encoded length exactly.

`traverse.decode_ratio` answers the second -- the declared ratio, or `1:1`
where the codec declares none and preserves length, and `None` only where the
size genuinely is not computable without decoding. The prose beside the
accessor was written for a codec that expands and said the decoded form was
"that much smaller", which at 1:1 is nothing; it now says the buffer is the
same size.

### 13.3 The decidability rule

**The compiler reasons only about property signatures, never about transform
semantics or transform results.**

For tier 1 this means it trusts declarations. For tier 2 it means the derivation
from kernel description to property signature is a separate, self-contained pass
whose output is a signature -- the lattice still sees only the signature.

Three hard prohibitions:

1. **The expression language may not reference transform output.** If a schema
   could branch on a transform result, "is this in-place mutable?" becomes
   undecidable. Sizes and discriminants must come from declared fields or from
   codec property arithmetic, never from decoded content.
2. **Unbounded expansion is conservatively fatal downstream.** Everything after
   an `unbounded` region becomes `offset = Dynamic`, `size = Unbounded`. No
   attempt to be clever.
3. **The kernel description language (13.4) must stay non-Turing-complete.**
   Bounded iteration over declared block sizes only; no unbounded loops, no
   recursion, no data-dependent iteration counts. Otherwise property derivation
   does not terminate.

### 13.4 Kernel families for derived codecs

The reassuring result of surveying this space: essentially every line code,
FEC, scrambler, and framing code in practical use is expressible as one of
seven kernel families, or a pipeline of them. This bounds the tier-2
design.

| Family | Description form | Covers | Derived properties |
|---|---|---|---|
| **table** | input symbol -> output symbol map, optionally padded to whole groups | Manchester, 4b5b, base16/32/64 (not NRZI, 8b10b, Gray or BCD -- see 26.140) | `ratio_exact` from symbol widths, or `ratio_padded` with `granularity = block(g)` where the code pads; `seekable = linear`; `deterministic`; not `systematic` |
| **polynomial** | generator polynomial over GF(2) or GF(2^m), plus init/reflect/xorout | CRC (all variants), Reed-Solomon. BCH takes the same route and none is written | `expansion = +N`; `systematic` for appended-parity forms; `seekable = linear`; parity recompute scope = block |

| **linear block** | generator or parity-check matrix over GF(2) | Hamming(7,4). Any other `(n,k)` derives a correct `ratio_exact` and generates nothing; a matrix in the schema is the language addition that would change that (26.144). Not LDPC, for the reason below the table | `ratio_exact(n,k)`; `systematic` iff the matrix is in standard form; `seekable = blockwise(n)` |
| **shift register** | taps, feedback source, initial state, and whether the feedback is complemented | additive and multiplicative scramblers, NRZI in both conventions. **Not convolutional codes, and not Miller** -- see below the table, which is a sharper refusal than the other rows' | `length_preserving`, always; `seekable = linear` iff feedback is from input only; `not seekable` and `error_propagating` if feedback is from output. `complement_feedback` moves none of them |
| **permutation** | index mapping, closed form or table | block interleavers. A bare `span` derives the properties and generates nothing, which is what a convolutional interleaver would get: a bank of delay lines is not an index map within a block | `length_preserving`; `seekable = permuted`; `deterministic` |
| **stuffing** | trigger predicate plus insertion rule | HDLC bit stuffing, COBS, SLIP, byte stuffing | `expansion = ratio_bounded`; `not seekable`; interior addressing lost |

**What the Covers column leaves out, and why it is one reason rather than
four.** That column named convolutional codes, LDPC, BCH and Miller for a
long time, none of which is written, which reads as four things nobody got
to. Three of them are one boundary and the fourth is a different absence.

**The boundary is memory, not difficulty.** A convolutional code's encoder is
this family with more output taps; its decoder is Viterbi, which needs a
traceback buffer. LDPC is belief propagation, iterative over a working
matrix. Both want storage proportional to the message, and section 20's rule
is that generated C, C++ and Rust allocate nothing. So they are tier-1
codecs: a hand-written signature naming an `extern` implementation, which is
expressible today and is where any soft-decision decoder belongs. This is
the same line decision 0017 draws for codecs generally, arriving from the
other side -- there it is about trusting a supplier's properties, here it is
about who owns the buffer.

**One of the four is a trap rather than a gap, and that is the difference
worth keeping.** A `linear_block` with an unimplemented `(n,k)` derives a
*correct* `ratio_exact` and generates nothing; a `permutation` with a bare
`span` does the same. Both are honest: the signature is right and the author
is told there is no implementation. A convolutional code declared as a shift
register is not honest, because this family derives `length_preserving`
unconditionally and has no argument that can say otherwise -- so a rate-1/2
encoder would claim its output is the size of its input, and a consumer
sizing a buffer from the map would under-allocate by half. The table
promised "`length_preserving` or `ratio_exact`" and the second was
unreachable. It says `length_preserving`, always, now.

**Miller is not this family's absence at all.** It is a 2:1 code whose output
depends on the previous *input* bit as well as the level -- a symbol map with
state, which is exactly what 26.142 records for 8b10b and CoAP as one open
question rather than several. Filing it under shift registers is the mistake
26.139 found for 8b10b under table codes, in a different row.

**Any whole number of bytes, and the reflection that hid in the palindromes.**
A CRC's polynomial, width, initial value, reflection and final xor are the
description; the table is computed from them rather than copied, so a variant
nobody has written down generates as well as a famous one. The width was the
four C word sizes, which meant a 24-bit CRC derived a correct *signature* --
`+3 bytes`, systematic, seekable -- and generated nothing. Bluetooth LE's
link-layer CRC is 24 bits, which is how that was noticed. It is any multiple of
eight up to sixty-four now: the register is held in the next word up and masked
after every shift, since there is no `uint24_t` and a struct for one would buy
nothing a mask does not.

Adding the first such entry found a real defect. **A reflected CRC starts its
register reversed**, and this emitted the initial value as written. Every
reflected CRC in `std/kernels.situ` starts all-ones -- CRC-32 and
CRC-16/MODBUS -- and all-ones is its own reverse, so every check value in the
suite agreed with the bug. CRC-24/BLE starts at `0x555555` and came out
`0xD39857` where the catalogue says `0xC25A56`. It is in the library now, and
in the check-value test, for exactly that reason: it is the first width that is
not a word and the first reflected initial value that is not a palindrome.
CRC-40/GSM is there too, as the same point on the other side of thirty-two --
forty bits in a `uint64_t`, where a mask written for narrowing would go wrong
in the other direction.

**A code name that names two codes is refused.** `manchester` is the instance:
IEEE 802.3's and G.E. Thomas's are both called Manchester and are bit-inverses
of each other, so a receiver built on one reads a sender built on the other as
the complement of what was sent -- plausible bytes, no error, and nothing at
run time that could notice. The compiler picked 802.3 in a comment beside the
table and a schema had no way to ask for the other. Both are named now,
`manchester_802_3` and `manchester_thomas`, and the bare name is a diagnostic
that names them and says they are inverses. Invariant 9, in the one place where
the wrong choice is invisible at run time rather than loud.

The evidence that this is a choice a practitioner has to make came from
outside: `rflab`, a radio project built against real hardware, makes the same
convention a compile-time option in its Manchester encoder.

Pipelines compose: `codec framed = rs_255_223 |> interleave(16) |> manchester_802_3;`
Property composition is pointwise and conservative -- the pipeline is seekable
only if every stage is, systematic only if every stage is, and the expansion is
the product of the stages' expansions.

That last one needs the vocabulary above widened, and this example is what
shows it: Reed-Solomon appends 32 bytes and Manchester then doubles all of it,
which is `ratio_exact(2, 1)` *and* `+64` at once. A composed signature may
carry both; a hand-written one still gives one form
(`doc/decision/0016-composed-expansion.md`).

**What is generated, and what a decoder is handed.** All seven families emit
an implementation, and the forty-two kernel-bearing codecs in
`std/kernels.situ` do so without a refusal among them. The interface is one
shape -- `(in, count, out) -> count` -- and what differs is what `count`
measures. A `table` kernel is bit-oriented
by construction; the byte families count bytes; and `stuffing` declares `unit`,
because HDLC counts bits where COBS scans bytes. That is not a detail a
generated call may guess at: passing a byte count to a bit loop decodes an
eighth of the region and returns confidently.

A `stuffing` kernel names its code, and only a named code somebody wrote is
generated -- `cobs`, `hdlc`, `ppp_async`, `slip`, `smtp_dot`, `usb`. The
list lives in one place now. It lived in two, the dispatch and the prototype
gate, and adding a code to one of them emitted a definition that nothing
declared.

The decode accessor on a coded region (13.5) covers `table` and `stuffing`,
which are the shapes settled here, in all four backends. It was `table` alone
in each of them, on the argument that the other families were described and not
generated -- an argument that stopped being true without any of the four
comments stating it noticing. `traverse.decodes_here` and
`traverse.decode_counts_bits` answer it once now.

Building the rest of it found three things. The accessor decoded `_span` rather
than `_len`, so it ran the codec over the delimiter as well as the content:
SMTP's `CRLF . CRLF` went through the unstuffer and came back as two extra
bytes. C's note above it said "there is no accessor for the decoded bytes"
whatever followed, which was true while none did and became a contradiction
sitting directly on top of one. And the other three backends emitted a
delimited coded region's bytes with no note at all -- so a reader had the
encoded form in hand and nothing saying it was not the value.

**Bit phase.** Sub-byte granularity codes force a change in the layout solver:
a region may begin at a bit offset rather than a byte offset, and its length may
not be a whole number of bytes. Manchester over an odd number of input bits
produces a region ending mid-byte. The solver must therefore track offsets in
**bits** internally and report bytes only where the value is byte-aligned. Do
this from phase 2 -- retrofitting bit-granular offsets into a byte-based solver
is a rewrite, and it costs nothing to carry the extra factor of eight from the
start. This is the one part of tier 2 that must be anticipated early.

**CRC is a checksum, not a transform.** Model it with the `tag` machinery from
14.1 rather than as a codec: it has coverage, it goes stale when covered bytes
change, and it needs recomputation before transmit. That is exactly the
tag-dirty mechanism, minus the cryptography. Introduce `checksum` as a keyword
sharing the tag implementation, with `covers` inference identical to 14.1. This
unification is worth taking: CRCs are near-universal in wire protocols, and
reusing the dirty-tracking machinery means they get correct in-place mutation
semantics for free.

### 13.5 Propagation through a transform

Given a region `R` with codec `C` and interior schema `S`:

| C properties | Consequence for S |
|---|---|
| `length_preserving` + `seekable = linear` + `granularity = byte` | S retains interior offsets; same-size field mutation re-transforms only that byte range; `mutate := InPlaceFixed` |
| `length_preserving` + `granularity = block(N)` | interior offsets retained; mutation re-transforms the containing block(s); `mutate := InPlaceSlack` |
| `length_preserving` + `seekable = permuted` | interior offsets retained via the position map; `access := Random`; no contiguous spans; `mutate := InPlaceFixed` per unit |
| `length_preserving`, `seekable = none` | interior offsets retained but any mutation re-transforms the whole region; `mutate := RewriteRequired` |
| `expansion = +N` | region size = interior size + N; following members keep static offsets |
| `expansion = ratio_exact(a,b)` | interior offsets scale linearly and stay static; region size = ceil(interior * a / b); following members keep static offsets; bit phase may become non-zero |
| `expansion = ratio_bounded(a,b)` | region `size := Bounded(interior, ceil(interior*a/b))`; following members `offset := Dynamic` |
| `expansion = unbounded` | region `size := Unbounded`; following members `offset := Dynamic` |
| `systematic` | interior fields readable **without decoding**, at computable offsets; `stage` stays at `ParseTime` for reads; writes require parity recompute over the containing block |
| not `systematic`, not `length_preserving` | interior `stage := TransformTime`; no access before decode |
| `error_propagating` | advisory only; reported in the map, no capability effect |
| not `invertible` | region is read-only; `mutate := Immutable` |
| `authenticated` | see 14.2 |

This table is the payoff of the whole transform design. Concretely, it means:

- a CTR-mode sealed payload keeps fixed interior offsets and single-field
  in-place mutation, while a CBC-with-padding payload does not
- a Manchester-encoded region keeps statically computable interior offsets,
  because 2:1 is exact
- a Reed-Solomon systematic block lets you read a field with no decode at all,
  and write one with parity recompute over 255 bytes rather than the frame
- an HDLC bit-stuffed region destroys interior addressing, which is precisely
  why real protocols apply stuffing only at the outermost layer -- and the
  advisor should say so when it sees stuffing applied inward

Every one of those falls out of declared or derived properties. The compiler
never learns an algorithm

---

### 13.6a Reaching the decoded bytes

The interior of a coded region is `stage = TransformTime`: the plaintext is
not in the message. So the decode is the one accessor situ generates that
needs somewhere to put its answer, and since nothing allocates (invariant 4)
that somewhere is the caller's:

```c
#define SITU_S_BODY_DECODED_MAX 4u
situ_err_t situ_S_body_decode(situ_view_t view, uint8_t *out,
                              uint32_t cap, uint32_t *len);
```

The bound is the schema's, from the codec's declared ratio against the
region's largest encoded form. A buffer short of it is refused rather than
half-filled: half a decode is not a shorter message.

**Only where the generated decoder's shape is settled**, which
`traverse.decodes_here` decides: a `table`, `permutation` or `shift_register`
kernel, and a `stuffing` one whose named code is in `DERIVED_STUFFING`. Not
for the families that are described and not yet generated (13.4). Guessing a signature for those
would be a header that names a function nobody agreed to write. The note
above such a region already says the transform is the caller's.

All four backends hand back the encoded bytes. Three of them decode, and the
one that does not says why:

| | the decode |
|---|---|
| C | calls the C implementation directly |
| C++ | the same, declared at file scope with C linkage -- a linkage specification is not allowed in a block, so it cannot go inside the class that calls it |
| Rust | `extern "C"` and therefore `unsafe`, at the call site with a note saying what is promised (26.18) |
| Python | **not emitted.** It would mean loading a shared object from a path situ has no convention for, and inventing one in a code generator is a policy decision. The module says what to call through ctypes and how large the buffer must be |

That split is decision 0017 arriving where it said it would: one
implementation, in C, and each language reaching it by whatever it already
has. C++ links it for free, Rust crosses an FFI boundary it can name, and
Python is the one that pays in a build step.

The encoded bytes are reachable in all four, delimited or not. They were not: a coded region
with no delimiter got a comment header and nothing else, so the bytes on the
wire were unreachable -- a strange thing for a treat-as-bytes region, and true
only of the non-delimited case, because the scan path has always emitted a
pointer.

---

## 14. Cryptographic model

The first real use case is compact encrypted protocols, so this is not an
add-on. It is a first-class part of the capability system.

### 14.1 Constructs

| Construct | Meaning |
|---|---|
| `authenticated { ... }` | plaintext, covered by a tag (AEAD associated data) |
| `sealed(codec, nonce = ref) { ... }` | encrypted and covered by a tag |
| `tag T[N] [covers(...)]` | authentication tag; coverage inferred if omitted |
| `nonce = ref` argument | names the field a `sealed` region takes its nonce from |
| `secret` attribute | marks a field as key material or plaintext secret |
| `checksum T[N] [covers(...)]` | non-cryptographic integrity field (CRC); shares the entire tag mechanism |
| `coded name(codec) [covers(...)]` | a transform, optionally over spans outside the region (14.1a) |

Coverage inference: an omitted `covers` clause means "every `authenticated` and
`sealed` region in the enclosing struct, in declaration order". Explicit
`covers(a, b)` overrides. Multiple tags in one struct are permitted; each
covers a disjoint or nested set of regions, and overlapping non-nested coverage
is an error (it makes recomputation order ambiguous).

Nested coverage recomputes **innermost first**, which is the only order that
terminates: an outer tag covers the inner tag's own bytes, so writing the inner
one afterwards would leave the outer one stale again
(`doc/decision/0011-nested-tag-coverage.md`).

A region no tag covers is an error. `authenticated { }` states that these bytes
are covered; with no tag in the struct that statement is false, and a construct
whose meaning is silently nothing is what 14.5 refuses.

### 14.1a Transform coverage: `coded ... covers(...)`

A `coded` region transforms its own bytes. `covers(...)` widens that to spans
outside it:

```
coded pn(hp) covers(first) { u8 number[..]; }
```

Header protection is what it is for. QUIC derives a mask from a sample of the
ciphertext and applies it to the first byte and to the packet number under one
operation, and those two are not the same field.

The clause is **additive and never inferred**. The codec sees the union of the
region and everything named; an empty clause means the region covers only
itself. That is deliberately the opposite of a tag, where an omitted `covers`
means "every region in the struct": there is a sensible default for what a tag
authenticates and none for what a transform reaches, and a transform that ran
over bytes nobody named would be a surprise rather than a convenience.

Unlike a tag, it may name a **plain field**. A tag covers regions because its
coverage is an authentication boundary and a region is what draws one; a
transform's coverage is a range of bytes, and a field names one just as well.
The first byte of a QUIC packet is a field, not a region anybody would wrap.

The interior of another `coded` or `sealed` region cannot be named. Those bytes
are transform output and do not exist until it has run (13.3), so naming one is
an ordering error rather than a coverage one.

**A clause sends the region to the scattered ABI** (13.2b), contiguous or not:
in place, over a list of spans, rather than through 13.2a's single pointer and
length. That is what makes a coverage with a gap in it emittable at all --
gathering the spans would copy what a zero-copy accessor exists to avoid, and
calling the codec once per span is a *different operation* from calling it once
over both, which the schema does not choose between.

The clause decides this, not the layout. A contiguous coverage goes the same
way, as a list of one span, so that inserting a field between two covered spans
does not silently change which symbol an implementation must provide.

`coded_spans` computes the list, merging adjacent members, so the count is the
number of *separated* pieces rather than of names written down. It sits beside
`covered_run` for the same reason that one is shared: which spans, in what
order, and where each ends is one question with one answer, and four backends
disagreeing about it would be four dialects of the language.

The one shape still refused is a covered member whose offset the layout cannot
fix. The span list is built before the codec is called, and an offset nobody
can compute is not an address.

**QUIC's header protection is expressible, checkable and emittable.** Its two
spans are separated by the connection id, and the generated span list carries
both with the gap left alone.

**The codec must preserve length.** A covered span sits at an offset the
layout has already fixed, and a transform returning a different number of bytes
than it was given would move it -- the decoded form would not correspond to the
struct. So `covers` on a `ratio_bounded` codec like SMTP's dot-stuffing is an
error, and a mask or a scramble is what the clause is for.

That rule has a consequence for tier 2, and the first statement of it was
wrong in the way that costs most. It read: **a derived codec never reaches
this clause** -- the scattered ABI being a tier-1 shape bound by an
`impl ... extern` symbol while a derived codec's implementation is situ's own
-- and on that basis the widening, which had been written into the derived
path in three backends, was **removed again**, since "an unreachable branch
claiming to handle a case is worse than an absent one".

**It reached the clause.** `coded prot(scr) covers(flags, pn)` with
`impl scr derived;` was accepted by every rung, all four backends emitted the
single-span shape, and `situc wire` published `prot covers: prot flags pn`
either way -- a contract claiming bytes the generated code never transforms.
The sentence was not merely stale: it was the recorded justification for
deleting the code that would have made the case work, and an impossibility
claim with no remedy beside it is the shape `evidence.md` calls the expensive
one, because it tells the next person not to write something that would run.

**It is refused now rather than widened** (26.475). A derived codec taking a
span list is a real feature and is not built; what is built is the refusal,
which says so and names the binding that works. The corpus's only
`coded ... covers(...)` is extern, and so is header protection, the case the
clause exists for -- so nothing that worked stopped working.

What that investigation turned up was a separate defect, since fixed and
recorded in 13.6a: a length-preserving derived codec emitted **no decode
accessor at all**, coverage or no coverage.

The clause is wire-visible and appears in the signature (`pn covers: pn flags`).
Widening it makes a peer transform bytes the old version left alone while every
member, offset and size stays identical -- so `wire.compare` gained the
*added-coverage* case for it. That case had been absent while only tags reached
that function, because a new tag brings a new field and the member comparison
already reports one; a `coded` region gaining a clause changes no member, and
without it two signatures compared equal while the peers disagreed.

### 14.1b Which comes first, the transform or the tag

Where a `covers` clause reaches bytes a tag also covers, two orders are
coherent and they disagree about what is on the wire. The schema says which:

```
coded pn(masking) covers(first) [tag_order = after] { u8 number[..]; }
```

- **`after`** -- the tag is computed first, over untransformed bytes, and the
  transform goes on top. QUIC's header protection: the AEAD's associated data
  is the *unprotected* header, and the mask is applied afterwards.
- **`before`** -- the transform runs first and the tag covers its output, so
  the tag authenticates what a peer actually reads off the wire.

**It is an error to leave it unsaid, and an error to say it where no tag is
involved.** The first is 17.0: an ambiguity whose wrong answer is undetectable
at run time, since both orders produce a message of the same length with the
same fields in the same places, and the peer that disagrees reports a failed
tag rather than a misordered transform. The second is 14.5's rule about
constructs that mean nothing -- an attribute that decides no order is one.

**Why this is settled by the schema and nested tag coverage is not.** Decision
0011 has situ decide an order without being told: an outer tag covers an inner
tag's own bytes, so innermost-first is the *only* order that terminates, and
there is nothing for an author to choose. That is a data dependency, and
deciding one is not the same as knowing an algorithm -- 13.1's rule is about
the latter. Here both orders terminate and both are used by real protocols, so
there is no answer to derive and 17.0 applies instead.

**The attribute is load-bearing, not documentation.** It decides the generated
signature:

- `after` -- applying the transform does not invalidate the tag, which was
  computed over the untransformed bytes. The accessor takes a view.
- `before` -- applying the transform leaves the tag stale, so the accessor
  takes the message as well and marks the dirty bit on success. It is not
  possible to call it without having somewhere to record that.

The dirty bit is set only where the codec returned `SITU_OK`. A transform that
refused its input has not changed the bytes, so the tag is exactly as stale as
it was.

Situ still does not *run* the sequence. It says which order is required and
makes the wrong one visible; performing it is the caller's, per 13.1.

### 14.2 Tag coverage and the dirty bit

This is the sharpest capability interaction in the language: **in-place
mutation and authentication are in direct conflict.** Touch one byte covered by
a tag and the tag is stale.

Rules:

- Every field carries `auth = Covered(t)` or `Uncovered`.
- Mutating a `Covered(t)` field sets tag `t` dirty in the message state.
- The generated API refuses to yield a transmittable buffer while any tag is
  dirty. In Rust this is typestate; in C it is a checked flag plus a
  `situ_msg_finalize()` that recomputes dirty tags and clears the flag.
- Fields that are `Uncovered` -- a plaintext routing header, a hop counter --
  are freely mutable in place, which is precisely why real protocols put such
  fields outside coverage. The compiler makes that design pressure visible.
- `require no_tag_invalidation(expr)` is statically checkable: it passes only
  if the mutated field is `Uncovered`.

**A checksum may cover its own bytes, if it says what they read as.** A
cryptographic tag inside the region it covers is an error -- computing it would
take its own value as input -- and the checksum family is the exception the
rule has to admit. RFC 1071 defines the Internet checksum over the header
*including* the checksum field, taken as zero; a GPT header CRC zeroes its own
field the same way, and a tar header sum uses spaces. `[self_as = N]` on the
tag is that sentence, and it carries a value for the reason tar gives.

What the compiler contributes is unchanged -- the algorithm is the caller's
(13.1) -- but there are now two things only it knows rather than one: the span
the algorithm runs over, and where inside that span the tag's own bytes are.
The generated pair is `X_covered()` and `X_self_span()` with `X_SELF_AS`; the
bytes are still in the buffer, so nothing is copied and nothing is allocated
(invariant 4). Writing the tag does not mark it stale: it is not covered by
itself.

**Coverage weakens `auth` and leaves `mutate` alone.** Writing a covered field
is still a store to the same bytes at the same offset; what it costs is a tag
recomputation, and that obligation is what the `auth` axis records. Conflating
the two would leave nothing able to express the difference, which is the whole
of what this section is about. `in_place` therefore reads both axes -- writable
where it sits, *and* no tag left stale -- and `in_place_dirty` reads only the
first. That is why the same field passes one and fails the other, and why the
diagnostic can say "possible, and here is what it costs" rather than a flat no.

The example in 5.3 deliberately contains a failing requirement:
`require in_place(packet.hdr.seq)` where `hdr` is inside `authenticated { }`.
The correct diagnostic explains that in-place mutation is *possible* but
invalidates tag coverage, and points at the two fixes (move the field out of
coverage, or accept recomputation and use `require in_place_dirty(...)`).
Implement both requirement forms.

### 14.2a Coverage over bytes the message does not contain

```
checksum u8 checksum[2] covers(summed) prefix(udp_pseudo_header) [self_as = 0];
```

TCP's and UDP's checksums run over a *pseudo-header* -- the source and
destination addresses, the protocol number and the transport length -- two of
which belong to the IP layer. The kernel's `csum_tcpudp_nofold` takes `saddr`
and `daddr` as arguments precisely because they are not in the datagram it is
summing.

**A pseudo-header is a byte layout, and that is what situ describes.** What
situ cannot do is fill one in from this message. So the clause names a struct
declared in the same file, the caller builds one with the accessors situ
already generates for it, and the generated header says how many bytes to hand
over:

```c
#define SITU_UDP_HEADER_CHECKSUM_PREFIX_BYTES 12u
```

**This widens which bytes are covered and nothing else.** Computing the sum was
always the caller's -- 14.1's rule that a signature says what a transform does
and never how -- so the clause adds no arithmetic and no algorithm. It adds the
one thing only the compiler knows, which is the same thing coverage always
added: which bytes, and when they went stale.

The earlier note said coverage over bytes another schema owns "would mean a
schema that cannot be checked against the bytes in front of it". That was the
right worry and it is answered by *not* pretending: the prefix is a separate
struct, checked against its own bytes like any other, and the message's own
accessors reach none of it. Nothing here is validated against data situ cannot
see.

It is wire-visible, so it appears in the signature as
`checksum covers: udp_pseudo_header(prefix) ...`. A peer that sums a different
prefix disagrees about the value while every member, offset and size is
identical -- exactly the class of change that signature exists to catch.

**`example/udp` uses it and is fully described. `example/tcp` still does
not, and the reason has changed twice.**

UDP has a `length` field, so its payload is its own and the coverage closes:
the pseudo-header is a struct beside the header, the checksum covers the whole
datagram, and `require in_place(udp_header.checksum)` became
`require immutable(...)` -- the field used to be freely mutable precisely
because situ knew nothing about it.

TCP was recorded here as inexpressible: its coverage "runs to an extent a
different layer knows", since the segment length is the IP layer's. **That was
wrong.** `remaining` means to the end of the enclosing frame, and the frame is
the view the caller acquired -- so a caller who knows the segment length
acquires a view of it and the coverage closes exactly. The claim reasoned about
what the *schema* knows when the question is what the *view* carries, and
`example/message` had been sizing its trailer that way the whole time. It was
disproved in one build, by checking the gap was real before designing for it.

Writing it uncovered two defects instead, one fixed and one open.

**Fixed:** a tag's covered span may end at a member's *offset function* --
`payload[remaining]` puts `situ_tcp_header_payload_offset` in the expression --
and the tag is emitted where the schema declares it, which for TCP's checksum
is eighty lines before the payload it reaches. Every such call sat ahead of its
declaration. It never fired while a covered span ended at a *field*: UDP's
payload is `length - 8`, arithmetic on a value, and needs no offset function.
The fix is the one `_struct_extent` records three lines above it -- emit after
the offsets, because the preprocessor is not a scope.

**Open, and it is why TCP is still a plain `u16`:** the four backends disagree
about a *negative computed length*. `options[(data_offset - 5) * 4]` with
`data_offset` below its `[min = 5]` is negative, and C casts it to `uint32_t`
and clamps to the frame, Python slices with a negative end and gets nothing,
and the walker refuses outright with "a computed size is negative". Three
answers to one question. It arises only on invalid input, which is exactly what
`test_the_four_agree_about_bytes_nobody_meant_to_send` generates, and TCP's
payload made it reachable because nothing had followed the options before.

**What a negative computed length means is a decision, not a bug to patch**:
clamp to zero, refuse, or wrap. Situ's own rule is that ambiguity is an error
(17.0), and three implementations disagreeing is the strongest form of it.

**Settled: a negative computed length is zero.** And the implementation is
not one clamp but a rule about where signed and unsigned meet, which took four
attempts to state because each backend crossed that boundary somewhere
different.

**Bound every leaf, in the domain it was read in; do the arithmetic signed in
64 bits; clamp once, saturating at both ends.**

Each clause is there because leaving it out shipped a defect the differential
caught:

- **In the domain it was read in.** `as i64` on a `u64` above 2^63 makes a
  huge length indistinguishable from a negative one, so a varint a lying
  message set to 1.6e19 clamped to *zero* and put the member after it a few
  bytes in, well inside the frame, where it was read rather than refused. C,
  C++ and Rust each needed two spellings -- `leaf_u` and `leaf_i` -- chosen
  from the schema's signedness.
- **Signed throughout, not cast at the end.** An unsigned getter, or
  `align_up` emitting `- 1u`, makes the subexpression unsigned and it wraps
  before anything can look at its sign.
- **In 64 bits.** The bound is what keeps it there. `SITU_LEAF_MAX` is
  `INT32_MAX`, and holding a *field* to it changes nothing observable,
  because a length at or past the view's limit saturates against the frame
  either way.
- **Saturating at both ends.** Clamping the low end alone left
  `(LEAF_MAX + 1) * 2` truncating to zero on the cast to `uint32_t` -- the
  same failure one step along.

All five implementations bound identically, including Python and the walker,
which cannot overflow. Their bound is there so the four *agree*: a bound
applied in three places and not the fourth is a disagreement waiting for a
lying message, which is exactly how this was found.

**`example/tcp` is fully described as a result** -- `[self_as = 0]`,
`prefix(tcp_pseudo_header)` and `payload[remaining]`, the three parts its
checksum needs.

Two corrections this section carried and should not carry again. There is no
write-path defect: C's setter guards with `situ_in_bounds` and no-ops, and its
getter returns zero, exactly as Python does -- that was read off a probe's
output rather than the emitted code. And the disagreement was never about
which offset terms were emitted; the terms matched all along.

**Describing UDP uncovered a walker defect, and it was the walker's worst
shape: `validate` answering OK for a message every backend refuses.**

`layout.place_authenticated` does not extend the path -- a region's members sit
at the offsets they would have had without it and keep the enclosing struct's
namespace, which is why 5.3 addresses `Packet.hdr.seq`. `pack._ast_members`
*did* extend it, recording `u.s.payload` for a placement whose path is
`u.payload`. The lookup missed, the member got no size program, and a counted
run with no program measures zero -- so `u8 payload[length - 8]` inside a
covered region read as empty and fitted any frame.

It had nothing to do with coverage and everything to do with which construct
opens a namespace. `example/icmp` has the same shape and its `type` and `code`
never had a size program either; the defect stayed invisible because no covered
region had yet held a member whose bounds C would reject. That is the walker's
value and its hazard in one: a second implementation catches what one cannot,
and only where something makes the two disagree.

### 14.2c A field that drives a length and is covered

Two emitters each owned a setter for such a field and neither knew about the
other. `_shifting_setters` claims it because writing it moves later members,
so the message's generation has to be bumped; `_covered_setters` claims it
because a tag covers it, so the dirty bit has to be marked. Both emitted
`situ_r_count_set(situ_msg_t *, situ_view_t, uint16_t)`, and the header did
not compile:

```
struct r {
	authenticated s {
		u16 count;
		checksum u8 sum[2] covers(s) [self_as = 0];
		u8  opts[count];
	}
}
```

**Neither emitter is wrong.** A write that both moves later members and leaves
a tag stale needs one setter doing both, not two doing half each -- invariant
34's shape, two places computing one thing. The covered path yields to the
shifting one, which marks the bit itself where the field is covered.

The half a de-duplication could silently drop is worth naming, because keeping
either setter alone compiles: without the touch a stale view survives a write
that changed a length, and without the mark the tag is never reported stale.
A test asserts both statements are in the surviving body.

**Only C was affected**, checked rather than assumed. C++ takes no owner for a
driver's setter, Rust relies on `&mut self`, and Python emits none at all, so
none of them had a second setter to collide with. That the three invalidate a
driver's write differently from C is a real difference and a separate
question; it is not this defect.

It reached no example because none combines the two: a covered field that also
sizes an array. It was found while building a fixture for something else, and
worked around at the time rather than reported -- which is the wrong instinct,
since a schema that generates a header no compiler accepts is the failure this
repository rates worst.

### 14.3 The doom principle as a stage gate

A sealed region's interior schema is `stage = VerifyGated`. The generated API
provides no way to obtain a view into the interior before the tag verifies.

This makes the entire bug class "parse attacker-controlled plaintext before
authenticating it" *unrepresentable* rather than merely discouraged. It is the
single highest-value security property in the design and it must not be
weakened for convenience. If a user genuinely needs unverified access, they
must declare `sealed(...) [allow_unverified_read]`, which is loud, greppable,
and reported in the capability map.

**Two things a codec must be before it may seal.** The gate is only worth what
stands behind it, so both are checked when a region names its codec, and the
diagnostic names the way out.

- **It must declare `authenticated`.** A codec with no tag has nothing to
  verify, so the gate would hand out the interior on a `verified` flag that
  nothing had checked -- the ceremony of this section with none of its
  substance. `coded(C)` remains for a transform that makes no security claim.
- **Its implementation must be `extern`.** A generated implementation is table
  driven, and a table indexed by the plaintext of a sealed region is the
  cache-timing channel 14.6 forbids. Situ cannot promise constant time and
  declines rather than pretending, so the timing properties are the supplier's
  to state. This is the same position decision 0017 takes on codecs generally.

`doc/decision/0019-sealing-requires-authentication.md`. The second refusal is
reachable only through a pipeline, since no kernel derives `authenticated`, and
the case it catches is encrypt-then-code: `sealed(aead |> rs)` would otherwise
emit a table-driven Reed-Solomon over sealed plaintext.

### 14.4 Canonical encoding

Canonical means: exactly one byte sequence encodes a given value. Situ gets
this structurally, and `require canonical(X)` must fail on any construct that
introduces encoding freedom. The known sources:

- `endian native`
- `reserved [unknown]`
- `tlv unknown = preserve`
- enum `default = pass`
- variable-length integer encodings (if added later)
- any codec that is not `deterministic`
- padding whose content is unconstrained

Every one of these must be detectable by the solver and named in the failure
message. This list is a checklist for the implementer: add a test per item.

### 14.5 Strictness and malleability

Situ is strict where protobuf is lenient, and this is a deliberate security
position, not an oversight.

- Unspecified and reserved bits are **rejected** on parse, not preserved.
  Every ignored bit is a malleability surface and a potential covert channel.
- Unknown enum values are rejected by default.
- Unknown TLV tags are rejected by default.
- A variant discriminant selecting no arm is rejected by default -- `default:
  error`, which is also the default when no `default` clause is written.
- Forward compatibility via silent retention is a liability in an
  authenticated protocol; version negotiation (Section 19) is the correct
  mechanism.

A per-schema `strictness = strict | lenient` directive may relax this for
non-security schemas, but `strict` is the default and `lenient` sets
`canonical = NonCanonical`.

### 14.6 Secret fields

`[secret]` on a field or region:

- suppresses generated debug/print/format accessors entirely (the most common
  way key material reaches logs)
- emits an eraser -- `x_zeroize()` -- in every backend, and the guarantee it
  carries is not the same in all four, so each says which it is. C and C++
  write through a volatile pointer the compiler may not drop as a dead
  store, which is `situ_zeroize` and is what "compiler-barrier-protected
  memset, not plain `memset`" means. Rust's `situ_rt::zeroize` is the same
  volatile write plus a `compiler_fence`, and takes `&mut self` so the
  borrow checker refuses a live reader of the bytes being erased -- the
  strongest of the four. Python clears the caller's buffer, which is the
  only storage it owns, and cannot reach a copy the interpreter already
  made; its docstring says so rather than leaving somebody to find out.

  This said "emits zeroization" with no target named while one backend of
  four did it (26.314). The four then erased the wrong span: three erased
  nothing where the length is data-decided, and C erased what the wire
  asked for without clamping it (26.316)
- forbids the field from being used in any expression that controls layout
  (no secret-dependent lengths or discriminants -- that is a length-based side
  channel)

  "Any expression that controls layout" was always the right scope and the
  check enforced one corner of it: a length written as a bare field name.
  An arithmetic length, an `at` offset and the discriminant this bullet
  names by name all walked past (26.315), and so did a secret whose own
  length is read from its own bytes (26.316)
- generated accessors avoid data-dependent branching and data-dependent
  memory access patterns; where that is not possible for a construct, the
  compiler refuses to generate the accessor and says why

**Where tag verification's timing lives, stated because a consumer asked
(`suggestion/fuzznet.md`) and confirmed by reading the emitted code rather
than the intent.** The generated code never computes or compares an
authentication tag. A sealed region's `_open()` takes the caller's verdict
as a parameter -- `open(view, int verified, ...)` -- and refuses on the
flag, so the comparison that decides `verified`, and whether it is constant
time, belongs wholly to the caller and the codec they chose. What situ
generates for a tag or checksum is the geometry: the covered span, the
field's own bytes, `self_as`. Its own branches are over structure --
lengths, discriminants, the verified flag -- never over tag bytes, and the
one `== tag` in any backend compares a TLV wire-tag *number*, not a MAC.
The division of labour is therefore exactly the safe reading: **the
primitive and its constant-time comparison are the caller's; the bytes it
runs over are the schema's.** A future generated comparison would be a
decision record, not a drive-by (13.1 already forbids situ knowing the
algorithm).

### 14.7 Padding and length hiding

`pad_to(n)` and `pad_random(min, max)` for traffic-analysis resistance.
Padding content policy is explicit -- `zero` or `preserve` -- and `zero` is
required for `require canonical`.

**`random` was a third policy and 0045 removed it**, accepted 2026-09-04.
Randomness is not a property of a message: a pad of random bytes and a pad of
any other bytes are the same bytes, so a schema declaring `random` would state
something the generated code cannot test, which is what 14.5 refuses in as
many words. The sender's obligation to use a random source is real and is the
caller's, beside the AEAD primitive and for the same reason. What `pad_random`
contributes is the part nothing else expresses -- **bounds** -- plus a name the
map and the wire signature can carry.

Not in phase 8: 26.8's acceptance criteria do not reach these constructs, and
the canonicity item they contribute -- padding whose content is unconstrained
-- is already covered by `reserved [unknown]`, which is the same freedom under
a name that exists. They belong with the schema-evolution work of section 19,
whichever phase takes that.

### 14.8 What the model does not yet say

**The intent, stated by the holder and recorded here because the rest of this
section reads narrower than it:** situ is eventually to cover all protocol
needs, including full understanding of complex, layered, nested and
distributed cryptographic contexts -- and to do so **even where a project
plugs in its own primitives and routines**. That last clause is the one that
moves things. "situ does not implement AEAD" is true and has been quietly
doing the work of "situ does not check AEAD", which does not follow. The
primitive is the caller's; the *contract around it* is this compiler's.

**The model is structural, and within that it is strong.** Measured by writing
schemas rather than by reading the section: the doom principle is
unrepresentable-by-construction, a sealed interior being VerifyGated with no
API to view it unverified; coverage is inferred, nested tags recompute
innermost-first, and overlapping non-nested coverage is refused; one nonce
field feeding two sealed regions is refused, which is real nonce-reuse
reasoning; `[secret]` suppresses debug accessors, zeroizes, and forbids
secret-dependent layout and data-dependent branching, refusing to generate
where it cannot; a block cipher with padding is expressible as
`expansion = +16`, and granularity and seekability decide whether the sealed
interior keeps fixed offsets. Post-quantum sizes are a non-issue: a 1184-byte
Kyber key and a 4595-byte Dilithium signature are accepted without complaint.

**Where it stops is dimensional, and the root is one decision rather than
several oversights.** Everywhere else in this language a declaration produces
a checkable claim in generated code. In this section it does not: the emitted
API is byte-identical whether a schema names a nonce or names none at all, so
`nonce = n` is currently a comment that happens to parse. Nothing derives an
obligation from it, which is why nothing can catch the cases below.

| what a schema may say today | what situ does with it |
|---|---|
| `tag u8[1]` -- an 8-bit authentication tag | refused where the codec states `tag_bytes` (0038); accepted where it does not |
| `u8 n [nonce]` -- 256 distinct nonces | accepted; advisor silent |
| `sealed(codec)` naming no nonce | accepted, and an omission looks the same |

**A codec cannot state its own parameters.** Its vocabulary is
`authenticated`, `deterministic`, `error_propagating`, `expansion`,
`granularity`, `invertible`, `kernel`, `length_preserving`, `seekability`,
`seekable`, `systematic` -- every one of them about *shape*, none about
*size*. There is no tag width, nonce width or key width, so a declared
`tag u8[8]` cannot be compared against what the codec actually produces. Note
that a truncated tag is legitimate rather than wrong -- OSCORE uses eight
bytes on constrained links -- which is precisely why it needs to be *sayable*
instead of banned: as it stands a deliberate truncation and a mistake are the
same text.

**Settled and built.**
`doc/decision/0038-codec-size-parameters.md` lets a codec declare
`tag_bytes` and `nonce_bytes`; `check_codec_sizes` compares them against the
tag and nonce fields a schema declares, and `[truncated]` is how a schema says
a narrow tag is meant. A key width is deliberately absent, because no
construct here names a key and the setting would be read by nothing. The three
constructs below are unblocked by it rather than settled by it: each needs its
own record.

**~~Three more, each a construct real protocols need and this cannot
express.~~ All three are sayable now, and the lead outlived every one of
its own bullets.** Two were closed by their own records -- key selection
by 0040, a wide conversation key by 0042 -- and each bullet says so
where it stands; out-of-band material closed half way under a different
name and 26.470 has the measurement. The sentence stayed because a lead
is read as the setup for the list under it rather than as a claim with
its own shelf life, which is how a line can be falsified three separate
times and survive all three.

- **Key selection -- expressible now (0040).** DTLS carries a 16-bit epoch,
  QUIC a key-phase bit, WireGuard a receiver index.
  `sealed(codec, nonce = n, key = epoch)` names the field whose value
  selects the region's key, with the nonce's ordering rule for the nonce's
  reason, and `example/dtls` is the worked case. Like the nonce argument
  it reaches no backend yet: its value is its checks and its sayability,
  and whether both belong in the capability map and wire signature stays
  with 26.117's open question.
- **~~Out-of-band material.~~ Half built, and what is left is a width
  ceiling rather than an expressiveness gap (26.470).** 0050's
  `parameter` is exactly "supplied by the caller", so
  `parameter u64 iv;` with `sealed body (ae, nonce = iv)` says it while
  an omitted `nonce =` says the other thing: the map prints
  `message.iv parameter`, the wire signature prints `parameter - u64 iv`
  beside `nonce=iv`, and `check_codec_sizes` holds the width against the
  codec's `nonce_bytes`. What remains is that a `parameter` is a member
  of ZERO WIDTH by 0050's own reasoning, so it is a scalar and scalars
  stop at 64 bits -- which puts the 12-byte nonce of AES-GCM and
  ChaCha20-Poly1305 out of reach while an 8-byte one works. **A key is
  named the same way** -- 0040's `key = field` takes a `parameter`
  too, and the wire signature prints `key=kid` beside `nonce=iv` --
  under the same ceiling. What a key lacks is the CHECK: there is no
  `key_bytes`, so a `key =` field is held against nothing where a
  `nonce =` field is held against `nonce_bytes`.
- **A conversation key of more than 64 bits -- expressible now (0042).** TLS
  session identifiers are 32 bytes, QUIC connection identifiers up to 20, and
  WireGuard and Noise identify a peer by a 32-byte public key. The collision
  question was the answer's shape: the old refusal existed because a
  truncated or hashed key silently matches two exchanges to each other, and
  that argument never said the key must be small -- it said the key must be
  *exact*. A key is the exact bytes of the compared fields now, packed into
  one word where it fits (byte-identical to what fitted before) and a
  byte string up to `KEY_MAX_BYTES = 32` where it does not, compared with
  `memcmp` and never hashed at any width. 26.130 carries the build.

~~**And one now half-built.**~~ **Both built.** 14.7 gives `pad_to(n)` and
`pad_random(min, max)` for traffic-analysis resistance. `pad_to(n)` came
first (0043): the alignment padding the parser used to refuse, one member
now, `<pad>` in the map and `pad-to=n` in the wire signature.
`pad_random(min, max)` followed in 26.359 and takes the same shape one rung
along -- `<pad>` in the map with its ceiling as the extent, and
`pad-random=min..max` in the wire signature, because a peer padding to
different bounds disagrees about what lengths are legal.

**The question this survey opened as unknown, now measured -- and the first
measurement of it was wrong.** It reported that QUIC-style header protection
crossed the model's boundary twice, on contiguity and on layout depending on
transform output. The second is not true, and the error came from testing the
wrong shape: sizing a member *outside* a `coded` region from a field inside it
is refused, and that was read as a rule about decoded content in general.

**Layout may depend on decoded content, inside the region that decoded it.**
`coded prot(hp) { u2 pn_len; u8 pn[pn_len]; }` is accepted, and so is the same
shape inside a `sealed` region. That is the staged model 12.1 already
describes: the transform runs, and behind the gate the interior is as dynamic
as any other message, at whatever cost the transform had. 13.3's refusal bites
only where a reference leaves the region, which is the right place for it --
what would be undecidable is the layout *around* a transform, not the layout
*within* one.

So the whole of QUIC's byte zero -- the protected bits, the two-bit packet
number length, and a packet number sized by that decoded field -- compiles
today.

#### A specimen from fuzzypickles, signalled 2026-08-26: a seal chosen by a flag

**Sent because a real payload is about to be decided about, and the question is
whether situ describes it or the hand-written codec moves as-is.** fuzznet is
weighing whether to take fuzzypickles' log relay, whose 145-line payload codec
is the piece nobody wants to hand-carry twice. This is that wire format, from
`core/src/log_relay_internal.h` at fuzzypickles `4e66b5a`, transcribed rather
than paraphrased:

    header, 4 bytes:  version u8 (= 1) | cmd u8 | sub_type u8 | mode u8

    sub_type = QUERY (1):  origin_host_pubkey[32] || since_seq u64 BE
    sub_type = LINES (2):  origin_host_pubkey[32] || entry_count u8 ||
                           repeated { seq u64 BE, line_len u16 BE, line[] }

    mode = PLAINTEXT:  the payload above, as named
    mode = ENCRYPTED:  nonce[24] || ciphertext || mac[16] wrapping it

**Three of the four things it does are plainly covered**, and they are said
first so the question below is not mistaken for a list of gaps. The `sub_type`
arm is 9.6's `variant` over an already-parsed field, discriminant before
variant in layout order, exactly as written. One arm is fixed at 40 bytes and
the other is a length-prefixed repetition, so the variant makes what follows
dynamic and the advisor would say so. And nothing sizes a member outside the
region from a field inside it, which is where 13.3 bites.

**The fourth is the question: `mode` decides whether the payload is sealed at
all.** Not which key, not which algorithm -- whether the same bytes appear
plainly or wrapped in `nonce || ct || mac`. Every example in 14.1 seals
unconditionally; this is a `sealed` region whose existence is switched by a
sibling field parsed three bytes earlier.

**Two shapes it could be, and they are not equivalent:**

- **A `variant` over `mode` whose arms are "the payload" and "a sealed region
  containing the payload".** Expressible with what 9.6 and 14.1 already have,
  and it states the interior twice -- which is exactly the duplication a schema
  language exists to remove, and two statements of one layout is the failure
  mode 26.95's relation work is about elsewhere.
- **A conditional seal -- one interior, sealed when a predicate over an earlier
  field holds.** No construct in 14.1 says this, and a search of this document
  for "conditional seal" returned nothing before this entry was written --
  which is worth saying that way, because the same search now finds this
  paragraph.

**The doom principle is what makes it more than spelling.** 14.3 makes a sealed
interior VerifyGated with no API to view it unverified, and that is the
property worth keeping. A conditional seal has to gate on the *runtime* value
of `mode`, so the generated API either exposes an interior that was never
verified when `mode` said PLAINTEXT, or forces the caller through a check that
does nothing in that case. Which of those situ should emit is a design
question, and it is the reason this is signalled rather than filed as a
feature request.

**Worth knowing before answering: the flag is vestigial in practice.** The
source says of `mode` that it is "Always ENCRYPTED in practice -- a
`trusted_network=1` sibling is the only case PLAINTEXT is legal". So the cheapest answer may be that the
schema should not describe the choice at all, and the protocol should drop it.
**That is fuzznet's and fuzzypickles' decision, not situ's**, and it is
recorded here because a language question raised by a protocol that could
simply stop asking it is one worth checking before building for.

**No answer is wanted here today.** The consumer decision this came from --
move the codec verbatim with its command id parameterised, or describe it --
is fuzznet's, and it is recorded in that project's `project.md` under the
2026-08-26 entries. This is the specimen, in case situ would rather find out
now than after somebody hand-carries a second copy.

**What blocked it was contiguity, and that is closed.** A `coded` region now
takes a `covers` clause (14.1a) and reaches its codec through the scattered
ABI (13.2b) -- in place, over a list of spans. QUIC's mask covers byte zero
and the packet number with the destination connection id between them, and the
generated span list carries both with the gap left alone.

The shape was borrowed from tags, as this section predicted it would be:
`covers(a, b)` already named a disjoint set for a tag, and a transform had no
equivalent until it was given one. What the borrowing did not survive is the
default -- an omitted clause infers every region for a tag and means "only
myself" for a transform, because there is a sensible default for what a tag
authenticates and none for what a transform reaches.

**Ordering is settled too** (14.1b, `doc/decision/0037-transform-and-tag-
order.md`). A transform covering bytes a tag covers must say whether the tag
sees its input or its output, and the answer decides the generated signature:
under `before` the accessor takes the message and marks the tag dirty, so the
staleness cannot be forgotten. What situ does not do is run the sequence --
it says which order is required and makes the wrong one visible, which is the
line 13.1 draws.

**What stays out of scope, and why that is not in tension with the intent
above.** Key schedules, transcript hashing and signature computation are
computation rather than layout, and a schema compiler that grew them would be
a cryptographic library with a parser attached. Covering a context does not
mean performing it: the obligation situ should carry is that a message's
shape, coverage, ordering, staging and *dimensions* are described and checked,
whoever supplies the arithmetic underneath.

---

## 15. MMIO target

The later use case: context-sensitive in-place manipulation on embedded targets
with small memories and registers that have side effects. Vocabulary borrowed
from SystemRDL and CMSIS-SVD rather than reinvented.

### 15.1 Target declaration

`target mmio` changes defaults: `volatile` becomes implicit, `access_width`
becomes mandatory per register, and "in-place mutation" means "issue the bus
transaction" rather than "modify bytes in a buffer". The surface API looks the
same; the codegen is entirely different, and the schema author must know which
model applies.

A schema may not mix `target buffer` and `target mmio` in one file. Cross-file
`import` of type definitions is permitted; register declarations are not.

**`target file` is the third, and it carries one assumption** (decision 0047):
a store lands in the file, so `effect := EffectOnWrite` for every member. It
is durable, another reader may see it, and it is therefore not a pure store to
be repeated, reordered or elided. The assumption belongs to the medium and not
to the member: whether there is a store at all is what `mutate` says, and a
tag that nothing may write still reports the effect a write would have.

`target file` also refuses what cannot live in a file. A `register` needs
`target mmio`, which the existing refusal already covers because it is keyed
on "not mmio" rather than on "buffer". And **an unbounded top-level extent
requires `append`**: the first struct is what a reader acquires, so its extent
is the file's, and a message with no end in a medium that has one is a schema
nothing could decide where to stop reading.

**`append` is written, not implied.** `target file append` makes the extent
growable and sets `address := Unstable` throughout, because a resize may move
the mapping and no pointer into it survives one. It is spelled because it is
not true of files as such: of the seven file-format examples measured for
0047, six are not growable and one -- `example/sqlite` -- is.

Sparse-file support is deliberately not part of this. A hole reads as zeros
cheaply and the first write into it allocates and may fail, which makes a
write's cost and its fallibility depend on *where within* a member it happens,
and no axis varies a property by position. That is a lattice question before
it is a target one.

### 15.2 Registers

```situ
target mmio;

register CtrlReg @ 0x00 {
    width        = 32;
    access_width = 32;          // partial access forbidden implicitly
    volatile;
    no_rmw;                     // reads have side effects; RMW unsafe

    bit  enable  [rw];
    bit  start   [wo, on_write = trigger];
    u3   mode    [rw];
    bit  busy    [ro];
    bit  error   [w1c];
    reserved u25 [preserve];
}
```

Access modes to support (SystemRDL vocabulary): `ro`, `wo`, `rw`, `w1c`, `w0c`,
`w1s`, `w0s`, `rc` (read-to-clear), `rs`, `wo_once`, `rsvd`.

Side-effect declarations: `on_read = { none | clear | pop | trigger }`,
`on_write = { none | trigger | clear }`.

Reserved behavior: `must_be_zero`, `must_be_one`, `preserve`, `unknown`.

Scoped defaults: `register_block` may declare `width`, `access_width`,
`volatile`, `endian` once for all contained registers.

### 15.3 Capability interaction

The point of unifying registers with protocols is that the same lattice
answers both. Specifically:

- `access_width = 32` plus a `bit` field means a single-bit write requires RMW.
- `no_rmw` (or `on_read` other than `none`) therefore makes that field
  `mutate = RewriteRequired`: the generated API exposes no `set_enable()`, only
  `CtrlReg_write(builder)` where the caller constructs the whole word.
  Setting one bit becomes a compile error rather than a runtime hazard.
- `require in_place(CtrlReg.enable)` fails with:
  `cannot synthesize single-bit write: access_width=32 and on_read=pop on CtrlReg`
- `w1c` fields generate `clear_error()` rather than `set_error(false)`, because
  the write semantics are not assignment.
- `wo` fields generate no getter. `ro` fields generate no setter.
- `busy [ro]` with `volatile` means the generated getter must not be
  cached or reordered; emit through a volatile access and document that the
  value may change between reads.

---

## 16. Assertions and requirements

Two keywords, deliberately distinguished:

- `require <capability_expr>;` -- a build-time gate. Failure is a compile
  error. This is how a schema states its budget so a later edit cannot quietly
  regress it.
- `assert <capability_expr>;` -- same check, but failure is a warning. For
  documenting intent during exploration.

Capability predicates:

```
size(X) == N            offset(X) == N          max_size(X) <= N
absolute_static(X)      frame_static(X)         static(X)
in_place(X)             in_place_dirty(X)       immutable(X)
random_access(X)        stable_address(X)
canonical(X)            deterministic(X)        deterministic_writer(X)
aligned(X, n)           atomic(X)               memory_identical(X)
no_tag_invalidation(X)  verify_gated(X)         uncovered(X)
no_alloc(X)             bounded_stack(X, n)     no_realloc(expr)
```

`memory_identical(X)` asks the `repr` axis: are the bytes the value, with no
swap, shift, scale or decode between them? It is the question a caller has
before pointing at a field and reading it as it lies, and the axis had no
predicate until fixed point and BCD gave it a third and fourth way to answer
no -- byte order and bit packing being the first two. A field can fail it for
several reasons at once, and the blame chain names each: `q8_8` in a
big-endian schema is both byte-swapped and scaled.

**Three of these the compiler names and cannot decide**, and it says which
and why rather than passing them. They were once recorded against the phase that
would implement them; every one of those phases landed without the predicate
arriving with it, so `needs phase 7` had become a promise the schedule no
longer backed -- a reader would wait for something that had already happened.
What is true of each is a reason, so that is what the diagnostic gives:

| Predicate | Why not |
|---|---|
| `deterministic(X)` | asks about a codec's property signature rather than a field's capability vector, and nothing connects a field to the codec above it |
| `no_alloc(X)` | generated code never allocates (invariant 4), so it always holds; the predicate would be a lint, not a requirement |
| `bounded_stack(X, n)` | needs a stack-depth model of the generated code, which the compiler does not build |
| `no_realloc(expr)` | depends on a runtime value, so it is a `SITU_CHECKED` check rather than a compile-time discharge, and that machinery is not wired |

None of them is silently satisfied, which is invariant 5: a requirement the
build cannot decide is reported as undecided.

Runtime-checked variants exist where the property depends on runtime values:
`no_realloc(expr)` cannot be decided statically when the new value's size is
dynamic, so it compiles to a runtime check in `SITU_CHECKED` builds. The
compiler must report, per requirement, whether it was discharged statically or
deferred to runtime. Silently downgrading a static check to a runtime one is
not acceptable.

---

### 16.1 Invariants

An invariant names a field and what it derives from:

```situ
struct frame {
	u16  total;
	hdr  header;
	u8   body[remaining];
}

invariant frame.total == size(frame.header) + size(frame.body);
```

Three things follow, and none of them needed new machinery -- the tag model of
14.2 already had this shape, for a harder case:

- **`total` loses its setter.** Its `mutate` is `Immutable`, and the header
  says which invariant decided that. Writing it directly would make the
  schema's own statement false.
- **What the right side reads becomes covered.** `header` and `body` get
  `auth = Covered(invariant total)`, so `in_place` fails on them and
  `in_place_dirty` passes -- which is exactly right, since writing one leaves
  something stale.
- **A recompute is generated**, taking whatever holds the dirty word, as every
  covered write does. Until it runs, the message is not transmittable.

The right side may use `size`, `count`, `offset` and arithmetic over them, and
nothing else. A call to anything else is refused with a diagnostic, because
the alternative is each backend declining it separately and reporting that
*this build* cannot evaluate it -- true of a dynamic offset on a target that
cannot resolve one, and misleading about a question that does not exist
anywhere. A value that has to be computed from the bytes is a codec or a tag,
which have their own machinery and their own bits.

Where a backend genuinely cannot evaluate an admissible expression, it emits no
recompute and says so. The refusal to write the field directly still stands, so
the invariant cannot be broken, only left unsatisfiable. That is the honest
half of not implementing something.

**The dirty word is shared with tags, and numbered once.** `traverse.obligations`
assigns the bits -- tags in declaration order, then invariants -- and every
backend reads that rather than counting for itself. Two of them counted
separately for one release and gave the same schema different bits; a caller
who stores a bit from one language and checks it in another has to find the
same answer. Tags keep bits 0..n-1 when an invariant is added, because
renumbering them changes what an already-generated header means.

Each of the four backends spells this its own way and means the same thing:

| | recompute | dirty word |
|---|---|---|
| C | `situ_s_total_recompute(msg, view)` | `situ_msg_t` |
| C++ | `s.recompute_total(msg)` | `situ::rt::message` |
| Python | `s.recompute_total()` | `Message`, reached through the view |
| Rust | `s.recompute_total(&mut dirty)` | `situ_rt::Dirty`, passed separately |

Rust passes it separately because a message that owned the buffer is the one
thing that backend cannot have: a view *borrows* the caller's slice, and that
borrow is how 12.3's invalidation rule is enforced at compile time. Handing the
borrow out from inside a message would tie the dirty word to the bytes, so
marking a bit would conflict with holding the view that wrote it.

**Both halves must be in the same struct.** An invariant is evaluated against
one view, and a field of another struct is not reachable from it. A field
cannot derive from itself, because recomputing it would read the value it is
about to write.

The `auth` axis is what carries this, and 11.1 states it as "which obligation
covers these bytes" rather than "which tag": a tag and an invariant are the
same obligation with different arithmetic behind them.

## 17. Diagnostics

Diagnostic quality *is* the product. Budget real implementation time here.

### 17.0 Ambiguity is an error

**Wherever an ambiguity exists, the schema must resolve it explicitly.** Situ
never guesses and never picks a silent default in a place where the wrong choice
is undetectable at runtime. This is the governing principle of the whole
diagnostic design, and it is why the error messages have to be good: a language
that refuses to guess generates a lot of errors, and each one has to teach.

The known ambiguity classes, each of which must be an error until explicitly
resolved:

| Ambiguity | Required resolution |
|---|---|
| endianness of a multi-byte scalar with no directive in scope | `endian` directive at file, struct, or field level |
| bit order with any sub-byte field present | `bit_order` directive |
| a bit field straddling a byte boundary | `[allow_straddle]` on the struct |
| overlapping non-nested tag coverage | explicit `covers(...)` on each tag |
| a transform covering bytes a tag covers | `[tag_order = after]` or `[tag_order = before]` (14.1b) |
| variant arms of unequal size | accepted, but the dynamic consequence is reported; `[equalize]` to pad |
| unknown enum value, TLV tag, or version | explicit `error` / `skip` / `preserve` / `pass` policy |
| non-minimal varint acceptance | `minimal` present or absent, never defaulted |
| duplicate TLV tags | `duplicate_tags = error \| allowed`, plus an ordering rule if allowed |
| layer order of codecs over the same region | explicit pipeline; encrypt-then-code and code-then-encrypt are never inferred |
| a codec signature with a `kernel` whose derived properties contradict the declared ones | error naming both values; no precedence rule |
| a field's alignment when the target may fault on unaligned access | `[require_aligned]` or explicit acceptance |
| whether an unimplemented codec is an error now or later | error at codegen, never at analysis |

Where a default does exist, it is chosen so that the *safe* option is silent and
the *unsafe* option is loud: `reserved` defaults to `must_be_zero`, unknown
values default to `error`, strictness defaults to `strict`. A schema that wants
the permissive behavior has to say so, and saying so appears in the capability
map.

Every capability failure must report:

1. the requirement that failed, with source location
2. the capability axis and the actual versus required value
3. **the blame chain**: the construct that caused the weakening, with its
   source location, transitively back to the root cause
4. the blast radius: how many other fields were affected by the same cause
5. one or more concrete remedies, with costs (Section 18.2)

Required format:

```
error: requirement not satisfied
  --> packet.situ:41:1
   |
41 | require absolute_static(message.recs);
   | ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
   |
   = offset(message.recs) is Dynamic, required AbsoluteStatic
   = caused by: message.opts has size Bounded(0,1500)
  --> packet.situ:28:5
   |
28 |     u8 opts[hdr.length];
   |     ^^^^^^^^^^^^^^^^^^^ dynamic size introduced here
   |
   = 2 further members lost absolute addressing: recs, trailer
   = remedy: move `opts` after `recs` and `trailer`
             cost: 0 bytes; restores AbsoluteStatic for 2 members
   = remedy: give `hdr.length` a [max = 1500]
             cost: nothing on the wire; bounds the worst case
```

This block said `pin `opts` to a fixed size with [size = 1500]` for a long
time, and the advisor said it too, while `[size = N]` was in the parser's
vocabulary and read by nothing: a reader who took the advice got a schema that
compiled, changed nothing, and produced the same suggestion on the next run
(26.60).

Both halves are settled now and they were settled separately, which is the
part worth keeping. The *advice* was wrong wherever the attribute stood: what
reaches this branch is an unbounded scan, and what bounds a scan is `max N`.
The *attribute* was not wrong, only undefined -- decision 0039 gives it a
meaning, and it is the one this block originally reached for. `[size = N]`
pins a member's footprint while its extent expression goes on saying how much
of it is meaningful, so `opts` can be a fixed 1500 bytes with `hdr.length`
still bounding its content and `recs` and `trailer` keeping their absolute
offsets. See 26.123.

Also emit machine-readable diagnostics (`--diagnostics=json`) so the advisor,
editors, and CI can consume them without parsing prose.

---

### 17.0a `import`, and what it brings

`import "other.situ";` splices the named file's declarations into the
importing one, before any whole-schema check runs. Four decisions, each taken
because the alternative is an ambiguity rather than because it is tidier:

- **Relative to the importing file**, the way `#include "..."` is. There is no
  search path, so which file an import names does not depend on how situc was
  invoked -- a search path makes that a property of the invocation, which is
  exactly what 17.0 refuses to leave open.
- **`import std "std/kernels.situ";` reads from where situ installed its own
  schemas**, which is the other half of that rule rather than an exception to
  it. 17.0's objection is to a search *path* -- a list, where
  "which of these won?" has no visible answer. This is one directory, found
  the way `bin/situc` finds its own package, so an import still names exactly
  one file. What it depends on is which situ is installed, and that is the
  point: a consumer describing a JSON-based format should not have to write
  `../../../usr/share/situc/std/kernels.situ` into a schema that then compiles
  on one machine. `situc --print-std-dir` prints the directory, for a build
  system that needs the path outside a schema.

  **The root is what HOLDS the schema directories rather than one of them**, so
  `<prefix>/share/situc` mirrors this tree entry for entry and one path names
  the same file in both -- `import std "example/ipv4/ipv4.situ"` compiles here
  and against an installed situ, unchanged. It costs a `std/` in the spelling
  of a `std/` file, which is the price of the mirror and is worth revisiting
  when the directories are named.

  **Neither form falls back to the other**, which is what makes the second
  spelling worth having rather than a convenience. A fallback would mean that
  dropping a file beside your own silently changes which file an existing
  import names -- the shadowing hazard `#include "..."` has, and the reason C
  keeps `<...>` separate. `std` is a soft keyword read the way `at` is, so no
  schema using it as a name stops parsing, and it is a word rather than angle
  brackets because 0006 settles that for this tree and because `<kernels.situ>`
  lexes as five tokens the LSP and the unparser would both have to learn to
  reassemble.
- **Flat.** Nothing is renamed and nothing is qualified: an imported
  `codec aes_gcm_128` is `aes_gcm_128` here. Two declarations reaching one
  name are refused by `check_unique_declarations`, which has caught that since
  it was written and needed nothing new to catch it across files.
- **Transitive**, because a file's imports are expanded before its
  declarations are handed back. A consumer cannot be asked to know what its
  dependency needs, which is what makes a library of contracts usable at all.
- **Once per compilation**, keyed on the resolved absolute path. A diamond --
  two imports that both import a third -- contributes the third once. Without
  that, a flat namespace would make every diamond a redefinition.
- **Declarations, not directives.** The four above are about things that have
  NAMES, and a directive names nothing: it is a claim about the file it is
  written in. `endian` and `bit_order` scope a struct positionally within its
  own source and stop at that source's edge; `encoding` and `whitespace` are
  resolved as each file is parsed and always did. `target` and `strictness`
  are the artifact's rather than a struct's -- one compilation has one target
  -- so the root file's holds and an imported file stating a different one is
  refused, naming both. Agreement is not refused.

  This is stated because it was not, and four directives rode in on the word
  "declaration": a file with no `endian` inherited the last imported one and
  compiled, which is 17.0's "situ never guesses a byte order" answered by a
  line the importing file never wrote. 26.295 has the measurements.

A cycle stops rather than recursing: the second visit contributes nothing,
which is the same mechanism the diamond uses and needs no separate rule.

A schema parsed from a *string* has no directory to resolve against, and says
so rather than falling back to the working directory. That is why `parse` is
split: `parse_decls` reads declarations, `imports.expand` splices, and
`Parser.finish` runs the checks over the whole thing. Checking a file whose
imported types have not arrived would refuse names that are about to exist.

**Three workarounds went with it**, and they were the honest half of not
having this. `check_types_resolve` stepped aside entirely when a schema
imported, because a missing name might legitimately live in the imported file;
the solver carried a note saying import resolution was not implemented; and
the codec lookup carried the same note, added only days before. All three were
dead the moment `expand` ran before the checks -- there is no `ImportDirective`
left in the schema by then -- and dead code that claims to handle a case is
worse than none.

## 18. The advisor

This is the differentiator. The compiler helps designers make protocols *less
dynamic*.

### 18.1 The capability map

`situc map schema.situ` emits the full capability vector for every field and
region, in a stable, diffable text format. `--format=json` for tooling.

**Commit the map to the repository.** `situc map --check` compares against the
committed `*.situ.map` and fails if it differs, exactly as a snapshot test
would. This makes capability regressions appear as a reviewable diff at the
moment of editing rather than as a performance surprise months later. The
committed map doubles as the protocol documentation that projects always want
and never have.

### 18.2 Suggestion catalog with cost model

Suggestions are mechanical and each carries a cost. Report typical and
worst-case separately -- embedded designers size static buffers off the worst
case.

| Suggestion | Trigger | Typical yield |
|---|---|---|
| move variable-length fields toward the tail | dynamic member with static members after it | highest-yield single change; everything before stays absolute |
| replace varint with fixed width | bounded varint field | if `max = 1500`, varint costs 2 bytes for most of the range anyway; fixed `u16` is free and restores fixed offsets |
| pin an unbounded region's max | `size = Unbounded` | makes the region statically allocatable |
| equalize variant arm sizes | variant with unequal arms | costs padding to the largest arm; restores static offsets after the variant |
| reorder to fill alignment holes | unaligned members with padding present | zero cost |
| group covered fields contiguously | scattered `authenticated` regions | one tag over one range instead of several; fewer recomputations |
| move a mutable field out of tag coverage | frequent mutation of a covered field | eliminates tag recomputation per mutation |
| convert `tlv` to `positional` for known-common tags | hot tags in a TLV region | O(1) access instead of O(n) scan |

Two of those triggers are not decidable from a schema and are implemented as
what the compiler can actually supply:

- **"frequent mutation of a covered field"** -- a schema does not say which
  fields are hot. The advisor reports one suggestion per tag rather than one
  per field, listing the candidates and pricing a write in bytes
  re-authenticated, and leaves the choice of which are frequent to the person
  who knows. A field inside a sealed region is not a candidate at all: moving
  it out of coverage means taking it out of the seal.
- **"unaligned members with padding present"** -- triggered off the weakening
  the lattice already recorded, because `Aligned(1)` is a weakened align axis
  even though its base is not `Unaligned`, and the rule that noticed is the one
  that knows.

`situc advise schema.situ` runs the catalog and prints ranked suggestions, and
exits 0 whatever it finds: a suggestion is advice about a design rather than a
verdict on one, and a build that failed on advice would teach people to stop
reading it. `situc diff` is the opposite -- it exits non-zero on a regression,
because that is what makes it useful in CI.
`situc explain message.recs[].value` prints one field's full vector plus the
blame chain for every axis that is not at its strongest value.

### 18.3 Revision diff

`situc diff old.situ new.situ` reports capability regressions and improvements
between two schema revisions: fields that lost in-place mutability, size bounds
that grew, canonicity that was lost. Intended for code review and CI.

**It is a cost linter and not a compatibility one**, and the difference is
worth stating here as well as in 19: the two questions are asked by different
people about the same edit. This one answers "does it still cost what it
cost". `situc wire` answers "can a deployed peer still read it" (19.3).

The clearest way to see that they are different orderings: moving a field out
of an authenticated region is an **improvement** here, and correctly so --
a field with no tag over it is cheaper to write. It is also a security
regression. Both readings are true of the same edit, which is why one tool
cannot serve both.

---

## 19. Schema evolution

Situ has no field numbers, so evolution must be explicit. This is the one place
where protobuf's design bought something real, and the replacement must be
deliberate rather than accidental.

### 19.1 The model

1. **Version is a field, not metadata.** Schemas that need to evolve carry an
   explicit version discriminant and use `variant` to select the layout.
2. **Old revisions are kept in the schema**, not deleted. Two ways, and the
   first is the one to reach for:

   - `[since = N]` on a member, for the case that is almost all of them: the
     format only ever gained fields at the end. Append-only is enforced rather
     than reviewed for, and every member keeps a static offset. See 19.4.
   - `variant` on the version field, where a revision genuinely re-laid the
     bytes rather than extending them:

```situ
variant body switch (hdr.version) {
    case 1: BodyV1 v1;
    case 2: BodyV2 v2;
    default: error;
}
```

3. **Three compatibilities, and they are not one question.** Saying a revision
   is "compatible" without saying which is how the word stops meaning
   anything:

   | | asks | who finds out |
   |---|---|---|
   | **wire** | can a new sender talk to an old receiver? | deployed peers, silently |
   | **API** | does code calling the accessors still compile? | the build |
   | **cost** | does it still cost what it cost? | performance, at scale |

   `situc diff` answers the third and part of the second. It was described
   here as the compatibility linter, which it is not, and the gap is not
   academic: flipping `endian big` to `endian little` changes every byte on
   the wire and `diff` reports "No capability change"; swapping two `u16`
   members reports zero regressions and exits 0.

   Worst, moving a field *out* of an authenticated region -- a security
   regression -- is reported as an **improvement**, because `Uncovered`
   genuinely ranks stronger than `Covered`: a field with no tag over it is
   cheaper to write. The lattice is right about cost. The mistake was reading
   a cost ordering as a compatibility ordering.

4. **`situc wire` is the compatibility linter**, and it answers the first
   question. See 19.3.
5. **Never silently accept unknown versions.** `default: error` is required
   under strict mode.

Explicitly rejected alternative: unknown-field retention. It is what makes
canonical encoding impossible (Section 3) and it is a malleability surface in
an authenticated protocol.

**The generator's output is not versioned, and that is a position rather than
an omission.** An adopter evaluating situ asked for the intended answer -- a
version marker in the generated header, a stability policy for output across
releases -- on the reasoning that every serious adopter reaches the question
and each inventing a different answer is how a generated-code ecosystem goes
bad. The answer is that there is nothing to version.

situc's job is to model the format as it actually is. Generated accessors are
therefore either **right or wrong** about the bytes, and a version number on
them would describe which release produced a wrong answer rather than making
it right. A schema that says what the format does yields accessors that agree
with the format; regenerate with a later situc and they agree with it still.
Where output does change between releases, either the earlier one was wrong --
in which case the fix is the point, and pinning to it is pinning to a defect
-- or the format description changed, which is `situc wire`'s question and is
answered there.

This is also the reason a compatibility promise about output would be
dishonest: it would be a promise that a future situc will keep reproducing a
byte layout regardless of whether that layout is correct.

**What situ owes the user instead is contradiction.** We cannot protect
anybody from a schema that misdescribes reality -- a wrong directive produces
wrong accessors, and no amount of versioning changes that. What we can do is
generate enough independent ways of looking at the same claim that a wrong one
*collides with something*: the capability map against the accessors
(`gen-checks`), the accessors against arbitrary bytes (`gen-fuzz`), the four
backends against each other, and -- 26.61 -- the accessors against a
third-party implementation of the same format. A contradiction is a solution
in progress. A version number is neither.

### 19.2 Importing `.proto`

`situc import-proto foo.proto -o foo.situ` produces a Situ schema describing the
protobuf *wire format* for those messages, in the style of Section 9.7. It is a
transpiler to a description of an encoding, not an attempt to preserve protobuf
semantics.

Translates mechanically:

- `message` -> `struct` containing a `tlv` region
- field numbers and types -> `known` entries with the correct wire type
- `repeated` -> `duplicate_tags = allowed`
- `enum` -> `enum` with `default = pass` (protobuf semantics) and a warning
- nested messages -> nested `tlv` with `prefixed(pb_varint)` sizing
- `oneof` -> `variant`, with a note that the "at most one set" invariant is a
  semantic constraint the layout cannot enforce

Does **not** translate, and must be reported rather than silently dropped:

- `map<K,V>` -- unordered, non-canonical, and its entry encoding is an
  implementation detail; emit an error with the recommendation to model it as
  `repeated` entries explicitly
- `Any`, `Struct`, `Value` and other reflection-dependent well-known types
- proto3's absent-versus-zero indistinguishability for scalars, which is a
  semantic property with no layout expression
- service and RPC definitions, which are out of scope entirely
- groups (wire types 3 and 4)
- field-number-based evolution semantics; the generated schema is a snapshot of
  one revision, and Section 19.1 versioning applies from then on

**The importer must emit a fidelity report** listing every construct it could
not represent, with the source location in the `.proto` and the reason. An
importer that silently produces a plausible-looking schema is worse than no
importer, because the user will trust it. Exit non-zero if anything in the
not-translated list appears, unless `--accept-lossy` is given.

Direction is one-way. There is no `.situ` to `.proto` export and there should
never be one; Situ can express layouts protobuf cannot represent.

---

### 19.3 The wire signature

`situc wire` emits the byte-level contract, and it is committed beside the
schema as `NAME.situ.wire` and checked. Same argument as the capability map
(18.1): the failure should be a red diff at the moment somebody edits the
schema. The stakes are higher here -- a capability regression is a performance
surprise, and a wire break is a message a deployed peer cannot read, on a
machine nobody can recompile.

```
struct udp_header size=8
  @0x0000   2         u16        source_port  big
  @0x0002   2         u16        destination_port  big
  @0x0004   2         u16        length  big min=8
  @0x0006   2         u16        checksum  big
```

**No capabilities appear in it.** Mixing the two would produce a signature
that churns whenever something gets faster, which teaches people to skim the
diff -- and skimming this diff is the failure mode it exists to prevent.

**It is positional, not nominal.** Position carries identity (section 4), so a
name is not on the wire at all; the signature records names only so a
comparison can say *which* member moved.

`situc wire --check` classifies what changed rather than printing a textual
diff, because the categories are not equally alarming:

| | meaning |
|---|---|
| **BREAKING** | a deployed peer misreads these bytes |
| **COVERAGE** | what a tag authenticates changed -- peers agree on the bytes and disagree on the tag, and if the region shrank the new build authenticates *less* |
| backward-compatible | a new receiver reads what an old sender produces; an old receiver may refuse what a new sender produces |
| forward-compatible | an old receiver reads what a new sender produces |
| api-only | the bytes are unchanged; calling code has to be edited |

Three of these are things `situc diff` gets wrong, and each was checked before
this was written rather than assumed:

- Flipping `endian big` to `endian little` rewrites every message and leaves
  every capability vector identical. `diff` reports "No capability change".
- Swapping two `u16` members reports zero regressions and exits 0.
- Moving a field *out* of an authenticated region is reported as an
  **improvement**, twice.

**Two directions, named separately.** Tightening a constraint keeps old
*senders* working and may make a new receiver reject them; loosening does the
reverse. Calling either "compatible" without saying which is how the word
stops meaning anything.

**What the bytes are, versus which of them are allowed.** A changed `max`
narrows the set both sides agree is valid, so old and new still read the same
number and one may refuse it -- compatible in one direction. A changed byte
order means the same bytes are a different number, which no agreement about
ranges helps with. The first version of this reported a byte-order flip as a
relaxed constraint, putting the same break twice in the reassuring half of the
output.

**A permutation is not a rename.** Swapping two members of equal width leaves
every byte where it was, so on the wire it is literally two renames -- which
is exactly why it needs its own finding. The names did not change; what moved
is the meaning attached to each position, and an old receiver reads each under
the other's name. Reported as a rename it would have looked cosmetic, which is
the most dangerous thing this file could do.

**Appending is not assumed safe.** An old receiver sized its buffer from the
old contract, and whether it ignores trailing bytes or rejects the message as
overlong is a property of that receiver which situ does not know. It is
reported rather than waved through.

**Adding an enum member** is free under `default = pass` and breaking under
`default = error` -- which is the default (8.7), so the common case is the
surprising one. The two schemas differ by one word and could not differ more
in a deployment.

### 19.4 More than one version in one file

`[since = N]` says a member arrived in version N, and `[version = f]` on the
struct says which member carries it:

```situ
struct msg [version = ver] {
	u8   ver;
	u16  length;
	u32  flags [since = 2];
	u16  extra [since = 3];
}
```

**One rule carries the whole construct: the versions across a struct's members
never decrease.** That is append-only, said structurally rather than reviewed
for. Situ has no field numbers -- position carries identity (section 4) -- so
a member inserted before an existing one moves every byte after it and every
deployed peer misreads the message. `situc wire` catches that after the fact;
here it is refused, because a schema that says "this arrived in v2" is making
a compatibility claim and the claim has to be true.

Two things follow, and they are worth stating together because they sound
contradictory:

- **Every member keeps a static offset.** Append-only means nothing before a
  versioned member can move, so `flags` is at `0x03` in every message that has
  it. The map says `AbsoluteStatic(0x03)`, not `Dynamic`.
- **The struct's extent is a range.** `size=3..9`: v1 is three bytes, v3 is
  nine. What varies is not where the members are but how many of them are
  there.

**A versioned member is `stage = ParseTime`.** Whether the bytes are present
is a value in the data, so nothing can reach them before the version field has
been read -- which is the same shape as the stage gate of 14.3, one axis
weaker.

**A field that is not there is not a field that is wrong.** `validate` reads
the version first and skips a `[since]` member the message does not carry,
rather than checking it and finding zeros. That is a check about *presence*
gating a check about *value*, and it needs the struct's version member to be
nameable at runtime -- which is why the packed image carries a table of them
(26.91) and not merely each member's `since`. Because offsets are static the
rest costs nothing: nothing moves, only the count of members present varies.

**The accessor reports rather than guesses, in both directions.** There is no
value to return when the field is not there, and one that handed back whatever
follows would return another member's bytes or another message's. Writing is
worse: those bytes land past the end of the older message, in whatever the
caller's buffer holds next. One build reads a v1, v2 and v3 message and
refuses exactly the fields each does not carry.

| | reads | writes |
|---|---|---|
| C | `situ_err_t x_get(view, T *out)` | `situ_err_t x_set(view, T)` |
| C++ | `[[nodiscard]] err x(T &out)` | `[[nodiscard]] err set_x(T)` |
| Python | property raising `VersionError` | setter raising `VersionError` |
| Rust | `Result<T>` | `Result<()>` |

`VersionError` is its own class in Python rather than a `ConstraintError`,
because the remedy differs in kind: a constraint failure means the message is
malformed, and this means the message is fine and older than the field. A
caller handling the two alike would reject a peer it is meant to interoperate
with.

**A version field with nothing versioned yet is fine.** It is the ordinary
first revision of an extensible format, and refusing it would force the
attribute into the same commit as the first new member -- the commit where its
absence matters least and its presence is noisiest.

**19.3 knows about this.** Appending a member is normally reported as
*probably* safe, because an old receiver sized its buffer from the old
contract and situ cannot know whether it ignores trailing bytes or rejects the
message as overlong. Appending one behind a `[since]` is *provably* safe: the
old receiver reads the version field and knows the bytes are not its own,
which its own schema said before the edit existed. The two read differently in
the verdict, and they should -- otherwise the construct that makes extension
safe still reports as a risk, and a reviewer learns to wave the category
through.

## 20. Code generation

### 20.1 Backends

**situ targets every language its users write in.** The schema describes the
bytes; what a caller reaches them from is not the schema's business. The
backends are planned in order of how many people need them, not of how
pleasant they are to write:

1. **C (C11)** -- done. Target is embedded: no allocation, no libc dependency
   beyond `<stdint.h>` and `<string.h>`, no recursion, bounded stack, no VLAs,
   MISRA-friendly where it does not conflict with clarity.
2. **C++ (C++17)** -- done. The largest population after C, and the one that
   can express parts of the lattice C cannot: a span carries its length, an
   error is `[[nodiscard]]`, and the stage gate is a type with no public
   constructor rather than a convention.
3. **Python (3.11+)** -- done. Reaches people who would otherwise not describe
   their format at all. It enforces the least of the lattice, which is a fact
   to state rather than a reason to skip it.
4. **Rust (2021)** -- done. Expresses the capability system most naturally of
   all: view invalidation is the borrow checker, so the generation counter the
   C runtime carries is not needed; a gate cannot be constructed outside the
   open that verifies it; and `Result` is `#[must_use]`, so an ignored error
   does not compile.
5. And after those, whatever the users are writing.

**"Expect it to expose places the C backend papered over" was right, and not
only of Rust.** Every backend after the first has found something in the ones
before it, and the pattern is consistent enough to plan for (invariant 13):

- C++ found that the count expression asked for a fixed offset before asking
  what kind of field it was, so a text-encoded length was refused as
  unsupported. C had the same bug and had declared a gap that was not real.
- Python found that the readable form of a delimiter -- `` `"\r\n"` ``, which
  is how a specification writes it -- cannot be embedded in generated source
  as it stands, because it holds both backslashes and double quotes.
- Rust found that its own `validate` omitted the delimited case, which is a
  bug no amount of reading the emitted text would show, because the bug was
  the absence of text.

Which is why each backend is finished by *running* the same worked example
through it and comparing bytes, rather than by inspecting what it emitted.

**Codecs have one implementation, and it is the C one.** Accessors are native
to every target -- they are shifts and offsets, and there is no algorithm to
get wrong -- but a codec is a real algorithm, and one that is correct beats
four that are each nearly correct. Every other backend binds the C
implementation through its own FFI. A per-language plugin slot is
*specified* for a native implementation where somebody eventually wants one,
and has never been built: `impl crc32 derived for rust;` is 0017's syntax and
the parser refuses the qualifier. This document and 0017 both said it
"exists ... and is empty" until 2026-09-04, when `git log -S"derived for"`
found no commit in the history that ever carried it. See
`doc/decision/0017-codec-implementation-sourcing.md`, which also records what
this costs -- generated Rust, C++ and Python that uses a codec links the C
runtime, so "vendors trivially with no dependencies" stays true of situ's
output only for the C target.

**A backend implements the features it can and reports the rest.** No language
expresses all thirteen axes; C already enforces several by convention and a
runtime check where Rust would enforce them in the type system, and Python will
manage fewer still. That is a fact about the language, and the way situ handles
facts is to state them. A backend that cannot enforce an axis says so in the
generated code and in the capability map -- it does not quietly emit an
accessor that looks like the C one and guarantees less. This is invariant 5
applied to backends rather than to requirements: never silently downgrade.

**Two readers per field, and the difference is not stylistic.** A field whose
bytes may not decode -- a text number, a varint -- gets a *lax* reader that
cannot fail and a *strict* one that returns an error. The lax one answers zero
for bytes it cannot decode; the strict one refuses them. Which is called is
decided by what the caller is doing rather than by taste:

- **Size and offset expressions call the lax one.** A length is needed to
  place the next member whether or not the field parsed, and a chain that
  refuses mid-way places nothing after it.
- **`validate` calls the strict one.** A field that is meant to be a number
  and is not is a malformed message, and that is exactly what `validate` is
  for.

Stated here because it is easy to build only one and not notice: the walker
had only the strict reader and called messages malformed that every backend
reads to the end -- once for text numbers, once for varints, and once more for
a truncated varint's *length* (26.92, 26.94). Three instances of one omission
in two sections. A backend that emits `_get` should ask itself what its
`_value` does.

### 20.2 Generated C API shape

Per schema, emit `<name>.h` and `<name>.c`.

Naming: `<prefix>_<namespace>_<Type>_<field>_get`, `_set`, `_view`. The prefix
defaults to `situ` and stacks *outside* any namespace the schema declares, so
`--prefix` prefixes rather than replaces. Generated types carry `_t`, which
keeps them clear of the accessor namespace.

Two constructs whose paths flatten to one C identifier are an error naming
both, because the flattening is not injective -- `A.b_c` and `A_b.c` reach the
same name -- and the alternative is a redefinition error in generated code with
no schema location in it (`doc/decision/0013-identifier-conventions.md`).

Principles:

- **Views are values, not pointers.** A view is a small struct
  `{ uint8_t *base; uint32_t limit; uint32_t generation; }` passed by value.
  No allocation, no lifetime management.
- **Field access within a view compiles to `base + K`.** Verify this in tests
  by inspecting generated code for constant offsets.
- **Errors are return codes**, never `errno`, never longjmp. A single
  `situ_err_t` enum with distinct codes per failure class: bounds, constraint,
  version, tag, stage, stale, truncated, checksum, depth.
  `test_the_failure_classes_match_the_runtimes`
  holds this list to `runtime/c/situ.h` and holds the other three runtimes to
  it as well -- a class C can report and Rust cannot is a condition a Rust
  consumer has no way to express, and `truncated` had to be added to four
  runtimes by hand to find that out.
- **`checksum` is not `tag`.** A tag that fails to verify is a hostile or
  corrupt message and a caller should treat it as an attack; a checksum
  mismatch is corruption or truncation, and a receiver that logs the two the
  same way reports a disk error as an attack (0053). The same distinction
  `stage` already draws against `tag`, one step further out.
- **`truncated` is not `bounds`.** The bytes so far are a valid prefix and more
  are needed, which a stream reader sees on every partial read; bounds means a
  read went outside the buffer, which is a bug or an attack. A receiver that
  cannot tell them apart treats normal progress as hostile.
- **An accessor whose extent comes from the data bounds itself.** The
  amortised frame check (above) covers offsets the frame is known to contain,
  and a length read from a field is not one: the message chooses it. Such an
  accessor returns what is *there*, and `validate` reports that the message
  claimed more. Invariants 41 and 42, and section 26.27 for how it was found.
- **Getters for `MemoryIdentical` fields may return by value or by pointer;
  getters for `ValueConverted` fields return by value only.** A pointer into a
  byte-swapped field is a bug waiting to happen; do not offer one.
- **Setters exist only where `mutate` permits.** Where it does not, the header
  carries a comment naming the reason and the requirement that would have to
  change. A user grepping for a missing setter must find the explanation in the
  generated header itself, not only in the compiler output.
- **Emit size constants**: `SITU_<STRUCT>_SIZE_MIN`, `_SIZE_MAX`,
  `_SIZE_FIXED` (when applicable) as `#define`s so callers can size static
  buffers without running the compiler.
- **`SITU_CHECKED`** compile-time flag enables bounds checks, generation
  checks, and constraint validation; release builds compile them out. The
  checked and unchecked builds must be ABI-compatible.
- **Build with LTO if speed matters, and with
  `-ffunction-sections -fdata-sections -Wl,--gc-sections` if size does.** The
  runtime's `situ_view_sub` lives in `situ.c`, so every element of a walk is a
  call across a translation unit without it. Measured on a forty-one element
  run, walked two thousand times: C 9.8ms to 7.2ms and C++ 8.5ms to 5.2ms,
  which is where C++ meets Rust -- a single crate has no such boundary.

  For size, the same flags in the other direction: nearly everything situ
  emits is `static inline` in the header and costs nothing when unused -- 31 to 40 functions per schema
  across the worked examples. What is left is `validate`, which is out-of-line
  because it is large, and those *do* survive into a binary that never calls
  them: measured at four symbols and 648 bytes on `example/message`. situ has
  no way to fix this from the generator side and had never said so.

### 20.3 Additional generated artifacts

These are cheap once the layout solver exists and they carry outsized practical
value:

| Artifact | Command | Why |
|---|---|---|
| golden-vector tests | `situc gen-tests` | schema + hex vectors -> cmocka test cases; the only reliable way to know a layout change broke the wire format |
| capability conformance | `situc gen-checks` | schema alone -> cmocka test cases holding the accessors to what the map claims, and every layer rung the schema reaches to what that rung promises; a map the generated code contradicts is worse than no map |
| fuzz harness | `situc gen-fuzz` | libFuzzer/AFL entry point per parseable struct; parse safety is the top risk in a protocol parser |
| byte-layout diagram | `situc doc --format=ascii` | RFC-style packet diagrams straight from the schema; what protocol documentation always needs |
| Wireshark dissector | `situc gen-dissector` | debugging an encrypted protocol without one is painful; large practical payoff. Lua rather than a C plugin, for the ABI reason in `doc/decision/0021-dissector-language.md` |
| wire signature | `situc wire` | Section 19.3 |
| capability map | `situc map` | Section 18.1 |

**Framing.** Every parseable struct also gets `<name>_required(data, have,
need)`: given a prefix, is a whole message here, and if not how many bytes are
needed? `SITU_OK` with the total, or `SITU_ERR_TRUNCATED` with a lower bound.

It is the extent computed defensively -- the extent function assumes its bytes
are present because a walk has already bounds-checked the frame, and this one
is called on a prefix. The case it exists for is the one every hand-written
framing loop gets wrong: a `u16` length read from one byte that has arrived and
one that has not is a guess, and the guess sizes the next read.

`SITU_ERR_TRUNCATED` is its own code rather than `SITU_ERR_BOUNDS`. A stream
reader gets it on every partial read; bounds means a read went outside the
buffer, which is a bug or an attack, and conflating them makes a receiver treat
normal progress as hostile.

All four backends emit it, and the shape of the answer is each language's:
`situ_err_t` with an out-parameter in C and C++, a `TruncatedError` carrying
`needed` in Python, and a `Framing { Complete(n), Need(n) }` enum in Rust --
`Result<usize>` would have to drop one of the two numbers or smuggle it
through the error, and a caller needs both: one to consume, one to size the
next read.

Two shapes are declined, each saying so where the function would have been: a
`[remaining]` tail, because it ends where the view ends and how long one is is
the transport's answer rather than the message's; and a struct whose complete
form can be zero bytes, because then every buffer already holds one.

A run used to be a third, and the reason was sound: the walk that finds a
run's terminator stops just as readily at the end of what has arrived, and
nothing it emits distinguishes the two. What it missed is that the *element*
does. A run is framed one element at a time, through the element's own
`required` -- the same question one level down -- so a run is frameable exactly
when its element is, which is what `traverse.frameable` answers. It was worth
chasing: the refusal was honest, and next to it a `while` run was reaching the
ordinary path and reporting complete messages from bytes that were nothing of
the kind (26.31).

It also inherits whatever a backend cannot measure, and declining is the right
answer rather than a second limitation: a framing function that skipped the
member it could not measure would report a total short by exactly that member.

Writing that down is what got the two remaining gaps closed. C++, Python and
Rust all declined a length field sitting behind a variable-length member --
there is no constant to read it at, and its own accessor knows where it is, so
the fix was to call it. And all three treated a *constant*-sized array as
data-sized, because `u8 id[DEVICE_ID_BYTES]` sets `sized_by` as well; they
looked for a field of that name and dropped the member. The honest question is
whether the *data* decides, which `array_count` answers, and it lives in
`traverse.classify` where all three ask it (invariant 43).

**Every one of these declines rather than guesses**, and says so in the file it
writes. A generated artifact that quietly omits a member is indistinguishable
from one that never had it, so each refusal names the member and the reason:

- `gen-checks` stops asserting offsets at a member whose reach the data
  decides, and the generated comment says which member and why. It used to
  place the members after a `while` run as though the run held one element,
  which is a confident assertion about an offset the accessor walks straight
  past.
- `gen-dissector` emits a Lua comment where it cannot compute an extent, in
  the position the member would have occupied.
- `doc` reports *how* a variable member's length is decided rather than a
  number: `to ":"`, `[len]`, `by form`, `while form == 0`. It used to print a
  variant's `size_bits` -- the smallest arm, so "0 bytes" -- in a column where
  everything else says where the member ends.

That last one is the general shape of this class of bug, and it has now
appeared four times: a true number, answering a question nobody asked, in a
place where a reader will take it for the answer to the one they did.

---

## 21. CLI surface

```
situc build   <schema>            generate code [--materialize] (26.30)
                                  [--owned] fixed-size C structs and a
                                  decode that copies into them (26.69)
                                  [--single-file] python with the runtime
                                  inlined, stdlib imports only (26.70)
situc map     <schema>            emit capability map
situc map --check <schema>        compare against committed map, fail on diff
situc gen-checks <schema>         tests holding the accessors to the map
situc gen-derived <schema>        implementations from kernel descriptions
situc gen-tamper <schema>         the harness that watches a tag's gate refuse
situc advise  <schema>            ranked design suggestions with costs
situc explain <schema> <path>     one struct or field's capability vector
                                  and its blame chains
situc diff    <old> <new>         capability changes between revisions
situc wire    <schema>            the byte-level contract [--check] (19.3)
situc verify  <schema> <vectors>  do real bytes conform? generates nothing
situc pack    <schema>            the packed layout image a walker reads
                                  [-o FILE] [--metadata] [--coverage] (26.33)
situc doc     <schema>            byte-layout diagrams and a field reference
                                  [--format=ascii|markdown] [--out DIR]
situc gen-tests   <schema> <vectors>
situc gen-fuzz    <schema>
situc gen-dissector <schema>      Wireshark dissector in Lua [--out DIR]
situc lsp                         language server over stdio (26.19)
situc dump-ast <schema>           debugging aid, phase 1 deliverable
situc gen-codec-tests <schema>    property tests from codec signatures
situc import-proto <proto> -o <schema>   [--accept-lossy]
```

Three global flags: `--diagnostics=text|json`; `--version`, which prints the
string in `situc/__init__.py` -- the same one the Debian packaging reads, so a
package and the binary inside it cannot claim different versions; and
`--print-std-dir`, which prints the directory `import std "..."` reads from.
That last one is the answer outside a schema to the question `import std`
answers inside one, for a build system that wants to depend on a shipped
schema or hand it to a tool that is not situc. It exits non-zero with a
diagnostic where the directory cannot be found, rather than printing nothing
and exiting 0 -- which a shell would read as an empty path and use. `--out`,
`--target` and `--prefix=NAME` belong to the subcommands that take them,
which is not the same thing and was written here as though it were. The three
that take `--target` do not accept the same set: `build` takes
`c|cpp|python|rust`, `gen-derived` takes `c|rust|python`, and `gen-dissector`
takes none, emitting Lua.
There is no `--strict`; it was listed for a while and never existed.

`--out` names a **directory** in nine subcommands and a **file** in two, and
this line said `--out=DIR` flatly until somebody passed a directory to `pack`
and got `IsADirectoryError`. The two that want a file are `pack` and
`import-proto`, each producing a single artifact rather than a set, and the
parser already marks them: they are exactly the two that also accept `-o`.
That correlation was there before it was written down, and
`test_a_short_out_flag_marks_a_file` now holds it, so a subcommand gaining
one meaning without the other fails rather than surprising a reader.

`test_the_cli_section_lists_every_command` holds this block to the parser, for
the reason section 0 gives: this list had drifted, and so had 11.3, and a
surface a reader is expected to type from is worth a check rather than a
promise.

---

### 21.1 The generator version is a contract, stated where it binds

An adopter commits the generated sources and treats situc as a build-time
tool, which raises the question every serious one reaches
(`suggestion/netcfgd.md`): when the sources are regenerated, what promises
that the same schema means the same bytes?

Three parts, the first now mechanical:

- **Every generated file names its generator.** The banner reads
  `Generated by situc 1.0 from x.situ`, with the version from the same
  `VERSION` file the package and `situc --version` read. A regeneration
  under a different situc is therefore a visible diff line, not a silent
  substitution -- the reviewer sees the generator moved even when nothing
  else did.
- **The wire signature is the compatibility oracle, not the generator
  version.** Two situc versions may emit different code for one schema;
  what must not change unnoticed is what the bytes mean, and that is what
  the committed `.situ.wire` diff answers. `situc diff` compares
  capability; the wire file compares contract. A generator upgrade that
  moves neither is a refactor of the emitted text.
- **Output stability is promised per wire signature, not per byte of
  code.** The emitted text may improve between versions; the committed
  copy pins what a given tree builds against, and regenerating is a
  reviewed change like any other. A guarantee of byte-stable output across
  versions would freeze every emitter bug, and nobody is served by that.

## 22. Testing strategy

| Layer | Tool | What |
|---|---|---|
| compiler unit | pytest | lexer, parser, expression intervals, lattice meet, propagation table |
| propagation table | pytest, table-driven | one test per row of the 11.3 table; this is the highest-value test suite in the project |
| requirement discharge | pytest | every predicate in Section 16, passing and failing, with expected blame chain |
| canonicity | pytest | one test per source of non-canonicity in 14.4 |
| codegen golden | pytest + file compare | generated C compared against committed expected output |
| capability conformance | generated cmocka | the accessors are held to the map they were generated beside: a field must occupy exactly the bytes claimed, a write must move nothing else, a constraint must refuse what it forbids. Generated from the schema alone, so it costs nothing to run over every example and a new construct is covered the day it lands |
| the layer ladder, rungs 3 to 5 | generated cmocka | the same suite, extended past the accessors: a relation's predicate accepts a matching pair and refuses a mismatched one, a framing reader fed a byte at a time yields exactly the messages that were in the stream and nothing from a partial one, and a conversation table matches a reply to its request, forgets it, and refuses one nobody asked for. Each rung emits both cases or neither, because a predicate that accepts everything passes the positive alone. Rung 6 is not here: `drive` owns I/O, and a suite built out of buffers cannot execute it |
| the layer ladder, in four backends | pytest + each toolchain | every rung's output *run* rather than compiled. This was `-fsyntax-only` for C++ and `--crate-type lib` for Rust, neither of which links or executes anything, so rungs 3 to 6 were checked in four backends and executed in one. A driver is given its clock as a parameter, so a retransmission follows from the number passed to `step` rather than from how long the test took |
| bounds that name a sibling | pytest + cc | `[max = chunks - 1]` is checked against a message in hand, so the bound is read rather than folded, and the test moves the field it names to prove it: a folded bound passes "3 of 4 accepted" and "4 of 4 refused" and fails only when `chunks` becomes 8. The widening refusal is checked in both directions, because a guard that refused every sibling bound would satisfy the first half and take the feature with it |
| generated code behavior | cmocka | compile generated C and exercise it; use `--wrap` for syscall-level mocking where needed |
| offset constancy | cmocka + disassembly check | verify view field access compiles to constant offsets |
| round-trip | pytest + hex vectors | parse then re-emit must be byte-identical for canonical schemas |
| fuzz | libFuzzer + ASan | `make fuzz` builds every generated harness with `-fsanitize=fuzzer,address` and runs each for `FUZZ_SECONDS`. Not part of `make test`: minutes rather than seconds, and a compiler `test` does not need. The suite still compiles every harness and smoke-runs it on eight inputs from `/dev/urandom`, which catches a harness that stopped running and reaches nothing else -- a length field is a 1-in-2^16 guess |
| diagnostics | pytest | snapshot-test the exact diagnostic text; regressions in message quality are real regressions |
| backend agreement | pytest + each toolchain | every backend's output compiled and compared against the C on the same buffer, field by field. Four backends that disagreed would mean a schema means four things, and this is the only test that would notice |
| backend agreement, hostile input | pytest + all four toolchains | the same question about bytes nobody meant to send, over every schema: the drivers are generated from the layout (`situc/codegen/differ.py`), so what is asked of one backend is asked of all four, and the answers are diffed. The well-formed comparison above cannot see a backend that differs only on a malformed message, which is where all four differed (26.27) |
| every example, in every backend | pytest + each toolchain | each worked example generated *and compiled* -- C since phase 4, the other three only recently, which is how three C++ examples and two Rust ones came to be broken with the suite green. Generating is not compiling, and the examples are the schemas anyone reads |
| compiler mutation | by hand, recorded in 26.13a | deliberate bugs in the generator, judged by what a *user's* suite catches rather than situ's own. Not automated: choosing the mutation is the work, and a mutation nobody thought of is the gap that survives |
| test mutation | by hand, at the point of writing | a probe that walks generated code is run once against a deliberately wrong expectation, to find out whether it is a test or a compile check. Three of mine were the latter -- `-fsyntax-only`, a `main` nobody executed, an `assert!` in an unrun binary -- and each passed identically before and after the fix it was written for (invariant 35) |
| the emitted Lua | lua5.4 | parsed for every schema, and four dissected over chosen packets: a UDP header, an IPv4 header with its bit-packed first row, a DNS name walked label by label, and an HTTP request line found by scanning, with offsets and values compared against the layout the accessors come from. Beyond those four, every dissector in the tree is run over random bytes and what it showed is diffed against the walker's own `listing` -- a third kind of check, and the reason this row understated itself for as long as it did |
| one list of schemas | pytest | five places answered "which schemas are there" and each had its own glob. They read one list now, except where a narrower one is deliberate and says so |
| every schema is built | pytest | `test/generated/Makefile` names the schemas it compiles, checks and fuzzes, by hand, and it is the third answer in this repository to "which schemas are there". A schema left out of it is one whose generated C nothing in the build ever runs -- the shape that let `edges.situ` go unbuilt in C++ for weeks |
| the emitted C and C++ comments | doxygen | run over every schema's headers in both languages, and its XML read back: the capability vector must arrive against the accessor it describes, and a reason for an accessor that does *not* exist must not be attached to whatever declaration follows it |

**Every artifact is now run the way its consumer runs it.** The language server
was the last one that was not: its tests drove `Server` in process over
`BytesIO`, which exercises the framing and none of what a subprocess adds --
the subcommand, binary-mode streams, and whether a reply is flushed before the
server blocks on the next request. An editor that gets no diagnostics because
they are sitting in a buffer sees a server that does not work. `situc lsp` is
launched as a process now, talked to over real pipes, and its diagnostics read
back.

**What the suite does not do**, stated because a reader would otherwise assume
it: no Wireshark runs here, so nothing proves Wireshark *accepts* a generated
plugin. Everything short of that now happens -- `luac -p` parses every
dissector this repository can generate, and four of them are executed over real
packet bytes through the Wireshark stub in `test/lua/dissect.lua`, which
reports what each dissector showed and where. And the aarch64 big-endian target
is compile-only; only the little-endian one runs under emulation.

**A green run reports what was installed as much as what was checked.** The
gates shrink quietly as tooling goes missing, and every intermediate state
prints the same `rc=0` as the full one. Measured on one machine as packages
arrived, with no line of the tree changing between the first figure and the
last: the suite went from 3354 passed and 130 skipped to 3449 and 36,
`cross-test` went from printing `skipped` for both aarch64 legs to running
them, and `make typecheck` went from ten errors to clean. So the number worth
reading out of a run is the skip count, not the exit status.

**The gates disagree about what an absent tool means, and the disagreement is
right.** `make test` skips and says which tool was missing, which is 26.61's
rule working; `cross-test` prints `skipped (...)` and exits zero, the same
choice. `make typecheck` refuses to run at all. That is not the inconsistency
it looks like. A skipped test still reports honestly on what it did not do,
whereas an unresolvable import makes the module `Any` -- so mypy would check
`test/unit/oracles.py` against nothing and call it clean, which is a vacuous
pass rather than a declined one, and the one failure mode this project treats
as worse than a red result.

**Configuring around it was measured rather than argued.**
`ignore_missing_imports` for the two modules silences the import errors and
then reports the `type: ignore` comments they needed as unused, an `Any` module
raising no `attr-defined` for them to suppress: a machine without the libraries
still fails, with two errors instead of eight. Silencing those as well needs
`warn_unused_ignores` off for the file, which withdraws a real check on every
machine, including the ones that have the libraries, to accommodate the ones
that do not. Excluding the file was rejected for the same reason as the `Any`
-- it reports success over something nobody checked. So the requirement stays
and only the diagnosis changed: `typecheck` now names the module it needs and
points at the different answer `make test` gives, instead of emitting ten mypy
errors that read as defects in the code.

**What a fully non-vacuous run needs**, beyond a C compiler and Python: lua5.4
for the dissectors, pymodbus and paho-mqtt as importable libraries, tshark,
protoc and sqlite3 as commands, aarch64-linux-gnu-gcc and qemu-aarch64 for the
two cross legs, and clang with libFuzzer for `make fuzz`. A run without them is
still worth having. It is just smaller than its exit status suggests, and
nothing in it says so except the skips.

**The failure that produced this note skipped nowhere it should have.**
`test_a_dns_name_is_walked_label_by_label` reached `dissect` with no `skipif`
where its eight siblings carried one, so a machine without Lua got a red result
about the machine wearing the costume of a defect in the dissector. The guard
and the thing it guards are maintained by hand and nothing checks that they
agree -- which is the same shape as a schema left out of the generated
Makefile, one row above.

**Performance is measured and never asserted.** `tool/bench.py` builds a
driver in each backend and reports what the offset cache of 26.30 costs and
saves; nothing in the suite holds a wall-clock number, because a threshold
loose enough to pass on every machine holds nothing and a tight one fails on
somebody else's laptop. What the suite does hold is that the two accessor
families agree about where a member starts, which is the property a wrong
index would break.

Implementation language for `situc`: **Python 3.11+, standard library only**,
with full type annotations checked by mypy in strict mode. The floor is stated
twice -- here and as mypy's `python_version` -- and was false for six phases:
one f-string in the C++ backend split an expression across lines, which is PEP
701 and therefore 3.12, and the machine this was written on runs 3.13.
`ast.parse(feature_version=...)` does not catch that, PEP 701 being a tokenizer
change, so `test_every_module_parses_at_the_declared_floor` runs a real
interpreter of the declared version over every module and skips where that
version is not installed. It found the f-string the day it was written, in
generated-code terms: a vendored toolchain on the version this promises would
have failed to import. Rationale: the
compiler is a symbolic solver and a text generator, both of which Python does
well; no dependencies means the toolchain vendors trivially into an embedded
build environment; and rewriting in Rust later is a known, bounded option once
the semantics are settled. Do not add a dependency without recording a decision.

---

## 23. Repository layout

```
situ/
  README.md                   what situ is, in a page: the pitch, a worked
                              schema, the commands, and what the suite needs
  CMakeLists.txt              the CMake entry point, beside the Makefile, and
                              `situ_generate()` for a consuming build
  project.md                  this document
  bin/
    situc                     the command: a launcher that finds its own
                              package, in the tree or under an install prefix
  doc/
    decision/                append-only numbered decision records
    grammar.ebnf              extracted, kept in sync with Section 7
    capability-axes.md        normative axis definitions
  situc/                      the compiler; Python 3.11+, stdlib only, mypy strict
    __init__.py
    __main__.py               so `python3 -m situc` works
    lexer.py
    parser.py
    imports.py                `import` resolution: relative to the importing
                              file, flat namespace, transitive, and once per
                              compilation whatever the path taken to it
    ast.py
    unparse.py                an expression back to schema source, for the
                              places a fact has to survive as text: a `while`
                              condition a backend rewrites over its own
                              getters, and a `case` value in a comment
    wellformed.py             whole-schema checks: duplicate names, unresolvable
                              types, recursion, constant/attribute collision
    types.py                  scalar type table: integers, floats, fixed point, BCD
    expr.py                   expression evaluation and interval arithmetic
    layout.py                 the layout solver
    capability.py             axes, lattice, meet
    propagate.py              the 11.3 table, data-driven
    resolve.py                the seam joining layout to the table
    layers.py                 the layer ladder of decision 0032: which rung a
                              schema needs, which members need storage rung 1
                              cannot give, and the two scalars the capability
                              map carries. Shared because three callers wanted
                              one answer (26.99)
    relation.py               what a cross-message relation means, once: the
                              walk from a path to the getter that reads it,
                              and which comparisons are expressible at all.
                              Shared so the four backends cannot disagree
                              about whether a schema compiles (26.95)
    traverse.py               the struct walk every backend and artifact shares:
                              which entries are a struct's own members, which
                              bytes a placement occupies, and -- the part that
                              had to be learned three times -- the order to ask
                              what kind of member it is. Also whether a struct
                              can be measured from its own bytes at all, and
                              what it measures: four backends worked that out
                              separately and none of them agreed (invariant 34)
    invariant.py              which right-hand sides an `invariant` may have
                              (16.1), and the walk over one -- the leaves are
                              each backend's and the rest is the language's
    names.py                  the shape of a schema fact, with the spelling
                              left to the caller: how a delimiter reads, which
                              field names an expression rewrites, and which of
                              C's operators a target spells differently. The
                              last of those is here because the difficulty is
                              an ordering -- `!=` before `!` -- and Python and
                              Lua would otherwise get it right separately
    wire.py                   the byte-level contract and its comparison; 19.3
    verify.py                 do these bytes conform? the schema used as a
                              specification, generating nothing; 26.67
    pack.py                   the packed layout image and its expression
                              bytecode, for a walker to read; emits only,
                              never walks; 26.33
    requirements.py           predicate evaluation and discharge
    namespaces.py             `::` qualification and `--prefix`; decision 0012
    capmap.py                 capability map construction
    diagnostics.py            diagnostic construction, blame chains, rendering
    dump.py, unparse.py       `dump-ast` and round-tripping
    advise.py                 suggestion catalog and cost model; phase 9
    revision.py               `situc diff`: capability *cost* between two
                              revisions. Not compatibility: see 18.3 and 19.3
                              for why those are different questions
    kernels.py                property signatures derived from kernel
                              descriptions; phase 12
    proto.py                  the `.proto` importer; phase 13
    doc.py                    `situc doc`: RFC-style diagrams and a field table
    dissector.py              `situc gen-dissector`: a Wireshark dissector in
                              Lua (decision 0021), over the same traversal the
                              code backends use -- only the reads are Lua's
    lsp.py                    `situc lsp`: diagnostics, hover, symbols, code
                              actions and definitions, over JSON-RPC on stdio
    codegen/
      differ.py               drivers that print what a schema says about a
                              buffer, in all four languages, so the answers can
                              be diffed. Four emitters in one file because what
                              has to agree is their output
      c/                      emit, checks, vectors, fuzz harnesses, codec
                              tests, derived codec implementations, MMIO
      cpp/                    the second backend: emit, and the naming rule C++
                              adds to decision 0013 -- a member may not be
                              called what its class is called (decision 0025)
      python/                 the third
      rust/                   the fourth
    cli.py
  runtime/                    one per backend, and each is thin: the arithmetic
                              lives once, in C
    c/
      situ.h                  views, bounds, generation, bit access, varints,
                              BCD, checksums, text validation, sign extension
      situ.c
      Makefile                self-contained; `cd runtime/c && make` works
    cpp/
      situ.hpp                a header over situ.h, whose functions are already
                              `extern "C"`. Adds a span, scoped errors and the
                              `situ::rt` namespace so generated code may use
                              `situ` without colliding
    python/
      situ_runtime.py         views over `memoryview`, and the generation check
                              of 12.3 -- the one place Python is stronger than
                              release-build C
    rust/
      situ_rt.rs              `no_std`, allocation-free. Invalidation is the
                              borrow checker, so no generation is carried
  std/
    codecs.situ               signatures for the standard codecs, hand written
    kernels.situ              the same codes as kernel descriptions, so the
                              derivation can be checked against the declaration
  .github/
    workflows/
      check.yml               `make check` on a machine nobody configured.
                              Installs every toolchain the suite would
                              otherwise skip, and asserts the skip count --
                              a runner with none of them reports green over
                              a fraction of the tests (26.83)
  editor/
    document.py               one message, placed and read: the core of
                              decision 0034, which the CLI, the TUI and the
                              GUI all drive and none of them duplicates
    text.py                   opening a document and rendering one as lines,
                              shared by the two frontends that want text
  walker/                     the interpreter: a packed image, walked over
                              bytes. A second program, never imported by
                              `situc` -- decision 0026, amended (26.81)
    image.py                  the sections, read back
    vm.py                     the section 10 bytecode, evaluated
    walk.py                   offsets, sizes and reads over a buffer
    report.py                 the differ's listing, for the fifth column
  tool/
    style_gate.py             the formatting enforcer; `make style`, and
                              `make lint` as an alias. Copied verbatim from
                              the shared source, so a fix belongs there
                              rather than here. Superseded
                              `lint_conventions.py` (26.68)
    hooks/
      commit-msg              the message check, installed by `make hooks`.
                              In the tree so it is reviewable and survives a
                              clone; the copy that runs is untracked
    bench.py                  what the offset cache costs and saves, in all
                              four backends. Not a test and not in the suite:
                              a wall-clock number is the machine's, so 26.30
                              records the machine beside it
    sweep.py                  every composition of constructs the language
                              admits, through four backends. Not in the suite
                              either -- the whole space is minutes -- and
                              `test_composed_schemas` runs a fixed sample of
                              it on every commit (26.50)
  test/
    unit/                     compiler tests (pytest), one file per module
    generated/                cmocka tests over generated C
    cross/                    behavioural checks on aarch64 under emulation
    golden/                   pinned diagnostic text; message quality is the
                              product, so a regression in it is a regression
    propagation/              the 11.3 table's own fixtures
    lua/                      a stub of the Wireshark API, big enough to run a
                              generated dissector over real bytes and report
                              what it showed (26.14)
    schema/                  five schemas. header.situ is example 5.1;
                              lenient.situ, native.situ and padded.situ each
                              isolate a construct nothing else in the tree
                              sets -- `strictness = lenient`, `endian native`
                              at the widths nothing else reaches, `pad_to(n)`;
                              and edges.situ holds constructs no protocol here
                              uses, which exists so their generated code runs
                              at all
  example/                   one directory per protocol, name matching its
                              `.situ` file, each with at least one `require`.
                              A `<name>.vectors` beside a schema is bytes
                              some other implementation wrote, and `gen-tests`
                              turns it into a cmocka suite (26.35)
```

Every example is parsed by the test suite, and every buildable one has a
committed `.situ.map` that the suite regenerates and compares. An example
marked `// STATUS: needs phase N.` is asserted to be *rejected*, naming a phase
no later than N; those pin the phase-gating behaviour and go live as phases
land.

### 23.1 Decision records

Append-only, in `doc/decision/`. A decision goes here when the reasoning
would otherwise be lost -- when the obvious reading of this document is not
what the code does, or when an alternative was rejected for a reason worth
keeping. They are referenced from the sections they bear on. There are 58, 0001
through 0058 with no gaps; the table below lists 0001 to 0022, and the
rest are cited by number from the sections they belong to rather than
from here.

| # | Decision |
|---|---|
| 0001 | `situc` is written in Python 3.11+, standard library only |
| 0002 | GNU Make only; CMake deferred |
| 0003 | Tabs for indent in every language, including Python; no autoformatter |
| 0004 | aarch64 is a compile-only target |
| 0005 | Widths that are a whole number of bytes are byte-aligned scalars |
| 0006 | `[` is disambiguated by the closed attribute vocabulary |
| 0007 | aarch64 is behaviourally tested under emulation |
| 0008 | Slack needs no field in the view; the limit already carries it |
| 0009 | `coded(C) { ... }` is the general transform region |
| 0010 | Regions and tags may be named, and default to their keyword |
| 0011 | Nested tag coverage recomputes innermost first |
| 0012 | Namespaces are blocks, one level deep, qualified with `::` |
| 0013 | Identifier casing is the author's, and collisions are the compiler's |
| 0014 | `endian` and `bit_order` are positional, and `native` is the C compiler's |
| 0015 | What a register adds to a struct, and what it does not |
| 0016 | A composed expansion may carry both a ratio and an addend |
| 0017 | One codec implementation, in C, with a per-language plugin slot |
| 0018 | `ratio_padded(a, b)`, for codes that emit whole groups |
| 0019 | What a codec must be before it may seal |
| 0020 | The lattice models delimiter-framed data |
| 0021 | The generated dissector is Lua, not a C plugin |
| 0022 | Accessor families, and what actually purges the unused ones |

0004 and 0007 look contradictory and are not. 0004 made aarch64 compile-only
and deferred a revisit; 0007 closes that revisit once user-mode emulation was
available. Both stay, because the record of why the weaker position was taken
first is part of what a decision log is for.

## 24. Build system

Both CMake and GNU Make, as separate and independently usable entry points --
`make` and `cmake -B build` each build the runtime and install the same five
things under a prefix.

That sentence was here for a long time before either half of it was true of
CMake: `git log --all` had never seen a `CMakeLists.txt`, and nothing checked.
`test/unit/test_cmake.py` is the check now, and it holds the three claims
worth holding -- the top-level build installs what `make install` installs, the
runtime builds standalone the way every sub-project here must, and
`situ_generate()` turns a schema into a library target in a consuming project,
with the schema a dependency of what reads it. Writing it found something else
again, which is the entry below.

- Sub-projects (compiler, runtime, generated-code tests) must be fully
  self-contained. The parent injects values via environment export for Make and
  via cache variables for CMake. **No shared include files between
  sub-projects.**
- Beware the GNU Make built-in defaults. `CC=cc`, `AR=ar` and `LD=ld` are all
  defined by Make itself, so `?=` never fires for any of them and a cross build
  silently uses the host tools while reporting success. Use `$(origin ...)` on
  each to distinguish a built-in default from an explicit user choice:

  ```make
  ifeq ($(origin CC),default)
  CC := $(CROSS_COMPILE)gcc
  endif
  ```

  A parent that compiles nothing should export a toolchain variable only when
  `origin` shows the user actually chose it; exporting unconditionally pushes
  the host tools into every sub-project's environment and defeats their own
  `CROSS_COMPILE` handling.
- Cross-compilation must work out of the box for aarch64; the generated code
  and runtime must build clean for a Cortex-A55 target with
  `-Wall -Wextra -Werror -Wconversion -Wsign-conversion`.

The top-level entry points:

```
make            build the C runtime
make test       pytest and the generated C, which is the fast one to run
                while working
make check      style, typecheck, lint, test, cross-test: what has to pass
                before a commit
make bench      what the offset cache costs, in all four backends (26.30)
make cross      aarch64 build of the runtime
make cross-test generated accessors run on aarch64 under emulation
make install    situc, the runtime and its header under PREFIX
make deb        situc and libsitu-dev .deb packages, into build/deb
make deb-check  build them, unpack into a scratch root, and compile a schema
                through the installed compiler against the installed runtime
make help       everything else

cmake -B build && cmake --build build && cmake --install build
```

Cross-compiling under CMake goes through `-DCMAKE_TOOLCHAIN_FILE` rather than
`CROSS_COMPILE`: a build system's users know its own convention, and a second
one invented here would be a third thing to keep in sync.

What a consuming project wants is not to build situ but to *run* it, so the
CMake side carries `situ_generate(name SCHEMA path TARGET c)`. A schema becomes
a static library target with the generated directory on its include path, and
the schema is a dependency of it -- editing the schema rebuilds what reads it,
which is the one thing checking generated code into a repository gets wrong.

`situc` itself is `bin/situc`: a launcher that finds its own package, works in
place or symlinked into a PATH directory, and works again from
`<prefix>/lib/situc` once installed. It is not a console entry point, because
generating one needs an installer and the requirement below rules that out.
`python3 -m situc` and `python3 -m situc.cli` both still work.

`pytest` and `mypy` come from the system (`python3-pytest`, `python3-mypy`) and
are used only to check the compiler. `situc` itself must run from a bare
interpreter with nothing installed, which is what makes the toolchain vendor
into an embedded build environment (Section 22).

### 24.1 Packaging

`make deb` produces two packages, and they are two because they do not have the
same architecture. `situc` is Python and is `Architecture: all`; `libsitu.a` is
compiled objects and is not. One package would have to claim the narrower of
the two, which would make the schema compiler uninstallable on every machine it
was not built on -- for a tool whose whole purpose is generating code for
targets other than the build host, exactly the wrong way round. They are
independent as well: a build machine wants the compiler and no runtime, and a
target wants the header and no compiler, so neither depends on the other and
`situc` depends on nothing but `python3`.

Built by `dpkg-deb --build` over a tree that `make install` staged, rather than
by `debian/` and `dpkg-buildpackage`. The packages then contain exactly what
`make install` produces, so a packaging bug and an install bug cannot be
different bugs -- and the step needs `dpkg-deb` and nothing else, which is the
same argument the paragraph above makes about `pip`. Anything `install` writes
that neither package claims stops the build rather than being quietly left out.

**`make deb-check` is the part that matters.** Packages that build are not
packages that work: it unpacks both into a scratch root and compiles a schema
through the installed `situc` against the installed `situ.h`, which is the
whole claim either package makes. No root, and nothing touching the machine's
own `/usr`.

The version is read from `situc/__init__.py`, which is also what `situc
--version` prints, so a package and the binary inside it cannot disagree.
`test/unit/test_packaging.py` holds the control files to the tree without
building anything; `lintian` is clean on both packages.

**`DEB_MAINTAINER` defaults to a placeholder, and a real build must pass
one.** The default is `situ maintainers <noreply@example.invalid>`, which
keeps the repository from carrying a maintainer identity nobody agreed to:

```sh
make deb DEB_MAINTAINER='Nabeel Sowan <nabeel@vibes.se>'
```

A package built for anyone else to install and left with the placeholder is
embarrassing rather than merely wrong, so set it whenever the output leaves
this machine -- and read the licence paragraph below first, because until
that is settled the packages are not distributable whatever the control
file says.

**There is no licence in this tree**, and `packaging/copyright` says so rather
than choosing one: no `LICENSE` file, no licence field in `pyproject.toml`, and
the default in that situation is that no rights are granted. The packages are
therefore built for local evaluation and are not distributable as they stand.
That is a decision for whoever owns the project, and the packaging records the
gap instead of papering over it.

## 25. Conventions

**The long form is `code-style.md` at the repo root**, which carries these
rules in detail along with the schema identifier rules and the decision
references. This section is the brief.

### 25.-1 A keyword that lands, lands in the README too

**Set by the copyright holder 2026-09-17.** The README is where the
language is described to somebody who has not read this file, so a
construct that exists and is not in it is a construct nobody outside the
tree can find.

So: **adding, renaming or removing a keyword or an attribute is not done
until the README says so.** Concretely, in the same commit as the change --

- the construct or declaration tables in *The language* gain, lose or
  rename their row;
- an attribute gains a row in *Attributes, requirements and invariants*
  where its placement rule is the interesting part;
- a construct with a shape worth seeing gets the few lines of schema that
  show it, beside the ones already there;
- *Recent additions* moves the feature between **Built**, **half built**
  and **not yet built**, and says which half is which.

**The last is the one that rots**, because it is the only part that
describes a STATE rather than a rule -- 0051 sat under "not yet built"
while all six descriptions carried it, and 0050's entry said `parameter`
was not built on the day it was. A reader trusts that section precisely
because situ's own rule is that a description stating what the code does
not do is worse than one that states nothing (14.5), and this file is a
description like any other.

### 25.0 Commits come in two parts, and that is deliberate

A finding lands as **two commits**: the work, and then a `fold:` commit
carrying the section 26 entry and any invariants it produced. This is the
practice the whole of section 26 is written by, and the history is full of
the pairs.

**It diverges from the standing rule that documentation rides along with the
code commit it describes, and the divergence is settled rather than
accidental.** It was raised and decided on 2026-08-04; what follows is the
reasoning, recorded so it is not relitigated.

The fold is not documentation *about* the work. It is the finding -- what
broke, which of the four backends were wrong and in what different ways, what
the shape of the defect turned out to be, and what invariant now holds. It is
routinely longer than the diff that provoked it, and it is the part with the
longer useful life: 26.59's five defects took a few hours to fix and the
paragraph explaining why a versioned accessor's shape defeats a validator is
what stops the sixth. Attaching that to the code commit would misrepresent
both -- the diff reads as though it came with an essay, and the essay reads as
a footnote to a diff.

Two practical consequences, and they are the reason the split earns its keep:

- **A fold is written after the work is green**, because half of what it says
  is what the tests then found. A combined commit would either be made before
  the fold could be written or held open until it was.
- **`git log --oneline` reads as a list of findings.** Alternating work and
  `fold:` lines is a table of contents for section 26, which is what somebody
  looking for "when did we learn that" actually searches.

What this is *not* a licence for: an ordinary comment fix, a typo, or a
paragraph that merely restates what the diff already says. Those ride along
with the code, as the standing rule requires. The exception is for a fold --
a numbered section 26 entry with its invariants -- and for nothing else.

- **ASCII only** in all source, comments, and docstrings. Non-ASCII belongs
  only in intentional runtime data values, and there are none in this project.
- **Tabs carry structural indent level; spaces carry alignment within a level.**
  Continuation lines use one tab for indent, then spaces to the alignment
  column. If lines are short enough to merge, merge rather than align.
- No prescriptive tab width anywhere in the codebase or in generated output.
  Elastic tabstops are the model: the viewer decides width.
- Generated C follows the same tab/space rule, and so does the Python -- and
  that is checked rather than assumed. `make lint` cannot see emitted code: it
  lands in `build/`, which is skipped, and before that in string literals,
  which are excluded so section 17's golden texts keep their gutter. So
  `style_gate.check_text` takes text rather than a path, and
  `test/unit/test_generated_sources_follow_the_conventions.py` runs it over
  what every schema generates in all four languages plus the checks, the fuzz
  harness and the dissector (26.58).
- **No autoformatter.** `black` and `ruff format` rewrite tabs to spaces
  unconditionally and cannot be configured out of it, so `tool/style_gate.py`
  under `make style` is the enforcement instead. See
  `doc/decision/0003-source-formatting.md`.
- Lowercase filenames unless there is a reason otherwise, and `snake_case` over
  `camelCase` in every language.
- Every module has a docstring stating its single responsibility. If a module
  needs two sentences joined by "and", split the module.
- **Identifier casing in a schema is the author's.** snake_case and PascalCase
  are both first-class and may be mixed; nothing in the compiler reads casing.
  What the compiler does check is that two constructs never generate the same C
  identifier, which is a property of flattening a path rather than of how
  either name is spelled (`doc/decision/0013-identifier-conventions.md`).
  `example/telemetry/` is snake_case throughout, as the working proof. C++
  adds one rule of its own -- a member may not be called what its class is
  called, which `struct option { u8 option; }` reaches without trying -- and
  that backend renames the class and aliases the schema's name to it rather
  than refusing the schema (`doc/decision/0025-cpp-class-and-member-names.md`).
- **A member named for a target language's keyword keeps its name; the
  emitter moves.** `int`, `class`, `type` and `lambda` are all words some
  backend cannot spell, and all four are what specifications actually call
  their fields -- DNS has a CLASS. Most generated identifiers carry the whole
  path in front of them and are safe; what needs mangling is the few emitted
  *bare*, which is the owned struct's field and every C++ accessor. One
  trailing underscore in C, C++ and Python, `r#` in Rust, and decision 0013's
  gate already refuses the collision the underscore could create, because
  `int` and `int_` reach one C identifier (26.90).
- Single source of truth: the AST is built once from the source text and all
  passes read it. Never re-parse generated output. Never mutate a file
  in a second pass without full knowledge of the first pass's state.

### 25.1 The subject names a subsystem, from a settled list

A subject is `subsystem: summary`, imperative, no trailing period, under 75
columns. The body is plain prose wrapped at 75, and only prose: an aligned
two-column table or a C declaration keeps the shape somebody gave it. That
much is the standing rule and is not this project's to change.

What *is* this project's is which names go before the colon. They are settled
because the whole history now uses them, and inventing a synonym for one is
the drift the rule exists to prevent:

| prefix | what it covers |
|---|---|
| `situc:` | the compiler and its four backends -- the bulk of the log |
| `fold:` | a section 26 entry and the invariants it produced (25.0) |
| `tests:` | the suite, the oracles, the differential probes |
| `tools:` | the style gate, the sweep, the benchmark, the hooks |
| `std:` | the standard codec and kernel schemas |
| `build:` | the Makefile and the CMake entry point |
| `packaging:` | the Debian packaging |
| `example/<name>:` | one worked protocol, named: `example/mqtt:` |
| `examples:` | more than one of them in the same change |
| `runtime:` | a runtime, where it is the point rather than a backend |
| `README:` | the front page |
| `doc:` | this document, where the change is not a fold |
| `docs:` | the decision records |
| `suggestions:` | the evaluations, one file per evaluating project |

**A change that genuinely spans the tree takes no prefix.** That is the
standing rule rather than an oversight, and two commits in the history
qualify: phase 0, and the adoption of the shared style gate.

Where a verb from the standing set -- feature, fix, rework, remove, build,
test -- fits without displacing the finding the subject carries, it goes in.
Where the finding is the whole subject, the finding wins. These subjects are
the table of contents for section 26, and a uniform register would cost more
than the filterability it bought.

The history was reformatted to this on 2026-08-06: 313 commits to 311, the
two squashes being a README that landed beside the CMake entry point and a
pair of corrections to the same evaluation file. No content changed -- every
commit reused its original tree object. What the reformat measured on the way
is 26.78.

### 25.2 A rung is numbered; a layer is named

The ladder of decision 0032 has two words for its parts and they are not
interchangeable. **`layer` is the axis and its named values** -- the flag is
`--layer`, and it takes `view`, `edit`, `relate`, `frame`, `converse`,
`drive`. **`rung` is a position on that axis, and takes a number.** So "rung
3" and "`--layer relate`" name the same thing from the two directions, and
neither "layer 3" nor "rung relate" is written.

**The reason is that `layer` is already taken.** This document has to say
"upper-layer protocol", "link-layer CRC", "stuffing at the outermost layer"
and "the segment length is the IP layer's", because that is the word the
subject uses. A tool for describing protocol layers cannot number its own
layers 1 to 6 without colliding with the reader's other meaning on every
page. Numbering the rungs is what keeps the two apart, and the mixed
metaphor is the mitigation rather than the problem.

Measured before it was written down, because it was already being followed:
61 uses of `rung <n>` against 8 of `layer <n>`, and all 8 of those are inside
`doc/decision/0031` and `0032`, which are append-only and stay as written.
`rung <name>` appears nowhere.
`test_a_rung_is_numbered_and_a_layer_is_named` refuses `layer <n>` in this
document and in the README now, so the convention is checked rather than
merely observed.

It is not in `style_gate.py`, and the reason is worth stating because the
gate is the obvious home: that file is a verbatim copy of the shared source,
so a rule added here would be drift by construction, and a rule about *this
project's* ladder has no business in a gate three other trees run. A
project-specific convention gets a project-specific check.

---

## 26. Implementation plan

Phases carry a **Status** line until they are reached; keeping those current is
how this document doubles as the record of where the work is. A phase is
complete when its acceptance criteria pass, not when its code looks finished.

**Phases 0 through 14 are complete.** They are the plan as first written, plus
the three things that landed after it ran out: front end, layout solver,
capability lattice, requirements and blame chains, the C backend, expressions
and dynamic layout, variants, opaque regions, TLV, indexed tables, varints,
both codec tiers, the cryptographic model, the advisor, the MMIO target, the
`.proto` importer, documentation generation, the Wireshark dissector, and fixed
point and BCD. Every command section 21 names has landed, and
`FUTURE_COMMANDS` in `situc/cli.py` is empty.

Phases 0 through 8 are ordered by dependency; everything after is largely
independent of the rest.

**26.15 through 26.23 are complete too**: the built-in codec set, the three
non-C backends, the language server, cross-field invariants, text protocols,
schema evolution, and the constructs the worked examples asked for.
**Everything describing a single message is done.** What situ deliberately
does not cover *within* one is named where the construct that would cover it
would go -- 8.6.6 for a grammar -- rather than in a list of absences, which
goes stale the moment one of them lands. Signed text was on that list and is
not any more (26.297); so is the fractional text format that stood beside
8.6.6 in this sentence until 0056 built it (26.332). Twice now, which is the
habit working.

**What is outstanding is the layer ladder, and only above the bottom rung.**
Decision 0032 puts six rungs on one axis chosen at `situc build --layer`;
`view` is what ships and is the default, and rung 2 (`edit`) exists in pieces
-- `--owned` emits a fixed-size decode and refuses variable-length members.
26.95 through 26.98 schedule rungs 3 to 6, and 26.99 finishes rung 2 against
the enumeration in
`doc/decision/0031-where-allocation-is-unavoidable.md`. The numbering is a
log rather than a running order: nothing in 26.95 through 26.98 waits for
26.99, because emitting a lower rung's files is not the same as needing its
emitter written first.

**Four backends over one layout**, and the claim that matters is that they
agree. Each is tested against the C output on the same buffer, field by field,
because four backends that disagreed would mean a schema means four things.
What differs between them is not the bytes but how much of the lattice each
language can enforce rather than document:

| | C | C++ | Python | Rust |
|---|---|---|---|---|
| bounds | run time | run time | run time | run time |
| invalidation (12.3) | generation, checked in `SITU_CHECKED` | as C | generation, always | **borrow checker** |
| a length that cannot be lost | `_COUNT` macro | `span` | `memoryview` | slice |
| an error that cannot be dropped | no | `[[nodiscard]]` | exception | `Result` |
| the stage gate (14.3) | a struct anyone can fill in | **no public constructor** | a run-time token | **private field** |
| `atomic`, `repr` | documented | documented | documented | documented |

The two in bold are the ones a compiler refuses rather than a runtime reports,
and they are the argument for having written those backends.

**The shape of the test suite**, because its size is the argument for trusting
any of the above:

| Layer | Count | What it holds |
|---|---|---|
| compiler tests | 2685 | pytest over `situc/`, one file per module |
| generated C checks | ~670 | cmocka, `gen-checks` output for every schema |
| schemas exercised | 27 | 23 examples, `header.situ`, `edges.situ`, two std |
| targets built | 3 | host, aarch64 under emulation, aarch64 big endian |
| backends compiled | 3 | every schema, as C++, Python and Rust, in the suite |

Every one of those numbers is a floor rather than a target, and every one of
them was stale when somebody last looked: the first said 1280 and the third
said 20, which is what a hand-maintained count does between the day it is
written and the day it is read. `test/unit/every_schema.py` is where the third
one comes from, and running the suite is where the first one does. The generated
checks are derived from the schemas, so adding an example adds coverage without
anybody writing a test. Diagnostics are snapshot-tested in
`test/unit/test_diagnostics.py`, which compares `render()` against the exact
expected text, because section 17 makes message quality the product rather
than a finish: a regression in the wording of a blame chain fails the build.

This line said `test/golden/` for a long time and no such directory has ever
existed. The docs gate could not see it, and by its own rules should not:
`doc_paths` reads backticked paths **in table rows** -- 26.144's finding that
"paths in prose are examples; paths in a table are a declared inventory" --
and skips any token ending in `/`, so a directory named in a sentence is
outside its scope twice over. The scope is right; the sentence was wrong.

Phases 0 through 8 are ordered by dependency, and 9 onward are largely
independent of each other. Do not implement ahead of the plan.

### 26.0 Phase 0: scaffolding

Moved to `doc/decision/26.0-phase-0-scaffolding.md`.

### 26.1 Phase 1: front end, static subset

Moved to `doc/decision/26.1-phase-1-front-end-static-subset.md`.

### 26.2 Phase 2: layout solver, static subset

Moved to `doc/decision/26.2-phase-2-layout-solver-static-subset.md`.

### 26.3 Phase 3: capability lattice

Moved to `doc/decision/26.3-phase-3-capability-lattice.md`.

### 26.4 Phase 4: C backend, static subset

Moved to `doc/decision/26.4-phase-4-c-backend-static-subset.md`.

### 26.5 Phase 5: expressions and dynamic layout

Moved to `doc/decision/26.5-phase-5-expressions-and-dynamic-layout.md`.

### 26.6 Phase 6: variants, opaque, TLV, indexed, varints

Moved to `doc/decision/26.6-phase-6-variants-opaque-tlv-indexed-varints.md`.

### 26.7 Phase 7: transforms, tier 1 (extern codecs)

**Status: complete.** `situc gen-codec-tests` output was checked against four
deliberately lying implementations. `coded(C) { ... }` is the general transform
region this table needed and this document did not name;
`doc/decision/0009-coded-regions.md` records why, and phase 8's `sealed`
becomes `coded` plus authentication rather than a second mechanism.

The property held throughout: **the lattice reads property signatures and
nothing else** (13.1). No rule in `propagate.py` knows what an algorithm does.

A `codec` with no bound `impl` must analyze cleanly and fail only at codegen.

### 26.8 Phase 8: cryptographic model

Moved to `doc/decision/26.8-phase-8-cryptographic-model.md`.

### 26.9 Phase 9: advisor

Moved to `doc/decision/26.9-phase-9-advisor.md`.

### 26.10 Phase 10: MMIO target

Moved to `doc/decision/26.10-phase-10-mmio-target.md`.

### 26.11 Phase 11: Rust backend

**Superseded by 26.18.** This number was allocated to the Rust backend before
the backend order was decided; section 20.1 now puts C++ and Python ahead of
it. The number stays rather than being reused, so that a reference to "phase
11" written earlier still lands somewhere true.

### 26.12 Phase 12: transforms, tier 2 (derived codecs)

Moved to `doc/decision/26.12-phase-12-transforms-tier-2-derived-codecs.md`.

### 26.13 Phase 13: `.proto` importer

Moved to `doc/decision/26.13-phase-13-proto-importer.md`.

### 26.14 Delivered after the plan ran out

Moved to `doc/decision/26.14-delivered-after-the-plan-ran-out.md`.

### 26.15 The built-in codec set

Moved to `doc/decision/26.15-the-built-in-codec-set.md`.

### 26.16 C++ backend

Moved to `doc/decision/26.16-c-backend.md`.

### 26.17 Python backend

Moved to `doc/decision/26.17-python-backend.md`.

### 26.18 Rust backend

Moved to `doc/decision/26.18-rust-backend.md`.

### 26.19 Language server

Moved to `doc/decision/26.19-language-server.md`.

### 26.20 Cross-field invariants

Open question 3's construct, built after the roadmap ran out. `invariant
frame.total == size(frame.header) + size(frame.body);` -- see 16.1 for what it
means and what each backend emits.

Worth recording is how little was needed and where the cost actually fell. The
resolution had said the maintenance obligation goes where coverage already
puts it, and that was exactly true: no axis was added, no propagation rule
changed shape, one row. What took the time was everything downstream of the
construct rather than the construct -- one numbering shared by four backends,
four recompute emitters over one shared expression walk, and five artifacts
that describe a field and each had to learn the word "derived".

Every bug in it was a disagreement rather than a crash, which is why
`test/unit/test_invariants.py` compares the backends instead of reading each.

### 26.21 Text protocols

Moved to `doc/decision/26.21-text-protocols.md`.

### 26.22 Schema evolution

Section 19's model, built after the roadmap ran out and prompted by one
question: can two `.situ` files be compared for a backward-compatible
extension, and can one file carry more than one version.

Both turned out to rest on a mistake in 19 itself. `situc diff` was described
as the compatibility linter and is a *cost* linter -- it compares capability
vectors. Three edits proved the gap before anything was built: a byte-order
flip reported "No capability change", a same-width member swap reported zero
regressions, and moving a field out of an authenticated region reported an
**improvement**. The third is not a bug in the lattice. `Uncovered` ranks
stronger than `Covered` because a field with no tag over it is cheaper to
write, and the lattice is right about cost. The mistake was reading a cost
ordering as a compatibility ordering.

Delivered:

- **`situc wire`** (19.3): the byte-level contract, committed as
  `NAME.situ.wire` and checked, with the currency test the map has. It
  classifies rather than diffing -- BREAKING, COVERAGE, backward, forward,
  api-only -- because the categories are not equally alarming.
- **`[since = N]`** (19.4): more than one version in one file, append-only by
  construction. Every member keeps a static offset and the struct's extent is
  a range, which sound contradictory and are both exact.
- The two meet: appending a member reports as *probably* safe, and appending
  one behind a `[since]` reports as *provably* safe, because the old receiver
  reads the version field and knows the bytes are not its own.

What building it cost, and what that says:

The same accessor was ungated in four backends and the write side in all four
including C (invariants 27 and 28). The C++ factory shadowed its own class
name for any schema with a struct called `msg` (invariant 29). `gen-fuzz` had
never compiled for a variable-size struct at all -- the artifact whose purpose
is hostile input did not build for the structs most likely to fail on it. And
the layout solver had been recording `array_count = 1` for a delimited member
since the construct was added, which six consumers had absorbed (invariant
25).

None of those are about schema evolution. They were found by it, because
adding a construct means walking every backend and every artifact, and that
walk is the only thing that reads them all in one sitting.

### 26.23 What the worked examples asked for

Moved to `doc/decision/26.23-what-the-worked-examples-asked-for.md`.

### 26.24 DNS name compression, and what it cost

A fourth worked example, chosen for the same reason as the other three: a shape
the tree did not have. A compressed name is a run of labels where a label is a
variant on its top two bits, and one of the arms is a pointer to somewhere else
in the message.

It asked for three things and got all three.

**A weakening the layout does not imply** (11.5). Every construct in the schema
is canonical taken alone, and the format admits many spellings of one name,
because the redundancy is between the name and bytes elsewhere in the message.
No per-member rule can see that, so `[non_canonical = "reason"]` lets the
schema say it -- one axis, a required reason, and no way to strengthen
anything.

**An extent for a variant** (11.6). This is the one that mattered. A variant's
extent had been refused outright on the grounds that it depends on the
discriminant, which is an argument that the extent is a *switch*, not that
there is none. The refusal propagated: no extent meant no run over a variant,
so the example could describe a compressed name and not walk one, which is the
thing it exists to show.

**A check nobody had written.** Walking labels immediately showed that
`default: error` was enforced by no backend, in a language whose specification
had said it was since 14.5 was written, with `SITU_ERR_VERSION` defined for it
and returned by nothing. Nothing had noticed because nothing could reach a
variant closely enough to care.

It also found the extent calculation copied into four backends, each dividing
every member to whole bytes before summing -- so a `u2` and a `u6` contributed
nothing where together they are a byte. That moved to `traverse.extent_parts`,
and the "can this be measured at all" predicate with it, because `gen-checks`
had been deciding it separately and disagreeing (invariant 34).

And what it did not get: situ describes a compressed name and walks one, and
does not follow the pointer. Resolving one means re-entering the parser at an
arbitrary earlier offset with a cycle check and a budget, which is control flow
rather than layout. Naming that boundary precisely is the difference between a
limit and a shrug -- the earlier `example/dns/dns.situ` had claimed situ could
not describe DNS names "statically at all", which was wrong by a wide margin.

### 26.25 The dissector reads the same layout

Making a variant walkable in the four code backends left the dissector saying
`no bytes of its own` for the variant and `elements of no fixed size` for the
run -- so Wireshark showed a DNS name as nothing at all. That is invariant 36
one artifact further out: the dissector is a fifth reader of the same walk, and
fixing four of them is not fixing it.

It now emits, per struct, the same two functions the C backend does -- an
extent and a run span -- from the same `traverse.extent_parts` and
`traverse.arm_members`. Only the reads are Lua's.

Three other things were wrong for the same reason and had never been noticed,
because nothing reached far enough into a variable struct to see them:

- a variable-size nested struct was dissected over `size_bytes`, its
  *minimum*, so a whole name showed as its first byte
- a fixed-width member after a variable one reported `no bytes of its own`,
  which is a strange thing to say about a `u16`: `byte_span` is None for a
  dynamic offset, and *where* it sits was the only thing unknown
- and it was therefore declared `ProtoField.bytes` and shown as hex

Two operator translations now share one function. The schema's operators are
C's; Python spells three of them in words and Lua spells four, and both need
`!=` rewritten before `!` or the result is `not =`. That ordering is the whole
of the difficulty, so it is written once (`names.translate_operators`) rather
than got right twice.

Why Lua at all is `doc/decision/0021-dissector-language.md`, written because
the question was asked and nothing answered it: no build step and no ABI to
match, against a Wireshark whose plugin ABI moves between minor releases, for
an artifact meant to be committed beside the schema.

### 26.26 What the folds keep finding

"Fold what the implementation learned back into this document" has been run
several times now, and the same thing happens each time: the prose is roughly
right and the *lists* are wrong. The propagation table was twenty rows behind,
the CLI surface named a flag that never existed, the repository layout was
missing a module, and the nested-copy field list in the solver had fallen six
behind (invariant 39).

The pattern is worth naming because it predicts where to look. Prose drifts
slowly, because a paragraph that has gone wrong usually reads wrong. A list
does not: an entry that should be there and is not looks exactly like a list
that is complete. Nothing about reading it says otherwise.

So the rule in section 0 is not "keep the document current" -- everybody
intends that -- it is that a list in this document should be *derived* or
*checked*, and the four that now are took one test each. The remaining
hand-maintained lists here are the glossary and the decision index, and both
are append-only by nature: an entry never becomes wrong, it only fails to
appear, and a missing glossary term costs a reader a search rather than a
wrong build.

### 26.27 What fuzzing every schema found

Moved to `doc/decision/26.27-what-fuzzing-every-schema-found.md`.

### 26.28 Where a generated artifact cannot be purged

Decision 0022 measured what removes an unused accessor. Two of the four
languages cannot do it fully, and the position taken is to accept that rather
than contort the generator around it:

- **Rust** eliminates everything unused in a binary crate and keeps 33 symbols
  in a library one, because `pub` is the public API and dead-code elimination
  may not touch it. A consumer who cares uses the module inside their own
  crate, where it is eliminated, or a feature gate.
- **Python** eliminates nothing, ever. Every method and property exists at
  import; there is no flag, and there is no version of this generator that
  changes that.

Both are properties of the language rather than of situ, and the alternatives
-- generating a subset from a declared use-list, or splitting every struct into
its own module -- cost the user a thing to maintain in exchange for bytes that
matter in neither language's usual deployment. C and C++ are the targets where
size is the argument, and there the answer is a linker flag, now documented in
20.2.

The consequence for the accessor families of 0022 is that "emit both and let
the toolchain purge" is a strategy for C and C++ only. That is a reason to
keep the lattice-driven rule -- emit the second family only where the first
one's vector is weak -- rather than a reason to abandon the split.

### 26.29 One list, four runtimes

The framing work needed a new failure class, `truncated`, and adding it meant
editing four runtimes by hand. Nothing would have caught a fourth omission, so
section 20.2's list of the classes is now held to `runtime/c/situ.h` *and* the
other three runtimes are held to it.

It found two holes on its first run, neither of them recent:

- **C++ had no `stale`.** The C runtime has named it since section 12.3 was
  written -- a view outliving a write that shifted layout under it -- and the
  C++ `err` enum did not, so a caller could not name the condition it was
  being protected from.
- **Python reported the stage gate as `TagError`.** A tag that fails to
  verify is a hostile or corrupt message; reaching a sealed interior without
  opening the gate is a bug in the caller. A receiver that cannot tell them
  apart logs the second as an attack and the first as a typo. It has a
  `StageError` now.

One exemption, written into the test rather than left implicit: **Rust has no
`Stale`**, because invalidation there is the borrow checker (26.18) -- a view
outliving such a write does not compile, and there is no run-time condition to
name. Having the exemption in the test is what makes a second one something
that has to be argued for.

The general point is the one section 0 keeps making, at a level above the
document: a *runtime* can drift from another runtime as easily as a table can
drift from the code, and four hand-edited copies of anything will disagree
eventually. What made this checkable is that the C enum is the definition and
the other three claim to mirror it -- where that is true, say it in a test.

### 26.30 The second accessor family, and what it materializes

Moved to `doc/decision/26.30-the-second-accessor-family-and-what-it-materializes.md`.

### 26.31 Where the frontier is

Moved to `doc/decision/26.31-where-the-frontier-is.md`.

### 26.32 One question, four answers, and the shape of being wrong

Moved to `doc/decision/26.32-one-question-four-answers-and-the-shape-of-being-wrong.md`.

### 26.33 The runtime image, and why it is late

Moved to `doc/decision/26.33-the-runtime-image-and-why-it-is-late.md`.

### 26.34 Running it, and asking four at once

Moved to `doc/decision/26.34-running-it-and-asking-four-at-once.md`.

### 26.35 The sample nobody chose

Moved to `doc/decision/26.35-the-sample-nobody-chose.md`.

### 26.36 The example that needed a construct

Moved to `doc/decision/26.36-the-example-that-needed-a-construct.md`.

### 26.37 The expression language, and the host's own byte order

Moved to `doc/decision/26.37-the-expression-language-and-the-host-s-own-byte-order.md`.

### 26.38 The notes that outlived their phases

Moved to `doc/decision/26.38-the-notes-that-outlived-their-phases.md`.

### 26.39 The checksum that covers itself

Moved to `doc/decision/26.39-the-checksum-that-covers-itself.md`.

### 26.40 The radix nothing had used

Moved to `doc/decision/26.40-the-radix-nothing-had-used.md`.

### 26.41 The rest of the list

26.40 chose its example by surveying every construct the language offers
against every schema in the repository, and the survey named more than one
unused thing. This is the rest of them: five in `test/schema/edges.situ`,
which exists to say exactly this, and eight register access modes in
`example/registers`, where the vocabulary they are borrowed from lives.

The result is worth stating plainly because it cuts both ways. **Two of the
thirteen were wrong. Eleven were right.** The eleven are not a waste: the only
way to know which two were wrong was to write all thirteen, and "we have never
run this" is not a defect report either way.

**`rc` and `rs`, wrong in both halves.** They are *read*-triggered -- the read
is what clears or sets the bit -- so there is no write side at all, and the
read is not pure.

- situ had them **writable**, because the test was "not read-only". Both
  generated a setter that writes a whole word of ones with one bit cleared: an
  operation the bus does not offer, aimed at a field nobody can write.
- And both came out **`effect = Pure`**, which is the axis reporting a
  destructive read as a pure one. `on_read = clear` was understood and `rc`
  was not -- the same fact in SystemRDL's other spelling, and situ read only
  one of the two. 15.2 lists both in the same table.

`w1s`, `w0c`, `w0s`, `wo_once` and `rsvd` were right, as were `namespace`,
`[escape]`, `[unknown]`, `[equalize]` and `[allow_unverified_read]`.

**Three defects around them**, each from the first schema to reach it:

- **The differential driver opened a gate that is not there.**
  `[allow_unverified_read]` is the one construct whose purpose is to give up a
  guarantee (14.3), and where it is written there is no gate type and no
  `_open` to call. The driver named both.
- **`gen-fuzz` emitted `wire::framed_body` into C.** `::` is not an identifier
  there; the compiler read it as a label and a stray colon.
- **And the check that every struct reaches the fuzz harness compared the same
  unflattened name**, so it would have passed on the broken output. The C
  compiler caught it first, which is luck rather than coverage -- the check
  was written against the generator's spelling rather than the target's.

**What the survey is now.** Every attribute in the vocabulary has a user, and
the constructs listed as unused in 26.40 have one each. The survey script is
not committed and should not be: it is twenty lines of regular expressions
over the schema sources, it answers a question worth asking about twice a
year, and a committed version of it would be a list to maintain rather than a
measurement to take. What is worth keeping is the habit -- ask the language
what it offers, ask the schemas what they use, and read the difference.

**Status:** 2729 unit tests, 7 skipped; generated C compiled and run on the
host and under aarch64 emulation; every dissector executed; `make fuzz` clean
over 27 harnesses; eight worked examples carrying bytes an independent
implementation wrote.

### 26.42 The search, and where the bracket was still a count

Moved to `doc/decision/26.42-the-search-and-where-the-bracket-was-still-a-count.md`.

### 26.43 Reading the artifact nothing compiles

Moved to `doc/decision/26.43-reading-the-artifact-nothing-compiles.md`.

### 26.44 Advice, taken rather than read

Moved to `doc/decision/26.44-advice-taken-rather-than-read.md`.

### 26.45 The number, and what the number counts

Moved to `doc/decision/26.45-the-number-and-what-the-number-counts.md`.

### 26.46 Writing the schemas nobody wrote

Moved to `doc/decision/26.46-writing-the-schemas-nobody-wrote.md`.

### 26.47 Putting the last fold's shapes in a schema

Moved to `doc/decision/26.47-putting-the-last-fold-s-shapes-in-a-schema.md`.

### 26.48 The constructs that had never met a run

26.47's family was one construct meeting an element width. This fold takes the
same run to the constructs it had never been written beside: a nested struct,
a variant arm, and a byte-order marker. Three probe schemas, and two of the
three produced something no backend should emit.

**A marker at a dynamic offset crashed the compiler.** `example/tiff` is the
only `endian_marker` in the tree and it is a header of constant offsets, so
the marker-conditional load had never been asked for a member behind a
variable-length one. C's asks the placement for its constant offset while
every other load on that path takes one from the caller -- so it hit the
assertion inside `offset_bytes` and came out as a Python traceback, where
section 17 asks for a diagnostic with a blame chain. The other three emit it
correctly, which is the shape of most cross-backend findings reversed.

**And Rust wrote `self.as_ref().as_ref()`.** The setter for a member at a
dynamic offset rewrites its body to run the *reads* on the immutable view, by
substituting `self.` -- and the store had already put an `as_ref()` on the
marker predicate, because every other setter site needs one. So the rewrite
found its own output and wrapped it again. That is invariant 53's rule from
the other side: a rewrite over generated text has to move past what somebody
already wrote there, not only past what it wrote itself.

**A variant arm may be a scalar, a byte run or a struct.** A run of *values*
was none of those, in any of the four: C and C++ said so in the generated file
("not a shape this backend reaches into yet"), and Python and Rust emitted
nothing at all -- no accessor and no note, which is invariant 27's floor and
one step below it. Both spellings were unreachable, `u16 x[n]` and `u16 x[3]`
alike, the arm's own base being dynamic whatever its count says. All four emit
the pair now, the count carrying the discriminant test and the getter reaching
through it.

**The differ asked that arm the wrong question**, which is why nothing found
it: `_arms` sorted a member into "byte run" or "scalar" by its element width
alone, so a run of `u16` was a scalar arm and the generated driver named a
getter taking no index. A driver that does not compile is a schema nobody
compares -- and no schema here had one.

**What did not break** is worth as much as what did. A nested struct's field
driving a run, a run of values under a marker, `[remaining]` with a wide
element, a fixed-point element reached by index: written, generated, executed
in all four, agreed. The sweep is only evidence when it says both.

**Status:** 2804 unit tests, 7 skipped; generated C compiled and run on the
host and under aarch64 emulation; five probe schemas built and run through
all four backends, three of them now in `test/schema/edges.situ`.

### 26.49 An offset the message decides, in the places that assumed otherwise

Moved to `doc/decision/26.49-an-offset-the-message-decides-in-the-places-that-assumed-otherwise.md`.

### 26.50 The sweep, as machinery rather than as an afternoon

Moved to `doc/decision/26.50-the-sweep-as-machinery-rather-than-as-an-afternoon.md`.

### 26.51 Emptying the list the sweep produced

Moved to `doc/decision/26.51-emptying-the-list-the-sweep-produced.md`.

### 26.52 The whole space, once

Moved to `doc/decision/26.52-the-whole-space-once.md`.

### 26.53 Widening the space, and what a region's extent means

Moved to `doc/decision/26.53-widening-the-space-and-what-a-region-s-extent-means.md`.

### 26.54 Nine hundred cells that were measuring the template

Moved to `doc/decision/26.54-nine-hundred-cells-that-were-measuring-the-template.md`.

### 26.55 MQTT, and what a real protocol found this time

Moved to `doc/decision/26.55-mqtt-and-what-a-real-protocol-found-this-time.md`.

### 26.56 The limitation that was not one

Moved to `doc/decision/26.56-the-limitation-that-was-not-one.md`.

### 26.57 Modbus, and the arm nobody selected

Moved to `doc/decision/26.57-modbus-and-the-arm-nobody-selected.md`.

### 26.58 The conventions reached the sources and stopped there

Moved to `doc/decision/26.58-the-conventions-reached-the-sources-and-stopped-there.md`.

### 26.59 A constraint behind a version

Moved to `doc/decision/26.59-a-constraint-behind-a-version.md`.

### 26.60 The advice that recommended an attribute nobody implemented

Moved to `doc/decision/26.60-the-advice-that-recommended-an-attribute-nobody-implemented.md`.

### 26.61 An oracle that is not us

Moved to `doc/decision/26.61-an-oracle-that-is-not-us.md`.

### 26.62 Seven oracles, and the one that was flaky before it was right

Moved to `doc/decision/26.62-seven-oracles-and-the-one-that-was-flaky-before-it-was-right.md`.

### 26.63 The rest of the stack, and a filter that had to be shared

26.62 left five formats with `randpkt` types and no oracle. They have them
now: ipv4, icmp, udp, tcp and dns, which with the earlier seven makes twelve
over layout and two over arithmetic. Every one of them has a mutation in
`LIES`, so every one has been shown able to fail.

Five formats, one shape. `_reader` and `_situ_reader` are built from a table
of tshark's name for a field, situ's name for it, and the base tshark prints
it in; only the extraction differs, because a bit-packed nibble is not a
`u16`. The layer offsets still come from tshark's own `_raw` entries.

**The first version had the bug invariant 98 was written about**, three days
after writing it. `_situ_reader` wrapped its accessor call in `try/except:
continue`, on the reasoning that a frame situ refuses is a frame situ cannot
answer about -- which is true, and which quietly let the situ side choose its
own subset. The udp oracle read seven rows against tshark's eight, every
shared value identical, one frame simply missing from one side. The rule was
already written down and the code broke it anyway, because dropping a frame
where you happen to be standing looks like error handling rather than like
choosing a subset.

`_comparable` is the fix: one predicate, evaluated identically on both sides,
taking the struct's own minimum. `_situ_reader` asserts that minimum against
the generated module's `SIZE_MIN`, so a schema whose extent moves fails here
instead of silently filtering frames neither side mentions.

**Two adjustments where randpkt's output and situ's question did not meet.**

`randpkt -t ip` fills the header with noise, so tshark reports `ip.version`
and gives up, and the comparison filtered to nothing. The IPv4 oracle takes
its corpus from the udp generator instead, which emits a well-formed IP
header underneath -- still randpkt's bytes, still the IPv4 layer under test.

And `tcp` was flaky at one run in twenty, always as *"tshark reported nothing
to compare"* -- the empty-comparison guard from 26.62 doing precisely its
job. Measured rather than guessed at: with eight packets a tcp run yields
zero comparable frames about five percent of the time. Forty packets, and
zero in twenty trials. The cause was the sample size, not the randomness,
which is invariant 99 in the direction it is easy to get wrong: the fix that
suggests itself is to relax the guard.

**Status:** 2972 unit tests, 7 skipped; `make check` clean over 109 files;
twelve layout oracles and two computation ones; twenty-six consecutive runs
of the oracle suite clean after the sample size went up.

### 26.64 Modbus, whose oracle is a library and whose corpus is a stream

Moved to `doc/decision/26.64-modbus-whose-oracle-is-a-library-and-whose-corpus-is-a-stream.md`.

### 26.65 MQTT, and a reader that was regenerating its own input

Moved to `doc/decision/26.65-mqtt-and-a-reader-that-was-regenerating-its-own-input.md`.

### 26.66 The example three projects asked for

Moved to `doc/decision/26.66-the-example-three-projects-asked-for.md`.

### 26.67 The smaller bargain

Moved to `doc/decision/26.67-the-smaller-bargain.md`.

### 26.68 The gate that did not pass the gate

Moved to `doc/decision/26.68-the-gate-that-did-not-pass-the-gate.md`.

### 26.69 The other bargain

Moved to `doc/decision/26.69-the-other-bargain.md`.

### 26.70 One file, and only the part of it that is reached

Moved to `doc/decision/26.70-one-file-and-only-the-part-of-it-that-is-reached.md`.

### 26.71 What the sweep covers, stated once

Moved to `doc/decision/26.71-what-the-sweep-covers-stated-once.md`.

### 26.72 The axis, and the question it asked immediately

Moved to `doc/decision/26.72-the-axis-and-the-question-it-asked-immediately.md`.

### 26.73 What the sixth axis was worth

Moved to `doc/decision/26.73-what-the-sixth-axis-was-worth.md`.

### 26.74 The consolidation that was not one

Moved to `doc/decision/26.74-the-consolidation-that-was-not-one.md`.

### 26.75 Asking the emitter

Moved to `doc/decision/26.75-asking-the-emitter.md`.

### 26.76 The run that examined nothing, and read as clean

Moved to `doc/decision/26.76-the-run-that-examined-nothing-and-read-as-clean.md`.

### 26.77 The instrument builds somewhere it does not own

26.76 taught the sweep to say whether it ran. This is the other half, and
it arrived immediately: three full runs were destroyed in one session, and
**not one of them failed for a reason inside situ**. Each time an unrelated
process on the same machine exhausted a filesystem the sweep happened to be
building in, and each time the shards died mid-cell.

situ was checked and cleared twice, by counting: a full sweep leaves exactly
one `situ-sweep-*` directory per shard at a time, because each cell's tree
is removed in a `finally`. At the worst moment there was one such directory
totalling 128K. The tool does not leak. It simply has no claim on the space
it needs, and no notice when the space goes.

**The whole failure is a resource the tool depends on and does not own.**
Every fold since 26.54 rests on a sweep, the sweep needs an hour of a
filesystem staying available, and it shares that filesystem with everything
else running. That is a dependency the record never stated, so it was never
a thing anyone could decide about.

**The remedy needed no code**, which is the part worth writing down.
`tempfile.mkdtemp` resolves through `tempfile.gettempdir()`, which honours
`TMPDIR`, so `TMPDIR=$HOME/.cache/situ-sweep python3 tool/sweep.py --all`
already builds somewhere private and off the shared tmpfs. The knob was
always there; what was missing was knowing the instrument had a shared,
volatile floor at all. That is 26.45's shape a second time in two folds --
the capability present, the question unasked -- and it is why this is
recorded rather than fixed with a patch.

**A diagnostic worth keeping, because the first reading was wrong.** `du`
reported 165G on a filesystem `df` called 859G full, and the 694G gap was
not an accounting quirk: it was two processes holding deleted files open,
530.2G and 163.8G. **When `du` and `df` disagree, the difference is held by
a process rather than by the filesystem** -- `lsof +L1` names it, and the
space returns the moment the holder dies. The wrong inference was drawn
first, out loud, that a reboot would not help; the opposite was true, since
process-held deletions are exactly what a reboot reclaims. Check before
concluding which of the two is lying.

**Afterwards, check the repository rather than the reasoning.** Work was
committed minutes after a filesystem hit zero, and the question of whether
anything had been written *during* that window has an evidence answer and a
reconstruction answer. The reconstruction was right -- the one write
attempted at zero bytes failed loudly with `ENOSPC`, which is what prompted
looking at `df` at all -- and it was still the wrong thing to offer, because
a commit written into a full filesystem can be corrupt in ways a plausible
timeline cannot rule out. `git fsck` exits zero, and the fold is confirmed
present in `git show HEAD:project.md` rather than in the working copy,
which is the only version that survives a reboot. Dangling commits in the
output were old `git stash` entries, dated days earlier, and not damage.

**Status:** no situ change. Recorded as an operating condition of the
sweep, with `TMPDIR` named as the way to meet it.

### 26.78 The conventions the log did not follow, and the gate that agreed

The history was reformatted to 25.1 in one pass, 313 commits to 311. The
reformat is not what is worth an entry; what measuring first turned up is.

**The written convention and the enforced convention were different sizes.**
`tool/hooks/commit-msg` checks two things -- generator attribution, and a
subject over 75 columns -- and the standing rule has at least six. So the
four nobody mechanised drifted for the entire history without one complaint
from the tooling: 237 of 313 subjects carried no subsystem at all, 2330 body
lines were wrapped at 80 rather than 75, and the hook accepted 301 of the 313
messages. Nothing was broken. The mechanised part of the rule was simply a
small part of it, and a green hook was read for years as the message being
right.

**The documented-path check reads five paths in a ten-thousand-line
document.** `make style` reports "project.md says nothing twice and names no
missing file" while section 23 declared `tool/lint_conventions.py`, renamed
to `style_gate.py` back in 26.68 and gone from the tree since. The gate is
not wrong: `doc_paths` deliberately reads backticked paths in *table rows*,
which is exactly what took its false-positive rate to zero across every
project that carries it. But section 23 is a fenced indentation listing, so
**the one place this document declares the whole tree is the one place the
check cannot look.** Of its 74 checkable entries 73 were accurate, one named
a deleted file, and `tool/style_gate.py` and `tool/hooks/commit-msg` were
missing from it altogether -- a listing that had drifted precisely where
nothing read it.

Both are the same shape as 26.76, one level up. There the sweep passed while
examining nothing; here the gates pass while examining a fraction, and in
both cases the number that would have said so was available and never
asserted.

**Status:** section 23, section 25 and the README corrected; 25.1 added.
Section 23 is left as a listing rather than converted to tables to satisfy
the gate -- that would be bending the document to fit the checker, which is
the fault the checker's own narrowness was written to avoid. The gate is
shared tooling, copied verbatim from one source into every project, so
widening it past table rows is a cross-project change and is raised rather
than made here.

### 26.79 The image, and what packing a schema into one found

Moved to `doc/decision/26.79-the-image-and-what-packing-a-schema-into-one-found.md`.

### 26.80 The names the generated code needed and the schema could take

Moved to `doc/decision/26.80-the-names-the-generated-code-needed-and-the-schema-could-take.md`.

### 26.81 The walker, and the three things it disagreed about

Moved to `doc/decision/26.81-the-walker-and-the-three-things-it-disagreed-about.md`.

### 26.82 Growing the walk: runs, arms, gates

Moved to `doc/decision/26.82-growing-the-walk-runs-arms-gates.md`.

### 26.83 The delimited members, and where a truncated one ends

Moved to `doc/decision/26.83-the-delimited-members-and-where-a-truncated-one-ends.md`.

### 26.84 The last two decoded widths, and a variant's extent

Moved to `doc/decision/26.84-the-last-two-decoded-widths-and-a-variant-s-extent.md`.

### 26.85 What the first CI run found, including a buffer overrun

Moved to `doc/decision/26.85-what-the-first-ci-run-found-including-a-buffer-overrun.md`.

### 26.86 What the owned form can promise for a text number

Moved to `doc/decision/26.86-what-the-owned-form-can-promise-for-a-text-number.md`.

### 26.87 The archive nothing built, and the compiler nobody heard

Moved to `doc/decision/26.87-the-archive-nothing-built-and-the-compiler-nobody-heard.md`.

### 26.88 A nested sub-view, a tag, and the extent that was zero

`nested` and `tag` take the walk to 504 members across 28 of the 32 schemas.
Neither is a hard probe -- `ok= extent=` and `present=` are the two simplest
answers in the differ -- and both were wrong for reasons about what a
*question* is rather than about bytes.

**A run of records is not a nested member.** sqlite's `cells` is an `indexed`
region of `table_leaf_cell`, so it has a struct type and is asked `count=`.
Answering `ok= extent=` is a line about a different question, and the
comparison only caught it because the names matched. `kind` tells them apart.

**A tag whose offset nobody can compute is absent.** keystore's tag sits
after a sealed region whose extent is the codec's, so C's pointer accessor
hands back NULL and the answer is `present=0`. A walker that summed its way
to an offset anyway said `present=1` about bytes nothing can find, which is
worse than not answering.

**A `while` run's extent was still the minimum.** `size_bits` had learned
varints, delimiters and variants and not this, so a struct whose only member
is a `while` run measured one element where it held two -- dnsname's `qname`
sub-view, off by a label. The run's extent is however far the walk got, which
the walk already computed and threw away.

**And zero is an answer.** A `name` whose first label does not fit holds no
labels and is zero bytes long; C makes a zero-length sub-view of it and says
`ok=1 extent=0`. `struct_extent` refused instead, on a guard that belonged
somewhere else entirely -- the run walk, where a zero-extent element would
mean never advancing. The guard was right and its position was wrong, which
is a distinction worth keeping: **a check that stops an infinite loop is not
a check that a value is legal.**

**Status:** 332 scalars, 67 runs, 42 arms, 3 gates with 2 interiors, 19
delimited, 4 varints, 5 `while` runs, 23 nested sub-views and 7 tags. Every
step of `make check` green, each run separately.

### 26.89 `validate`, which cannot be rendered by halves

Moved to `doc/decision/26.89-validate-which-cannot-be-rendered-by-halves.md`.

### 26.90 A member named for a keyword, in four languages

Moved to `doc/decision/26.90-a-member-named-for-a-keyword-in-four-languages.md`.

### 26.91 The two things `validate` deferred, and the six bugs behind them

Moved to `doc/decision/26.91-the-two-things-validate-deferred-and-the-six-bugs-behind-them.md`.

### 26.92 The last twenty-one, and what they were really deferring over

Moved to `doc/decision/26.92-the-last-twenty-one-and-what-they-were-really-deferring-over.md`.

### 26.93 The file nothing referenced, except the tests

`packaging/situc.control` and `packaging/libsitu-dev.control` were the
hand-rolled dpkg-deb build's inputs. Debhelper replaced that build the day
after it was written and the two files sat unread for months, so removing
them was right. The removal commit says "nothing has referenced either
since", and the search behind that sentence did not include `test/`, where
`test_packaging.py` had been reading both since they were written. Eight
tests started failing with `FileNotFoundError`, and master was red until
somebody looked.

**A test is a reader like any other.** That is the whole finding, and it is
worth writing down because the search that establishes a file is dead is
exactly the search that feels complete when it has covered the build. The
build is where a *product* dependency lives; a test dependency lives
somewhere the build never looks, and it is the one that turns red.

The tests read `debian/` now, which is where the packaging actually is, and
in moving them three of the six claims changed shape rather than location:

- **The version is not substituted any more, it is in `debian/changelog`,**
  so what the test holds is the changelog against `situc.__version__`.
  `make version-check` asks the same question and *skips* where
  `dpkg-parsechangelog` is absent -- a check reporting success over nothing,
  on precisely the machines least likely to have the tool. This one parses
  the line itself and cannot skip.
- **`Depends` is a list now**, because debhelper adds `${misc:Depends}` to
  every binary package. "No comma" was the old way of saying "nothing but
  python3" and stopped meaning that; the test splits the field and ignores
  the substitution instead, which says what was meant in the first place.
- **`Architecture: any`, not `@ARCH@`.** The old build substituted the
  string; debhelper resolves it. The claim -- compiler architecture-
  independent, runtime not -- is unchanged and is the reason the packages
  are split at all.

**One claim was added rather than moved.** `Build-Depends` carries a Python
floor too, and nothing checked it against `pyproject.toml` while the two
lived in different files. A source package that builds under an interpreter
older than the code's floor fails inside `dh_auto_build`, a long way from
anything that names a version.

**And the same rule's own gate, which skipped.** `make version-check`
asked `dpkg-parsechangelog` and printed "skipped" when it was absent --
success reported over nothing, on exactly the machine least likely to have
`dpkg-dev` installed. The topmost changelog entry *is* the current version
by the format's own definition, so `sed` reads it and there is no machine
where the rule has to give up. `dpkg-parsechangelog` is still asked where it
exists and the two are compared with each other rather than one being
preferred, which is what keeps the fallback honest: a path that only ever
runs where nothing can check it is a path that is wrong the first time it
matters.

Six invocations to establish that, and one of them is worth naming. With
`dpkg-dev` unreachable -- a scratch `PATH` holding symlinks to `cat`, `sed`,
`sh` and `make` and nothing else -- the rule reads line 1 and passes. With
`VERSION` bumped it refuses either way. With line 1 malformed it refuses
*closed*, saying the changelog names no version rather than falling through
to a silent success, and a double space before the parenthesis is enough to
trigger that -- `dpkg-parsechangelog` calls the same line badly formatted,
so both readers agree it is broken.

**The disagreement branch is a guard rather than a tested path, and is
recorded as such.** No changelog both readers accept can make them differ:
where the line is well-formed they read the same token, and where it is not
`dpkg-parsechangelog` returns nothing and the rule fails on the missing
version instead. It costs one comparison, it runs on every machine with
`dpkg-dev`, and it is there for the day the fallback is edited.

`version-check` guards `deb` and is not part of `make check`, so none of
this runs in CI -- which is why the invariant is *also* a test, reading the
changelog with no tools at all. Two routes to one fact, deliberately: the
rule stops a bad package being built, and the test stops the disagreement
being committed.

**Six mutations, six caught.** Turning `situc` into `Architecture: any`,
giving `libsitu-dev` a dependency on `situc`, dropping either floor to 3.9,
disagreeing the changelog with `VERSION`, and deleting `Maintainer` -- each
was applied to the real files and each failed the suite, and the files were
restored and re-run to confirm the tree came back. A test that reads a file
in a directory that has just been reorganised is one to check has not
quietly stopped reading anything.

### 26.94 The attribute that was parsed, accepted and never checked

Moved to `doc/decision/26.94-the-attribute-that-was-parsed-accepted-and-never-checked.md`.

### 26.95 Rung 3: `relate`

Moved to `doc/decision/26.95-rung-3-relate.md`.

### 26.96 Rung 4: `frame`

Moved to `doc/decision/26.96-rung-4-frame.md`.

### 26.97 Rung 5: `converse`

Moved to `doc/decision/26.97-rung-5-converse.md`.

### 26.98 Rung 6: `drive`

Moved to `doc/decision/26.98-rung-6-drive.md`.

### 26.99 Rung 2: finishing `edit`

Moved to `doc/decision/26.99-rung-2-finishing-edit.md`.

### 26.100 The ladder, folded

Moved to `doc/decision/26.100-the-ladder-folded.md`.

### 26.101 The divergence that was recorded with the wrong side named

Moved to `doc/decision/26.101-the-divergence-that-was-recorded-with-the-wrong-side-named.md`.

### 26.102 The table nobody cleared, and the stack that was zero

Reading `situ_walk_open` on the way to the delimiter scan found the varint
commit's other half. It added three fields to `situ_walk_image` and a branch
to the section loop that fills them, and did not add them to the list of
fields cleared before the loop runs -- which was ten lines of `= NULL` and
`= 0u`, one per table. **A section the image does not carry leaves its entry
exactly as the caller's stack left it**, so an image with no varints sent
`varint_rules` binary-searching a pointer and a count nobody wrote.

Every test passed, and every test passed for the reason that makes this the
worst shape of defect: the callers declare `situ_walk_image` on the stack at
the top of `main`, where it is zero. `memset(&image, 0xAA, sizeof image)`
before the open is an immediate segfault under a sanitizer, in `u32_at`,
reached from `situ_walk_read` on udp -- a schema with no varint in it.

Two things follow, and the second is the one worth keeping.

**The fix is `*out = (situ_walk_image){0}`, not another line in the list.**
A list of what to clear is a list that goes stale the next time a table
arrives, and this is invariant 39 in its smallest form: the hand-maintained
enumeration was wrong within one commit of being right. Clearing the whole
struct cannot be.

**The differential cannot see this class at all.** Two walkers are compared
on what they answer, and this one answered correctly whenever it did not
crash -- there is no divergence to find, because uninitialised memory is not
a second opinion. What found it was reading the function, and what makes it
a regression test is the poison: every C case in the file now opens against
a struct filled with `0xAA`, so "the stack was zero" stops being available
as an accident. A test that supplies a *clean* environment tests the
environment as much as the code.

### 26.103 The delimiter scan, and the value a run does not have

0035 predicted delimited members as the next place `read_scalar` would read
bits where the walk's public answer wants values, and named the Python
walker as the one to watch. It was right about the construct and wrong about
the ownership again, in the same way and for a different reason each time.

**Both walkers answered a delimited member as a number.** Python read the
span, so `"GET "` came back as 1195725856. The C walker had no scan at all,
so it took the record's `size_bits` -- which for a delimited member is its
*delimiter's* width, the number invariant 25 names as the one that is not
the answer -- and gave 0x47, then placed the `u16` after it at offset 1
where it belongs at 4. Two implementations, two different wrong numbers, one
question neither was asking: does this construct have a value at all.

It does not, and both refuse it now. Two of the three constructs in this
pattern are settled by refusing and one by decoding, which is the shape of
the question rather than an inconsistency: a run and a delimited member have
no single value, a varint has one.

**The scan is what the C walker actually gained**: quoting, escapes, the
`max` cap, and the truncated case where the delimiter never arrives and the
member reaches as far as it got rather than becoming unplaceable. Two
binary searches over two side tables became one, which is the second
consumer arriving and the answer being private to the first -- `relation.py`
and `layers.py` one level down.

**The differential compares widths now, not only values.** A walker can
agree about every value in a struct and still disagree about the struct: the
extent is what places everything after it, and the last member's extent is
not observable through anything's offset. The scan is exactly the answer
that shape of check was missing -- before this, "they agree" meant "they
agree about the numbers each was willing to produce".

**The owned decode is where the refusal would have cost something.** Rung 2
is whole-or-nothing, so `read_scalar` refusing means every schema with a
delimited member fails to produce an owned value -- the editor's document,
for HTTP and SMTP and every text protocol. It routes a delimited member to
its content instead, which is what a backend's `_ptr` and `_len` hand back:
content and not span, the two differing by exactly the delimiter, because
the span is what places the member after it and the value is not. `[trim]`
now has one derivation serving the probe's length and the owned value's
bytes, rather than a second copy waiting to disagree.

**A bounds hole found on the way through.** `fits` checks that a table's
`count * stride` lies inside the image, which is the table -- and says
nothing about a row narrower than the fields this build reads out of one. A
hostile image declaring a four-byte varint stride would have had
`situ_walk_varint` read past the last row. Each table this build understands
now states how far into a row it reads, checked when the section is bound. A
stride *wider* than expected stays fine, because that is how the format
grows.

### 26.104 The constraint cpio's own comment defends, checked by nobody

Moved to `doc/decision/26.104-the-constraint-cpio-s-own-comment-defends-checked-by-nobody.md`.

### 26.105 The other form, and the schema that had to be written for it

Moved to `doc/decision/26.105-the-other-form-and-the-schema-that-had-to-be-written-for-it.md`.

### 26.106 The last construct, and the width its values could not show

Text numbers were the fourth and last construct in the pattern 0035 named
when `read_scalar` first answered a byte run as an integer, and the C walker
parses both forms now. The pattern's four instances settle two ways, and the
split is the question rather than an inconsistency: **a byte run and a
delimited member have no single value and are refused; a varint and a text
number have one and are decoded.**

**The refusal this replaces was true for a false reason.** `decimal u32 n[4]`
is one number in four digits, and the C walker declined it through the branch
that refuses a run -- because `array_count` is 4. The caller got the answer
it should have got, from a premise about the construct that is wrong, which
is invariant 18's other half: a real gap has to be attributed to whatever
actually causes it. `traverse.classify` puts the text-number branch before
the array branch for exactly this reason and has since three backends read
the bracket as a count; the walker now does the same.

**Writing it found a width bug that no value differential could show.**
`size_code` is set on a fixed-width text number, so C's sized-run branch read
`[4]` as four 32-bit elements and answered *sixteen bytes* for a four-byte
member -- the same arithmetic that once put `edges`' `text_driver` tail
twelve bytes past every backend, in the Python walker, and the reason
`walk.py` carries that branch with a comment.

Why it hid is the part worth keeping. **The layout solver hands a member
after a fixed-width text number a constant offset**, because the width is
known at compile time -- so `situ_walk_offset_bits` returns that constant and
never sums the wrong width. The struct was measured four times too long and
every value in it still read correctly. Only the extent was wrong, and only
a check that asks for extents could see it: the width differential added one
commit earlier, for a reason that turned out to be this one.

One divergence is left and is written down rather than waiting to be found:
the C parse accumulates into `uint64_t` and refuses on overflow, Python's has
arbitrary precision and does not. cpio's widest text number is eight digits,
so the two agree everywhere the tree can ask.

### 26.107 The member that says where it is

`at expr` is the fifth row of 0035's table and the smallest: a member that
joins no offset chain, because it states its own offset. The Python walk has
evaluated one since it had a VM; the C walker refused it by name, which was
honest and is now unnecessary -- the program is the answer, the VM was
already there, and the summing loop had known to skip a located member when
measuring what comes before it since it was written.

**The test uses two messages, and that is the whole of its design.** A
located member whose expression happens to land where the chain would have
put it proves nothing, because both readings agree there. `where = 4` places
the `u16` at 4; `where = 2` places it at 2, overlapping the member before
it. That overlap is deliberate: `at` is what a format uses when a header
declares an offset, and nothing says the regions it names are disjoint --
bmp's `pixels at file.pixel_offset` is the case the tree already has.

It is the same lesson as 26.101's `96 01`, in the smaller form it takes when
you are writing the check rather than reading somebody's conclusion: choose
the input on which the readings differ.

### 26.108 The run a device could measure and not read

Moved to `doc/decision/26.108-the-run-a-device-could-measure-and-not-read.md`.

### 26.109 The loop in the arena, and the bound that had a public entrance

A `while` run's extent is the one thing in the C walker that is a loop, and
0026's argument for shipping an evaluator to a device does not cover it: the
VM is safe because section 10's language is total, and a run walk is not an
expression. So its termination is argued separately and written in the
source, four ways, two of which do not depend on the message at all -- the
cap, and the frame, the latter holding because every iteration advances by at
least one byte. An adversary choosing every byte cannot make it run longer
than the buffer.

**Recursion needed a decision rather than a guard.** A run's extent is the
sum of its elements', and an element is a struct that may hold another run:
mutual recursion through `size_bits`, the walk and `struct_extent`, bounded
in principle by the schema's nesting and in practice by nothing, since the
schema arrives in an image at run time. On a device the stack is the arena's
neighbour, so the depth is a constant -- eight, against a corpus whose
deepest nesting is three.

**The first version of that bound had a public entrance.** The depth was
threaded through the measurement and *not* through `situ_walk_offset_bits`,
`situ_walk_read` or the evaluator's load callback -- each of which starts at
zero, and each of which the measurement calls. An expression names a field;
reading that field sums the members before it; one of those may be a run.
Two structs referring to each other through that path would have recursed
freely past a limit that looked enforced. A bound with a public entry point
that restarts the count is not a bound, and this one read as complete.

**Reaching the limit is a refusal, not a short answer.** The first version
absorbed it into "the run ends here", which is the mistake this component
exists not to make: an extent that stops early reads exactly like one that is
right. A ten-deep schema now answers `refused` for the run and nothing else,
where before it answered a length. `SITU_WALK_UNSUPPORTED` propagates; a
frame that runs out still counts the elements that fit, those being different
questions.

**And the harness was measuring the wrong struct.** Every C differential
walked struct 0, which is whichever the packer put first -- `beat`, not
`beats`, so the container holding the run was never the thing under test. The
drivers take the struct as an argument now. A check that always asks about
the first of something is one nobody has pointed at anything.

### 26.110 The variant, and the fifth thing that is not a number

A variant's extent is a switch -- read the discriminant, take the arm it
names, answer that arm's size -- and the C walker measures one now. Not the
minimum, which made a dnsname label one byte long and walked thirty-nine of
them through a thirty-eight byte buffer; not the worst case; and nought
bytes rather than a refusal where the discriminant matches nothing, because
that is a malformed message and saying so is `validate`'s job.

The arms table is the first in the image that is not one row per placement:
a variant owns several contiguous rows, so the search lands on one and walks
back to the first.

**The differential found the pattern's fifth instance, and this time the
Python walker had it.** The two agreed about every extent on the first run --
two bytes for the small arm, eight for the large, nought for an unmatched
discriminant, with `tail` correctly placed in each -- and disagreed about the
*value*: `read_scalar` read the selected arm's bytes as an integer, so a
two-byte arm came back as 43707 and an eight-byte one as 4822678189205111.

A variant is a shape the discriminant chooses, not a number. The arm is what
holds a value and has its own placement to be read through. That makes the
tally five constructs and the split three-to-two: a byte run, a delimited
member and a variant have no single value; a varint and a text number have
one.

**The schema omits `[equalize]` on purpose**, and the reason generalises.
Padding every arm to the largest is what buys back the offset axis for
everything after a variant -- and it would also hide a walk that picked the
wrong arm, because every answer would then be right by construction. A test
whose construct is uniform cannot tell a correct selection from any other.

### 26.111 `validate` in C, and the difference between two refusals

The walker could say where every member of a message is and what it holds,
and not whether the message was a legal instance of the schema. Anything
asking that still needed Python -- which is the dependency 0026 exists to
remove, so this is the row that changes what the C walker *is* rather than
what it can reach.

**Whole or nothing had to be built rather than ported.** Every other probe
renders per member and skips what it cannot do, each being a separate line.
`validate` is one verdict about a whole struct, so a partial one reports OK
for the rules it happened to be given -- the one wrong answer that cannot be
told from a right one. Two refusals of the *struct* follow: the image's
per-struct bit saying it carries every check, and any kind of check this
build does not render.

**The return value and the verdict are different questions**, and keeping
them apart is the whole of the API's shape here. The verdict is about the
message: OK, BOUNDS, CONSTRAINT. The return is about the walker: can it
answer at all. Folding them together is exactly how "this build cannot say"
becomes "well-formed", and it is the same distinction 26.108 found in the
signed flag -- a caller cannot ask what the callee does not say.

Two things the differential settled while it was being written. A **nested
struct** is `validate` called through with the inner verdict returned as it
stands rather than folded into CONSTRAINT; the first version refused nested
structs wholesale, which would have left most real schemas unanswerable for
a reason that was mine rather than the format's. And **the frame check
belongs at the top** -- section 20.2's check, which the Python walk does
when it acquires a view. Without it the two agreed about every well-formed
message and disagreed about short ones, C answering for the members that
happened to fit. That the disagreement appeared only on malformed input is
the point: the shapes a validator exists for are the ones a happy-path test
never reaches.

Sixteen cases across four shapes -- constraints, enum membership, a nested
struct's own rules, a text number's range -- each with a well-formed
message, a rule broken, and a frame too short. `[since]` members, the span
checks and gated regions are refused by name and are what remains.

### 26.112 The span checks, and `remaining` measured from the wrong end

Moved to `doc/decision/26.112-the-span-checks-and-remaining-measured-from-the-wrong-end.md`.

### 26.113 The walker, folded

Moved to `doc/decision/26.113-the-walker-folded.md`.

### 26.114 The ladder nobody had run

The six rungs were built, in four backends, and **no schema in the repository
declared a relation.** Rungs 3 to 6 existed only in the fixtures inside
`test/unit/test_relations.py`, which put them outside every mechanism section
22 calls the highest-value coverage: the example sweep, the generated cmocka
suites, the fuzz harnesses and the four-backend differential had none of them
ever been pointed at one. `example/dns` carries the exchange now -- RFC 1035
makes ID and OPCODE the match and asks for a retry interval of two to five
seconds, so none of it is invented -- and the capability map gained
`layers: floor=view reach=drive`, which makes a schema's reach up the ladder a
reviewable line rather than a property nobody would notice changing.

**Including a header is what compiles it.** `<name>_frame.h` had never been
compiled by anything, and the moment the checks suite included it, it did not
compile: `framed_structs` returned every struct that was not a register, while
`traverse.frameable` answers whether a whole one can be recognised in a prefix
of a stream. Four structs in `test/schema/edges.situ` got a reader built out
of a `_required` only a frameable struct has, and a variable-extent view was
passed three arguments where it takes four. Both had shipped. This is the third
time this repository has paid for "generating is not compiling", after the
`edges.situ` C++ gap and the fuzz harness that named a macro only fixed structs
have.

**A check that hangs is worse than one that fails.** The reassembly check
looped while the reader kept answering `SITU_OK`, which against a reader that
always does is not a loop that ends. It was found by breaking the reader rather
than by reading the code: the mutation that should have failed in a second ran
until it was killed. CI reports that as a timeout with no name on it. The loop
is bounded by the answer now -- three messages cannot arrive from a
two-message stream, so seeing a third is the failure.

**Five probes proved nothing on their first attempt**, and every one returned a
clean result that read like evidence. A venv built to stand in for a machine
without the oracle libraries also lacked pytest, so it reported 419 errors
rather than the ten under test. A regex meant to break a C++ predicate named a
parameter `reply` where the schema calls it `response`. Two harnesses picked
the first `main.rs` or `main.cpp` in a file rather than the one that mentions
the thing under test, and ran a program that could not have failed. One import
named `situ` where the package is `situc`. Each was caught only because a
specific failure was expected and did not arrive -- which is the argument for
predicting the failure before running the probe, rather than reading whatever
comes back as confirmation. The mutation harnesses select by content and assert
that the mutation landed.

**What is not covered, and why it is not a gap.** Rung 6 has no schema-derived
suite: `drive` owns I/O, and a cmocka suite built out of buffers cannot execute
it. It is driven from `test/unit/test_relations.py` in three backends instead,
each given its clock as a parameter. The generated build stops at `converse`
and says so, because a boundary that is not written down is read as an
oversight by whoever finds it next.

### 26.115 One keyword doing two jobs, and the cast that lost a comparison

Moved to `doc/decision/26.115-one-keyword-doing-two-jobs-and-the-cast-that-lost-a-comparison.md`.

### 26.116 A struct named `protected`, and the keyword nobody checks

Moved to `doc/decision/26.116-a-struct-named-protected-and-the-keyword-nobody-checks.md`.

### 26.117 The attribute nobody declared, and the position nobody tried

Moved to `doc/decision/26.117-the-attribute-nobody-declared-and-the-position-nobody-tried.md`.

### 26.118 Three checks that could not have failed

Moved to `doc/decision/26.118-three-checks-that-could-not-have-failed.md`.

### 26.119 The floor check that skipped where the floor is broken

Moved to `doc/decision/26.119-the-floor-check-that-skipped-where-the-floor-is-broken.md`.

### 26.120 The standard that was requested and never enforced

Moved to `doc/decision/26.120-the-standard-that-was-requested-and-never-enforced.md`.

### 26.121 The floor the generated code was never held to

Finishing what 26.119 started, and the sweep it belongs to. Four backends
declare a language version in section 22 -- C11, C++17, Python 3.11+, Rust
2021 -- and by the end of 26.120 two of them were enforced and one, Rust, had
been all along: every `rustc` invocation in the tree passes
`--edition 2021`. Python was the one left, and it was unenforced twice over.

**The shipped modules nothing held to the floor.** Both floor checks read
`bin/situc`, `situc/**` and `tool/*`. That leaves out `runtime/python`,
`walker` and `editor` -- and the Makefile records `mypy` having exactly those
three omissions and fixing them one at a time, in a comment that is worth
reading in full because it names the shape:

    The shipped Python runtime is checked too, and was not: `mypy situc
    tools tests` reads the compiler and its suite, and `runtime/python` is
    neither -- so the module every generated module imports was the one
    nothing checked.

`walker` was added "for the same reason", and `editor` "named on arrival
rather than after the same lesson a third time". The floor check never got
that widening, so the module every generated module imports was held to no
declared version at all. That is invariant 151.

**And the generated code, which is the claim's actual subject.** "Python
(3.11+)" is a promise about *output* -- the modules a user runs -- and
nothing asked them anything. Thirty-three modules and some nineteen thousand
lines, verified only by being imported on whatever interpreter is present,
which here is 3.13. The backend could emit 3.12 grammar and every test in the
repository would pass.

**Both measure clean, and that is the argument for doing it now.** Ninety-one
shipped modules and thirty-three generated ones, zero findings between them.
A check added while a tree is clean costs one commit; the same check added
after a violation lands costs the violation as well, and 26.119's incident
ran six phases.

**One list, in one place.** `test/unit/python_floor.py` holds the floor, the
module list and the detector, because three checks now ask the same question
and the way this goes wrong is that they stop agreeing -- which is precisely
what happened between `mypy` and the floor check. Same rule as
`every_schema.py` one directory over, and the same reason.

**Two instruments, because each is blind where the other sees.**
`ast.parse(feature_version=...)` catches grammar added since the floor and
misses everything tokenizer-level; the PEP 701 scan catches exactly the
tokenizer-level f-string changes and nothing else. `below_floor` runs both,
and both now have controls: PEP 695 type parameters must be caught, `match`
must not, since it arrived in 3.10 and is *below* the floor. A check keyed on
"looks modern" fails that one.

**Mutation-tested at both ends rather than trusted.** A same-quote f-string
appended to `runtime/python/situ_runtime.py` fails the widened check by file
and line -- which is the proof the three added trees are read rather than
merely listed. The same construct appended to what the Python backend emits
fails all thirty-three generated modules.

### 26.122 The tag whose width meant nothing

Decision 0038, accepted and built. `example/packet` declared
`codec aes_gcm_128 { authenticated; length_preserving; ... }` and `tag u8[16]`
beside it, and nothing related the sixteen to the codec. `tag u8[1]` compiled,
and so did `tag u8[64]`.

**The framing in 14.8 was not quite right, and the difference decided the
design.** That section says the codec vocabulary has nothing about size.
`expansion` carries a number -- it says how a region's own extent changes under
the transform. Where a codec appends its overhead, `expansion = +16` *is* the
tag; where the tag is a separate field, as in `packet`, the codec is
length-preserving, expansion is zero, and it says nothing about the sixteen
bytes beside it. So an inference from expansion would have been right for one
spelling and silently wrong for the other, which is why the sizes are declared
rather than derived.

`std/codecs.situ` had a comment saying exactly this and drawing the opposite
conclusion: "the tag is a separate field in the schema, which is why these
signatures do not carry its size". The observation was right and the "which is
why" did not follow.

**Truncation is the reason this is a check rather than a ban.** OSCORE uses an
eight-byte tag on constrained links, so a rule refusing narrow tags would
refuse a real protocol rather than a mistake. `[truncated]` is the author
saying the loss is intended, and it excuses a narrower tag only: a tag *wider*
than its codec produces is refused with no exemption, since no spelling gets
more authentication out of a primitive than it produces. A nonce mismatch is
refused either way -- a nonce is an input rather than a result, so a narrow one
is not a truncation of anything, it is a different nonce.

**What was deliberately left out is the part worth recording.** `key_bits` is
not added. No construct in this language names a key, so the setting would have
had nothing in any schema to check against -- which is precisely the shape
`[nonce]`, `[trusted]` and `[covers]` had, and which 26.60 and 26.117 spent two
passes removing. Adding a fourth would have re-created the defect those passes
existed to delete. The width goes in when the construct that gives it something
to check does.

That constraint also shaped the rest: both settings are optional, because an
extern codec's implementation belongs to somebody else and an author who does
not know its tag width must still be able to declare the codec. Silence claims
nothing, which is the rule the codec accumulator already stated for a
declaration the compiler cannot verify.

**Units are in the names.** `tag_bytes`, not `tag`. `expansion = +16` carries
no unit and gets away with it because expansion is only ever bytes; a tag and a
nonce are bytes while a key is conventionally quoted in bits, so the moment a
third setting arrives two units are in play. A unit in the name cannot drift
from one in a comment.

**Every committed schema already agreed, which is what the check is worth.**
All 33 parse unchanged with the widths declared: the four authenticated codecs
-- `aes_gcm_128` and `chacha20_poly1305` in `std/codecs.situ`, `aes_gcm_256` in
`example/keystore`, and `packet`'s own -- carry a 16-byte tag and a 12-byte
nonce, from NIST SP 800-38D and RFC 8439 rather than from this compiler. The
check confirms what the schemas said rather than correcting them, and narrowing
`packet`'s tag to eight bytes now fails by file and line.

**Attributes: forty-nine now**, with `[truncated]` placed on a tag or checksum
and nowhere else, named in the table on arrival rather than after a sweep finds
it unplaced. Thirty-two are placed by the table, seven by checks of their own,
four are refused as unimplemented, and six remain unplaced.

**What 0038 does not settle**, and says so: key selection -- DTLS's epoch,
QUIC's key-phase bit, WireGuard's receiver index -- and conversation keys wider
than 64 bits. Both are named in 14.8, both are unblocked by this, and neither
follows from it. Each needs its own record.

### 26.123 The attribute that was parsed for eleven phases and read by none

Decision 0039, accepted and built. `[size = N]` had been in
`parser.ATTRIBUTE_NAMES` since the layout chapter and read by nothing;
26.60 found it because the *advisor* was recommending it, refused it with
`UNIMPLEMENTED_ATTRS`, and left the construct undefined. That was the honest
holding position and not an answer, and it stayed the answer for two more
sessions.

**What the language could not say.** A fixed-footprint buffer whose meaningful
content is a prefix of it. `u8 body[used]` keeps the relationship and costs
every member after it its absolute offset; `u8 body[64]` keeps the offsets and
forgets `used` entirely, so nothing checks `used <= 64` and the schema has
stopped describing the format it started with. The author had to pick which
half to throw away, and nothing recorded which.

**The implementation was smaller than the design suggested, for one reason.**
`data_sized` -- the shared decision about whether a member's extent comes from
the data -- asks about `array_count` and `sized_by`, and never about the
extent interval. So pinning the *footprint* while leaving those alone meant
every accessor in four backends kept working unchanged: the length machinery
was already reading the right thing. The layout gains `pinned_bits`, the
extent becomes `Fixed(N)`, and members after it get their offsets back.

**The differential earned its cost twice in one sitting.** Adding a pinned
struct to `edges.situ` -- the file that exists to hold constructs no worked
example has -- broke two tests immediately, and both were real:

- **The four backends disagreed about a 28-byte buffer.** `validate` had the
  pin check and the *accessor* did not, so a declared length of 127 inside a
  16-byte pin came back as whatever each backend's own clamp allowed: 18 from
  C, 91 from the walker. Both were wrong; the answer is 16. The clamp went
  into `_length_expression` in each backend rather than at the accessor
  sites, because there are four such sites per backend and the site that
  matters is the one that gets missed.
- **`validate` then needed the raw value back.** Clamping inside the shared
  expression made the pin check read `min(declared, 16) > 16`, which is never
  true -- a check that cannot fail, one commit after 26.118 spent a section on
  exactly that. `_raw_length_expression` is the unclamped one and the check
  asks for it by name.

**And the walker cost an invariant.** The Python walker needed the same clamp,
and `size_max_bits` already held the pin -- so clamping every length to its
maximum looked like the same rule stated more generally. It broke `arp` and
`ble` at once, because the compiled backends clamp to what is left in the view
rather than to a declared bound. A `PINNED` flag in the image tells a pin from
an ordinary maximum, and it uses a spare bit rather than a new field, so the
format version did not move. That is invariant 152.

The C walker got the same clamp without a test demanding it. It did not parse
`size_max_bits` at all, and nothing exercised the pinned struct through it
yet -- which is exactly the shape of invariant 145, a lesson recorded in one
implementation being invisible to the next. Written in rather than waited for.

**What the construct now costs a schema that uses it.** `edges.situ` carries
one: `u8 body[used] [size = 16]` with `used [max = 16]`, and
`require absolute_static(pinned.trailer)` holds -- which it could not have
before. `validate` refuses a `used` above sixteen in all four backends and
both walkers, and the accessor hands back at most sixteen bytes whatever the
message claims.

### 26.124 An argument parser, as an evaluation of the text machinery

Moved to `doc/decision/26.124-an-argument-parser-as-an-evaluation-of-the-text-machinery.md`.

### 26.125 The bound that was stated once and readable nowhere

Moved to `doc/decision/26.125-the-bound-that-was-stated-once-and-readable-nowhere.md`.

### 26.126 The two defects the argv exercise left behind

Both were found by 26.124 and both were the silently-nothing shape; neither
was the use case's problem. Fixed as refusals rather than features, which is
the smaller honest move in each case, and each refusal names the construct
that would implement it properly if a schema ever needs one.

**A string in a run condition was accepted, and the C did not compile.**
`while (text != "--")` passed the front end; the C backend emitted the
comparison against the string literal's address, through a `_get` accessor a
delimited member does not have. gcc's own diagnostic named the function the
condition language never wired in -- "did you mean `situ_arg_text_eq`?" --
and no committed schema compares text in a condition, so the per-schema
compile gate could never have fired: invariant 92's blind spot, exactly
where 26.118 found it for the Lua operators.

The refusal closes both halves at the same door. A string literal anywhere
in a `while` predicate is refused by name, and so is a reference to a
delimited or array member -- `_find_member` used to confirm the field
existed and nothing asked whether it carries a *value*, which is 26.113's
rule ("does this construct have a value at all?") arriving in the condition
language. The dnsname shape -- integer fields against numbers -- is the
control, untouched.

**`at` on a variant arm member was accepted and ignored by all four
backends.** The layout recorded `located='0'` faithfully; every backend's
arm path then resolved the offset by summing what precedes the member and
consulted nothing else -- C hard-coded `offset = 1u` with the expression
nowhere in it. A schema saying "the positional's text starts at 0, dispatch
byte included" parsed, did nothing, and said nothing about doing nothing.

Refused rather than implemented, deliberately: an arm member re-addressed
over its own discriminant is *overlap*, and overlap is a canonicity
question -- two names for one byte is 14.5's malleability concern in
miniature -- that deserves a decision record, not an emitter patch. The
refusal converts four silent wrongs into one loud one, and implementing
later is additive. A top-level `located` member is a working construct and
the control holds it untouched.

### 26.127 The refusal the survey promised

Moved to `doc/decision/26.127-the-refusal-the-survey-promised.md`.

### 26.128 The field that says which key

Decision 0040, accepted and built. `sealed(codec, nonce = n, key = epoch)`
names the field whose value selects the region's key -- the construct 14.8
listed first among what real protocols need and situ could not say, and
0038 unblocked without settling.

**Sized by what the nonce argument turned out to be.** 26.127's reading --
`nonce = field` is consumed by `wellformed` alone -- fixed the honest scope
before any design: checks plus sayability, no ABI change, no backend
touched. The selector gets the nonce's ordering rule for the nonce's reason
(the key is picked before anything it decrypts is decoded), must carry a
value (26.113's rule, so QUIC's key-phase bit passes and a byte run does
not), and reaches no committed artifact yet -- whether nonce and key belong
in the capability map and wire signature is 26.117's open question,
answered once when it is answered.

**The relaxation 26.127 anticipated does not happen, and the record says
why rather than leaving the change of mind implicit.** Two regions naming
one selector field see one value, so one key; two regions naming different
fields can still hold one value. Distinctness of key is not a structural
fact, and the nonce-reuse refusal stays unconditional. DTLS's rekey case --
old-key and new-key records in one datagram -- is two messages, which situ
already frames as two structs.

**No coverage requirement on the selector, because a real protocol refuses
it.** DTLS authenticates its epoch and QUIC its key-phase bit, and
requiring that would have been the obvious rule -- but WireGuard's receiver
index is deliberately outside the AEAD: it routes, it is not trusted.
Where the selector sits is the layout's to record; whether an
unauthenticated selector is acceptable is the protocol's threat model.

**The vocabulary gap came out in the same pass.** `sealed(ae, wibble = x)`
was accepted with the argument read by nothing -- the acceptance 26.117
closed for attributes, one construct over. A sealed region's arguments are
`nonce` and `key` now, and anything else is refused naming both.

**`example/dtls` is the worked case, and its nonce is the reason it
earns the directory.** RFC 5288 builds the 12-byte GCM nonce as a 4-byte
out-of-band salt concatenated with the 8-byte explicit part on the wire.
`nonce = explicit` names the on-wire half; declaring `nonce_bytes = 12` on
the codec would refuse the 8-byte field, and declaring 8 would misstate
the primitive. The codec stays silent on the width -- 0038's "silence
claims nothing", met by the protocol that shows why the setting had to be
optional.

### 26.129 The signature keeps only kept promises

Decision 0041, accepted and built. The wire signature's charter -- "every
line here is a promise to a receiver that is already deployed and cannot be
recompiled" -- is enforced in both directions now: the four attributes 26.60
held out of the placement table have places, and the two enforced
associations 0040 left unrecorded are on the region lines they describe.

**The four placements, by 26.60's method.** `trim` and `case_insensitive`
sit on a delimited member, where text is scanned and compared;
`nul_terminated` on a counted byte array, where something else already
decides the extent; `must_be_zero` on a `reserved` member, where saying the
default out loud stays legal and not meaningless. Everywhere else each was
inert in every backend while the signature carried the fact. The unplaced
set closes at `secret` and `non_canonical`, both genuinely read in any
position -- the table 26.60 opened is finished.

**The sweep found tcp's defect three times, not once.** 0041's draft named
`tcp_pseudo_header.zero [must_be_zero]` -- a wire line promising a check the
generated `validate` never made. Refusing the placement then caught
`udp_pseudo_header.zero` and edges' versioned `pad` carrying the same false
claim. All three now say `[must_eq = 0]`, which `validate` enforces, and
their signatures changed from an unenforced fact to an enforced one -- the
wire diff a reviewer should want to see, three times over.

**`nonce=` and `key=` are on the sealed region's signature line**, carried
through a `Placement` field the way `sized_by` is, because which field
seeds the nonce and which selects the key is what those bytes mean and a
receiver that disagrees about either cannot interoperate. packet and
keystore gained `nonce=nonce`; dtls gained `nonce=explicit key=epoch`. The
capability map is untouched: it prices capability, and neither association
changes an axis.

**Enforcement has a measurable price, and the suite billed it at once.**
The moment `[must_eq = 0]` was real, the owned round-trip for tcp and udp
stopped reaching its accept path -- a pseudo-random byte is zero once in
256 draws, and 64 draws were 64 refusals, so the suite skipped with "needs
committed vectors" exactly as it did for packet and tiff. Both have
vectors now, laid out from RFC 9293's and RFC 768's field lists over
TEST-NET addresses rather than by situ. A check that begins to bite is a
corpus that stops arriving by accident; the vectors are what keep the
accept path exercised on purpose.

**In passing: the singular-directory pass had two stragglers.** The suite
did not collect on master -- two `sys.path` inserts still said `tools/`, so
`import style_gate` failed before a single test ran. The pass's own claim
was "each project's gates run against the result", and the gate that would
have said otherwise was the one broken. Fixed here because nothing lands
over a suite that cannot collect.

### 26.130 The key that was never allowed to be wrong, made wider

Moved to `doc/decision/26.130-the-key-that-was-never-allowed-to-be-wrong-made-wider.md`.

### 26.131 Three answers to the consumers

Moved to `doc/decision/26.131-three-answers-to-the-consumers.md`.

### 26.132 The floor stated, and the clamp that was checked by nothing

Moved to `doc/decision/26.132-the-floor-stated-and-the-clamp-that-was-checked-by-nothing.md`.

### 26.133 The session, folded

26.117 through 26.132 are one session's log, and a reader should not have to
read sixteen entries to learn what it settled. Decisions 0038 through 0044
and invariants 146 through 153 carry the durable part; this names the shape.

**One thing kept happening, and it is the session's whole lesson: a check
that could not fail, or a claim that was never measured, reads exactly like
one that works.** Nearly every finding was that shape.

- **The version-claim family** (118, 119, 120, 121; invariants 147-151). A
  language version is declared and the check meant to enforce it cannot.
  `luac` is 5.4 and cannot refuse the Lua 5.2 decision 0021 protects; the
  Python floor check runs a real 3.11 and skips on the 3.13 the code is
  written on; `-std=c++17` selects a dialect and `-pedantic-errors` is what
  makes it binding, so the C++ backend emitted a compound literal for
  years; and the generated Python was held to no floor at all. Four
  versions, one mistake: selecting a standard is not conforming to one, and
  a guard that skips where the fault is produced has not run.
- **The crypto survey's promises** (122, 127, 128, 130; decisions 0038,
  0040, 0042). 14.8 listed the constructs real protocols need and situ
  could not express, and the list is empty now -- codec tag and nonce sizes
  (0038), key selection (0040), conversation keys wider than a word (0042).
  On the way, the survey's own flagship claim -- "one nonce field feeding
  two sealed regions is refused" -- turned out never to have been
  implemented (127, invariant 153): a claim inside a measured survey is not
  itself measured. Each construct's refusal, kept or added, rested on the
  same argument: an exact bytes comparison, never a digest, because a
  collision in the pairing layer is a silently wrong pairing.
- **The wire signature made honest in both directions** (129, 131;
  decision 0041). A line a deployed receiver relies on is a promise, so a
  recorded-but-unenforced fact is a false one -- `[trim]` on a plain `u8`
  in the signature over bytes nothing trims. The four held attributes got
  places, the enforced associations got recorded, and the sweep found the
  same false-claim shape in three committed schemas' `must_be_zero`.
- **The consumers, answered** (124, 125, 126, 131, 132). The `suggestion/`
  directory was the backlog: six evaluations from the other private
  projects, asks with reproductions attached. The argv exercise (124) was
  an instrument rather than a use case -- it found two silently-nothing
  defects and stated where situ stops for a grammar -- and its one
  buildable idea became value-domain constants (125). fuzznet's timing
  division, netcfgd's version contract and `gen-tamper`, apt-emerge's
  generator-mutation check, beerssh's floor, hydra's four: each answered in
  the file that asked, because the directory is correspondence.

**What is settled and what is proposed.** 0038 through 0042 are accepted and
built; 0043 (`pad_to`) and 0044 (utf16 encoding) are proposed, each because
it carries a wire-visible fork a consumer raised and did not have to resolve.
The attribute placement table (opened at 26.60) is closed: every attribute
is placed, read everywhere, or refused. 14.8's unexpressible list is empty.

**The instrument that earned its keep, again.** The four-backend differential
and the mutation-tested checks are what caught the wrong diagnoses -- a
paren-balanced clamp drop that a first check could not fail, a byte-run guard
that fired on a record run and did not compile, a constant that fit the
buffer it was checked against. The session's method was the session's
subject: a check is not evidence until it has been watched failing.

### 26.134 pad_to, built across the stack

Decision 0043, accepted and built: `pad_to(n)` pads to the next multiple of
n bytes, the construct section 8.4 specified and the parser had always
refused. `base::Pickle`, cpio and most RPC framings write it by hand as
`reserved u8 [align_up(length, 4) - length];`; it is one member now, the map
labels it `<pad>` rather than `<reserved0>`, and the wire signature carries
`pad-to=n` because a peer padding to a different multiple disagrees about
where the next field starts.

**The size is the solver's, not the schema's.** A static pad folds to a
constant; a dynamic one -- after a variable run -- is `align_up(offset, n) -
offset`, computed by the same offset function every member after a variable
one already gets. The reserved must_be_zero validation is reused wholesale:
padding is `reserved` bytes with a computed length, so the only new emission
is that a pad advances the running offset to a multiple rather than by a
fixed size. `situ_align_up_u32` joins `situ_advance_u32` in each runtime, the
image gains a `pad_to` field (format version 3), and both walkers align the
same way.

**The four-backend differential earned its keep, and the bug it caught is the
lesson.** A pad added to `edges.situ` surfaced a validation disagreement --
and it was not the pre-existing struct it first appeared to be, but the pad
itself: with `u8 n` where `1 + n` is already a multiple of four, the pad is
*zero* bytes, and Rust's fast path for a one-byte reserved read checked a
byte the empty pad does not own. C, C++ and Python routed the pad through
their run-validation and agreed; Rust's scalar shortcut was the odd one out.
The pad is a *run* of 0..n-1 bytes and classifies as one now, so Rust takes
the same path as the others.

**Its own schema file, not a case in edges.** Adding the pad to `edges.situ`
changed that schema's pseudo-random draws and the failure first read as a
bug in an unrelated struct -- a false lead that cost a diagnosis. A dedicated
`test/schema/padded.situ` draws its buffers against exactly the pad shapes,
including the `u8`-driven zero-pad case on purpose rather than by a lucky
draw, which is what makes the differential's pass mean something.

### 26.135 utf16, and the byte view a wide run never had

Decision 0044, accepted and built: `[encoding = utf16le]` and
`[encoding = utf16be]` join `ascii` and `utf8`. `base::Pickle` writes a
UTF-16 string as `u32 length; u16 data[length]`, and the count is the point
-- for UTF-16 it counts *code units*, not bytes, and nothing on the wire says
which, which is why the consumer's hand-written reader carries an overflow
guard. The attribute states that `length` is code units, which no other
construct says. **Bare `utf16` is refused**: a text encoding's byte order is
a property of the text, not inherited from the field's `endian` scope, and a
default would be a guess a byte-swapped string passes silently. The strict
validator refuses a lone surrogate the way the utf8 check refuses an overlong
form (8.8) -- decoded through each language's own UTF-16 decoder rather than a
hand-rolled state machine, the same way utf8 already was.

**The real work was not the encoding -- it was that a `u16` run had no byte
view.** Every backend emits a byte-span accessor only for a `u8` run; a
`u16[length]` run is ValueConverted, read through an indexed getter, with no
pointer to hand a validator. So the encoding check reads the message bytes at
the run's static offset directly -- `view.base + offset` with a byte length
of `count * 2` (a literal for a fixed run, the same length expression every
member after a variable one already computes) -- rather than through an
accessor that does not exist. The image carries the encoding as a code
(`pack.ENCODING_CODE`: ascii 0, utf8 1, utf16le 2, utf16be 3) that both
walkers read, and the C walker keeps its own standalone `utf16_valid` for the
reason it keeps its own `utf8_valid`: it links nothing.

**Two gaps the first wide run in an example surfaced, both older than this
record.** `pad_to` had no `unparse` case -- `padded.situ` lives in
`test/schema/`, which the round-trip test does not cover, so the pickle
example was the first to round-trip a `Pad`. And a ValueConverted run that is
unwritable said nothing about why: three backends keyed their "no setter"
note on the byte-span accessor a wide run does not have, so a `u16` run that
was the *only* unwritable member in a schema left the explanation empty. Both
are the shape 26.35 names -- an absent operation that does not say why it is
absent -- and both were invisible until a schema exercised them.

**The example is the Pickle string itself.** `example/pickle/pickle.situ` is
`u32 length; u16 data[length] [encoding = utf16le]`, drawn against by the
four-backend differential and both walkers: random `u16` draws are full of
lone surrogates, so the pass means all six agree on which byte sequences are
UTF-16 and which are not.

### 26.136 Nine drivers, and the five bugs they found

Decision 0033's driver axis is built across all four backends: `epoll`,
`poll`, `select`, `ppoll`, `blocking` and `io_uring` in C, `qt` in C++,
`asyncio` in Python, `tokio` in Rust. The core did not learn a single new
fact -- each is an additive artifact over `--layer drive`, refused on a
backend it is not available for, naming both. That is the axis working as
designed, and it is the least interesting thing here.

**The completion shape was proven rather than assumed.** 0033 made the
vtable completion-shaped on the argument that a readiness loop implements
completion trivially while the reverse is impossible, and named `io_uring`
as the driver that would test it. It does: the same submit vtable and the
same `step`/`on_message` machine drive it with no change to the core. Its
raw ring handling depends only on the kernel uapi header, so it compiles and
runs where `liburing` is not installed, which is what let it be verified at
all.

**A host runtime may return its outcome, and the first one could not.** Qt
delivers completion by callback, which read at the time as what a host
runtime does. It is not: it is what *Qt* does, a C++ function that installs
a notifier having no way to return something that has not happened. A
coroutine and an `async fn` are precisely the runtime's way of saying
"finishes later and yields a value", so asyncio and tokio return theirs.
Only the per-event reports stay callbacks in all three, because no single
return value can carry events that happen *during* an exchange.

**Five pre-existing bugs fell out, and not one was found by the suite.**
Every one surfaced because something finally consumed the layer:

- Qt defines `slots` as an empty macro, and the C++ drive and converse rungs
  emitted a constructor taking `slot *slots`. In a Qt translation unit the
  parameter name is deleted and `slots_(slots)` becomes `slots_()`, a null
  the constructor writes through. It compiled clean under `-Wall -Wextra`
  and segfaulted at construction. Live for every Qt consumer of those rungs,
  which is three of these projects, and it had never fired because nothing
  here built C++ against Qt.
- Rust's `step` dropped the expiry count on the call that gives up on the
  last exchange. C writes it alongside SITU_ERR_TRUNCATED and Python returns
  it beside a `None` deadline, both for the reason C's comment gives: a
  caller that is never told waits for ever. Rust was the third backend with
  it, and Python has carried a named regression test since it was fixed
  there.
- tokio's sink sent with `try_send`, which answers `WouldBlock` out of
  tokio's readiness bookkeeping *without attempting the syscall*, re-armed
  only by a runtime that cannot run inside a synchronous callback. Since
  `step` spends a retry before it calls `submit`, a swallowed `WouldBlock`
  cost the datagram and its retransmission together.
- The Rust key builders cast an enum accessor's `Option` to an integer, so a
  relation keyed on an enum did not compile -- `example/dns` among them.
  Sweeping for the shape rather than the symptom found the same thing in the
  `must` predicates and in a bound naming a sibling.

**The pattern is worth more than the bugs.** A schema compiler's tests
generate code and check what it says; they had not linked it against Qt, run
it inside tokio, or keyed a relation on an enum. Writing a consumer is a
different instrument, and it found in a day what the suite had not in the
life of four backends. The differential's own lesson, one level up: a second
implementation is what catches the first.

**Two smaller lessons, both about evidence.** The tokio driver was reported
green with its failures blamed on machine load; twelve runs on an idle box
said otherwise, and the fix for the expiry count is what made the remaining
failure legible enough to diagnose. And the `xfail(strict=True)` pinning the
enum-key defect had been passing for the wrong reason -- its schema declared
a `u4` with no `bit_order`, so generation refused before the compile the pin
existed to watch ever ran. A vacuous pass wearing an xfail. Both pins that
were left behind have since flipped to XPASS and been retired, which is what
`strict=True` is for: a pin that outlives its bug asserts the wrong thing.

---

### 26.137 Scope: what a protocol needs, situ implements

Moved to `doc/decision/26.137-scope-what-a-protocol-needs-situ-implements.md`.

### 26.138 A delimiter is a byte, and now it can be written

Moved to `doc/decision/26.138-a-delimiter-is-a-byte-and-now-it-can-be-written.md`.

### 26.139 8b10b is not a table code, and was listed as one in four places

Moved to `doc/decision/26.139-8b10b-is-not-a-table-code-and-was-listed-as-one-in-four-places.md`.

### 26.140 NRZI was in the same list, and the family that has it already did

Moved to `doc/decision/26.140-nrzi-was-in-the-same-list-and-the-family-that-has-it-already-did.md`.

### 26.141 `make fuzz` run, and what "no crash in 36 harnesses" is worth

The target has existed and is documented at 26.x and in the table at 4343.
Nothing records it having been run. It has been now: 36 harnesses built
with clang under `-fsanitize=fuzzer,address` and run for 30 seconds each,
**no crash**, exit 0.

That is worth having and it is worth deflating, because the summary line
the target prints is a count of harnesses rather than of tests that could
have failed.

**Coverage across the 36 spans 1 to 2510.** `edges` reached 2510 and
`http` 1371; the median is 52; `register` and `kernels` reached 1. The
first reading was that the low ones had failed to get past a magic or a
length field -- random bytes cannot guess a four-byte magic, and the
corpus starts empty because nothing seeds it, though `example/*.vectors`
holds twelve files of accepted messages that could.

**That reading was wrong, and testing it is what showed so.** Seeding
`ntp`'s corpus from its vectors moved its final coverage not at all: 21
seeded, 21 unseeded, starting from 18 rather than 0. `tiff`, chosen
because a TIFF magic is exactly what random bytes cannot reach, did the
same: 14 either way. Both saturate. An NTP header is forty-eight bytes of
straight-line field reads with no length, no loop and no variant, and 21
edges is all there is to cover.

So the coverage spread measures how much branching a schema has, not how
well the fuzzer did, and the seeding improvement this was heading toward
is not warranted. Measured before it was built, which is the only reason
it was not built.

**What is real is smaller and is about the claim.** Two of the 36
harnesses have an empty entry point -- `LLVMFuzzerTestOneInput` is
`(void)data; (void)size; return 0;` -- because `std/kernels.situ` has no
struct at all and `example/register/register.situ` has nothing a byte
stream can acquire. They cannot crash, and the target counts them:
"no crash in 36 harnesses" is 34 tests and two tautologies.

**That was recorded as having no clean seam, and the deferral was wrong.**
Three routes were named -- report the count, emit no harness, keep the
source -- and the first was called a question about the generator's
interface. It is not: `pack`, `gen-tests`, `gen-codec-tests` and
`gen-derived` all print `wrote <file> (<count> <thing>)` already.
`gen-fuzz` printing a bare filename was the odd one out, and adding the
count is conformance rather than a decision.

It prints `(0 fuzz target(s))` for `std/kernels.situ` and for
`register.situ` now, and the number is counted from the emitted text
rather than re-derived from the schema, so it cannot disagree with the
file it describes. `test_gen_fuzz_says_how_many_targets_it_emitted` pins
both ends and fails if the reported count stops coming from the artifact.

Neither empty harness is a fault. `std/kernels.situ` declares no struct,
and this generator excludes registers deliberately -- "a register is a bus
transaction, not bytes off a wire" -- having already paid once for the
opposite mistake, when `example/protobuf` was filtered out by a
`size_bytes > 0` test and fuzzed nothing at coverage 1 for 16 million
executions. What was missing was only that nobody was told.

**And the Makefile half is closed too, by the rule this file already
carries.** The generated `_fuzz.c` was deleted as a spent intermediate,
so the summary could not read it. `test/generated/Makefile` already keeps
a *named* `.SECONDARY:` -- named rather than bare, with a comment
recording that a bare one catches every pattern rule and that this
repository has met that rule three times -- and the fuzz sources join it.

The summary now reads what it counts:

    no crash in 34 harnesses; register kernels have no parseable struct
    and test nothing

**The first version of that count had the fault it was written to fix.**
`grep -q ... 2>/dev/null` on an absent file is false, so a missing source
would have counted as an empty harness, and every source missing would
have reported all 36 as empty -- a confident, wrong number in place of a
confident, wrong number. `fuzz` now depends on the sources so they exist,
and the recipe refuses outright rather than counting when one does not:

    build/gen/arp_fuzz.c is missing, so this count would call every
    harness empty; refusing to report one

Checked by deleting a source with rebuilds suppressed, which exits 1.

### 26.142 State between items, which three protocols have wanted this week

Moved to `doc/decision/26.142-state-between-items-which-three-protocols-have-wanted-this-week.md`.

### 26.143 What the CI history is evidence of, which is two things

Moved to `doc/decision/26.143-what-the-ci-history-is-evidence-of-which-is-two-things.md`.

### 26.144 What is open, and what was tried and did not work

Moved to `doc/decision/26.144-what-is-open-and-what-was-tried-and-did-not-work.md`.

### 26.145 What stands behind the last thirty commits

**Local evidence only, and it should be said plainly rather than inferred
from 26.143.** No commit since 2026-08-14 has been executed by CI, so the
thirty in this stretch -- the restored CRC oracle, SLIP and PPP framing,
`\xNN`, six CRCs, three scramblers, NRZI, the SLIP example, the
`DERIVED_*` consolidations, the attribution work, the copyright guard --
rest on `make check` on one developer machine. That is 4118 passed and 38
skipped, style and mypy clean, C built and run on the host and under
aarch64 emulation, and 36 fuzz harnesses under libFuzzer and ASan with no
crash.

It is a good deal of evidence and it is one machine. The gap CI would
close is named in 26.143 and is narrow -- Python 3.11, which nothing here
has -- and two substitutes stand in its place. Nothing else in the
workflow is absent locally: every differential oracle ran, so `tshark`,
ImageMagick and the rest are present.

**A green run says less than it used to, and the wording now says so.**
`tool/style_gate.py` printed "N files conform" until 2026-08-30 and now
prints "N file(s) pass: whitespace, and indentation except
under-indentation, which is not checked", because the converter it
compares against never *adds* indentation, so a line with too few tabs is
invisible to it. Every earlier record in this document citing "files
conform" was quoting a summary wider than the check behind it. Nothing
those records assert is wrong; the guarantee attached to them is narrower
than the sentence implied.

**The habits that produced the findings, since they cost little and paid
repeatedly.** Sabotage before trusting: break the thing, watch the check
go red, restore -- which is how the CRC vectors, the scrambler periods,
every `DERIVED_*` guard and the copyright guard were shown to discriminate
rather than merely pass. A control inside a one-off sweep, classified
before any real file is read, so that a clean result means something: the
two population sweeps and the PEP 701 check each carry one, and each would
have been quoted wrongly without it. And reading the artifact rather than
a summary of it -- `git diff-tree` rather than `git show --stat`, the
commit's file list rather than a rendering that also contains its message.

### 26.146 USB's NRZI, and a gap that was filed as a decision

Moved to `doc/decision/26.146-usb-s-nrzi-and-a-gap-that-was-filed-as-a-decision.md`.

### 26.147 A named code's overhead is the code's, and three declare it short

Moved to `doc/decision/26.147-a-named-code-s-overhead-is-the-code-s-and-three-declare-it-short.md`.

### 26.148 USB's bit stuffing, and the whole of its line coding

26.146 left this open by name: USB stuffs a zero after six consecutive ones,
situ's bit stuffing was HDLC's five, and the two are a separate stage from
the line code because stuffing changes the length and NRZI does not.

**One algorithm, two constants, two names.** `_hdlc` became
`_bit_stuffing`, the run is its argument, and `BIT_STUFFING` carries the run
with the sentence saying what the insertion buys -- HDLC's five keeps the
flag sequence 0x7E out of a frame, USB's six keeps the *line* moving. They
are named apart for the reason the two NRZIs are: a decoder built on the
wrong length is wrong from the first stuffed bit onward, with nothing at run
time to notice.

**And that is the opposite trade from SLIP and PPP**, which decision 0017's
third amendment refused to merge. Those two share a shape -- escape-stuffed
byte codes -- and differ in what they *do*: SLIP substitutes from a table
and refuses an escape it was not told about, PPP transforms with an xor and
therefore reverses escapes nobody enumerated. Sharing an implementation
would have made one of them wrong. Here the implementation is genuinely
identical and only a number differs, so one generator with an argument is
the honest shape and two names is still the honest surface. The test for
which case you are in is whether merging them would change any behaviour,
not whether the descriptions rhyme.

**The refactor moved nothing.** Regenerating `std/kernels.situ` and
`std/codecs.situ` gave byte-identical output, 111,866 and 515 bytes --
including HDLC's generated comment, which was rewritten as a per-code
string and had to come back the same.

**26.147's guard paid for itself one commit after it was written.** Adding
the row to `STUFFING_BOUNDS` before the codec existed failed
immediately with the three populations printed side by side, and adding the
codec without the measurement row failed again. The point of that guard was
that a stuffing code could join the build with nothing checking what it
actually expands by; the first new code since is the proof it works.

**Checking USB alone would have proved nothing.** A generator that ignored
the run emits the same function twice, and each copy passes its own round
trip. So the discriminating input is a run of exactly five ones followed by
a zero, which is HDLC's trigger and not USB's:

    11111000  ->  111110000   (hdlc, stuffed)
    11111000  ->  11111000    (usb, untouched)
    11111111  ->  111110111   (hdlc)
    11111111  ->  111111011   (usb)

The first pair differs in length and the second in content, so neither a
length check nor a content check is carrying the result alone.

**The pipeline says what the pair costs**, and it is a shape the tree did
not have. `framed` is exact ratios all the way down; `usb_line` is a
bounded expansion followed by a length-preserving one:

    codec usb_line unbound expansion=ratio_bounded(7,6) seekable=none
                   granularity=bit invertible deterministic error_propagating

The bounded ratio survives the length-preserving stage, seekability is gone
because stuffing took it, and `error_propagating` arrives from the NRZI
half. None of that was declared.

**The order is not free, and the reason is sharper than "the standard says
so".** USB stuffs before it encodes. Stuffing the encoded stream instead
would not be the same code applied later -- in the line stream a held level
is a run of *identical* bits, which may be zeros as easily as ones, so a
rule counting runs of ones leaves half the stuck-line cases unstuffed, and
the run it did break would correspond to no run in the data a receiver
recovers.

**What USB has now.** `crc16_usb` and `usb3_scrambler` were already here.
With `usb_bit_stuffing` and `nrzi_transition_on_zero` the low- and
full-speed line path is expressible end to end -- CRC, stuffing, line code,
in one schema and in the order the standard puts them.

**Three sabotages, each watched going red**: USB's run set to five, so it is
HDLC twice, caught by the discriminating input; the decoder's run left one
behind the encoder's, caught by the round trip returning nothing; and the
`usb` row deleted from the bounds table, caught by the population guard.

### 26.149 The partial symbol three table codes encoded half of

The lens that found 26.147 was: measure the object file rather than reading
the declaration. It had paid three times in one afternoon, so it was pointed
at the rest of the codec library -- every derived codec's declared expansion,
measured against what its generated encoder actually writes.

**Everything holds at a whole number of units.** Manchester at 2:1, base16
and 4b5b at their exact ratios, base32 and base64 padding to whole groups,
every shift register length-preserving, the interleaver too. The declarations
are right and nothing had checked them.

**What was not right is a length that is not a whole number of units.** The
non-padded table codes walk bits, and their loop condition is
`at + inputs <= bits`, so a trailing partial symbol falls out of the loop and
is dropped:

    situ_base16_encode(in, 7, out)   ->  8

Seven bits in, one symbol encoded, three bits gone, and a return value that
looks exactly like success. The generated comment above that function
promised "the number of output bits written, which is exactly
bits * 8 / 4" -- which for seven bits is 14. Manchester's *decoder* did the
same to an odd trailing bit, and 4b5b's encoder to up to three.

**It is invariant 154's shape and the tree already had the answer.**
`interleave_16` refuses a partial block outright, with a comment saying a
partial block has no defined permutation, and a partial symbol has no
encoding for exactly the same reason. So the five affected functions refuse
now -- base16 and base16_lower, code_4b5b, and both Manchesters' decode
side -- and the generated comment states the refusal rather than promising a
formula the code does not compute.

**Zero is not ambiguous here, which is worth stating because it looks it.**
The decode direction already returned 0 for a symbol the code does not
define, so the convention was in place. In the encode direction 0 was
previously only "nothing to do", and the two now share a value -- but a
caller that passed a nonzero length and got 0 back was refused, and a caller
that passed 0 had nothing to encode. The only length that could have
produced 0 from real input was a sub-symbol one, which is the case being
refused.

**Manchester's encoder is untouched and provably so.** Its input symbol is
one bit, so the guard would be dead code and is not emitted; the diff over
the generated file is 56 lines added and 13 replaced, all of them in the
five functions named above, with `std/codecs.situ` byte-identical.

**Three sabotages, each watched going red**: the encode guard removed,
caught at base16 encoding 17 bits as 32; the decode guard removed, caught at
Manchester decoding 7 bits as 3; and the encode loop's stride doubled,
caught by the ratio assertion rather than by any of the refusal ones.

**And the guard is now general rather than about this bug.** It reads every
`table` codec out of `std/kernels.situ`, measures the exact ones in bits and
the padded ones in whole groups, and holds each to the ratio it declares --
so a ninth table code is covered the day it is declared. The padded half is
checked by the group formula rather than by a listed expectation: a group is
the shortest run that is both a whole number of bytes and a whole number of
symbols, which is what makes `ratio_padded` mean anything.

### 26.150 The axis the scrambler pair was built to demonstrate, measured

Moved to `doc/decision/26.150-the-axis-the-scrambler-pair-was-built-to-demonstrate-measured.md`.

### 26.151 The README, and which direction a front page goes stale in

Moved to `doc/decision/26.151-the-readme-and-which-direction-a-front-page-goes-stale-in.md`.

### 26.152 CI, and the half of the floor gate nobody was holding

Moved to `doc/decision/26.152-ci-and-the-half-of-the-floor-gate-nobody-was-holding.md`.

### 26.153 The first CI run in forty commits, and what it caught

The billing block lifted, and `check` ran for the first time since
2026-08-14. It went red in 48 seconds with every install step green and
`make check` failing -- which is what 26.143 said this run would be worth:
nothing about what broke in August, something about now.

**Five errors, one shape**, in `test_relations.py` and `test_editor.py`:

    Argument 2 to "acquire" has incompatible type "bytearray";
    expected "bytes"  [arg-type]

**The cause is not the Python version, which was the obvious guess and is
wrong.** `pyproject.toml` already sets `python_version = "3.11"`, so mypy
was targeting the floor here too, and running it with `--python-version
3.11` explicitly reproduced nothing. It is the *mypy* version. mypy promoted
`bytearray` to `bytes` for years and stopped; `strict` implies the stricter
rule in a new enough one and did not in the 1.15 installed here, and the
workflow installs the latest. Same tree, same command, two answers.

**The annotations were genuinely too narrow, so the newer mypy was right.**
`acquire` calls `len()` on its buffer and stores it; `open_document` calls
`bytes(message)` on the first line. Both accept a `bytearray` and said they
did not. And the `bytearray` in the editor test is load-bearing rather than
incidental -- `test_the_document_outlives_the_buffer_it_was_read_from`
mutates the buffer after opening the document and asserts the fields did not
move, which is a test `bytes` cannot express at all.

**Widening `View.buffer` cascaded to exactly three sites, and the cascade is
the second finding.** Each is a function returning `bytes` that returned a
slice of the buffer. Three sibling sites in the same two modules already
wrote `bytes(view.buffer[...])` and three did not -- the same operation
spelled two ways, where the three that did not were handing out a view of a
caller's mutable buffer under an annotation promising an owned value.
Nothing could see it while the two types were promoted to one. All six copy
now, which is what the majority already did and what the annotation says.

**The fix that matters is one line of configuration.** `strict_bytes = true`
in `pyproject.toml`, so the position is the project's rather than a property
of whichever mypy is on the machine. Proved both ways, with one `bytes(...)`
removed again:

    with strict_bytes:      Found 1 error in 1 file
    without strict_bytes:   Success: no issues found in 183 source files

That second line is what this repository had. A gate whose strictness is
inherited from a floating tool is a gate whose answer changes without anybody
editing it, in both directions -- and the direction that hurts is the one
where CI is stricter than the machine the code is written on, because the
author cannot reproduce the failure.

**And `testpaths` named a directory that has never existed.** It was
`["tests"]`; the directory is `test`. Bare `pytest` answered "No files were
found in testpaths; searching recursively from the current directory
instead" and walked `build/` and the caches, while `make test` passed
explicit paths and never met it. **That is the fourth place the same wrong
pair of names has turned up** -- `tools` and `tests` for `tool` and `test`.
`python_floor`'s docstring records the Makefile's, 26.151 found the README's
and `make help`'s, and this is `pyproject.toml`'s. The directories were never
called that.

**Named rather than taken with a red on the board, and taken once there was
a green.** Every run annotated "Node.js 20 is deprecated ...
actions/checkout@v4, actions/setup-python@v5 are being forced to run on
Node.js 24" -- a warning today and a break when the runners drop it, but
bumping both is its own change and belonged after a green rather than mixed
into the commit trying to get one.

Both are at v7 now, which is the current major rather than the minimum that
clears the warning. The minimum is checkout v5 and setup-python v6, each
being the release that moved to Node 24; taking those would be pinning to a
version that gets annotated again. The majors in between were read rather
than assumed: checkout v6 persists credentials to a separate file, which
this workflow never reaches because it does not use the token after
checkout, and v7 refuses to check out a fork PR under `pull_request_target`
or `workflow_run`, neither of which is a trigger here. setup-python v7 is an
ESM and dependency refresh. Nothing in `make check` reads the workflow, so
the verification for this one is the run itself.

### 26.154 The first green in three weeks, and what it is worth

`check` passed at 2026-08-31 23:26, 7m14s, every step: the first green run
since `7ec7b01` on 2026-08-09. 26.143 predicted the shape of it and the
prediction held -- it says nothing about what broke in August, whose logs
have expired, and it says one thing about now, which is that Python 3.11 has
finally run the suite it was declared for.

Two numbers from it are worth keeping:

- **`skipped: 40`, against a ceiling of 45.** The workflow refuses a run that
  skips more than it should, and its comment records 34 "at the time of
  writing". It is 40 now, on a runner with every toolchain installed, so the
  headroom the guard was given is down to five. Raising it is a deliberate
  act by that comment's own instruction; nobody should do it to make a build
  pass. Recorded so the next person meets a number rather than a surprise.
- **7m14s**, which is what a full run costs on a hosted runner: install,
  `make check` including the aarch64 emulation, and the skip audit.

**What the green establishes and what it does not.** It establishes that the
tree parses, typechecks, tests and cross-builds on the declared floor -- the
one axis no local machine here covers. It does not establish anything about
the thirty-odd commits between 2026-08-14 and now individually: they were
executed as a batch, so a defect any one of them introduced and a later one
removed leaves no trace. That is the ordinary cost of a build that stopped
running, and it is worth stating once rather than discovering later.

### 26.155 The skip guard counted the wrong population

Moved to `doc/decision/26.155-the-skip-guard-counted-the-wrong-population.md`.

### 26.156 A kernel argument nothing reads, and the one that costs

Moved to `doc/decision/26.156-a-kernel-argument-nothing-reads-and-the-one-that-costs.md`.

### 26.157 Perl and PicoLisp, when there is nothing left to hold them up

Moved to `doc/decision/26.157-perl-and-picolisp-when-there-is-nothing-left-to-hold-them-up.md`.

### 26.158 The step that verifies the toolchains could not fail

Moved to `doc/decision/26.158-the-step-that-verifies-the-toolchains-could-not-fail.md`.

### 26.159 A form the vocabulary could not see, and a lesson that reached one family

Moved to `doc/decision/26.159-a-form-the-vocabulary-could-not-see-and-a-lesson-that-reached-one-family.md`.

### 26.160 One rule, four constructs, and it was written on one

`wellformed.py` refuses an argument a *sealed* region does not read, and
says why: "anything else would state what the generated code does not do".
Its three neighbours -- `coded`, `indexed` and `tlv` -- accepted anything.
Measured, before:

    sealed   noncee                  refused
    coded    bogus                   accepted
    indexed  basse, offset_typ       accepted
    tlv      duplicat_tags, wishful  accepted

**`indexed` is where it costs.** `_index_base` scans the argument list for
`base` and returns `IndexBase.REGION` when it does not find one, so
`basse = page_type` measures every offset in the table from the region
rather than from the named member -- a parser reading entirely the wrong
bytes, one transposed character apart. Three readers each step past it:
`_index_base` skips anything not named `base`, the layout reads
`offset_type` and `count` by name and ignores the rest, and
`check_attribute_places` never sees them at all, because it walks
`member.attrs` and a region keeps its arguments in `.args`.

`_index_base`'s own docstring records that this argument has been ignored
before: "section 9.3 has written `base = table_start` since before there was
a parser for it, and nothing read the argument at all". That fix taught it to
read `base`, and left every other name unread.

**`check_region_arguments` is table-driven over all four**, which is the
generalisation the fourth copy would not have been.

**Building the table was the work, because of what happened an hour
earlier.** 26.159 records a vocabulary built by scanning for `argument("x")`
that refused `symbol`, a form read as `arg.name == "symbol"`. So this one
was assembled from every reader shape: the `.argument("x")` calls, the
parser's structural dispatch for `tag_decode`, `value_size` and `known`, the
helper lookups `_tlv_policy` and `_tlv_identity`, and the predicate forms --
`any(arg.name == "ordering")` in `parse_tlv`, and `layout._region_argument`,
**which is how `coded` turned out to take a nonce at all.** The wellformed
check named only `sealed`; the layout helper is shared, and
`coded body(ctr, nonce = iv)` solves with `sealed_nonce = iv` recorded.
Refusing it would have been 26.159 repeated in the same session.

Two of the nine tlv names, `ordering` and `length_type`, are read by code and
written by no schema -- the shape `symbol` was in. They stay: a vocabulary
narrower than its readers refuses valid input, which is the failure this
table exists to prevent rather than cause.

**The old test caught a loss in the generalisation, which is what old tests
are for.** `sealed`'s message said "`nonce = field` and `key = field`",
telling a reader the argument names a field rather than carrying a value.
Replacing it with "reads `nonce`, `key`" dropped that, and
`test_an_unknown_region_argument_is_refused` failed on the missing phrase.
Each construct carries its own sentence now rather than a list of bare names.

**Three sabotages, each watched going red**: the check not called; `coded`
losing `nonce`, caught by the test that writes the working form rather than
by any refusal case; and `indexed` losing `offset_type`, caught by
`example/sqlite`, which is the committed schema that writes it.

### 26.161 The arguments only the emitter reads, and their defaults

26.156 closed the argument you wrote that nothing reads. This is the other
half: the argument you did not write that something needed.

**A signature and a generated implementation need different things**, and
`shift_register` is where they diverge furthest. It derives every property
it reports -- seekability, error propagation, invertibility -- from one word,
`feedback`. Taps, width and seed are read nowhere but the emitter, which
defaulted two of them and fell silent on the third:

    no seed    generated with 0xFFFF, a different keystream
    no width   generated a 16-bit register, where `taps = 0x1` is NRZI at
               width 1 and something else entirely at 16
    no taps    no implementation, and nothing naming the missing argument

**No committed schema omits any of them**, so those defaults existed only to
be wrong. Every other family's needs are already required where the signature
is derived -- `_table` calls `_positive` for `input_bits`, `_linear_block`
for `n` and `k`, `_stuffing` for `worst_case` and `per` -- which is why this
family alone had the gap and why the table has one entry.

**The check hangs off the `impl`, not the derivation**, and the two reasons
agree. 13.1 makes a signature with no implementation the normal case for a
protocol under design, and such a codec wants none of this; and putting it in
`kernels.derive` would refuse the derivation tests that state nothing but
`feedback`, which is exactly what those tests exist to check.

**The test claimed to cover the guard and did not, and the sabotage is what
showed it.** The first version asserted that a codec with no `impl` still
parses -- but such a codec never enters the loop the check hangs off, so
removing the `ImplKind.DERIVED` guard left the test green. It read as
coverage of the guard and was coverage of nothing. The guard's real subject
is an **`extern` binding**: the implementation is the caller's, situ emits
none, and the emitter's arguments are as unwanted there as for a bare
signature. Both cases are written now, and removing the guard fails.

That is this session's shape arriving in the instrument again, and by now the
count is worth stating: a check that cannot fail reads exactly like one that
works, and tonight it turned up in a kernel argument, a table row, a skip
ceiling, a toolchain report, a region vocabulary, a `make check` piped into
`tail`, a `pgrep` matching itself, and a test asserting something it could
not observe. The instruments are not safer than the code. **Sabotage is the
only thing that tells the two apart**, and it has to be aimed at the specific
line the test claims to hold, not at the feature.

### 26.162 A caveat in the position of a reason, and the cache that hid a sabotage

Moved to `doc/decision/26.162-a-caveat-in-the-position-of-a-reason-and-the-cache-that-hid-a-sabotage.md`.

### 26.163 A fix that stayed in the rule that produced it

Moved to `doc/decision/26.163-a-fix-that-stayed-in-the-rule-that-produced-it.md`.

### 26.164 A bound that parsed, changed nothing, and asked for itself again

Moved to `doc/decision/26.164-a-bound-that-parsed-changed-nothing-and-asked-for-itself-again.md`.

### 26.165 The cheapest number in the range, under the name of the dearest

18.2 calls the cost column the advisor's differentiator, and the test module
says so in its opening: "the tests below check the numbers, not just that a
suggestion fired". **Checking a number is not measuring one.** `Cost` counts
bytes on the wire and `worst` is the most a frame pays -- which is what
`equalize-variant-arms` reports, 8 bytes for the arm that was smallest.

`varint-to-fixed` computed both its numbers from the varint's *widest*
encoding. `worst` was therefore the extra paid by a frame that was already
paying the most, which is the **smallest** cost in the range. Measured on
`v n; u32 seq` with `max_bits = 16`: frames run 5 to 7 bytes before the
change and 8 after, so a frame carrying a small value pays three bytes. The
rule said one.

**The sharp case prints "nothing".** At `max_bits = 14` the fixed width is
`u16` and the widest encoding is already two bytes, so both numbers came out
zero and the suggestion read `cost: nothing`. Frames run 5 to 6 bytes before
and 6 after: a frame carrying a small value pays a byte for a change priced
as free. `Cost` refuses to do this for an unbounded region and says why in
its own docstring -- reporting zero "would be a lie in the cheapest possible
direction" -- and the same class was doing it two rules over.

Two rules disagreed about what `worst` meant and **the name was the only
thing that said so**, which is why neither test caught it: each asserted the
number its own rule produced.

`worst` is measurable without knowing any value distribution. Where the
rewritten schema is fixed, the frame that was smallest before is the one that
pays most, so the worst cost is the difference between the two minima --
pinned now for all four priced rules. `typical` is not measurable that way and
is not pinned: each rule averages over what it can see, arms for one and
encoding widths for another, and those are different claims that happen to
share a field.

A suggestion makes three claims a reader acts on -- what to do, what it
costs, what it buys. **Two of the three were checked by assertion rather than
by measurement, and both had a defect** (26.163, 26.164 and this). The
remaining one is the summary text, which is the only one where the assertion
*is* the measurement.

### 26.166 A heading, a verb, and a sample nobody could have checked

Moved to `doc/decision/26.166-a-heading-a-verb-and-a-sample-nobody-could-have-checked.md`.

### 26.167 The annotation said the new check was dead code

Moved to `doc/decision/26.167-the-annotation-said-the-new-check-was-dead-code.md`.

### 26.168 An unmarked cut, and a gate that could not see an absence

Moved to `doc/decision/26.168-an-unmarked-cut-and-a-gate-that-could-not-see-an-absence.md`.

### 26.169 The document was right about a tree that no longer exists

Moved to `doc/decision/26.169-the-document-was-right-about-a-tree-that-no-longer-exists.md`.

### 26.170 The specification refuted itself, fourteen thousand lines apart

Moved to `doc/decision/26.170-the-specification-refuted-itself-fourteen-thousand-lines-apart.md`.

### 26.171 `make test` was credited with three things it has never run

Section 20's build reference said

    make test       pytest, mypy strict, lint, cmocka, cross

and `test` is `test-py test-c`. The types, the lint and the cross build are
`check`, which the reference did not mention at all. **A contributor who ran
`make test` and believed that line had run neither mypy nor the linter** --
which is how this tree spent eight commits with a red `typecheck` while its
tests passed (26.167).

**The README said it correctly the whole time**: "`make test` is pytest plus
the generated C, which is the fast one to run while working. `make check` is
what has to pass before a commit". Two documents in one tree described one
target two ways, and nothing compared them to the Makefile or to each other.

Three vocabularies are shared between the documents and the tree with nothing
holding them, and each is now read from the tree rather than from a list
written in a test -- a list in the test is a third copy, and then two of the
three have to be right:

- **`make check`'s prerequisites**, parsed out of the rule, must each be named
  where the document describes it. That is what was wrong.
- **Every `make X` a document invokes** must be a rule. Only inside a code
  span or a fenced block: prose says "make it allocatable", and a pattern
  that reads those the same way reports `it` as a missing target. The
  reference's own bare `make` line, with its description in a column, reports
  `build` unless a target is required to follow a single space.
- **Every `require` predicate** in `PREDICATES` and `DEFERRED_PREDICATES`.

**A fourth was written and thrown away, because it already existed.**
`test_the_cli_section_lists_every_command` has held section 21's verb list to
the argument parser since it caught that section naming a `--strict` that
never existed. The duplicate was written before looking, which is the second
time this session -- 26.163 was the first -- and both times the existing check
was in a file the search did not reach. **Looking for the gate is part of
writing one**, and the cost of not is a second copy that has to be kept
right.

**The predicate check passed its first sabotage**, which is why it is written
the way it is. Searching project.md for `name(` finds every predicate,
because each is discussed in prose somewhere -- so renaming a row in the table
left it green, and so would deleting the table outright. It locates the block
and reads that. Six sabotages now, all red: one per check, plus the renamed
row, the deleted table, and a verb the reference invents.

The pattern across 26.169 to here: **a vocabulary written twice is a copy, and
the copy in prose is the one that goes stale**, because the code is exercised
and the prose is only read. Where the tree holds the vocabulary once --
`KERNEL_ARGUMENTS`, `PREDICATES`, the argument parser, the Makefile's own rule
-- the check reads it there and the document cannot drift without saying so.

### 26.172 A struct's size was printed as its minimum, with nothing to say so

Moved to `doc/decision/26.172-a-struct-s-size-was-printed-as-its-minimum-with-nothing-to-say-so.md`.

### 26.173 The fix left the same defect one `if` away from itself

26.172 made the map's struct line print `min..max` instead of the minimum.
It has two branches, and only one was fixed. The other returned `size_bits`
alone for any struct that is not whole bytes, so

    struct s { bit a; bit rest[remaining]; }

-- one bit and an unbounded bit run -- printed `size=1bit`, which is the
defect the commit was written to remove, surviving in the branch beside it.

**Nothing disagreed, because nothing exercised it.** No bit-sized struct is
committed anywhere in the tree: 158 structs and not one. A branch with no
coverage reads exactly like one that works, and this is the fourth time this
session that sentence has been the finding rather than the moral.

Both bounds go through one `_extent` now -- bytes where the count is whole
bytes, a `bit` count where it is not -- so `3bit`, `1bit..`, `4` and
`8..65535` come out of the same two lines. The committed maps are unchanged
by it, which is the check that it touched only the branch nothing reached.

**It was found by asking the fix the question the fix answers.** After
committing, the same lens went back over the new code: *what does this print
where nothing measures it*. Three shapes were tried before one worked -- a
variable bit array is refused because `n` is not in scope, a variant of
bit-sized arms is refused because a variant must start on a byte boundary --
and the third is legal, which is exactly the kind of construct no schema
author writes and no committed example covers.

**And the test written for it carried its own version of the fault.** The
first draft asserted `... in struct_line(...) or True`, which passes on any
input at all. Caught before it was committed, by reading the assertion rather
than the result -- a green test is not evidence that it can be red.

### 26.174 What a `file` target would actually change, measured

The copyright holder asked whether situ should have a third target beside
`buffer` and `mmio` -- `target file`, as a shorthand carrying the assumptions
that follow from working on a file, with append and sparse support alongside
it, motivated by making `situ-edit` write as well as read.

Recorded in `doc/decision/0047-a-file-target.md`, which is `proposed`. The
measurement behind it, over the seven file-format examples with five wire
formats as a control:

- **`effect` is `Pure` on all 130 file members and all 209 wire ones.**
  Tree-wide it is 1075 `Pure`, 25 `EffectOnRead` and one `EffectOnWrite`, and
  those 26 are the `mmio` register example. **Outside a register, nothing in
  situ has ever had an effect.** A durable, externally visible write is
  exactly `EffectOnWrite`, is true of a file as such, and cannot be said
  today -- which is the one assumption a `file` target would carry that earns
  its place.
- **The editor does not need the target.** The 130 members already partition
  into 107 writable in place, 22 whose write moves what follows, and one the
  schema refuses to let anyone write, with `auth=Covered(t)` marking the
  writes that invalidate a tag. Everything `situ-edit` decides per field, the
  capability map decides now.
- **`append` is not a default**: it flips 117 of the 130 addresses to
  `Unstable`, and exactly one of the seven -- `sqlite` -- has an unbounded
  top-level extent that needs it.
- **Sparse has nowhere to land**: it makes a write's cost and fallibility
  depend on where *within* a member it happens, and no axis varies a property
  by position.

**The first answer given was wrong and the record says so.** It argued that a
target must be a capability distinction rather than a bundle of assumptions --
and `mmio` is a bundle, as the compiler's own diagnostic says: "`target mmio`
makes `volatile` implicit and `access_width` ...". It also read 9.8's "no
`target file` for it to hang off" as a ruling against the target, when that
argument is about where to hang `at expr` and concludes only that *that
construct* does not follow the file line.

**Incidental, and worth fixing or not on its own terms:** `at expr` is used by
one committed schema. 9.8 names BMP's `pixel_offset`, TIFF's `ifd_offset` and
DNS name compression as the three that wanted it; only `bmp` has it. The
argument does not need all three to be committed -- its point is that they do
not share a medium -- but a reader counting uses finds one.

### 26.175 The `file` target, built

Moved to `doc/decision/26.175-the-file-target-built.md`.

### 26.176 The axis moved and the header said nothing

26.175 built `target file` and the check was green: the axis moves, the map
prints `effect=EffectOnWrite`, `explain` blames the row. **Then the generated
header was read**, and a file schema's C came out byte-identical to a
buffer's -- the same accessors, the same comments, nothing anywhere saying a
store lands in a file.

That is the feature working at the lattice level and not arriving where
somebody uses it. And the header already carries the argument against it, in
`_invalidation_note`'s own docstring: "The C type system cannot enforce view
invalidation, so the generated header has to say what does it -- otherwise
the only record of the rule is in the compiler's head."

**Durability is the case where that is most true**, because `target file`
changes no generated code at all. The accessors are the same arithmetic over
a mapping; there is no `volatile`, no bus transaction, nothing a compiler
would emit differently. The header is therefore not one record of the rule
among several -- it is **the only one** a C programmer reading this API will
meet.

So a `DURABILITY` banner, beside `INVALIDATION`, per struct, saying that a
store outlives the process, that another reader may already have seen it, and
that it is not a store to be repeated, reordered or elided on the caller's
behalf -- and saying what situ does *not* claim: it writes the bytes and says
nothing about `msync`.

**Keyed on the axis, not on the directive.** What the header documents is
what the lattice concluded, so the emitter asks the vector whether the effect
is `EffectOnWrite` rather than asking the schema what its target was. The
sabotage that matters is the second one: making the banner unconditional
fails, because a buffer schema getting it would be a false statement about
the caller's own memory.

**Only the C backend.** `INVALIDATION` is a C-header convention and the other
three emitters have none, so adding this to C alone follows the tree rather
than splitting it. Recorded here so that a later reader asking why C says it
and Rust does not finds the reason rather than the omission.

### 26.177 Every parameterised value in the image said "could not tell"

Moved to `doc/decision/26.177-every-parameterised-value-in-the-image-said-could-not-tell.md`.

### 26.178 `readable` finally got its mirror

Moved to `doc/decision/26.178-readable-finally-got-its-mirror.md`.

### 26.179 The write path, and the row it arrived through

Moved to `doc/decision/26.179-the-write-path-and-the-row-it-arrived-through.md`.

### 26.180 A run written at its own length moves nothing

Moved to `doc/decision/26.180-a-run-written-at-its-own-length-moves-nothing.md`.

### 26.181 The contract that exists to prevent a second model had no test

Moved to `doc/decision/26.181-the-contract-that-exists-to-prevent-a-second-model-had-no-test.md`.

### 26.182 The diagram was drawn to the minimum, not to the struct

Moved to `doc/decision/26.182-the-diagram-was-drawn-to-the-minimum-not-to-the-struct.md`.

### 26.183 The dissector read the located member wherever the walk had reached

Three places print that something "cannot drift from the parser": `situc doc`,
`situc gen-dissector`, and the CLI's own docstring for the second. 26.182
found `doc`'s claim true of its offsets and false of its row width. This is
the same sweep reaching the dissector.

**The claim holds for every construct but one.** The dissector reads
placements, and its one use of `size_bytes` -- the length guard
`if tvb:len() < N then return 0` -- is the *correct* use of a minimum: a
dissector should accept a frame at the struct's smallest legal size, and
using the maximum would reject valid short frames. Not every use of the
minimum is the defect 26.172 found.

**`at expr` is the exception, and `dissector.py` mentioned `located` zero
times.** A located member's `offset_bits` is `None` -- layout.py says so
directly, "None for a located member: its offset is whatever the field says"
-- so it fell through to the running cursor and was dissected wherever the
walk had got to. Then the cursor advanced past it, which section 9.8 forbids
in as many words: it "joins no offset chain: it contributes nothing to the
enclosing struct's extent, and the member declared after it sits where it
would if the located member were not there".

**It was right by coincidence.** In a typical BMP the two headers end at 54
and `file.pixel_offset` is also 54, so the cursor and the declared offset
agree and every test passed. They disagree exactly when the file has a colour
table or a gap between the headers and the pixels -- which is the entire
reason the field exists, and the reason 9.8 gives for the construct. **A
feature that is correct whenever it is not needed passes every test written
from the common case.**

The C backend never had this: it has `_located_accessor` and dispatches on
`placement.located`. So the schema, the map (`offset=DataPlaced`) and the
accessors all agreed, and the fourth description -- the one whose whole
argument is that a hand-written dissector is "a third description of the
layout, and the one nobody remembers to update" -- was the one that had
drifted.

It reads the driver at its constant offset now (`situ_uint(tvb, 10, 4, true)`
for BMP), dissects there, and leaves `at` alone. Where the driver is not at a
constant offset it declines and says so, which is the answer `_key_read`
already gives for a conversation key and for the same reason: a byte range
that moves is not one to read from.

### 26.184 A packed expression read a nested field at the wrong base

Moved to `doc/decision/26.184-a-packed-expression-read-a-nested-field-at-the-wrong-base.md`.

### 26.185 The differential compares the intersection, and the defects were outside it

Moved to `doc/decision/26.185-the-differential-compares-the-intersection-and-the-defects-were-outside-it.md`.

### 26.186 The fidelity report was the feature, and it under-reported

Moved to `doc/decision/26.186-the-fidelity-report-was-the-feature-and-it-under-reported.md`.

### 26.187 The diff called a regression neither direction, and a growth better

Moved to `doc/decision/26.187-the-diff-called-a-regression-neither-direction-and-a-growth-better.md`.

### 26.188 DTLS's whole payload came back as one byte, in three backends

Moved to `doc/decision/26.188-dtls-s-whole-payload-came-back-as-one-byte-in-three-backends.md`.

### 26.189 Two remedies that could not be taken

Moved to `doc/decision/26.189-two-remedies-that-could-not-be-taken.md`.

### 26.190 The gate that compared four empty sets

Moved to `doc/decision/26.190-the-gate-that-compared-four-empty-sets.md`.

### 26.191 The third construct of a class the signature knew about

The wire signature states a construct "whose contract is a list rather than an
extent" on its own lines, and its docstring names the reason: a variant's arms
and a tlv region's grammar decide which bytes a peer reads as what, and neither
shows up in an offset or a width. **There are three such constructs and it
carried two.** An `indexed` region's offset table -- section 9.3, decision 0024
-- was the one left out.

So both of the table's own facts were invisible. Verified by sabotage, on the
one schema in the tree that has an `indexed` region:

    base = page_type -> region     signature byte-identical
    offset_type = u16 -> u32       signature byte-identical

The first moves every cell in every page. The second changes both how far the
table runs and which bytes each offset is read out of. Both are the
INTERPRETATION shape exactly: the table is where it was, holds the same
numbers, and the bytes those numbers name are somewhere else. `--check` now
calls each `breaking` and exits 1.

**Two of the table's four facts are deliberately not repeated, and the reason
is a property of the diff rather than of brevity.** The element type is the
member line's type column and the count arrives as `sized-by=`; state either
again and one change prints twice under two headings that disagree -- a member
fact whose value moved is `backward`, an annotation's is `breaking`. What is
left is the pair with nowhere else to live.

**The corpus exercises `indexed` once.** `example/sqlite` exists because the
construct had no real format behind it (26.143), and it is still the only one.
A signature blind spot survives exactly as long as the sample that would have
shown it, and one schema is how long this one lasted.

### 26.192 Two sections under one tag, and the pool that shadowed the pool

`--metadata` opened a string pool of its own and appended it as
`SECTION_STRINGS` -- the same `strings = 4` the core pool had already been
written under. `std/image.situ` declares each `image_section_tag` once, and
both walkers read the directory into a table keyed by kind: `found[kind] =
(offset, records, stride)` in `walker/image.py`, one field per tag in
`walker/c/situ_walk.c`. **A second section under a tag does not sit beside the
first, it replaces it.**

So with `--metadata` the core pool was unreachable, and every core string
resolved into the tail's pool at the same offset. Measured, not argued:

    example/dns      tags [.. 4, 12, 13, 4]   offset 0 -> 'dns_header'   was 'reply_to'
    example/packet   tags [.. 4, 12, 13, 4]   offset 0 -> 'header'       was 'aes_gcm_128'

The second is the one that bites. A codec name is in the *core* pool for a
stated reason -- "a walker cannot dispatch a transform it cannot identify" --
and a walker asking which codec guards `packet.sealed` was handed the name of
a struct. It would not have failed; it would have dispatched on the wrong
string.

**The tail interns into the core pool now, after the core has finished with
it, so the merged pool begins with the core pool byte for byte.** Checked
against the previous packer as a second module: **37 of 37 bare images
byte-identical**, 35 metadata images changed. `--metadata` is additive in the
sense the name claims, which it was not before.

`FORMAT_VERSION` does not move, and should not. No tag, stride or record
meaning changed. What changed is that an image now carries one strings section
instead of two, the first of which no walker could read.

**And `_assemble` refuses a repeated tag now.** The invariant was in the
format all along and was enforced nowhere, which is why it was broken by an
append. A duplicate raises `PackError` naming the tag rather than being found
by whoever notices a wrong string.

**Separately, a relation the planner could not encode vanished in silence.**
`except RelationRefused: continue` dropped it with a comment saying the
backends had already given the reason -- but `situc pack` runs no backend, so
nothing in that command said anything. `Coverage.unencodable` exists precisely
because an image is opaque. It carries the refusal now, and the pre-existing
"non-empty unencodable means exit 1" rule makes it visible. No committed
schema is affected; all 37 pack with `unencodable` empty.

### 26.193 The image's own schema said version 2, and nothing had ever asked

`std/image.situ` opens by naming what protects it: "`gen-fuzz` fuzzes the
image parser, `situc wire` pins the format, `map --check` catches a layout
change nobody meant, and the differential check compares four backends reading
one image." Four mechanisms, and **the schema had said `format_version
[must_eq = 2]` since version 2, through two bumps**, while

    situc/pack.py        FORMAT_VERSION = 4     writes it
    walker/image.py      FORMAT_VERSION = 4     refuses anything else
    walker/c/situ_walk.c u16_at(image+4) != 4u  refuses anything else

The consequence, generated and read rather than reasoned about: C's `validate`
for `image_header` compiles to `if (situ_image_header_format_version_get(view)
!= 2)`. **Every image the packer has ever produced fails its own schema's
check.** And `situc wire` did its job perfectly -- `std/image.situ.wire` line
74 records `must_eq=2`, faithfully pinning a version no image has ever
carried.

**None of the four could have caught it, and the reason is the same in each
case.** All four are closed loops over the schema: the fuzzer, the signature,
the map and the differential all ask whether the *generated code* agrees with
the *schema*, and it did. The packer is hand-written Python that no generated
code checks and that reads no constraint out of the schema it implements.

**The round-trip test could not catch it either, and it looked like it
should.** `test_the_image_schema_packs_and_reads_back` asserts `seen["version"]
== packer.FORMAT_VERSION` -- reading the field the packer wrote and comparing
it to the constant that wrote it. That agrees with itself whatever the schema
says. Its neighbouring fixture goes further and states the promise outright:
"Generated rather than committed, so that editing the schema and forgetting
the packer fails here instead of drifting." It generates *accessors*, which
read; the Python backend emits no constraint validator at all, so there was
nothing in that module to fail.

The schema now says 4, and one test reads the number out of all four files and
compares them. The C walker's copy is read out of its source with a regular
expression, because nothing imports it -- which is not a shortcut but the
point: **a constant in a file no test loads is exactly where the fourth
statement of a number goes unchecked.** Each of the four, changed on its own,
turns it red.

### 26.194 Half a gate nobody had watched fail

Moved to `doc/decision/26.194-half-a-gate-nobody-had-watched-fail.md`.

### 26.195 Two witnesses, one hand: the round trip that could not see ten fields

Section 26.1's property is that `situc dump-ast` round-trips: "parsing,
unparsing and reparsing must reach the same tree". The test spelled it

    assert dump(again) == dump(first)

and `situc/dump.py` imports its expression renderer from `situc/unparse.py`.
**So the witness to the unparser's faithfulness was written by the same hand,
and agreed with it wherever both were silent** -- which is the guidelines'
"two documents agreeing are one witness", arrived at from the other end.

Comparing the trees instead, over all 37 schemas, ten AST fields were dropped
on the round trip:

     40  CodecDecl.kernel / has_kernel      slip, smtp, std
      5  CodecDecl.tag_bytes                dtls, keystore, packet, std
      4  CodecDecl.nonce_bytes              keystore, packet, std
      3  Field.repeat                       dnsname, ipv6ext, netlink
      2  VarintDecl.declared_max_bytes      mqtt, sqlite
      2  TagField.prefix                    tcp, udp
      2  CodecDecl.pipeline                 std
      1  Field.located                      bmp
      1  CodecDecl.expansion_add            std

and `unparse` **refused `test/schema/edges.situ` outright** -- `cannot unparse
Invariant`, raised for years with nothing asking.

Every one of them changes what the schema says. `at expr` is where the *data*
puts a member; dropping it puts it back in sequence. A `while` run reprinted
as a bare array ends at the frame instead of at its own condition. A tag's
`prefix` is TCP's and UDP's pseudo-header, so the checksum covers different
bytes. A pipeline codec re-rendered as a property body states the composed
answer as though it had been written and drops the stages that produced it:
`framed` came back with `expansion = +0` and no stages.

**The measurement that settles which half was at fault.** Each fix sabotaged
in turn, and both witnesses asked how many schemas notice:

    sabotage       dump sees   tree sees
    at expr                0           1
    while                  0           1
    tag prefix             0           2
    max_bytes              0           3
    kernel                 0           4
    tag_bytes              0           4
    nonce_bytes            0           3
    pipeline               0           1

**Zero and eight.** The dump is not a weak witness to this, it is no witness
at all: it does not print these fields either, so it cannot disagree about
them.

**The corpus was the second half of it.** The round trip ran on `example/`
alone while the two snapshot checks in the same file run on everything --
`test/schema/edges.situ` "exists to carry the constructs no worked example
has", and 26.35 already recorded that reading `example/` alone left its map
and its wire signature stale and unread. The lesson had been learnt in this
file, for two of its three tests.

**Two docstrings in `unparse.py` state the property that was false.**
`_codec_lines` says "Every property is written out, including the ones that
were defaulted ... a round-tripped one that dropped a silent default would say
something different from what it came from", while dropping the kernel, both
AEAD sizes and the pipeline. And `_until_to_source` exists because this
happened once before and was caught by something else entirely: "Absent
entirely until an attribute-placement check found it: a delimited `decimal`
field reprinted as a plain binary scalar, which parses, means something else,
and says so nowhere." One of ten found by accident, and the accident is what
wrote the comment.

The comparison walks the dataclasses now and shares no code with `unparse`, so
**a field added to the AST is compared whether or not anything learned to
print it** -- which is the only version of this check that does not rot the
same way.

### 26.196 The one attribute whose whole purpose is to give up a guarantee

Moved to `doc/decision/26.196-the-one-attribute-whose-whole-purpose-is-to-give-up-a-guarantee.md`.

### 26.197 The piece 26.188 said was owed

26.188 closed with one member still unanswered: "Python emits nothing: its
gate has no byte-run path at all ... The Python gate's byte-run accessor is
the piece still owed." It is paid here, and the shape of the gap is worth
recording because it is not the one that entry describes.

Python's gate did have a run path. Its interior filter admitted
`indexed_elements(placement) or not _is_run(placement)` -- **a run of values
got in and a run of bytes did not**, because a byte run is a run whose
elements are not indexed. So `dtls.record.sealed.fragment`, the entire
encrypted payload of a DTLS record, reached neither branch and was emitted as
nothing at all:

    C       situ_record_sealed_fragment_len / _ptr
    C++     ::situ::rt::bytes fragment() const
    Rust    pub fn fragment(&self) -> &[u8]
    Python  --

The map is not silent about it. `example/dtls/dtls.situ.map` gives the member
a full vector -- `size=Bounded(0, 16384) ... stage=VerifyGated
auth=Covered(tag)` -- so this is the same disagreement as 26.196 with the
sides swapped: there the map said a member was reachable and three backends
gated it; here the map describes a member in full and one backend emits
nothing for it at all.

The bytes *are* the values for a byte run, so the answer is a slice -- the
same one `_opaque` gives outside a gate, read through the gate's view because
the length is plaintext at the same offsets (13.3). Executed rather than
grepped: a five-byte payload written into a message and read back as
`b"abcde"`, because an accessor that reaches the wrong bytes looks exactly
like one that reaches the right ones.

**Two smaller things fell out of it, and both were invisible before.**

A `[secret]` run is a run, so it reached the interior list the moment byte
runs did -- and it is already in the list of members that deliberately have
none. Both write a note, so the member declined *twice*: one member, two
findings, in a report whose whole job is to say what has no accessor. It is
excluded from one of the two lists now, and a test counts the note.

And the generated gate class would not type-check. `Gate._view` is the base
`View`, so an interior member whose length is a field of the enclosing struct
-- `min(nonneg(leaf(self._view.length) - 24), ...)` -- reads an attribute
`--strict` cannot see. **This was latent in the existing indexed-run branch,
which builds the same expression**; no schema had exercised it, so the path
had never been checked. A bare `_view: "<struct>"` annotation on the emitted
gate narrows it, adds no attribute at run time, and needs neither a cast nor
an ignore.

### 26.198 Five rows checked, one not, and the sixth is the one that drifted

Moved to `doc/decision/26.198-five-rows-checked-one-not-and-the-sixth-is-the-one-that-drifted.md`.

### 26.199 Two claims the falsifier never attacked, and never said so

`codegen/c/codectests.py` exists to "generate the tests that would falsify a
lying codec signature", and its docstring says "**Each declared property gets
the cheap falsifying test** from the section 13.1 table". Its emitting loop is
sharper still, about what a builder returning nothing means:

> A builder with no test may still have something to say -- that the signature
> claims nothing here, or that the claim cannot be checked mechanically. Both
> are worth emitting: **silence would read as coverage**.

Six builders, eight declarable properties. **`authenticated` and
`error_propagating` had no builder at all**, so a codec claiming either got
neither a test nor a note -- and `authenticated` is claimed by four extern
codecs, which are the AEADs guarding every sealed region in the tree.

**Neither is a test that was owed, and that is the finding.** `authenticated`
is genuinely unfalsifiable at the tier-1 ABI, for two compounding reasons: the
ABI passes bytes in and bytes out and names no key and no nonce -- deliberately,
because "no construct here names a key" -- and a `length_preserving` codec has
nowhere in its own output to put a tag, so the tag is a *field* and 13.5's
answer for this row is already "see 14.2". All four are that combination.
`error_propagating` is falsifiable here in principle and is measured that way
for *derived* codecs (26.150); no tier-1 codec declares it, so a generated test
would have no implementation to attack.

So the fix is the note in each case, which is what the loop's own comment asks
for. The third branch is worth having separately: where a codec's expansion
does account for `tag_bytes`, the tag *is* in the output and `decode` of a
modified one must refuse -- a cheap falsifier, unwritten because nothing in the
corpus is that shape, and the note says to attack it by hand rather than
reading like the case that cannot be attacked at all.

**All four branches are watched, including the two with no subject in the
corpus.** A note that fires for nothing is indistinguishable from one that
never fires, so the tests use synthetic signatures -- and each of the four
sabotages takes down exactly the tests that claim it.

**The tree contains a codec whose `authenticated` is a lie, and says so.**
`test/generated/codec_impl.c`'s `my_sealing_aead` is XOR with a constant: "It
authenticates nothing, which is the one property here that is a lie. It is a
lie the schema tells too." That is deliberate and correct -- 13.1 is that situ
describes properties and never algorithms, and a test needing real AES-GCM
would be a test of OpenSSL. It is also exactly the lie this harness was built
to catch, and the reason it cannot is now written where the harness is read
rather than only where the stub is.

### 26.200 One source line, two answers, and the editor picked one

`test_lsp.py` opens by naming the two things worth testing, and the second is
that hover's answers "are the ones `situc explain` and the blame chains already
give, rather than a second, weaker computation of the same thing". **No test in
it invoked `explain`.** Every reference to the CLI in that file is prose.

It had already drifted once for exactly that reason, and the fix is commented
beside `_render_blame`: the propagation table has carried a reason and a remedy
since 11.3, "`situc explain` printed them and hover did not, so the editor gave
a weaker answer than the CLI". Fixed by hand; nothing added that would catch it
again.

Comparing the two doors over the corpus found the claim true -- 547 members
with a source span of their own, and hover omits nothing `explain` prints -- and
found two other things on the way.

**A struct declared once and reached twice has one span and several members.**
`hover_at` kept the narrowest match and, among equals, the first, so it
answered a question about a *line* with one of that line's answers, chosen by
which struct happened to be declared first. In `example/ble` one line reads
`advertising_event event_type;` and resolves to two members:

    adv_report.event_type                       offset = AbsoluteStatic(0x00)
    le_advertising_report.reports[].event_type  offset = FrameStatic(0x00)  <- weakened

The editor showed the first: no weakening, no remedy, nothing to say another
answer existed. **218 spans in the tree carry more than one member and 165 of
them disagree about at least one axis**, up to six axes and fifteen members on
one span. Hover names the other uses now and prints only the axes that differ,
which is what the source text cannot show and what a second full vector would
bury.

**And a bullet meant two opposite things.** `_render_vector` listed the
weakened axes as bullets and the rest after an `Unweakened:` header -- except
when one list was empty, where the `elif` printed the *strongest* axes as
bullets with no header. A member with nothing weakened and one with everything
weakened rendered identically: thirteen bullets, no header. That is invariant
154, and 137 members in the tree are the first case. A reader looking at a
fully strong field saw a full list of consequences it does not have. A bullet
means weakened now, always.

**The evidence that this misleads is that it misled me.** Three successive
attempts to compare the two doors mechanically parsed a hover card wrongly --
counting all thirteen axes as weakened, then reading the `**Why:**` section's
lines as vector lines, then comparing a card against a different member that
shared its span. Each looked like a real disagreement and each was my parser.
The gate that went in is containment rather than parsing -- every axis value,
every reason and every remedy `explain` prints has to appear somewhere in the
card -- because the two are different doors and should look different; what
must not happen is hover knowing *less*.

Sabotaged three ways, including the historical one: drop the remedy, and 34 of
35 schemas go red.

### 26.201 The dissector was the description nobody compared, and it was wrong twice

Moved to `doc/decision/26.201-the-dissector-was-the-description-nobody-compared-and-it-was-wrong-twice.md`.

### 26.202 `pad_to` aligns from a base 0043 does not name, and nothing has asked

Moved to `doc/decision/26.202-pad-to-aligns-from-a-base-0043-does-not-name-and-nothing-has-asked.md`.

### 26.203 Two grammars, one hand, and nine spellings neither knew

`test_grammar_sync.py` holds `doc/grammar.ebnf` to `project.md` section 7, and
its docstring is clear-eyed about why: "Two copies of a grammar disagree
eventually. The file is only safe to keep if a reader knows which one is the
bug." It was written after a commit added the `invariant` production to
section 7 and forgot the extracted copy.

**They did not disagree. They agreed, and both trailed the parser.** Nine
spellings the compiler accepts were named in neither file:

    file  append  pad_to  tag_bytes  nonce_bytes
    max_bytes  systematic  error_propagating  ratio_padded

plus `granularity = bit(N)` and `symbol(N)`, and the whole
`seekable = linear | permuted | blockwise(N)` form -- `codec_prop` had
`[ "not" ] "seekable"` and no way to say which. Every one of them is used by a
committed schema, except `target file` and `append`, which no schema has
adopted yet and which this session added.

The guidelines' sentence is exactly this: **two documents agreeing are one
witness if the same hand wrote both.** The test between them can only catch
one of the two ways they rot, and it is the less likely way.

**The third witness is the code**, and it is now a test. Four AST enums --
`TargetKind`, `Granularity`, `Seekable`, `Expansion` -- are what the parser
turns source text into inside an *enumerated* production, so every member of
them is a spelling somebody can type and the grammar has to name it. Two are
spelled differently from their value (`expansion = +16` for
`Expansion.add`, `not seekable` for `Seekable.none`) and both are listed by
name rather than skipped by a rule, so a member that stops being surface
syntax has to be removed from the list rather than quietly passing a filter.

**What is deliberately not gated is the attribute vocabulary.** `attr = ident
[ "=" expr ]` is generic on purpose -- the grammar does not claim to enumerate
attribute names, and `wellformed.py` is what checks a name is one situ knows.
Measuring "32 of 49 attribute names are absent from the grammar" and calling
it a gap was my own false positive, caught by reading the production rather
than the count. The README's attribute table is where that vocabulary is held
to `PLACED_ATTRS`, which is a different check in a different place because it
is a different claim.

### 26.204 The oracle that existed to avoid situ-against-situ, and one family it never reached

`test_differential_oracle.py` opens with the clearest statement of this
session's whole subject, written before it:

> Every other test of the generated code compares situ against situ -- four
> backends against each other (which finds disagreement but not shared error),
> the accessors against the capability map, the accessors against arbitrary
> bytes. All of them are downstream of one schema written by one person
> reading one specification. **This is the one that is not.**

It grades its own evidence, too: `zlib.crc32` and `binascii.crc_hqx` are
independent *implementations*, and a published check value is "weaker, and
saying so matters: it comes from the same catalogue the kernel parameters were
transcribed from, so it is not a second implementation". Fifteen polynomial
codecs are oracled or excused by name, and
`test_every_polynomial_codec_is_checked_or_excused` refuses the silence.

**The table family had none of the three.** No independent implementation, no
published value, no completeness guard -- and four of its eight members encode
an RFC 4648 alphabet that the standard library implements. `base64` was
compared only against situ's own derivation of the same table, which is one
hand writing both sides. Where a derived codec is concerned that is exactly
what `test/generated/Makefile` already says out loud: "Nothing supplies
anything here -- both sides are generated, which is what a derived codec
means."

They are right. `base64`, `base64url`, `base32`, `base16` and `base16_lower`
all agree with CPython, byte for byte, on RFC 4648's own vectors. That is the
answer worth having, and it was not available before.

**The first version of the test could not have told.** RFC 4648's published
vectors are "f" through "foobar", and `base64` and `base64url` differ in
exactly two symbols -- 62 and 63 -- which none of those inputs encodes to.
Giving `base64url` the standard alphabet passed all seven. `bytes(range(256))`
covers all 64 symbols, and the three alphabet swaps go red now.

**A test whose vectors cannot distinguish the thing it names is a check that
cannot fail**, and the only reason this one is not still in the tree is that
the sabotage was run before the commit rather than after.

Three table codecs stay unchecked and are named: the two Manchester line codes
and FDDI's 4b/5b. Nothing in the standard library speaks them, and a
hand-written vector for a line code would be this schema read twice.

### 26.205 The construct with one example and no outside witness

Moved to `doc/decision/26.205-the-construct-with-one-example-and-no-outside-witness.md`.

### 26.206 Counting the corpus, not just the codecs

Moved to `doc/decision/26.206-counting-the-corpus-not-just-the-codecs.md`.

### 26.207 One schema, two meanings: the host's precedence table decided

Moved to `doc/decision/26.207-one-schema-two-meanings-the-host-s-precedence-table-decided.md`.

### 26.208 `/` is C's, and two of the four backends are not C

Moved to `doc/decision/26.208-is-c-s-and-two-of-the-four-backends-are-not-c.md`.

### 26.209 The offset is not the extent, and the map had already said so

Moved to `doc/decision/26.209-the-offset-is-not-the-extent-and-the-map-had-already-said-so.md`.

### 26.210 The exclusion the stub could have earned its way out of

Moved to `doc/decision/26.210-the-exclusion-the-stub-could-have-earned-its-way-out-of.md`.

### 26.211 Coextensive, tested in one direction only

Moved to `doc/decision/26.211-coextensive-tested-in-one-direction-only.md`.

### 26.212 Four vacuous gates, and the sweep that found no fifth

Moved to `doc/decision/26.212-four-vacuous-gates-and-the-sweep-that-found-no-fifth.md`.

### 26.213 One invariant in the corpus, and three meanings for the rest

Moved to `doc/decision/26.213-one-invariant-in-the-corpus-and-three-meanings-for-the-rest.md`.

### 26.214 The closed set nobody opened

Moved to `doc/decision/26.214-the-closed-set-nobody-opened.md`.

### 26.215 A third of the diagnostics have never been read

Moved to `doc/decision/26.215-a-third-of-the-diagnostics-have-never-been-read.md`.

### 26.216 What this session did, and what it cost to find out

Moved to `doc/decision/26.216-what-this-session-did-and-what-it-cost-to-find-out.md`.

### 26.217 Two documents that said what the code does not, both settled

Moved to `doc/decision/26.217-two-documents-that-said-what-the-code-does-not-both-settled.md`.

### 26.218 The register, settled: five closed, and three that are not mine

The copyright holder settled the open items. Five of 26.144's entries close;
three do not, and the reason differs in each case.

**Closed by decision.** 0045 and 0046 are accepted on the reasoning they
already carried -- 0045 because a `random` content policy would state what
the generated code cannot test, which is 14.5's rule and not a new one, and
0046 because 26.137 is the standing answer that a protocol needing something
makes it work. 14.7 has lost `random`. Neither is built; accepting a record
is not doing its work, and both say so in their status.

**Closed by evidence, not preference.** 0017's Rust question turned on a
distinction the record had not drawn: "one implementation beats four nearly
correct ones" is an argument about a *hand-written algorithm*, and a
`derived` codec has no author -- it is generated from a kernel description
the compiler already holds, so four renderings are four spellings of one
description. That is situ's thesis rather than the failure the record guards
against, and `ImplKind`'s two members are where the line falls: an `extern`
implementation is somebody's C and binding it stays right. What makes it
Rust's question alone is the `#![no_std]`, no-C-dependency promise a codec
currently breaks, which C++ does not have because `situ.hpp` includes
`situ.h` already.

**Closed by the repository.** The plugin slot asked which of two histories
happened -- designed and never built, or built and dropped. `git log --all
-S"derived for"` matches no commit and `ImplDecl` has never carried a field
to qualify a binding by target, so: never built. Both documents said the
slot "exists ... and is empty" and now say it is specified and
unimplemented. **A question about the past is answerable and was left open
for weeks as though it were a matter of judgement.**

**Closed by a decision record, 0048.** The frame delimiter is the code's and
`ratio_bounded` gains a constant term. A decoder finds the frame end by the
delimiter, so an encoder that omitted it would produce output its own decoder
cannot bound -- `decode(encode(x))` would not close within one buffer. And
the emitted C already carries "plus the trailing delimiter" in the doc
comment a caller reads: the emitter knows the constant and the header tells
the truth, so this is a gap in what the signature can *express* rather than a
disagreement about what a code does. No generated byte stream changes, which
is the other half of why this branch and not the one that stops three
encoders writing a byte.

**Three stay open, and none of them is waiting on a preference.**

- **`linear_block` knows one code.** Going further needs the generator
  matrix expressible in a schema. That is a language addition -- work with a
  design in front of it, not a decision that unblocks work.
- **8b10b and CoAP.** 26.144 is right that answering them separately is how
  a language grows two spellings for one idea, and the shared absence is
  state carried between items, which situ holds inside codecs and nowhere in
  the layout. Answering needs a record covering both and a design for where
  that state lives. It is the one genuine design question left on the list.
- **Which `copyright` file survives, and the switch-label style.** These are
  not mine under any instruction that does not name them. A licensing
  statement is a grant to other people and the guidelines are categorical
  about not choosing between two of them; the style is a convention change,
  which belongs to a deliberate pass signalled to the shared guidelines
  rather than to the project that noticed it. Both resting states are
  already deliberate and both are guarded -- `test_the_two_copyright_files_`
  `agree` asserts the pair match rather than picking one, and `code-style.md`
  records the 45 labels without standardising them.

### 26.219 The refusal was not strictness, it was the only rule five can hold

Moved to `doc/decision/26.219-the-refusal-was-not-strictness-it-was-the-only-rule-five-can-hold.md`.

### 26.220 The shift rule, built, and the width that is not the field's

0049 decided that a shift is refused where the amount is not provably below
the operand's width, and left it unbuilt. It is built now, in both places
the decision names.

**The width is 64 and it is not the field's own.** This is the part worth
measuring rather than assuming: `u8 body[(code >> 2) + 1]` emits
`situ_leaf_u64(situ_s_code_get(view)) >> 2`, so a layout expression is
computed at 64 bits whatever the fields in it are; and a relation widens
every operand to a signed 64-bit value before comparing, for the reason
`_widen` already gives. One bound in both, and a rule keyed on each field's
own width would have been wrong in the safe-looking direction -- accepting a
shift of 16 on a `u8` that the generated code performs on a `uint64_t`.

**64 is the boundary, not a number above it.** A shift *by* the width is
undefined in C, not merely one past it, so `[max = 64]` is refused and
`[max = 63]` is not. `expr.py` already carried a constant-folding guard
saying "at most 64", which is off by one for a shift that reaches generated
code -- that guard is about a fold whose result no scalar can hold and is
left alone; the new rule is about the shift the backend performs.

**The remedy is checked, not just suggested.** The diagnostic tells an
author to bound the field, and `layout.constrain` folds `[max]`, `[min]` and
`[must_eq]` into the interval that reaches the rule, so `[max = 63]` makes a
`u8` amount pass. It is a case in the table rather than a sentence, because
a refusal that suggests a fix nothing accepts sends the reader in a circle.
A `u5` passes on its type alone, which is the same fact from the other end.

**A relation refuses an expression as the amount rather than analysing it.**
`a.hdr.index + 1` is bounded in fact and proving it needs interval machinery
a relation is checked without. Unknown is unprovable, which is the direction
where being wrong refuses a legal schema instead of emitting three programs.

**And it caught one of this session's own tests, correctly.** 26.214's
vocabulary case shifted by `a.hdr.chunks`, an unbounded `u8`, so the new
rule refused it -- the case meant to exercise `<<` was exercising the
refusal instead. It shifts by a literal now, and says why in the helper that
builds it. The first attempt to give the shift cases their own fields added
them to the shared `head`, which changed every offset in it and failed 127
tests that had nothing to do with shifting; they have their own schema.

### 26.221 The ladder's one gate had never refused anything

Moved to `doc/decision/26.221-the-ladder-s-one-gate-had-never-refused-anything.md`.

### 26.222 Packed decimal read as bits, and a valid clock refused

Moved to `doc/decision/26.222-packed-decimal-read-as-bits-and-a-valid-clock-refused.md`.

### 26.223 The half-fix that broke a round trip nothing was running

Moved to `doc/decision/26.223-the-half-fix-that-broke-a-round-trip-nothing-was-running.md`.

### 26.224 The image said which end the bits start from

Moved to `doc/decision/26.224-the-image-said-which-end-the-bits-start-from.md`.

### 26.225 The write pass never wrote a negative number

Moved to `doc/decision/26.225-the-write-pass-never-wrote-a-negative-number.md`.

### 26.226 Two sweeps that found nothing, and the description that was right

26.224 ended by naming the packer as the next place to look, on the ground
that it sits upstream of every description. It is clean, and so is the seam
beside it. Both are recorded because a session that reports only its hits
misdescribes what it did.

**The packer says what `resolve` computed.** Two representations of one
layout by different code -- `layout`/`resolve` against `pack` plus
`walker/image` -- compared field by field over the corpus: **670 placements,
every field the image carries**, offsets, sizes, element and array counts,
`since`, `repeat_cap`, `pad_to`, bit order, byte order, radix, and the
signed, marker-governed and tag flags. No disagreement.

Six differences turned out to be one deliberate clamp. `_u32` stores
`min(value, NONE - 1)`, so a `size_max_bits` above 32 bits lands as
`0xFFFFFFFE` -- `bmp`'s pixel run, `sqlite`'s payload and four others whose
theoretical maxima are absurd. It is unobservable: the walker reads
`size_max_bits` only where `pinned` is set, which is an explicit
`[size = N]` and always small.

The first two versions of that check reported dozens of disagreements and
both were the instrument. One paired the image against `sorted(structs)`
rather than against the image's own struct table; the other compared every
*flattened* placement against a struct's *own* members. Invariant 157, twice
in one sitting, and both times the shape of the output -- values shifted by a
constant -- was what gave it away rather than any single line being obviously
wrong.

**The committed artefacts are current.** All 37 `.situ.map` and all 37
`.situ.wire` files regenerate byte-identical. Those gates work.

**And the dissector already had the bug the walker had.** `_bitmask` shifts
by `skip` for lsb-first and by `span - skip - size` otherwise -- the choice
the walker never made. Checked rather than assumed, because the previous
entry's finding is exactly the kind that travels: the same fact, needed by
five descriptions, is a fact four of them can each get wrong separately.
This one was right, and is now pinned, because the path is reachable only
from a schema written for it -- the tree's one `lsb_first` schema is a
register, which gets no `Proto`.

`0xAB` read from the low end is `low 3, high 21` in the generated C, in the
walker since 26.224, and in the dissector all along.

### 26.227 A rule stated twice, tested once, and implemented nowhere

Moved to `doc/decision/26.227-a-rule-stated-twice-tested-once-and-implemented-nowhere.md`.

### 26.228 `at expr` measured from the wrong base

Moved to `doc/decision/26.228-at-expr-measured-from-the-wrong-base.md`.

### 26.229 The construct seam, followed to three clean answers

26.227 and 26.228 both came from one question -- which constructs does the
corpus barely exercise -- so the rest of that list was worked through.
Nothing more came out of it, which is worth the paragraph.

**`indexed`'s offset base is packed correctly**, and the hypothesis that it
was not was wrong in an instructive way. `_index_base` reads

    {"region": 0, "message": 1}.get(str(base), 2)

and `ast.IndexBase` is an `Enum`, so `str(IndexBase.REGION)` is
`"IndexBase.REGION"` and the lookup should never match -- which would have
recorded *member* for every table, in the unsafe direction 0024 is about.
Measured: `region` packs 0, `message` 1, a member 2. The AST field holds the
enum's *value*, a plain string, so `str()` is a no-op and the mapping works.
**Checked before fixing, and the fix would have been a bug.** Third time this
session a "defect" read off the code dissolved on execution.

**The walker declines an indexed region, and says nothing about it.** It
never reads the image's `INDEXES` section -- the constant is defined in
`walker/image.py` and consumed nowhere -- so `sqlite`'s cell table produces
no line at all in a listing, rather than a stated refusal. Honest in the
sense that matters, since a wrong line is worse than a missing one; but the
tree prefers a decline that names itself, and this one does not. Recorded
rather than changed: adding a line moves the counts two differentials floor
against, which is a cost the observation does not carry on its own.

**The walker's relation arithmetic matches C, including the signs.**
`report.relate` runs the bytecode VM rather than evaluating Python, and
`vm._div` is `abs(a) // abs(b) * (1 if (a < 0) == (b < 0) else -1)` with
`_mod` derived from it -- truncation toward zero, the same definition
`expr.BINARY_OPS` carries and the reason 26.208 and 0049 exist. The one gap
worth naming is that the walker's `relate` is asserted against written-out
constants rather than against a compiled predicate, which is the weaker of
the two available proofs and the one `test_walker` explicitly argues against
elsewhere: "the four are five spellings of that module, so comparing against
it would be asking one implementation whether it agrees with itself."

### 26.230 Two constructs accepted, and what each is waiting on

0050 and 0051 are accepted, not built. Both came from the copyright holder
in one sitting, and they turned out to be the same question asked twice:
**what may a schema say that the bytes do not.**

**0050, external arguments.** A format whose shape follows a fact the
message does not carry -- a negotiated cipher suite, a card class, a block
size. `prefix(...)` was already the precedent that a caller may know
something the message does not, and `const` already parsed with no way to
set one from outside. The design crosses two axes, *when* the argument is
known against *what* it may affect, and takes the cells that cost nothing:
`--define` for a generation-time `const`, and `parameter u8 name;` as a
zero-width member so that sizes, `at expr`, `[since]`, `require` and
`invariant` read it through machinery that already exists. A parameter may
decide meaning freely and may move a member only under `[stream]`, because
a per-message argument cannot reach a Lua dissector and 0049 settled that
four of five is not a rule.

**0051, messages a schema carries.** A schema should be able to say "when
these fields hold, this is what it means", so a dissector and `situ-edit`
can be helpful about a format without format-specific callback code --
which is a sixth description of the layout, in a place none of the five can
check. Three separable gaps: no predicate over several members of one
message, no identity for a failed check, and no notion of a message that is
not a failure.

**The holder's constraint is what made 0051 a design rather than a
feature.** The first draft left "when does this run" open, and the answer
returned was: flat, once, at the most useful time. situ already had exactly
one such point -- `validate` is "one line about the whole struct", the
caller invokes it, and it recurses into nested structs, so validating a
file's top-level struct is "the end of processing the buffer" already
spelled. Pinning `when` there makes the model flat *by construction*: no
evaluation on an accessor call, so a getter stays arithmetic; no `when`
reading another, so no order and no cascade; no `on_read` form, because
that is the `effect` axis and means something else.

Reading `_validate` to place it turned up the wrinkle that decides the API.
It short-circuits -- "order matters because the first failure is the
answer" -- which is right for a verdict and wrong for collecting messages,
so the messages come from a sibling that does not stop and that a caller
who wants none never links.

**Two things worth carrying out of both records.**

*The identity is the contract; the text is a default.* The `_SCALE` split,
for the reason `_scale_macros` already gives about fixed point: the exact
fact belongs in the header and the rendering needs a type -- here a locale,
a log format and a flash budget -- situ cannot choose. It also answers
localisation without inventing any.

*And for once the Lua dissector is the description that gains most.* 0049
and 0050 both turn on it being the one that cannot follow; Wireshark's
expert info is 0051's construct exactly, and `gen-dissector` emits none
today.

**The hazard is stated rather than left to be discovered.** situ cannot
check prose. A `note` that says something false about the format is shown
with the compiler's authority behind it, and nothing in the tree can catch
it -- the first construct where a schema asserts something no check can
hold it to, which is 14.5's rule pointed the other way.

Neither is built. 0050's `parameter` has no position, so the lattice needs
an `offset` answer for it -- 26.209's question about an arm's offset is the
nearest precedent -- and its view-constructor change is the same API-shape
cost 0046 carries, which is an argument for landing those two together.

### 26.231 The walker names the check that refused, and nothing else changed

Moved to `doc/decision/26.231-the-walker-names-the-check-that-refused-and-nothing-else-changed.md`.

### 26.232 C names the member that refused, and `validate` is a wrapper

Moved to `doc/decision/26.232-c-names-the-member-that-refused-and-validate-is-a-wrapper.md`.

### 26.233 Two bounds accepted where the answer was a refusal

Moved to `doc/decision/26.233-two-bounds-accepted-where-the-answer-was-a-refusal.md`.

### 26.234 The magic situ cannot write, found by three schemas independently

Moved to `doc/decision/26.234-the-magic-situ-cannot-write-found-by-three-schemas-independently.md`.

### 26.235 The generated comment that disagreed with the generated code

Moved to `doc/decision/26.235-the-generated-comment-that-disagreed-with-the-generated-code.md`.

### 26.236 A guard whose every clause was true, over the wrong set

Moved to `doc/decision/26.236-a-guard-whose-every-clause-was-true-over-the-wrong-set.md`.

### 26.237 Two attributes that parsed, and then were skipped

Moved to `doc/decision/26.237-two-attributes-that-parsed-and-then-were-skipped.md`.

### 26.238 One constraint, two spellings, two generated surfaces

Moved to `doc/decision/26.238-one-constraint-two-spellings-two-generated-surfaces.md`.

### 26.239 The construct that seals names its algorithm; the one that summarises cannot

0053, proposed, from respec's reader accepting a truncated image. Their
diagnosis was right and this entry is what checking it turned up.

**It is true of `tag` as well as `checksum`, which the report did not
claim.** A tag over a sealed region generates `tag_covered()`,
`tag_is_dirty()` and `tag_finalize()`, and a comment instructing the caller
to *write the result through `tag_ptr()`*. Neither construct computes
anything. `gen-tamper` does not either -- it takes a `situ_verify_fn` the
caller supplies.

**The asymmetry is in the AST and is an inheritance rather than a
decision.** `Sealed` and `Coded` each carry a `codec` field and name their
algorithm at the site -- `sealed(aes_gcm_256, nonce = nonce)`. `TagField`,
which serves both `tag` and `checksum`, has no such field. `checksum` was
defined as "`tag` with a non-cryptographic algorithm", and it inherited a
shape designed around the one thing situ deliberately does not implement.

**14.1 is right about the case it was written for and is being applied to a
different one.** Situ declines to implement AEAD, and that is sound: no
layout compiler should own constant-time behaviour or key handling. A CRC
has no key, no secret-dependent branch, and **situ already generates it** --
`std/kernels.situ` declares five polynomial codecs, each `impl ... derived`,
and `gen-derived` emits a real table and function. Both halves exist and
nothing joins them, so every caller writes the same three lines and respec's
wrote none.

**`derived` is the whole of the proposed restriction**, which is what makes
this a wiring change rather than a reversal: tier 2 is already the set situ
generates from a kernel description, so binding one to a checksum adds no
implementation. A `tag` still may not name one, and 14.1 stands untouched
for it.

**The part that needed deciding rather than assuming is when it runs.**
`validate` must not: it is a bounded constraint walk over a view, and a
coverage running to EOF is a pass over the whole file. Folding it in would
make a field check cost a file read and would reopen exactly the question
0051 was written to close. So the check is a function the caller invokes at
the flat model's one point, and `--refuse-ungenerated` is where a schema can
insist it exists.

**And respec's stated blocker was not the real one.** They believed the
coverage could not be declared because their schema stops after the INFO
chunk while the sum runs to EOF. `authenticated body { u8 rest[remaining]; }`
under `target file` is exactly that region, accepted, with its interior
deliberately undescribed. The half that was genuinely missing is the one
above.

### 26.240 A magic is one comparison now, in four backends and not the fifth

Moved to `doc/decision/26.240-a-magic-is-one-comparison-now-in-four-backends-and-not-the-fifth.md`.

### 26.241 The image carries the bytes, and a preamble states its content

Moved to `doc/decision/26.241-the-image-carries-the-bytes-and-a-preamble-states-its-content.md`.

### 26.242 A signature has no byte order, so the enum has no integer

0052 is built. `enum signature : u8[2] { bmp = "BM", pe = "MZ" }` names
spans, and a field typed by one is two bytes whose value must be an arm.

**The width is on the backing type because that is what it describes.**
How wide one value of this enum is. And a field typed by one is normalised
into the run it denotes immediately after parsing, rather than teaching the
solver a second way for a member to have a count -- every downstream reader
already knows what `u8[2]` means, and a second mechanism is one half of
them would have missed.

**Membership turned out to be the pinned-run check asked of a set.** That
is why this construct cost so much less than the two before it: one
emitter per backend, one image section, one walker branch, all already
written. A `[must_eq]` and a `preamble` pin one alternative; an enum pins
one per arm. `default = pass` pins none, because it says unknown values are
accepted and a check emitted anyway would refuse messages the schema
admits.

**No backend enumerates anything.** There is no integer to enumerate, and
giving the arm one would hand it the byte order the construct exists to
avoid -- `"BM"` is 0x424D or 0x4D42 depending on which way you read it, and
a format reference writes signatures as text precisely because the
question does not arise. So C emits `situ_m_bmp[2]` and `situ_m_is_known`,
and the other three their own spelling of the same pair.

**Four defects, and each was found by a different instrument.**

*mypy*: `Env` gained a field and two positional constructions shifted
silently. They are keyword calls now -- a six-field dataclass is not a
shape anybody should have to count.

*The compiler*: two pinned members in one Rust `validate` declared `WANT`
twice in one scope. `let` would have shadowed happily; a `const` does not,
so the check is a block now.

*`-D warnings`*: a `pub use m as M;` alias nothing used. Dropped rather
than allowed, since the module already carries the schema's name.

*The corpus*: `situc wire` crashed on the new enum, because it reads
`env.enums` and a byte-run enum is deliberately not there. It renders the
arms as written now, which matters more in that file than anywhere -- the
wire signature is the document a reader checks the bytes against, and a
number in it would put back the byte order the construct removed.

**`env.byte_enums` is separate from `env.enums` on purpose**, and the crash
is the argument for it: every reader that wants an integer fails loudly
instead of getting a plausible wrong one.

### 26.243 The checksum computes itself, in the two backends that can

0053, built. `checksum u8 crc[4] covers(body) is crc32;` generates
`crc_compute` and `crc_check`, and respec's scenario closes: a truncated or
corrupted image is refused by the layer whose job is to say an image is not
what it claims.

**`SITU_ERR_CHECKSUM` rather than `SITU_ERR_TAG`**, which is the same
distinction `stage` already draws one step further out. A tag that fails to
verify is a hostile or corrupt message; a checksum mismatch is corruption or
truncation. A receiver that cannot tell them apart logs a disk error as an
attack.

**Neither is called by `validate`.** A coverage may run to the end of the
message, so folding it in would make a field check cost a file read and
would reopen the question 0051 was written to close. The walker therefore
agrees by doing nothing, which is correct rather than a gap.

**Built for C and C++, refused by Rust and Python, and the split is the
construct's shape rather than a staging decision.** `gen-derived` emits C.
C has the implementation and C++ calls it across a linkage it already has;
Rust and Python have no derived codec at all, so a binding there generates
a call to a function no file in their language defines -- clean output,
correct-looking, failing at the first call. That is 26.241's accessor bug
one layer out, and 17.0 says refuse rather than surprise. **So it is
deliberately not in the corpus**: the four-way differential builds every
corpus schema in every backend, and two refuse this one by design.

**Four things the work turned up, and three were mine.**

*The byte order belongs to the codec's output, not the member.* Setting
`Placement.endian` from the scope to give the backend somewhere to read it
put `big` on a `u8[2]` in eight committed wire signatures -- describing an
order a byte run does not have. It is `tag_codec_endian` now, and the
member's own `endian` stays None. WOZ2 stores its CRC little-endian and PNG
stores its big, so guessing would be right for one format and silently
wrong for the other.

*A probe that declares what the generated code owes is a test of the
probe.* The first C probe declared `situ_crc32` itself, which would have
hidden a header that failed to declare it.

*And the fix for that went into the wrong backend.* Removing the hand
declaration showed C was already fine -- the codec's own block declares it
-- so the emitter gained a second, redundant one. C++ was the backend with
the gap, and its fix belonged in `_codec_prototypes`, whose docstring
already said why: a linkage specification is not allowed in a block. **The
sabotage is what found it**: removing the new declaration changed nothing,
because the real one was thirty lines further up and had been there all
along.

*The runtimes police themselves.* `situ_err_str`'s switch is exhaustive
under `-Werror=switch` and refused the new code until it was handled, and
`test_the_failure_classes_match_the_runtimes` holds `project.md` section
20.2 to the C enum -- so the document is the fifth mirror of the error
vocabulary and fails when it falls behind. Both fired, neither needed
looking for.

### 26.244 Writing one real example found three bugs nothing else could

Moved to `doc/decision/26.244-writing-one-real-example-found-three-bugs-nothing-else-could.md`.

### 26.245 Every CRC anybody uses, and not the sum on almost every packet

Moved to `doc/decision/26.245-every-crc-anybody-uses-and-not-the-sum-on-almost-every-packet.md`.

### 26.246 Three languages, one polynomial

`gen-derived` emitted C, so 0053's `checksum ... is <codec>` was honoured
by two backends and refused by two. It has Rust and Python backends now,
for the two families a checksum can name -- `polynomial` and
`ones_complement`. The other five decline with a note, exactly as C does
for a kernel it cannot generate.

**The arithmetic moved before the second backend was written.**
`kernel_math` holds the CRC table, the accumulator width, the reversal and
the reflected start; three renderers read it. Writing the table twice is
invariant 13's failure with a worse ending than usual -- two backends whose
digests differ for a reason no test would name -- and this tree has already
paid for four copies of a struct walk. The refactor was proved by
regenerating the C and diffing: identical.

**Where the implementation goes is a language question, not a taste one.**
C leaves it to `gen-derived` and the linker, which is how C joins
translation units. Rust and Python have no such culture: expecting a symbol
from a module the generated file cannot name would invent a path
convention the caller then has to satisfy. Those two emit the codec into
the module that calls it, once per codec, and only where a checksum names
one.

**Rust's type system found something C hides.** An eight-bit reflected CRC
shifts its register right by eight, and `u8 >> 8` is a compile-time
overflow that rustc refuses. C emits the same shift and is correct, because
`crc >> 8` on a `uint8_t` promotes to `int` and evaluates to zero. The
expression reduces to one table lookup at that width in both directions,
which is what all three now emit -- and it took a stricter language to make
anybody notice that the term was always zero.

**Python's divergence is the same shape from the other side.** A Python
integer does not wrap, so `crc << 8` grows without bound and every width is
masked, 64 included. In C and Rust the mask is a narrow-width correction
and the machine word does the rest; in Python the mask *is* the word.

**`[self_as]` is refused rather than computed, and that is the remaining
limit.** A checksum inside its own coverage says its own bytes read as zero
while the algorithm runs. A generated `compute` reads the span as it
stands, so it would sum the stored value and be wrong for every message --
and a wrong checksum is worse than an absent one, because it looks like a
verdict. IPv4, ICMP and UDP are all that shape and keep the caller's
arithmetic; PNG's CRC is outside what it covers and binds.

**The oracle got stronger by the same change.** It compared `zlib`'s CRC
over the span situ named, so situ supplied boundaries and something else
supplied arithmetic -- which was enough to catch 26.244. Now the schema
computes its own sum and `zlib` checks the whole answer: situ's table,
situ's loop and situ's span against an implementation nobody here wrote.
Inverting the reflection flag in the Python codec fails it.

**And the verification that mattered was not the vectors.** Thirteen CRCs
agreeing with a table this repository generated is one witness asked
twice. What settles it is a bit-at-a-time reference written from the
parameters in `std/kernels.situ` rather than from `crc_table`, agreeing
with all thirteen over nine inputs -- with a deliberately wrong `xorout`
shown to separate them, so the cross-check is known able to fail.

### 26.247 The hole becomes a parameter, and IPv4 computes its own checksum

Moved to `doc/decision/26.247-the-hole-becomes-a-parameter-and-ipv4-computes-its-own-checksum.md`.

### 26.248 Two spans, and the last two IP checksums bind

`prefix(...)` names bytes the algorithm sums before the message's own --
UDP's and TCP's twelve-byte pseudo-headers, built by the caller from a
layer the schema does not describe (14.2a). A generated `compute` had a
view and nothing else, so the clause was refused. It is threaded now, and
all four IP checksums that can bind do.

**The codec takes two spans rather than a seed.** `X_spans(a, alen, b,
blen, hole_at, hole_len, fill)` walks the concatenation and indexes the
hole against it, so one loop serves a CRC and a one's-complement sum alike.
The obvious alternative was a seeded or chained form -- sum the prefix,
carry the running value into the message -- and it is worse for a reason
worth keeping: **it publishes intermediate state.** A CRC's running
register is not its digest, the fold and the final xor have not happened,
and an entry point handing that out invites a caller to treat it as an
answer. Two spans is a wider signature and a narrower promise.

The three entry points nest rather than duplicate: `X` calls `X_holed`
calls `X_spans`, so there is one loop per codec however many shapes reach
it.

**Verified against sums nobody here wrote.** A hand-written RFC 1071
reference gives UDP 0xe855 over a pseudo-header and an odd-length payload,
and TCP 0xf383; situ agrees with both. The cases that matter are the last
two of each probe: corrupting a byte of the **pseudo-header** -- which is
not in the datagram at all -- must refuse, and a prefix of the wrong length
must refuse rather than be summed. The first is the whole reason the clause
exists; the second is the mistake a caller will actually make.

**And the punctuation caught me.** `compute` takes `out` last, so its
prefix parameters end with a comma; `check` takes nothing after, so they
need one in front. Reusing one string and trimming produced
`(situ_view_t view const uint8_t *prefix, ...,)` -- two errors in one line,
from one convenience. The first schema to use it did not compile, which is
the good ending; a generated signature that happened to parse would have
been the bad one.

**ICMP still does not bind, and now it is the only one.** Its declared
coverage is eight bytes where RFC 792 sums the whole message (26.247), so
what stops it is the schema rather than the construct.

### 26.249 The example was wrong, and only computing found it

Moved to `doc/decision/26.249-the-example-was-wrong-and-only-computing-found-it.md`.

### 26.250 An arm's magic, checked whatever arm arrived

Moved to `doc/decision/26.250-an-arm-s-magic-checked-whatever-arm-arrived.md`.

### 26.251 The sixteen-byte magic, and two databases rather than one

Moved to `doc/decision/26.251-the-sixteen-byte-magic-and-two-databases-rather-than-one.md`.

### 26.252 The entry a test cited and nobody wrote

Moved to `doc/decision/26.252-the-entry-a-test-cited-and-nobody-wrote.md`.

### 26.253 `remaining` reached the contract and not the code

Moved to `doc/decision/26.253-remaining-reached-the-contract-and-not-the-code.md`.

### 26.254 One construct, six descriptions, and five of them wrong

`chunk chunks[] while (remaining > 8)` is the shape a stream with no count
and no sentinel has to use, and 26.253 fixed it in the four compiled
backends. It was wrong in the other two, and wrong differently in each.

**The dissector abstained, and its reason was false.** It printed
`elements of no size this dissector can compute` for an element that is
eight bytes. `traverse.extent_parts` returns None for two unrelated
reasons -- a FIXED struct, because there is nothing to compute, and an
unmeasurable one, because there is nothing to compute it from -- and the
consumer read the first as the second. Invariant 154's case exactly: two
states, one piece of evidence. The four compiled backends had the fixed
arm already, C walking the same run with `SITU_CHUNK_SIZE_FIXED`, so this
was the fifth description missing a decision the other four shared.

**Then it abstained again, for the condition.** With the stride fixed the
run still declined, because the dissector's `_over_fields` never gained
the `extra` name table 26.253 gave the other four, so `remaining`
resolved nowhere and the whole condition was refused. A gap in one
description is invisible to a differential that compares only what both
sides speak about.

**And it blamed the size for it.** Those two refusals shared one message,
so a condition situ cannot render reported an element whose size is a
constant. A specific wrong cause sends the next person somewhere, which
is worse than a vague one; they are separate messages now.

**Both walkers took one element too many.** `_evaluate(sub,
placement.repeat_code)` measured `remaining` from the element's START
rather than from past it -- the only `_evaluate` in `walk.py` that did not
say where `remaining` begins. A `while` is a post-condition, so the cursor
has already passed the element the condition asks about. On a 40-byte
frame it took four eight-byte elements where the other five take three,
**swallowing the two members after the run**: `crc` and `marker` were
absent from the report and the message was called malformed. The C walker
had it too, passing `left` -- which correctly frames the element -- as
`remaining` as well.

**None of this was caught because nothing used it.** Six `while` runs
exist in the corpus and every condition read a plain field of the element
(`nla_len >= 4`, `kind == 0x11`, `separator == 0x2D`). The construct
reached the wire contract and no test. So `edges.situ` gains `hops`, whose
`trail` and `mark` are the assertion rather than decoration: an overrun by
one element is exactly what eats them. `structs_of` gives every acquirable
struct a section, so the four-way differential now probes `step count`,
`trail` and `mark` on 48 buffers, and
`test_the_walker_agrees_with_the_compiled_backends` compares the walker to
C line by line. Sabotaged back to the pre-advance read, that test fails;
with the fix it passes.

**The probe that proves it has to be `remaining > 8`, not `> 0`.** Three
chunks under `> 0` gives three either way, the walk's own bound stopping
it regardless. This is 26.253's lesson arriving a second time, and it cost
seven minutes here: the first fixture used `remaining`, which the
dissector was refusing for the *condition*, so it could not tell a fixed
element from a variable one in that backend at all. `while (length > 0)`
on the same pair discriminates.

### 26.255 A byte has no byte order

`_host_order` answered `placement.endian is NATIVE` and nothing else, so
netlink's `ifa_family`, `ifa_prefixlen`, `ifa_flags` and `ifa_scope` -- four
`u8` fields -- were drawn as opaque `ProtoField.bytes` under "the capture
does not record which machine wrote these". One byte reads the same on
every machine there has ever been.

`endian native` is a fact about a MULTI-byte number. The width is not the
whole test either: a byte-wide field straddling two bytes does draw from
two, so the guard asks whether the field lies entirely inside one byte.
Measured across every schema in the corpus, exactly one dissector changes.

### 26.256 A run that is not unrolled does not move the cursor

`_uncomputable` exists because four branches declined a member by
returning a comment and leaving `at` where it was, so everything after was
placed with confidence at a wrong offset. The delimited-run-of-records
branch was still doing it: it says honestly that it does not walk the run,
and then let the walk continue.

`edges.kv_block` is the one instance in the corpus, and it is the bad kind.
`payload` was emitted as `subtree:add(kv_block_f.payload, tvb(at))` with
`at` still 0 -- the whole frame, INCLUDING the `entries` run it follows,
shown as the payload field. A wrong line is worse than a missing one, and
this file says so three hundred lines above the branch that did it.

### 26.257 The traceback respec reported, still available under another name

26.253 resolved `remaining` and called the second fault the worse one:
`UnknownName` is meant to be caught by a caller that then declines the
member, "and on the `repeat_while` path nobody catches it." That half was
not fixed, only the one name that had been reported.

`situc wire` accepts `while (chunk.length > 0)` and records it in the
contract; `situc build` dies with `situc.names.UnknownName: 'chunk.length'`
for every backend -- a traceback where every other refusal situ gives is a
diagnostic, which is respec's complaint verbatim.

`_check_one_repeat` validates `path.rpartition(".")[2]`, the LAST component
only, so a qualified path whose tail happens to be a real field of the
element passes. A fully bogus name (`foo.bar`) is refused properly, and so
is a genuinely nested one (`h.len`) -- dotted paths are not supported in a
condition at all. **So the spellings that got past the check were exactly
the spellings that crashed.** Six while-runs in the corpus, none dotted, so
refusing them costs nothing.

### 26.258 Refused, and nothing refused

`situ_<struct>_check(view, &which)` exists to name the member that
refused -- "the id is the contract and the name is a macro, so nothing
here costs a string (0051)". Its header promises `*which` is "one of the
ids below on a refusal and 0xFFFFFFFF when nothing refused". For a
fixed-width text number it returned refused AND wrote the sentinel, so
one call's two outputs contradicted each other and the caller was handed
an id that names no member.

**The cause is a regex that decides what a refusal looks like.**
`_REFUSES` matched `return SITU_ERR_<something>;`, and a digit check does
not return a constant -- it propagates the accessor's own status with
`return e;`. So the member was grouped as refusing nothing, was given no
CHECK id, and the loop that writes `*which` before each refusal skipped
it. `cpio_header` carries thirteen such fields and named none of them:
`ino = "ZZZZZZZZ"` was refused as "nothing refused" while `namesize` out
of range was named correctly, three lines apart in the same function.

**Python already got this right**, which is what made it worth chasing:
it raises `ConstraintError: cpio_header.ino is not 8 digits in base 16`.
Two descriptions of one schema, one naming the member and one losing it.

18 refusals across four schemas are named now -- cpio 13, edges 3, http
and smtp one each -- and the ids renumber where they were previously
absent, which is a generated-header change rather than a schema one.

**The test asserts the population, not the case.** Over every schema in
the corpus, every line in a `check()` body that returns a refusal must be
preceded by `*which = `. Asserting cpio's thirteen would pass while the
next refusal shape nobody has thought of arrives silently; this way it
arrives as a failure. Reverting `_REFUSES` fails it, naming cpio and http.

**What found it was reading the differential's own list of what it does
not compare.** `codegen/differ.py` says the probed subset "is the thing
to grow", and names a fixed-width text number's value as excluded -- so
that is where four descriptions were free to disagree. They did not
disagree about the value; they disagreed about how a bad one is reported,
which is one layer up from where the sweep was aimed.

**And one instrument error, the third of its kind in a day.** The first
probe printed `which` in the same `printf` as the call that writes it,
where the evaluation order is unspecified: it read the sentinel it had
initialised and reported `which=0`, which is `MAGIC` -- a wrong member, in
a finding about reporting the wrong member. A second program with a
distinctive sentinel, reading after the call, gave the real answer.

### 26.259 One schema, one language building it and three raising

Moved to `doc/decision/26.259-one-schema-one-language-building-it-and-three-raising.md`.

### 26.260 A reader calling the function its own header declined to write

Moved to `doc/decision/26.260-a-reader-calling-the-function-its-own-header-declined-to-write.md`.

### 26.261 The axis was enumerated and the member was always data-sized

`compose.py` exists to put constructs next to each other and see what four
backends do with the pair, and it names the axis this needed:

    **what precedes it** -- nothing, a variable-length run, or a
    delimiter. This is the axis that decides whether the member's offset
    is a constant, and 26.49 is eleven places that assumed it was.

The axis was there. 26.259 still happened -- `u8 head[2]` behind a
variable-length member, building in C and raising `offset is dynamic` in
the other three -- because of one line:

    FORMS = {"count": "[{read}]", "arith": "[{read} + 1]",
             "remaining": "[remaining]"}

**All three forms are sized by the DATA.** The member under test always
carried a count read from the message, so the simplest member of all --
a literal count, where everything visible about it is static and only
its offset is not -- never occupied the position this file was built to
test. That is exactly the shape a backend assumes is constant, which is
why it was the shape that broke.

`"fixed": "[2]"` takes the space from 6300 cells to 9000.

**150 of the new cells, seed 23: 104 agreed, 46 did not.** And the
element axis predicts which way, almost perfectly:

    element   crash  build  disagree
    rec          12      0         0
    vrec          0     27         0
    i32/u16       0      0         6
    u8            0      1         0

So four defects rather than forty-six. A fixed-count run of a FIXED
record crashes the compiler; of a VARIABLE record it emits code that
does not build -- `run_span` called by a later member's offset and never
declared; a fixed-count run of WIDE scalars is read without a frame
check; and a byte array in a variant arm has C calling an offset
function it does not declare.

**The wide-scalar one is memory-safety, not a wrong number.** C bounds
its element read with `situ_in_bounds`; C++ emits a bare
`situ_get_be32(raw_.base + offset + index * 4)`. Under
AddressSanitizer that is a heap-buffer-overflow, zero bytes past a
14-byte region, reached from a schema situ accepts and compiles
silently. Python and Rust check the INDEX and not the frame, so they
answer wrong values where C answers zero. The same generated file
frame-checks `tail`, a scalar at the very same dynamic offset.

**All forty-six reproduce at `cdf5eeb~1`**, before 26.259 and 26.260 --
checked by copying this `compose.py` into a worktree of that commit and
running four representatives. Nothing here is a regression; the form
made pre-existing defects visible, which is what it is for.

**`test_composed_schemas` still passes**, its fixed 25-cell sample
having drawn none of them. That is the seed's luck rather than evidence,
and it is worth stating plainly: the enumerated space now contains cells
that are known to fail, and a future sample may draw one. The module's
own argument is that this is the right trade -- "the point is not to run
every cell but to have the cells *enumerated*, so a sample is a sample
of something known rather than of whatever somebody thought of."

### 26.262 An element the frame does not reach, and the instrument that cannot see it

Moved to `doc/decision/26.262-an-element-the-frame-does-not-reach-and-the-instrument-that-cannot-see-it.md`.

### 26.263 The C walker read a nested struct as its minimum

Moved to `doc/decision/26.263-the-c-walker-read-a-nested-struct-as-its-minimum.md`.

### 26.264 A guard that tested alignment under a comment about dynamism

Moved to `doc/decision/26.264-a-guard-that-tested-alignment-under-a-comment-about-dynamism.md`.

### 26.265 A pad counted as no bytes, and the total that is not an offset

Moved to `doc/decision/26.265-a-pad-counted-as-no-bytes-and-the-total-that-is-not-an-offset.md`.

### 26.266 The third branch of the same emitter

Moved to `doc/decision/26.266-the-third-branch-of-the-same-emitter.md`.

### 26.267 Two predicates, one spelling apart, six descriptions wrong

Moved to `doc/decision/26.267-two-predicates-one-spelling-apart-six-descriptions-wrong.md`.

### 26.268 The copy that knew less, and a message covering five causes

Moved to `doc/decision/26.268-the-copy-that-knew-less-and-a-message-covering-five-causes.md`.

### 26.269 A value in scope, and the sentence beside it not asking

Moved to `doc/decision/26.269-a-value-in-scope-and-the-sentence-beside-it-not-asking.md`.

### 26.270 A negative reads as zero, in five descriptions and not the sixth

Moved to `doc/decision/26.270-a-negative-reads-as-zero-in-five-descriptions-and-not-the-sixth.md`.

### 26.271 The strict reader where the lax one belongs, and two near-misses

Moved to `doc/decision/26.271-the-strict-reader-where-the-lax-one-belongs-and-two-near-misses.md`.

### 26.272 Generated code the size of hand-written, except where it was 3x

Asked whether situ's accessors are bigger or smaller than hand-crafted
ones, and measured rather than answered. IPv4's fixed header, every field
read once, against a competent hand-written set -- named offsets, explicit
byte order, no bounds checks. `.text` of the object, gcc x86-64:

                        -Os              -O2
    byte-aligned    situ 36  hand 36   situ 40  hand 40
    bit-packed      situ 252 hand 77   situ 80  hand 82

**The byte-aligned answer is the one the design predicts and it holds
exactly.** `situ_ipv4_header_total_length_get` is one load and one bswap,
which is what a person writes. Identical at both levels.

**The bit-packed row was a defect, and `-Os` is where it lived.** At -O2
situ is two bytes smaller than hand-written. At -Os gcc stops inlining
`situ_bits_get_msb` once a function holds enough of them -- six here --
and emits one out-of-line copy. The generated call passes literals,
`situ_bits_get_msb(view.base, 51u, 13u)`, and out of line those become
register arguments, so the byte-at-a-time loop that folds to two loads and
a mask survives into the object and runs. Seven calls into a run-time
loop, where hand-written code has fourteen instructions.

    5e:  call 0 <situ_bits_get_msb>       x7
    0000 <situ_bits_get_msb>:
    19:  cmp %ecx,%edx ; 1d: movzbl (%rdi,%rcx,1) ... 2c: jmp 19

Slower as well as bigger, and `-Os` is what `build-and-commit.md` mandates
for these projects. Nine of the thirty-one example schemas emit a
bit-field read: tcp, mqtt, rtc, dns, ipv4, telemetry, ntp, dnsname,
message.

`SITU_ALWAYS_INLINE` on the four bit helpers takes 252 to **78**, against
77 by hand. C++ reads bit fields through the same four functions, so it
inherits this; the walker does not, its offsets being run-time values by
construction.

**The cost, measured in the other direction, because a fix tested only
where it helps is not tested.** The derived codecs call these in loops
with a run-time offset, and inlining there grows twelve functions --
`base16`, `manchester`, `4b5b`, the two stuffings -- by 3 to 54 bytes,
8475 to 8799 total. Taken: those are per-symbol inner loops, so what is
being bought with 324 bytes is a call removed from each iteration, and
the accessor path is code every consumer of a bit-packed schema pays for
without having written it.

**The first version of the test passed with the fix reverted.** Four bit
fields is under gcc's inlining threshold, so the fixture named the hazard
and could not reach it -- a test over the safe path, green either way. It
reads sixteen now, gcc declines at six, and the sabotage fails through the
`nm` assertion rather than through a compile error. The assertion is on
the symbol rather than on a byte count, since a count is a claim in a
particular compiler's units.

### 26.273 A bound the descriptions disagreed about, behind a subcommand split

openmlx4 reported four subcommands giving three answers about one file:
`map` and `wire` exit 0 on `[must_eq = (0 - b0 - b1 - b2) % 256]` while
`build` and `verify` exit 1. They had been about to recommend `situc map
--check` as their gate and changed the recommendation to run `verify` too.

**They were right, and the divergence was the visible end of a defect
that is not about subcommands.** Two causes, and the second is the one
worth the entry.

**One: `invariant.OPERATORS` was `frozenset("+-*/")`.** `%` was missing
rather than excluded -- `relation.OPERATORS` has carried both since it
was written, and the docstring's exclusions are comparison and bit
twiddling, neither of which `%` is. An unrenderable bound reaches
`bound_refusal` and a renderable one does not, so `build` refused what
`map` published a row for.

**Two: a bound naming a sibling is rendered as TEXT by each backend, and
`/` and `%` do not mean the same thing in all six.** C, C++ and Rust
truncate toward zero; Python and Lua floor. Measured, on the two bytes
`03 7f` against `[must_eq = (0 - b0) / 2 + 128]`:

    C      situ_s_check(view)  -> SITU_OK
    Python v.validate()        -> ConstraintError, wanting 126

Six descriptions of one layout, built from one schema, disagreeing about
which messages are valid. That is the property the compiler exists to
hold, and it had been available since bounds learned to name a sibling.

**`interval_of` has carried the refusal from the beginning.** "The left
operand of `%` may be negative", with the note about C against Python and
the remedy of `[min = N]`. It carried it for SIZE expressions, because a
size is what asked. A bound reaches the same four backends by another
route and had never asked -- **so the rule was live on one of the two
paths that need it, and the path it was missing from is the one whose
failure is silent.** A wrong size is a wrong length and something
notices; a wrong bound is a message one description accepts and another
rejects, with no run in which both are present.

**Where the guard goes decided itself twice.** It is in the solver rather
than in each backend's `_attr_checks` for 26.233's reason: `map` and
`wire` call `solve` and not codegen, so an emitter-side guard leaves them
publishing a diffable artefact for a schema that does not compile. And it
is in `record_interval` rather than in `constrain`, which was the first
attempt: `constrain` takes no `Walk` and so no field env, and hung one
level out the check refused `[must_eq = "BM"]` -- a signature on a byte
run, arithmetic-free -- in six committed schemas. `record_interval`
returns early for anything that is not a scalar, which is exactly the
population that can carry arithmetic.

**What the peer could not have found, and what they could.** They had the
symptom and no way to see the cause; the reasons were here. The reverse
also held earlier in the same exchange -- their `--owned` BCD report is a
defect this tree's own tests do not reach. Neither side could have
produced the other's half.

**The population assertion caught the operator addition arriving
alone.** `test_the_builtin_cases_are_the_builtins` asserts
`set(OPERATOR_CASES) == set(OPERATORS)`, so adding `%` to one set without
a case in the other went red immediately. A set stated in one place and
walked from another cannot grow on one side.

Both fixes sabotaged separately, each failing through its own test and
leaving the other green. The byte-sum check openmlx4 wanted is
expressible with the subtraction lifted above zero --
`[must_eq = (768 - b0 - b1 - b2) % 256]`, 768 because it must be
congruent to 0 mod 256 and at least 765 -- and C and Python agree on all
1260 byte quadruples tried, 12 of them accepted, so it discriminates.

### 26.274 The conversion the accessor applies and the checker beside it skips

Moved to `doc/decision/26.274-the-conversion-the-accessor-applies-and-the-checker-beside-it-skips.md`.

### 26.275 A round trip cannot see a conversion dropped from both sides

Moved to `doc/decision/26.275-a-round-trip-cannot-see-a-conversion-dropped-from-both-sides.md`.

### 26.276 A capability withheld for a reason that was false

Pointing 26.275's lens deliberately -- wherever a value is stored in one
encoding and read in another, the conversion and the check beside it are
separate paths -- found a fifth instance immediately, and it is a
different species from the four that produced the lens. Nothing was
converted wrongly. **A conversion that never happens was described in
prose as though it did**, and a capability was withheld on the strength
of it.

`declared_value_bounds` is the shared decision point all four backends
render, and it excluded fixed point and BCD:

    Fixed point and BCD are excluded because the getter's value is
    scaled or decoded, so exporting the raw bound would hand a caller a
    constant in the wrong domain -- worse than none.

**Nothing scales anywhere.** Measured across all four for
`q8_8 trim [max = 100]`: every getter returns the raw stored integer
(`int16_t`, `std::int16_t`, `i16`, `int`), every `validate` compares the
bound against it, and `_SCALE` and `_FRAC_BITS` are emitted beside them.
Section 8.1 says so outright -- "neither generates floating point ... the
caller does the arithmetic in whatever type it has" -- so the sentence
contradicted the specification it was implementing.

**And the BCD half stopped being true the same day.** Its bound is
compared decoded in all four only since 26.274; before that Rust compared
it against the packed nibbles. So the rationale was describing a real
hazard, in the one type it applied to, which had just been fixed one
entry earlier.

**The claim lived in three places and had been copied twice** --
`traverse.py`, `test_value_bounds.py`, `test_codegen_cpp.py` -- with two
tests asserting the absence and citing the false reason as their
docstring. That is the shape working-practice.md warns about: a
correction has to go where a reader will next look for the claim, and
here the two tests were the copies that would have outlived a fix to the
source.

**Held open rather than resolved, and that was right.** Two coherent
answers existed -- bounds are raw, so the exclusion is unjustified; or
bounds are scaled, so `validate` is wrong in all four backends -- and
nothing in the tree decided between them, because **no committed schema
puts a bound on a fixed-point field.** Only two test fixtures do. The one
committed fixed-point schema reads it the other way: `rtc.situ` describes
`trim_ppm` as running "-128 to just under +128 parts per million", which
is the scaled figure. A person's natural reading and the compiler's
behaviour disagreed, and neither was written down.

Settled by the copyright holder: **bounds are raw**, and 8.1 now says
which domain a bound on a converted type is in, with the `[max = 25600]`
spelling for a schema that means 100 ppm. The macros are exported for
both types, in the getter's own type, and the scaled variant was
deliberately declined -- the caller has `_SCALE`, and a second constant
is a second thing to be wrong.

**What tests it is the executable half, and only for a converted type.**
`test_value_bounds_agree_with_validate` has driven the module's own
constants through its own `validate` at both boundaries since 26.125 --
over a `u16`, for which a right-domain and a wrong-domain constant are
the same number, so it could not have caught this. The two added beside
it can: a fixed-point constant exported pre-scaled would be 25600 and
fails, and a BCD constant exported as packed nibbles would be 0x12 = 18
and fails. **A canary over the one type where the two domains coincide is
a canary that cannot sing.**

### 26.277 The library situ installed and gave nobody a way to name

Asked whether a consumer can import an example schema without knowing
where situ is installed. Measured: **no**, and the reason was good while
the consequence was not.

`import "..."` resolves relative to the importing file, deliberately and
with 17.0 cited: "there is no search path, so which file an import names
does not depend on how the compiler was invoked." That rule is right and
is unchanged. What sat behind it is that **`make install` puts
`std/*.situ` in `$(PREFIX)/share/situc/std`, `debian/situc.install` ships
it, and nothing anywhere tells a consumer where it went** -- no flag, no
mention in `--help`, and `grep -rn "share/situc"` across `situc/`,
`bin/situc` and the README returned nothing. The library was installed
and reachable only by writing an absolute path that varies per machine
into a schema.

**17.0's objection does not reach this case, and that is the opening.**
What it refuses is a search *path* -- a list, where "which of these won?"
has no visible answer. One directory is not a list. `import std
"kernels.situ";` reads from where situ put its own schemas, found the way
`bin/situc` finds its own package, and an import still names exactly one
file.

**Neither form falls back to the other**, which is what makes the second
spelling worth having rather than a convenience. A fallback would mean
that dropping a file beside your own silently changes which file an
existing import names -- the shadowing hazard `#include "..."` has, and
the reason C keeps `<...>` separate. The test for it had to be written
twice: the first sabotage did not change behaviour for its fixture, so it
passed, and only a sabotage that actually implements the shadowing --
prefer a local file where one exists -- made it fail. **A sabotage that
cannot reach the code it is aimed at reports the same green as a working
guard.**

A word rather than angle brackets, per 0006, and because `<kernels.situ>`
lexes as five tokens that the LSP and the unparser would both have to
learn to reassemble -- context-sensitivity in a lexer that has none, to
save a word. `std` is a soft keyword read the way `at` is.

`situc --print-std-dir` is the same answer outside a schema, for a build
system that wants to depend on a shipped schema or hand it to a tool that
is not situc. It exits non-zero where the directory cannot be found
rather than printing nothing and exiting 0, which a shell would read as
an empty path and use.

**Two instruments earned their keep on the way, and one nearly did
not.**

`test_it_names_only_the_flags_that_are_global` holds the parser's global
flags against what 21 claims, so the new flag arriving without the
section being updated went red immediately -- the population rule again.
But it then failed through a `ValueError`, because it sliced the section
from the literal phrase "Two global flags" and the fix had made it
"Three". **A true failure reported through the wrong half of itself**: a
reader would have gone looking for a broken index rather than a missing
sentence. It anchors on "global flags" now.

And fourteen unrelated tests failed in the same run, all at the linker,
for a missing `build/host/runtime/libsitu.a`. That is 26.87 exactly --
the Makefile's own comment says `test-py: runtime` exists because "on any
tree where the archive did not already exist, ten tests failed at the
linker" -- met by running pytest directly and bypassing the prerequisite,
in a shared tree where the archive had gone. **Fourteen compile-and-run
tests failing together, in a run that took fifty minutes instead of
fourteen, is a fact about the machine before it is a fact about the
code**, and reading one error message settled it faster than reading any
diff would have.

### 26.278 The examples ship, and become a library by being importable

Instructed by the copyright holder: install the examples, split out the
teaching ones, install the rest. "Why would we create them if not to use
and inform a user?"

**They were already importable and nobody had tried.** `import
"ipv4.situ"` into a consumer's `tunnel` struct produces a full capability
map, checksum coverage carried through onto `tunnel.inner`, directives
not colliding. So this was never a rework -- only 27 of them did not
ship, and none of them could be named.

**The root moved from `<tree>/std` to the tree itself**, which it had
been for about an hour. The reason is a wall worth recording, because
every cheaper arrangement hits it: **an installed layout that categorises
schemas cannot be mirrored by a source tree that does not, and one import
spelling cannot mean two things.** Either the source tree is categorised
-- which is a 27-directory move touching 35 path-constructing references,
and which decides the naming question the holder had explicitly deferred
-- or the install mirrors the source. Mirroring costs a `std/` in the
spelling of a `std/` file and buys the property that matters: a schema
that compiles here compiles against an installed situ, unchanged, and a
later rename of these directories moves the import paths with them
without touching the language.

**The split is recorded rather than built into a path**, for the same
reason. `example/designed.txt` lists the five schemas situ invented --
`keystore`, `message`, `packet`, `register`, `telemetry` -- and the other
26 are described. **The corpus names its own taxonomy**, which is why
this needed no judgement: `keystore.situ` says "every other directory
here is a public format somebody else specified ... or an exercise named
for the capability it demonstrates", and `telemetry.situ` says "designed
rather than described. The other examples document formats that already
exist." The list is held to the tree by a partition assertion and to the
schemas by a second test reading each one's opening comment, because a
list of names is second-hand and the direction that costs somebody
something is a designed schema borrowed as though it were specified.

**And making them a library found what being a document had hidden.**
Importing `example/ipv4/ipv4.situ` and `std/kernels.situ` together is
refused: `codec internet_checksum is declared more than once`. Measured
across the corpus:

    6 of 31 examples redeclare a name std/ has
        internet_checksum   icmp, ipv4, tcp, udp
        crc32               png
        aes_gcm_128         packet

    5 names are declared by more than one example
        header      dnsname, message, packet
        msg_type    message, netlink, packet
        packet      mqtt, packet
        record      dtls, message

Nothing is broken: 17.0a's namespace is flat by decision, and
`check_unique_declarations` is doing exactly what it says. What the
measurement shows is that **each example was written to be read alone**,
and self-containment is correct for a document and wrong for a library
member. One example imports fine; two may not.

Left open, and it belongs with the naming: whether a library member
should `import std "std/kernels.situ"` rather than restate a codec, or
whether these want the `namespace` that decision 0012 already provides.
The second is the larger answer and the one that scales past six
collisions.

### 26.279 Three breakages in `make test-c`, stacked behind each other

Moved to `doc/decision/26.279-three-breakages-in-make-test-c-stacked-behind-each-other.md`.

### 26.280 A guard aimed wider than its subject, and two that hid each other

Moved to `doc/decision/26.280-a-guard-aimed-wider-than-its-subject-and-two-that-hid-each-other.md`.

### 26.281 Bounded recursion, and the one line that was doing the refusing

0054 implemented as far as description goes. A struct that names itself
and declares `[depth = N]` is accepted, laid out and mapped; one that does
not is refused as before, now with the remedy in the diagnostic.

**What "non-terminating" meant was one function re-entering itself.**
Section 2 justified the ban for eight months on the grounds that size and
capability computation do not terminate, and lifting the refusal to look
showed exactly where:

    layout_of -> place_members -> place_one -> element_extent
              -> layout_of ...  RecursionError in Scope.narrow

`layout_of` had no re-entry guard because nothing could re-enter it. One
`in_progress` set and one early return is the whole of the solver change,
and **the placeholder it returns needed no new machinery at all**: a
self-reference is worth zero-to-unbounded, which is an ordinary
variable-length nested member, and every backend already understands one.
The feature that had been called non-terminating was four lines and a set.

**The upper bound is honest rather than derived, and that is a limit
worth stating.** 0054 says `depth` is what makes `size_max` a number, and
for a *counted* recursion it is not sufficient: unrolling 32 levels of
`node children[count]` with an unbounded `u16 count` gives a finite
maximum that is astronomically large and no use to anybody sizing a
buffer. A usable bound needs `[max]` on the count as well as `[depth]` on
the type -- which is openmlx4's `size=20..17179869200` met from the other
end, and the same answer: the number is the schema's silence becoming
visible.

**Describable is not generable, and the refusal says which is which.**
All four backends emit for a recursive type and the C header does not
compile -- `situ_node_children_extent` calls `situ_node_extent` above its
declaration -- and a forward declaration would only convert that into
run-time recursion in generated code that 20.1 promises has none, with
`depth` and `limit` enforced nowhere. `situc build` refuses by name on
26.69's precedent; `map`, `wire` and `verify` are correct, which is the
specification path two consumers actually adopted.

**Three tests asserted the old rule and one asserted the old wording,
which is the guard working rather than a cost.** `test_direct_recursion_
rejected` is `test_direct_recursion_is_rejected_without_a_declared_depth`
now, with an acceptance case beside it: the old name said the refusal was
unconditional, and it has stopped being.

**And one failed for a reason worth keeping.** `test_every_documented_
make_target_exists` refused 26.280 for naming another project's `schema`
target in a `make` command, which is
*fuzznet's* target quoted in their report and not one situ has. The guard
cannot tell a foreign command from a claim about this tree, and it should
not be taught to -- so the prose changed instead. **A document that
quotes another project's commands will keep tripping a check that reads
every backtick as a claim about here**, which is the voice rule arriving
somewhere nobody had put it: their command in their voice still reads as
this tree's to a regex.

**What JSON gets from this, which is less than the question implied.**
Recursion was never its main blocker. A JSON object is a string-keyed TLV
and situ's `known` takes integer literals -- buildable -- but members
identified by name with no order is what a text codec *means*, and 9.6's
"discriminant strictly before the variant in layout order" is a statement
about a world where order exists rather than a restriction to relax.
netcfgd put it best and it is their sentence: the two projects' premises
point opposite ways. What 0054 does serve is their case and not JSON's --
a document of known types, 117 nodes and depth 7, where the recursion is
in the codec rather than in the type described.

### 26.282 A proxy that agreed with the thing for every schema in the corpus

0054's generating half, for C. The routing bug that blocked it is the
find worth keeping.

**`_is_array` asked the resolver a question about the schema.**

    return self.resolved.find(placement.path + "[]") is not None

That is *"did the resolver absorb this element's members under `x[]`"*
standing in for *"is this member a run"*. The two agree for every
non-recursive element, which is why nothing had ever caught it. They come
apart on a struct that names itself: absorbing a recursive element's
members would not terminate, so the resolver correctly emits none -- and
the proxy then answered "not an array" for `node children[count]`,
routing a run to the single-nested-struct path. The header called a span
nothing defined.

**Replacing the proxy outright was wrong, and the suite said so in 25
places.** `dnsname`, `netlink`, `ipv6ext` and `edges` have members that
satisfy "has a count or a length field" and are single nested structs to
the resolver, so asking the placement instead routed them to the run path
and their `_view` helpers stopped being emitted. The resolver's answer is
kept and the placement is asked only where the resolver cannot have one,
which is the self-referencing case and nothing else. **A proxy that is
wrong in one case is not a proxy to throw away** -- 26.5's rule says ask
the object you mean, and the object here is "is it a run", which the
resolver answers correctly everywhere it can answer at all.

**What the depth buys, measured by running it.** The extent carries a
depth and the run's span passes `depth + 1u` down, so nesting is bounded
by the schema rather than by the message's length:

    levels= 1  bytes=  3  extent=  3  whole
    levels=31  bytes= 93  extent= 93  whole
    levels=32  bytes= 96  extent= 96  whole
    levels=40  bytes=120  extent= 96  SHORT (bounded)

Without it a hostile message nests as deep as its own bytes allow -- for
a three-byte node, a 64 KB message is 21845 frames -- and 20.1's
"bounded stack" is what the bound is for.

**At the limit the extent answers zero, which is a short answer and not a
refusal**, and 26.113 is right that a limit folded into an answer
produces wrong values indistinguishable from right ones. `extent` returns
a length and has no error channel. So the refusal belongs in `validate`,
and this is a stack backstop -- safe for the reason a text number's
`_value` is safe: a validated frame never reaches it. **That is 26.271's
bargain reached independently by a different route on the same feature.**

**And two forward declarations, because no order exists.** `extent` calls
the run's span and the span calls `extent` back. Every other struct in
this compiler is emitted in containment order; a cycle has none, and a
prototype is what C has for that.

**The verify half was openmlx4's, and the crash was its smaller part.**
`situc verify` caught every exception from the generated module and
rendered it as "does not conform ... from an implementation that is not
this schema". An `AttributeError` for an extent the Python backend does
not emit is situc failing, and telling a reader their bytes are wrong
sends them to their bytes -- where they went, for two probes, before
doubting the compiler. It renders as a compiler fault now, in the summary
line as well as the body, because the summary is what a CI log keeps.

**Three mistakes in the fixing, each caught by something already here.**

- The refusal base was imported at the top of `verify.py`, and `_module`
  builds a *fresh* `situ_runtime` per call -- so the class is a different
  object and `isinstance` was always false, turning every honest refusal
  into "situc failed". The control test beside the new one caught it, and
  it existed only because separating two verdicts is worth nothing unless
  the other still reads correctly.
- `broke` was added *between* `refusal` and `mismatches` in a dataclass,
  silently reassigning every positional `Outcome(case, text, mismatches)`
  in the file. Four verify tests went red for a reason unrelated to what
  was being changed. It is the last field now, and says why.
- The guard on documented make targets refused **this section** for
  quoting another project's target. A sweep finds its own report, and the
  fix is the prose rather than the guard.

**And the docstring in `test_recursion.py` claimed verify worked while
nothing in the file called it.** openmlx4 found that by using it. An
untested claim in a docstring is a signal with no artifact, and it
reached HEAD because the sentence was easier to write than the call.

### 26.283 The same bound in four descriptions, and the directive behind it

Moved to `doc/decision/26.283-the-same-bound-in-four-descriptions-and-the-directive-behind-it.md`.

### 26.284 Where a depth refusal can live, and where it cannot

Moved to `doc/decision/26.284-where-a-depth-refusal-can-live-and-where-it-cannot.md`.

### 26.285 Two paths answering one question from two different facts

Moved to `doc/decision/26.285-two-paths-answering-one-question-from-two-different-facts.md`.

### 26.286 The walker's ceiling was a number nobody had compared

Moved to `doc/decision/26.286-the-walker-s-ceiling-was-a-number-nobody-had-compared.md`.

### 26.287 Two readers of one image, agreeing about different messages

Moved to `doc/decision/26.287-two-readers-of-one-image-agreeing-about-different-messages.md`.

### 26.288 The cycle is the unit, and six descriptions disagreed by one

Moved to `doc/decision/26.288-the-cycle-is-the-unit-and-six-descriptions-disagreed-by-one.md`.

### 26.289 What JSON asked for, and the eight things it found on the way

Moved to `doc/decision/26.289-what-json-asked-for-and-the-eight-things-it-found-on-the-way.md`.

### 26.290 A character is not a byte, and a terminator is not a separator

Moved to `doc/decision/26.290-a-character-is-not-a-byte-and-a-terminator-is-not-a-separator.md`.

### 26.291 A terminator is not a separator, and JSON is described

Moved to `doc/decision/26.291-a-terminator-is-not-a-separator-and-json-is-described.md`.

### 26.292 Whitespace is a member's, and the bill said it could not be

Moved to `doc/decision/26.292-whitespace-is-a-member-s-and-the-bill-said-it-could-not-be.md`.

### 26.293 Two reasons one gate was red, and the second hid the first

Moved to `doc/decision/26.293-two-reasons-one-gate-was-red-and-the-second-hid-the-first.md`.

### 26.294 A check that fits in one byte, and a corpus that does not

`gen-checks` tested a pinned value only when the member was **exactly one
byte wide**. The gate is two lines and neither carries a comment:

    if placement.offset_bits % BITS_PER_BYTE or placement.size_bits != BITS_PER_BYTE:
        continue

Counted across the corpus, that is 20 of 60 `[must_eq]` and 39 of 62 enum
members. What went untested is the characteristic field of each format --
`bmp.signature`, `sqlite.magic` at 128 bits, `tiff.magic`,
`image_header.magic`, `ethernet.ethertype`, `mqtt.packet.kind`,
`arp.operation`.

**And the drop was silent, with the mechanism for saying so two functions
away.** `Suite.skip` exists and `generate` renders what it collects; the
`must_eq` loop used it for an unfoldable value and fell through the width
without a word. So `bmp_checks.c` did not contain the string `signature`
anywhere: from the artifact, an untested BMP magic and a tested one are the
same file. That is the standard `_declined` states one module over -- a file
that quietly omits something is indistinguishable from one that never had
it.

**The width was never the hard part.** `_reserved_checks` had placed a value
at any width since it was written, through `_placed_bytes`, which knows the
member's byte order and bit order. Extracting that as `_break_to` and
pointing the other two checks at it is the whole of the fix; the arithmetic
was already in the file, in the one check that did not need it most.

    _must_eq_is_enforced                17 -> 50
    _rejects_a_value_no_member_names    32 -> 43
    _must_be_zero_is_enforced           28 -> 28   (untouched)

The enum check gained the same thing twice: it searched `range(256)` for a
value no member names, beside a gate that had already excluded everything
wider than a byte. It searches the member's own width now, and where an enum
names every value its width can hold it says so rather than dropping the
check -- four of those, all 2 and 3 bits, and each is a fact about the schema
rather than a gap.

**The refactor was proved inert before any behaviour moved**, which is what
made the rest safe to read: extracting `_break_to`, then splitting the
run-check body from its naming, left all 39 generated files byte-identical
each time. Only then did the gate change.

**And the control was missing, which is the finding this one was hiding.**
The `must_eq` check never asserted that its fixture validates before
breaking a field -- so a refusal proved nothing, since a fixture the schema
already refuses refuses everything after it too. Adding the control turned
`cpio_header` red at once, and correctly: thirteen `hex u32 ...[8]` text
numbers sat at `0x00`, which is not a digit in any radix, so no check on
that struct had ever had an honest control.

`_baseline` writes the digit spelling now -- the smallest admitted value, in
the field's own radix, zero-padded to its width -- which is the same gap the
pinned runs had in 26.293, one construct over. **Twice in two days the
fixture every constraint check rests on was invalid for a construct the
baseline had never been told about**, and both times the check went on
passing until something made it assert the fixture was good.

**What the sabotages proved.** Making the wide poke write the DEMANDED value
turns the new checks red and leaves the byte-wide ones green, which is what
says the new coverage is real rather than emitted. The digit baseline needed
no sabotage: the run before it existed failed exactly the two checks it
fixes, on the artifact, which is better evidence than a fixture.

### 26.295 A directive is a claim about the file it is written in

Moved to `doc/decision/26.295-a-directive-is-a-claim-about-the-file-it-is-written-in.md`.

### 26.296 The packer resolved a character against the wrong file

Moved to `doc/decision/26.296-the-packer-resolved-a-character-against-the-wrong-file.md`.

### 26.297 A text number the schema declared signed, and nothing read

Moved to `doc/decision/26.297-a-text-number-the-schema-declared-signed-and-nothing-read.md`.

### 26.298 The trim set was HTTP's, in a compiler that is not HTTP's

`[trim]` removed space and horizontal tab, everywhere, in every schema.
That is HTTP's OWS and SIP's LWS, and it was the right default and the
wrong answer: **a format that CALLS CR and LF whitespace did not get them
trimmed**, and JSON does, by RFC 8259 section 2.

The constant was in five places -- the four backends and the walker -- and
`report.py`'s own comment beside its copy said what a second copy costs. So
the fix is not a better constant. `whitespace` already existed as a
file-level directive, put there by `skip`; `traverse.whitespace_set` reads
it and everything else asks that. One decision, six readers.

**The image carries the set, in a section of its own.** Not a column on
`image_placement`, because it is the SCHEMA's fact and not a member's -- and
written unconditionally, because "this schema states nothing" and "this
image predates the section" are different facts and a walker that could not
tell them apart would have to guess for one of them. An image with no such
section means OWS, which is what every schema written before the directive
meant.

**And the wire signature says which bytes.** A signature reading only `trim`
cannot tell a reader whether a trailing newline is part of a value, and the
answer used to be HTTP's whatever the format said.

### 26.299 The gap named the construct that closed it

`example/json` carried this in its bill, one line above the whitespace line
26.292 corrected:

> Escapes are the reader's to interpret -- situ frames the span and does
> not decode it, and a `\"` inside the content ends this scan early. That
> is the third thing this file cannot do and the one that is a genuine gap
> rather than a language boundary: `[escape = "\\"]` exists for exactly
> this and is asserted below.

**The sentence names the attribute that fixes it and calls itself a gap in
the same breath.** It was not a gap in situ; it was a gap in this schema,
and the fix is `[escape = "\\"]` on `text.chars`.

The measurement, because a short extent with no complaint is the answer this
repository rates worst and it was being produced here:

    {"a":"say \"hi\""}    18 bytes    extent 13    before
    {"a":"say \"hi\""}    18 bytes    extent 18    now
    ["a\"b"]               8 bytes    extent  6    before

The rule is that the byte after the escape is content whatever it is, itself
included, so `"back\\slash"` ends at its own closing quote rather than two
bytes later. That case framed correctly before and still does, which is the
half worth checking: a fix to the failing case that breaks the passing one
is not a fix.

What situ still does not do is DECODE one. `\u0061` is framed as six bytes
and means `a` to a reader, which is the division of labour every other
member in that file already follows -- and it weakens `canonical` a second
way, which the map says.

**Why it sat there.** A bill is written once, when a schema is written, and
nothing brings it back together with the language as the language grows.
Both lines corrected in two days were true when written and false by the
time anybody read them, and neither was found by reading the bill -- 26.292
came from implementing `skip` and this came from asking what else the file
claimed it could not do. The question is cheap and the answer is not
discoverable any other way.

### 26.300 Two attributes in one file, one question, two answers

Moved to `doc/decision/26.300-two-attributes-in-one-file-one-question-two-answers.md`.

### 26.301 A generated comment that named the wrong one of a pair

Moved to `doc/decision/26.301-a-generated-comment-that-named-the-wrong-one-of-a-pair.md`.

### 26.302 The differential could not see a delimited member's length

Moved to `doc/decision/26.302-the-differential-could-not-see-a-delimited-member-s-length.md`.

### 26.303 A reason that was true when it was written

Moved to `doc/decision/26.303-a-reason-that-was-true-when-it-was-written.md`.

### 26.304 A private copy of a decision the layer owns

`declared_depth` and `depth_limit` say in their own docstrings that the
decision layer answers them once "so four backends and two walkers spell one
rule rather than each deriving it". The C backend carried `_declared_depth`
and `_depth_limit` as private methods, byte-for-byte the same logic -- and
26.301's wrong number lived in exactly that code.

Found mechanically rather than by reading: for every public function in
`traverse.py`, which files name it. The rows worth looking at are the ones
where **three of four backends** ask, and this pair was two of them.

Deleted, and the proof is that the change is invisible: **every generated C
file for all 39 schemas is byte-identical** before and after.

Two others in the same sweep were checked and cleared, which is the half a
sweep usually does not write down. Python guards a variant's arm without
calling `arm_of`, and agrees with C including on a discriminant inside a
nested struct. `containment_order` says "three backends need this" and three
read it; Rust abstains because Rust has no declaration-order constraint.

### 26.305 The members nothing compared were the ones that underflow

Moved to `doc/decision/26.305-the-members-nothing-compared-were-the-ones-that-underflow.md`.

### 26.306 A check on every access that nothing can make fail

Moved to `doc/decision/26.306-a-check-on-every-access-that-nothing-can-make-fail.md`.

### 26.307 The check is real now, and one line is why it was not

Moved to `doc/decision/26.307-the-check-is-real-now-and-one-line-is-why-it-was-not.md`.

### 26.308 A sweep that was wrong five times out of six

Moved to `doc/decision/26.308-a-sweep-that-was-wrong-five-times-out-of-six.md`.

### 26.309 A differential that disagreed about a value nobody disputes

The census in 26.308 named `endian native` at the widths nothing reaches as
uncovered, so the next move was to write that schema. It found something on
the first run, and not the thing it was written for.

    c        big -6232767268286081968
    cpp      big -6232767268286081968
    rust     big -6232767268286081968
    python   big 12213976805423469648

Same bits: `12213976805423469648 - 2^64 = -6232767268286081968`. The
four-way differ printed **every** scalar as a signed 64-bit word -- `(long
long)` in C and C++, `as i64` in Rust -- and Python's accessor returns the
unsigned value. Exact for every width but one, so a `u64` above 2^63 is a
disagreement the tool reports about a value the four agree on.

**The corpus could not have shown it: five `i64` in it and not one `u64`.**
The one `u64` anywhere is in a comment.

The ask carries the scalar's width and signedness now, and each driver
prints accordingly. `test/schema/native.situ` keeps it covered, along with
the two shapes native byte order has accessors of its own for -- `u24`
through the bit extractor, `u64` through `situ_get_ne64`.

### 26.310 Five more copies of one rule, and a suite I had not run

Making `invalidating_members` the shared answer (26.306) turned up consumers
that each had their own. Two were found by reading; **three were found by
`make test-c`, which I had not run** -- `test_codegen_c.py` passed and I
reported the C side green, which is a population that does not include the
hand-written C tests or the generated check and vector suites.

    situc/codegen/c/emit.py        `_drivers`, `sized_by` only
    situc/codegen/cpp/emit.py      none at all
    situc/codegen/python/emit.py   none at all
    situc/codegen/differ.py        `writes()`, `sized_by` only
    situc/codegen/c/vectors.py     `sized_by` only -- round-trip vectors
    situc/codegen/c/checks.py      a boundary-value witness, no notion at all
    test/generated/*.c             hand-written, the two-argument check

Each agreed with the emitter only while the emitter was wrong in the same
way. The vectors generator's comment even cited the case that taught it
(`le_advertising_report.num`), and `checks.py` already computed the
arithmetic drivers for its span checks while calling the plain setter for
them.

**And one API regression, caught by the same suite.** Marking a nested
driver in the struct that owns it dropped the ENCLOSING struct's setter:
`situ_message_hdr_length_set` vanished. A dotted driver is marked in both
now -- the nested struct, whose own setter has to invalidate, and the outer
one, which reaches it through the outer view. Removing an accessor is not
what a fix to 12.3 is for.

**Two hand-written tests changed rather than broke**, and both had encoded
the absence of the guarantee: one read through a view it had just written a
length through, and one called `situ_view_check` with the message. The first
is the same edit the Python suite needed, and `test_message.c` already had
`test_reacquiring_after_a_shift_is_valid_again` two lines below it -- the
rule stated on purpose, beside a test that broke it by accident.

**The lesson is about the population, not the copies.** `test_codegen_c.py`
compiles generated C; `make test-c` compiles generated C *and* the check
suites, the vector suites, the fuzz harnesses and the hand-written tests.
Reporting "the C suite is green" from the first is the vacuous pass with a
true result: it inspected something, and not the thing that broke.

### 26.311 Two gates in `make check` that nobody had run

Moved to `doc/decision/26.311-two-gates-in-make-check-that-nobody-had-run.md`.

### 26.312 The same omission, one document over

26.300 put `[trim]`'s byte set on the member line of the wire signature,
because `trim` alone "cannot tell a reader whether a trailing newline is
part of a value". `situc doc` -- the human-facing description, the one
somebody implements a format FROM -- said the same bare word, and said
`whitespace + 1 byte` for a lead.

    before   v      whitespace + 1 byte    w...  trim
    after    v      space, tab + 1 byte    w...  trim: space, tab

Named rather than hex, because the audience differs: a signature is diffed
by a tool and `trim=20090d0a` is exact, while a document is read by a person
and four hex numbers ask them to decode which one is a newline. JSON's four
bytes now read `space, tab, CR, LF` and HTTP's two read `space, tab`, which
is the difference between the two formats stated where a reader will look.

**How it was found is the reusable part, and it is not a new lens.** It is
the old one asked of a different artifact: after fixing a claim in one
place, ask which other document makes the same claim. 26.300 fixed the
signature and the schema comment; nobody asked what `doc` said, and `doc` is
the one of the three a stranger reads.

### 26.313 An empty sweep, recorded because it is one

`[encoding = utf16be]` and `[encoding = utf16le]` came out of 26.308's
census as constructs the corpus does not exercise, and the census had been
three for three: signed text numbers, `import` and native-endian `u64` each
carried a defect. This one does not.

Swept across all six readers -- four backends and both walkers -- on cases
chosen because random bytes never reach them:

    plain BMP, both endiannesses      accepted by all six
    a proper surrogate pair           accepted by all six
    a lone HIGH surrogate             refused by all six
    a lone LOW surrogate              refused by all six
    the same, little-endian           refused by all six

The little-endian rows are the ones worth having: the bytes `00 D8` are the
code unit 0xD800, and a reader that forgot to swap sees 0x00D8 and accepts
it. Nothing did.

**Recorded rather than dropped, because an absence with no method behind it
licenses nothing.** What was looked for is written above, and what the code
does instead is that every backend emits `situ_utf16be_valid` or its
sibling and both walkers carry `utf16_valid` with an endianness flag. So
that family is swept, and the next fault in this area needs a different
lens.

**And pinned, in `edges.situ` rather than a new schema.** That file exists
to carry "the constructs the worked examples happen not to have", which is
this exactly -- and unlike `native.situ` one entry back, this one earns its
place by holding a result rather than by having found a fix. The compiler
refuses the byte form of the attribute by name, which is why no schema had
stumbled into a `u16` run and why the six had never been asked to agree.

### 26.314 `[secret]` erased in one backend of four

Moved to `doc/decision/26.314-secret-erased-in-one-backend-of-four.md`.

### 26.315 One rule read from both ends, and a set counting the wrong thing

Moved to `doc/decision/26.315-one-rule-read-from-both-ends-and-a-set-counting-the-wrong-thing.md`.

### 26.316 An eraser that erased nothing, and one that erased too much

Moved to `doc/decision/26.316-an-eraser-that-erased-nothing-and-one-that-erased-too-much.md`.

### 26.317 The fuzz harness could not have caught it, twice over

Moved to `doc/decision/26.317-the-fuzz-harness-could-not-have-caught-it-twice-over.md`.

### 26.318 Opening the gates, and a sweep that measured nothing

Moved to `doc/decision/26.318-opening-the-gates-and-a-sweep-that-measured-nothing.md`.

### 26.319 Two skips in a row, each right on its own

Moved to `doc/decision/26.319-two-skips-in-a-row-each-right-on-its-own.md`.

### 26.320 The gap that was written down, and a wild pointer behind it

Moved to `doc/decision/26.320-the-gap-that-was-written-down-and-a-wild-pointer-behind-it.md`.

### 26.321 The fix with no test, found by listing them

Asked whether the session's findings were stored as regression tests, the
answer came from listing them against the fixes rather than from memory --
and one fix had none. The smoke input was raised from 32 bytes to 256
because 13 of 181 structs have a SIZE_MIN above 31 and were being fed
nothing (26.318); nothing asserted it, so putting `head -c 32` back would
have passed every gate.

`test_the_smoke_input_clears_every_size_floor` reads the size out of the
Makefile and the floors out of the schemas, so a schema arriving with a
larger minimum fails it rather than quietly dropping out of the smoke test.
Reverted, it says which numbers disagree.

**The general point is the method, not the miss.** A fix to a *test's* own
configuration has nothing downstream to trip over -- no code changes
behaviour, no gate goes red -- so it is exactly the kind that ships
untested and stays that way. Twenty of the session's twenty-one tests were
written beside the code they defend; the twenty-first existed only because
somebody asked for the list.

**What is stored, and what is not.** Twenty-two tests across five files,
plus `secret_run` in `edges.situ`, and every defect this session found
fails at least one of them when its fix is reverted -- each checked by
reverting it. What is NOT stored is the sanitizer work: the ASan witnesses
that demonstrated the eraser overrun, the harness's buffer blind spot and
the C++ wild pointer were scratch programs, and `make check` builds nothing
under a sanitizer. `make fuzz` does -- `-fsanitize=fuzzer,address` -- and
needs clang with libFuzzer and runs for as long as it is told, so it is not
part of the standing gate.

That is a real limit rather than an oversight to close in passing: the
substance of each finding is covered by a stored test that needs no
sanitizer -- a canary past the frame in C, an asserted span size in C++, an
asserted allocation in the harness -- and putting ASan into `make test-c`
would change what every build costs, which is the copyright holder's call
rather than a session's.

### 26.322 What the fuzzers found the moment they could see

Moved to `doc/decision/26.322-what-the-fuzzers-found-the-moment-they-could-see.md`.

### 26.323 CI had been red for five days, and nobody had looked

Asked to wire `make fuzz` into CI, the first thing to establish is whether
there is a CI to wire it into. `gh repo view` says the repository is
PUBLIC, so Actions are free and the standing rule wants the workflow on
rather than deactivated. It is on, and it has been failing.

    runs in the last 100 (2026-09-04 to 2026-09-09): 86 failure, 14 success
    most recent success: 2026-09-04 14:06

**`make check` passes on the runner.** The step that fails is the one after
it, "Refuse a skip this machine caused", and what it refuses is

    check: 4 skip(s) this machine caused:
    skipped: 44 (40 accounted for, 4 not)
      SKIPPED [4] test_codegen_c.py:577: no struct this schema frames

which is not a machine-caused skip at all: a schema that frames nothing has
nothing for the frame backend to emit. The reason arrived on 2026-09-05 in
`a25eb6b`, the day after the last green run, and the workflow's list of
expected reasons never gained a line for it.

**The guard is working exactly as designed and that is the point.** 26.155
replaced a count with a classification and said so in the workflow: "Adding
a line here is the deliberate act the old ceiling asked for, and it costs a
reason rather than a number." A line was owed for five days and this is
what the design does when nobody pays it -- fail, by name, on every push.
The alternative it replaced would have absorbed four more skips silently.

**What it cost is a session's worth of pushes onto a red badge.** Seven
commits went in today without anybody reading the result, which is the
failure mode the rule about deactivating dead workflows exists to prevent,
arriving from the other side: not a badge that is red because nothing runs,
but one that is red for a real reason nobody is reading.

**`make fuzz` is now its own job**, beside `check` rather than inside it,
because it needs clang with libFuzzer and takes minutes. Three things make
it worth more than a smoke run:

- **The corpus is cached**, so each run starts from what earlier runs
  reached rather than rediscovering that a length field exists. It cannot
  grow without bound -- libFuzzer keeps an input only when it reaches new
  coverage -- and it is 23 MB after a day of local runs, 256 KB after one
  cold CI run.
- **A crash uploads the input that caused it.** Without that the report is
  "something crashed" and the mutation is gone.
- **The budget is 60 seconds a harness**, 37 of them, about forty minutes
  against the check job's ten. It was 20 to begin with, which was a guess
  made before anybody knew what the runner could do: it turned in 493
  million executions on a cold corpus, roughly four times what this
  project's own machine manages in the same wall clock. The budget was the
  thing to spend rather than save, and the holder said so.

Verified against a clean tree rather than assumed: `make fuzz` with a fresh
`BUILD_ROOT` builds and runs all 37 from nothing, needs no cmocka and no
archive, and puts its corpus where the cache step looks for it.

**AND THE NEXT RUN FAILED ON A SKIP LINE OWED BY THIS SESSION.** The commit
that paid the five-day-old line introduced another one the same day:
`test_backends_refuse_the_same_members.py` skips a schema whose structs are
all registers or have no floor, which is as much a property of the schema
as the other eight, and no line was added for it. 44 of 48 accounted, four
not, red again.

**Which is the entry's own point arriving at its author.** The rule is not
"remember to update the workflow when CI goes red"; it is that a skip and
its line are one change. Written a commit apart they are two, and the
second is owed from the moment the first lands -- for five days last time
and for one push this time, the difference being only that somebody was
watching.

**The bound was missing too, and that is the same shape.** `make fuzz`
stops itself, so the job needs no timeout for the case that works; it needs
one for the case that does not, where a harness hangs inside a single input
and holds a runner for the six hours GitHub allows by default.
`timeout-minutes: 30`, against twelve minutes of fuzzing plus the builds.
**Both jobs are bounded, and the numbers come from the runs rather than
from taste.** `check` measured 8.7 to 10.3 minutes over nine runs and gets
25; `fuzz` at 60 seconds a harness predicts about 40 and gets 55. The
second bound can sit that close because a fuzz job's duration is its
budget rather than its work -- four runs at 20 seconds took 14.5 to 14.8
minutes, a spread of two percent.

**Raising the budget forced the bound**, which is the sort of pair that
gets missed: 60 seconds over 37 harnesses is 37 minutes of fuzzing, and
the 30-minute timeout set an hour earlier would have killed the job every
time. A number changed in one place and enforced in another.

**And the caching was written up as working before it had worked once.**
The sentence above said "restored by prefix" while the only evidence was
that the step existed. Measured on the next run: `Cache not found for input
keys: fuzz-corpus-<sha>, fuzz-corpus-`.

**The miss was self-inflicted and the timestamps say so.** Run 2's restore
ran at 21:40:14 and run 1's save landed at 21:42:08 -- two minutes later,
because the second commit was pushed while the first run was still fuzzing.
There was nothing to find. Both caches exist now, 261 KB and 256 KB on
`master`.

**Demonstrated, in two steps.** A `workflow_dispatch` run against the same
commit -- no push, no overlap -- reports `Cache restored from key:
fuzz-corpus-cea9217...`, an EXACT key hit, which shows the path, the scope
and the mechanism work. The next ordinary commit then restored from
`fuzz-corpus-cea9217...` as well: a DIFFERENT commit's cache, reached
through the `restore-keys` prefix, which is the half that makes the corpus
accumulate rather than merely persist.

**The two steps were worth separating because the failures print the same
line.** A prefix that never matches and a prefix that had nothing to match
yet both say `Cache not found` -- the first meaning every run starts cold
for ever, the second meaning somebody pushed twice inside a quarter of an
hour. Only the second run tells them apart, and it was nearly not done: an
exact hit reads easily as the whole answer.

### 26.324 The other reading of the layout, never fuzzed

Moved to `doc/decision/26.324-the-other-reading-of-the-layout-never-fuzzed.md`.

### 26.325 C++ was the backend nobody fuzzed, and it had two

Moved to `doc/decision/26.325-c-was-the-backend-nobody-fuzzed-and-it-had-two.md`.

### 26.326 Two empty sweeps and a detector worth keeping

26.325's arm accessor asked which arm and not whether the bytes were there.
The family it belongs to is "an accessor that is conditionally present", so
the other members were checked and both are sound.

**Versioned members already carry the bound**, in the getter and the
setter, in all four backends: `if (!situ_in_bounds(view, 3u, 4u))` beside
`if (ver < 2u)`. **Setters at computed offsets carry it too** --
`situ_framed_count_set` writes only inside `situ_in_bounds(view,
situ_framed_count_offset(view), 2u)` -- and **arms have no setters at
all**, so the write half of the arm question does not exist. Three lenses,
three empty results, recorded because an absence of findings and an absence
of looking print the same thing.

**What came out of it is a detector.** An accessor with no bounds check is
safe exactly when it stays inside `SIZE_MIN`, because that is the whole of
what acquisition promises. So: every unguarded constant-offset reader in
the generated C, against its own struct's minimum. 394 of them across the
corpus, none past it.

**The zero is a measurement because the control fires.** Removing the guard
26.325 added puts exactly one back -- `situ_label_body_pointer_low_get
needs 2, minimum 1` -- and nothing else.

**The detector was wrong twice before it was right, both times about its
own instrument rather than about the tree.** It counted `situ_base(view) +
N` in a `_ptr` function as a read, and eighteen pointers at exactly the
minimum came back as findings -- an address one past the fixed part is not
a dereference. And `situ_get_ne16` was missing from the width table, so a
two-byte native read was charged as eight.

**Then it charged a member against the wrong struct.** Owner by longest
matching prefix said `situ_image_relation_must_count_get` belonged to
`image_relation_must`, whose minimum is 8, when it is `image_relation`'s
`must_count` and the minimum is 24. Both structs exist and one name is a
prefix of the other. The map is built the way the emitter builds the name
now, which is the difference between asking the schema and guessing from a
string.

**And the scratch sweep could not see the schema that produced that.** It
globbed `example/` and `test/schema/`; the test uses
`every_schema.SCHEMAS`, which includes `std/`. The finding was a false
positive, but it was a finding the narrower scope could not have made
either way -- which is `evidence.md`'s point about choosing where to look
before knowing who owns the answer.

**What it cannot see is in its docstring**, because a detector whose limits
are unwritten gets quoted for guarantees it never made: a read at a
computed offset, a read through a returned pointer, and any guard spelled
differently from the four it knows. It is a floor under one shape of
mistake.

### 26.327 A vocabulary for text protocols, to be designed

Moved to `doc/decision/26.327-a-vocabulary-for-text-protocols-to-be-designed.md`.

### 26.328 The harness that could not call the accessor it was for

26.325 found an arm accessor reading past its frame in C, and found it with
the C++ harness. The C one had been running over `dnsname` since phase 22
and could not have: `own_entries` drops a dotted path, so an arm member
never reached the walk and the harness named none of them. Zero arm calls
across 37 harnesses.

**The same gap as the sealed interiors** (26.318), one family over, and the
same fix: a branch on `kind == "variant"` that walks `arm_members` and
calls whichever of the four accessors that arm's shape gets -- a scalar's
`_get`, a byte run's `_ptr` with its length, an indexed run's `_count` and
indexed getter, a struct arm's `_view`. The conditions are `_arm_member`'s
own, in its order, so the harness names an accessor exactly when the
emitter wrote one. A fifth branch appearing there is a build failure in 37
harnesses at once, which is a loud way to find out.

70 arm reads now, and the control fires: with 26.325's bound removed the C
harness reports the overread through `fuzz_label` in under a minute, where
before it ran clean for ever.

**The sweep with the new coverage found nothing else** -- 37 harnesses, no
crash. Recorded because an empty sweep and a sweep that could not reach
anything print the same thing, and this harness has now been both.

### 26.329 A token set, and the line it did not cross

Moved to `doc/decision/26.329-a-token-set-and-the-line-it-did-not-cross.md`.

### 26.331 What converting smtp and http to a token set actually cost

Moved to `doc/decision/26.331-what-converting-smtp-and-http-to-a-token-set-actually-cost.md`.

### 26.332 A text number with a point, and the reason that was refused

Moved to `doc/decision/26.332-a-text-number-with-a-point-and-the-reason-that-was-refused.md`.

### 26.333 The scaled number's only asker cannot use it (until 0057)

Moved to `doc/decision/26.333-the-scaled-number-s-only-asker-cannot-use-it-until-0057.md`.

### 26.334 A non-consuming dispatch: what is known before starting

Moved to `doc/decision/26.334-a-non-consuming-dispatch-what-is-known-before-starting.md`.

### 26.335 What the text-vocabulary run left open

Moved to `doc/decision/26.335-what-the-text-vocabulary-run-left-open.md`.

### 26.336 A dispatch that does not consume what it reads

Moved to `doc/decision/26.336-a-dispatch-that-does-not-consume-what-it-reads.md`.

### 26.337 json's number: unblocked, and not converted

Moved to `doc/decision/26.337-json-s-number-unblocked-and-not-converted.md`.

### 26.338 The walker validates the arm a variant selects

Moved to `doc/decision/26.338-the-walker-validates-the-arm-a-variant-selects.md`.

### 26.339 The bounds policy: two rules, and json converted

Moved to `doc/decision/26.339-the-bounds-policy-two-rules-and-json-converted.md`.

### 26.340 What the dissector shows as characters, and where that stops

Moved to `doc/decision/26.340-what-the-dissector-shows-as-characters-and-where-that-stops.md`.

### 26.341 The INDEXES section, loaded at last -- and it was two gaps

Moved to `doc/decision/26.341-the-indexes-section-loaded-at-last-and-it-was-two-gaps.md`.

### 26.342 The `count=` lines the walk was not answering

Moved to `doc/decision/26.342-the-count-lines-the-walk-was-not-answering.md`.

### 26.343 A `tlv` region's items, and the image that could not describe them

Moved to `doc/decision/26.343-a-tlv-region-s-items-and-the-image-that-could-not-describe-them.md`.

### 26.344 The C walker's `tlv` and `indexed` counts

**Two walkers, one design, and one of them could not answer.** 26.342 and
26.343 gave `walk.py` a `tlv` region's item count and an `indexed`
region's entry count; this build had the INDEXES section loaded and the
two TLV sections not loaded at all, so it answered nothing for either.
That is the drift `test_walker_c` exists to prevent, and it lasted
exactly as long as nothing asked.

**They are new entry points rather than a wider `situ_walk_count`.** That
function answers for a COUNTED run -- a declared count, or a `size_code`
program -- and `python_elements` mirrors its refusals exactly, member for
member. Folding these in would have moved the C population without moving
the Python one, and the probe that compares them would have gone on
passing because both sides ask `situ_walk_count`. So
`situ_walk_tlv_count` and `situ_walk_index_count`, and a probe that asks
both.

**A `tlv` count is a walk and an `indexed` count is an evaluation**, which
is the whole difference between them: nothing in a `tlv` region records a
count, while an indexed table's size is a program the image carries. So
the two walkers agree about the second by running the same bytecode, and
about the first by two loops written to the same generated C.

`OP_TAG` went in beside `OP_REMAINING` for the selector, and
`situ_walk_eval` keeps its signature: it calls an `eval_tagged` with no
tag, which refuses `OP_TAG` rather than substituting one. A selector
evaluated without a tag would pick a rule, confidently, for an item
nobody read.

**A sabotage that did not apply looks exactly like a check that cannot
fail.** One of the three here was written with two tabs where the source
has one, so it never landed and the test passed -- reported as "the check
does not fire" until the substitution was asserted. All three fire now
and name their case: `three, all kinds`, `a fixed 4 in two bytes`, and
`count=0` for the index count read from the wrong field.

~~**Found and not fixed: `situ_walk_count` refuses a `while` run for a
reason that is no longer true.**~~ **Done in 26.345.**

### 26.345 A stale sentence that read as a design limit

Moved to `doc/decision/26.345-a-stale-sentence-that-read-as-a-design-limit.md`.

### 26.346 A field that was `none` in every image ever written

**`image_tlv.tag_varint` was a `u32` documented as an index into the
varint table, and that table is keyed by PLACEMENT.** A `tlv` region's
tag varint belongs to no placement -- it is the region's grammar rather
than a member of it -- so `varint_index.get(...)` found nothing and the
field was `none` in every image this tree has ever produced. 26.343
carried the decoder parameters by value beside it and left the field
where it was; this removes it.

**It moved the format version, and the schema's own words say why.**
`image_section.stride` is carried "so that a walker can skip a section
whose records GREW under it, which is what makes a record extensible
without a format version" -- growth being an append a reader stops
short of. A field taken out of the MIDDLE is not growth: it moves
`selector_code` from offset 12 to 8 and the tag parameters with it, so a
v4 image read by a v5 walker would be read at the new offsets and answer
confidently. Version 4 to 5, in the four places that state it and the
test that holds them together.

**The bump is checked rather than assumed.** A freshly packed image with
its version byte set back to 4 is refused by name -- "image format
version 4, this walker reads 5" -- and the C walker returns MALFORMED at
the same check. Without that, removing the field would have been a
silent misread of any image already on a device.

**And it is the second dead thing this record has turned up.** The first
was the whole section, written and loaded by nobody (26.341); this is a
field inside it that could never have been used. Both survived because
nothing read the record: an unused table is where an unusable field hides
indefinitely, and neither the packer nor a walker had any reason to
notice.

### 26.330 Text encoding, scoped and named by the data

Moved to `doc/decision/26.330-text-encoding-scoped-and-named-by-the-data.md`.

### 26.347 Encoding was the odd one out, and endian had solved both halves

Moved to `doc/decision/26.347-encoding-was-the-odd-one-out-and-endian-had-solved-both-halves.md`.

### 26.348 An encoding the data names: the rules, and the readers still to come

Moved to `doc/decision/26.348-an-encoding-the-data-names-the-rules-and-the-readers-still-to-come.md`.

### 26.349 Asking the two walkers about the corpus, and what it found

Moved to `doc/decision/26.349-asking-the-two-walkers-about-the-corpus-and-what-it-found.md`.

### 26.350 The C walker validates a token set, and the bug was upside down

Moved to `doc/decision/26.350-the-c-walker-validates-a-token-set-and-the-bug-was-upside-down.md`.

### 26.351 The encoding the message names, in both walkers

Moved to `doc/decision/26.351-the-encoding-the-message-names-in-both-walkers.md`.

### 26.352 The fixed-count half, and the two faults found reaching it

Moved to `doc/decision/26.352-the-fixed-count-half-and-the-two-faults-found-reaching-it.md`.

### 26.353 `tonumber` is Lua's rule, not situ's, and the clamp was one symptom

**Found by a draw that moved.** Adding two structs to `edges` changed
which buffer the dissector differential drew for `text_driver`, and the
new one begins `"  12"` -- a `decimal u32 n[4]` whose first two bytes are
spaces. The dissector read 12 and the walker read 0, which put `tail`
twenty-four bytes apart.

**Five readings to one, and the tie was broken by asking a third
party rather than by reconciling the two.** C's generated accessor says
what the others do, in a comment written when it was built: `validate`
refuses a frame whose digits are not digits, `situ_parse_uint` returns at
the first byte that is not one without writing its output, and the value
accessor keeps the zero it started with -- "the same bargain every other
accessor makes with the bounds check it did not do". So 0 is the answer
and the dissector was wrong.

**And the bug was already half-fixed, which is the part worth keeping.**
`situ_digits` clamps a negative result, and the comment above it records
why: `tonumber` reads a leading minus, four bytes of `"-26"` gave a
length of -26 and an offset of -48, and Wireshark raised. That fix
treated the one symptom that had bitten. `tonumber` also skips leading
and trailing space, so the same gap was still open one step over --
**a list of special cases only ever grows by being bitten**, and each
entry makes the list look more complete than it is.

The fix is to stop asking `tonumber` what a number is. Every byte is
checked against the base first and a non-digit answers 0, which is
`situ_parse_uint`'s own rule rather than a second opinion about it.
Measured in Lua against the bare call:

    "  12" base 10   ->  0   tonumber says 12
    "12  " base 10   ->  0   tonumber says 12
    "-26 " base 10   ->  0   tonumber says -26
    "0012" base 10   -> 12   tonumber says 12
    "00FF" base 16   -> 255  tonumber says 255

**It is pre-existing and the schema change only exposed it.** Checked by
running the committed schema against the same buffer: the walker answers
0 there too, so nothing about the new structs is involved. Sabotaged back
to the bare call, `edges` fails and the other thirty-nine schemas pass.

**The differential earned its keep by being asked a new question.** It
had run green over this corpus for as long as it has existed, because no
draw had put a space in front of a digit. That is not a weakness of the
test -- it is what a fixed corpus of draws is: a sample, and a sample is
evidence about the cases it holds. What changed the sample was unrelated
work, which is the cheapest way it ever changes.

### 26.354 The C walker learns which arm a discriminant selects

**Measured rather than guessed at, which is what made it the right next
piece.** The C walker declined 32 of 388 struct-and-message pairs over
the corpus, and nothing said why. Reporting `__LINE__` at each of its 47
refusal sites, through a scratch copy, ranked them in one run:

    12  a member whose byte order is `native`
    10  a check kind this build does not render
     8  a counted run whose element is neither a record nor sized
     2  four bytecode operations the walk does not implement

**Those are FIRE counts, and 26.355 had to correct what they were read
as.** A refusal unwinds through several sites, and the apparatus that
produced this table read stderr for the whole image rather than per
struct -- so it attributed to one struct the refusal of whichever ran
before it. The ranking above is right about which sites fire and wrong
about how many structs each one is the CAUSE for. Asking one struct per
run is what separates the two.

**The second was `arm_selected` and nothing else.** `CHECK_ARM_SELECTED`
was not defined in the C walker at all -- not in the known-kinds list,
not handled -- so every struct holding a variant was unanswerable to it
while the Python walk answered. Sixteen structs across the corpus. The
arms section was already loaded and `variant_bits` already resolved an
arm for SIZING; only the check was missing.

**And the verdict it needs did not exist either.** An unmatched
discriminant is VERSION and not CONSTRAINT -- a message this build cannot
READ rather than one that breaks a rule, which is 14.5's distinction and
the schema's. `situ_err_t` has carried the code all along and the four
backends return it; the walker's own enum stopped at CONSTRAINT, and the
test driver folded everything above BOUNDS into "2". **Adding the check
without adding the code would have produced a wrong agreement at the one
moment the two walkers finally had something to compare.** Sabotaged both
ways afterwards, and they fail differently, which is the discrimination
worth having: with the refusal removed C answers 0, with the rendering
folded C answers 2, and the truth is 3.

**The first draft was wrong in a way the corpus could not see.** Every
arm has to fit the frame, whether or not it is a struct, and the draft
returned early for an arm that was a plain run -- so `dnsname.label`
declaring three bytes of text over one answered OK where the Python walk
and the C backend both answer BOUNDS. The corpus comparison said zero
disagreements throughout, honestly: its draws are 64 bytes long, and a
short frame is precisely what this construct gets wrong. **A sample is
evidence about the cases it holds**, and the case was found by writing
eight vectors for one struct by hand.

`dnsname.label` is the test because it documents itself and produces
three of the four verdicts from one struct: `00` is a length and that
many bytes of text, `11` a pointer, and `01` and `10` are reserved, which
`default: error` refuses rather than guessing which of the two they
resemble.

**Where it leaves the two walkers**: 366 pairs compared and agreeing, 22
declined, none disagreeing. No check kind is among the refusals any more
-- the pinned run was the last before this and `arm_selected` the last
after it. What remains is one deliberate refusal and two structural gaps,
named in the corpus test so the next reader starts from the measurement
rather than from a guess.

### 26.355 Two refusals left in the C walker, and one of them is the answer

Moved to `doc/decision/26.355-two-refusals-left-in-the-c-walker-and-one-of-them-is-the-answer.md`.

### 26.356 One byte has no byte order, and one question that is not mine

Moved to `doc/decision/26.356-one-byte-has-no-byte-order-and-one-question-that-is-not-mine.md`.

### 26.357 A stuffing code's constant, and the fourth codec nobody had measured

Moved to `doc/decision/26.357-a-stuffing-code-s-constant-and-the-fourth-codec-nobody-had-measured.md`.

### 26.358 The two grammars agreed about names and disagreed about the language

Moved to `doc/decision/26.358-the-two-grammars-agreed-about-names-and-disagreed-about-the-language.md`.

### 26.359 A bounded pad, and the floor that was not a placement

Moved to `doc/decision/26.359-a-bounded-pad-and-the-floor-that-was-not-a-placement.md`.

### 26.360 0046's cost, re-measured before building it

Moved to `doc/decision/26.360-0046-s-cost-re-measured-before-building-it.md`.

### 26.361 Expansion in bits, and two of the three sub-byte codes

Moved to `doc/decision/26.361-expansion-in-bits-and-two-of-the-three-sub-byte-codes.md`.

### 26.362 `--define`, and why it edits the source rather than the tree

**0050's cheap half, which the record calls most of the real cases.** A
declared `const` can be set at generation: `situc map --define block=8192`.
No lattice change, no new syntax, and the layout is as static afterwards
as before -- `trailer` keeps an absolute offset either way and only the
number moves. `const` has parsed and scoped since the beginning with no
way to set one from outside, so even the easiest external argument had no
spelling.

**It splices the SOURCE, at the const value's own span, and that is the
whole design decision.** The obvious implementation rewrites the parsed
tree. It would have been silently wrong: every command re-parses its
source, `build` alone several times for the backends and the relation and
frame layers, so the rewritten tree is thrown away by the next
`parse(source)` and what is generated disagrees with what was resolved --
about a constant, which is the kind of disagreement nothing downstream
trips over. Splicing at the span means every later parse sees it, the
offsets come from the parse rather than from a pattern over text that
looks like a declaration, and a diagnostic still points at a line
somebody can read.

**Every command that reads a schema takes it, and the test derives that
population rather than listing it.** Sixteen of them. A flag half the
commands honour is worse than one none of them do: `map` and `build`
disagreeing about a constant puts a committed artifact out of step with
the code beside it and nothing says why. The test walks the parser's own
subcommands, so one added later joins the population without anybody
remembering -- and with `map`'s flag removed it names `map`.

**What it refuses**: a name the schema declares no const for, listing
what it does declare; a value that is not an integer; and a `--define`
with no `=`. A define nobody reads is a deployment that thinks it
configured something.

**What it is not** is `parameter`, the other half of 0050 -- a run-time
argument, a member of zero width, and the half that has to answer what a
moving member costs the descriptions that walk a cursor. Nothing here
touches that.

**One thing worth knowing rather than fixing.** A map generated under
`--define` records the layout it was given and does not record the
define, which is what 0050's "no artefact change" asks for. Two maps of
one schema can therefore differ with nothing in either saying why, and
`map --check` compares against whatever defines the checker passes. That
is inherent to a generation-time constant rather than a defect, and the
record's own answer for the run-time case is the opposite -- the wire
signature names every `parameter` -- so the asymmetry is deliberate and
worth a reader knowing before they meet it.

### 26.363 Asking the two walkers WHY, and the BCD the C walk never had

**0051's identity half, from the reader that had neither end of it.**
`report.failed_check` has answered `(member, check)` since 26.231 and the
generated C has named the member since 26.232; this walk answered neither,
so there was nothing to hold it against. `situ_walk_failed_check` answers
it now -- `validate` untouched, same signature and same short circuit,
because the identity is a second question about the same call.

**Recorded on the way out, twenty sites.** The same design 26.231 argued
for: a second pass that re-ran the checks would be a second implementation
of an order the schema owns, free to disagree with the first.

**The completeness sweep was wrong, and the comparison is what said so.**
A syntactic check over `*verdict = SITU_WALK_(BOUNDS|CONSTRAINT|VERSION)`
reported every site covered. Three assignments are not of a literal -- two
TERNARIES and one propagated `inner` -- and the regex could not see them,
so the terminator check and a refused value read recorded nothing while the
sweep said 20 of 20. The corpus comparison found 55 disagreements on its
first run. **A proof over the shape of a line is a proof about lines that
have that shape**, and the population it silently omitted is the one the
independent reader found.

**And the population was the wrong one twice.** The first sweep scanned
`validate_deep` alone and reported every refusal covered; five more live in
the arm helpers, which refuse too. Coextensiveness is the test, not
plausibility: every refusal the WALK can make, not every refusal in the
function being edited.

**Then a guard wider than the other walker's.** C refused a message-sized
run at a placement check where `report._validate` reaches `fits_frame` --
Python's guard is the pair `fixed and not offset_known` and its comment
says the two cases "do not overlap", while C knew only the offset half and
had no name for `SIZE_FIXED` at all. Verdicts agreed throughout, which is
why nothing had seen it.

**The find worth the whole exercise: the C walker had no BCD.** It read a
`bcd2 seconds` holding `0x3E` as 62 where the four backends, the Python
walk and `situ_bcd_decode` all answer 44. Both walkers answered CONSTRAINT
for the buffer that found it -- one because 62 breaks `[max = 59]`, the
other at a `must_be_zero` two members later -- so the verdict comparison
could not see it and the member comparison could not either. Only *which
check* separated them.

That is the third time packed decimal has hidden behind a comparison that
could not reach it: 26.222 records `codegen/differ` skipping a BCD member
outright, so C's side never mentioned one. A construct that keeps falling
out of differentials is worth suspecting the differential over.

**What is left apart is named**: `pickle_wstring` and `png.chunk`, where
`size_bits` refuses on an overflowing count and the Python walk clamps.
The C walker's own comment already called that "the half still apart", so
the comparison rediscovered a divergence the tree had recorded -- which is
the cheapest evidence that it is sensitive enough to be worth having.

**Standing**: 197 refusals compared for their identity and agreeing, 4
held out by name, 21 declined. The verdict comparison is unmoved at 380
agreeing and none differing, which is the property that had to survive.

### 26.364 The editor says which field the schema refuses over

**0051's first consumer, and the channel was already there.** 26.232
closed with "the knowledge is now usable and nothing uses it yet -- and
the consumers are the next one". `editor/document.py`'s `Field` carries a
`note` that reaches every frontend, and a document that will not validate
now says which member is why, without a frontend that knows the format.

The CHECK's name rather than a sentence. A sentence a schema wrote is
`when`'s and is not built; `max` is a rendering of an identity, which is
0051's own split -- a consumer keys on the id and a person reads the word.

**Appended rather than replacing.** A field can be unreadable AND be the
one the schema refuses over, and a note that dropped either half answers a
question nobody asked.

**And it immediately found four check kinds with no name.** The note read
`refused: bounds` for a pad outside its bounds, because `CHECK_NAMES` had
no entry for `pad_length_max` -- nor for `pad_length_min`, `encoded_from`
or `encoding_arm`. Each was added as a kind by whoever built the construct
and none of them was named, so every refusal through one rendered as
`bounds`.

**Which is the same word a refusal with NO identity renders as**, and that
is what made it invisible: an unnamed kind and an unrecorded refusal are
the same string to a reader. The four had been that way since the day each
construct landed -- `encoded_from` for a day, the pad checks for hours --
and nothing in either differential could see it, because both compare
answers rather than the words a reader would be shown.

A population fixes it rather than vigilance: `report.ALL_CHECKS` is
checked against `image_check` in `std/image.situ`, parsed from the schema
rather than listed, so a kind added there joins the test without anybody
remembering. With one name removed it names the kind.

**What this consumer is not.** `situ verify` already reports an identity
of a sort -- the Python backend's exception text, `"declared_fixed.label
is not ascii"` -- and that is precisely the "text with no identity" 0051's
alternatives rejected: it cannot be localised, every embedded target
carries the strings, and a consumer has nothing stable to match on. Giving
the generated code an ID is the half 0046-style work in three more
backends, and it is not this.

### 26.365 Reading the diagnostics nobody produces

**26.215 counted 93 unproduced `error(...)` sites and said which half
rots: not the refusal but the WORDING.** A check that stops firing is
noticed downstream; one that fires with a message that has stopped
describing its own case is noticed by nobody, because the only reader is
whoever typed the mistake and they have no second opinion.

**A deliberately loose proxy, and its looseness was the point.** The
sweep listed every diagnostic whose wording appears nowhere under
`test/` -- 199, of which 47 are the parser's. A phrase-level search
over-reports here by construction: a message split across two string
literals, or reworded since the test was written, reads as unproduced. It
over-reports in the direction that costs reading rather than the one that
hands back a comfortable zero.

**Both suspects were PRODUCED rather than assumed, and the first was
wrong.** A top-level typo looked like it would report an incomplete set;
running it showed a complete message. Reporting it would have been a
defect that does not exist, and the cost of checking was one command.

**The second was real and was mine.** `expansion = wibble` answered with
a label naming `+N`, `unbounded` and the three ratios -- the five forms
that existed before 0046 added `+N bits`. A schema author who asked the
compiler what to write was told a set that had been wrong for a day.

**Then the first suspect was right after all, by a lens that was not
looking at it.** Having fixed one enumeration, the same question was put
to every label in `situc` that lists alternatives: `buffer, mmio or
file`, `big, little or native`, `strict or lenient`, `msb_first or
lsb_first`, `error or pass`, `derived or extern`, the varint properties,
`feedback = input or output`, the stuffing units, `after or before`.
**Every one matched its table exactly.** The one that did not was
`unknown declaration`, whose note named thirteen declarations of
twenty-two -- missing `codec`, `impl`, `register`, `register_block`,
`namespace`, `whitespace`, `strictness`, `endian_marker` and
`varint_type`.

That is the frame error worth keeping. The first probe asked whether the
message named `tokens`, it did, and the message was recorded as complete:
**one member present is not the set**, and a list is exactly the kind of
claim that is right about whatever you happened to check.

**The fix is not a longer list but a derived one.** The note is now
spelled from the dispatch table it is raised beside, so a declaration
added to the table joins the message with nobody remembering. The
expansion-form label cannot be derived that way -- `+N` is not an enum
value and the ratios carry `(a, b)` -- so it is pinned from both
directions instead: every form in the test's table is parsed, and every
member of `ast.Expansion` that is not `length_preserving` or `+N` must
have an entry, so a seventh expansion fails as a message addressed to
whoever added it.

**And the first control passed with the bug reinstated.** Checking the
forms against the rendered diagnostic found `+N bits` -- in the NOTE
below the label, which glosses it. The refusal names the form somewhere
and the list a reader is told to choose from was still five long. The
assertion reads the label line only, and with the stale label it fails
naming the missing form. A control has to fail through the check under
test, and this one was being saved by the sentence underneath it.

### 26.366 One refusal, two doors, and a label that knew one of them

Moved to `doc/decision/26.366-one-refusal-two-doors-and-a-label-that-knew-one-of-them.md`.

### 26.367 A coverage span that was short, and one that was not there

Moved to `doc/decision/26.367-a-coverage-span-that-was-short-and-one-that-was-not-there.md`.

### 26.368 A codec the compiler describes, declines, and then names anyway

Moved to `doc/decision/26.368-a-codec-the-compiler-describes-declines-and-then-names-anyway.md`.

### 26.369 The loop that runs at the top of a byte

Moved to `doc/decision/26.369-the-loop-that-runs-at-the-top-of-a-byte.md`.

### 26.370 A byte has no byte order, said twice and not a third time

Moved to `doc/decision/26.370-a-byte-has-no-byte-order-said-twice-and-not-a-third-time.md`.

### 26.371 A checksum narrower than a byte, and seven generators

Moved to `doc/decision/26.371-a-checksum-narrower-than-a-byte-and-seven-generators.md`.

### 26.372 The stored value of a checksum narrower than a byte

Moved to `doc/decision/26.372-the-stored-value-of-a-checksum-narrower-than-a-byte.md`.

### 26.373 One bit at a time, checked three ways

Moved to `doc/decision/26.373-one-bit-at-a-time-checked-three-ways.md`.

### 26.375 The bit order two backends read the other way

Moved to `doc/decision/26.375-the-bit-order-two-backends-read-the-other-way.md`.

### 26.374 The span a byte range cannot say

Moved to `doc/decision/26.374-the-span-a-byte-range-cannot-say.md`.

### 26.376 example/usb, and what three bytes took

**The last item in 0046, and the packet the record was written for.** A USB
token is a PID byte and eleven bits of address and endpoint under a five-bit
CRC: three bytes, in which neither the check field nor the span it covers is
a whole number of bytes. Every part of it was refused by something when 0046
was accepted -- `place_tag` refused a sub-byte checksum, `covered_run`
refused a span that is not whole bytes, `crc_width` refused the loop, and no
codec had an entry point that counts bits.

    SETUP addr=21 ep=14  ->  2D 15 EF
    crc=0x1D  check=OK  validate=OK  dirty cleared
    one flipped address bit -> SITU_ERR_CHECKSUM

**Cited rather than recalled, which is 26.32's rule and the reason this
example is the shape it is.** The eight-bit PIDs are
`include/linux/usb/hcd.h:24-40` on this machine, each nibble beside its
complement; the address is seven bits because `usb_pipedevice` masks `0x7f`
(`include/linux/usb.h:1980`) and `devmap` is 128 wide (`usb.h:471`); the
endpoint is four because `usb_pipeendpoint` masks `0xf` (`usb.h:1981`). The
CRC-5 parameters are the catalogue's, as `std/kernels.situ` carries them.

**What could not be cited was left out.** The SOF packet is the same eleven
bits carrying a frame number instead of an address, and this machine has no
header that says the frame number is eleven bits wide -- so the example is
the token packet and says so, rather than asserting a width from memory. A
worked example is a claim; the way to keep it true is to write down less.

**`bit_order lsb_first` is the fact the example exists to carry.** USB sends
a token least significant bit first and a reflected code consumes each byte
the same way, so the two agree and situ accepts the schema. Written
`msb_first` it is refused (26.374), which is a rule that did not exist a day
ago and was written because this packet broke it.

**Three vectors, and what makes them evidence.** The PID bytes are cited and
the addresses chosen, so the only derived number is the CRC -- and it is not
situ agreeing with itself: `crc5_usb` is held to the catalogue's check value
over "123456789", and to its RESIDUE over a token and its own check bits,
which is the one property a sub-byte span can be held to. The vectors are
compiled into a cmocka suite by `make test-c` like every other example's.

**The cost, stated plainly.** Three bytes needed: a sub-byte `place_tag`
(26.371), a bit-valued coverage accessor and a `_store` (26.374), a
bit-counting entry point in three languages (26.373), a left-aligned CRC
loop (26.369), and a bit-order rule. On the way it found seven generators
disagreeing about what an accessor is, a one-byte checksum reading
`situ_get_be0`, and three of six readings assembling a straddling
`lsb_first` field the other way round -- which 26.223 had recorded as a
fault the corpus could not reach, and which stayed unreachable until a
protocol needed the axis for its own reasons.

### 26.377 `when`: the construct, and the two descriptions that carry it

Moved to `doc/decision/26.377-when-the-construct-and-the-two-descriptions-that-carry-it.md`.

### 26.378 The messages an image carries, and who runs them

Moved to `doc/decision/26.378-the-messages-an-image-carries-and-who-runs-them.md`.

### 26.379 Expert info, which is what a `when` already was

**0051 said the dissector gains most and it was right.** Wireshark's expert
info is a severity and a sentence attached to a packet, filterable -- which
is `when` exactly -- and `situc gen-dissector` emitted none. It does now:
one `ProtoExpert` per message, registered with the name the schema gave, and
`subtree:add_proto_expert_info` for each predicate that holds.

**The predicate needed no new machinery.** `_over_fields` has rewritten
schema expressions into Lua reads since the dissector learned to size a
member, and a `when` is an expression over one struct's own fields -- so it
is `expr_to_source(expr, explicit=True)` with the struct prefix stripped,
handed to the function every other expression here goes through. Where it
declines, the message comes out as a comment naming what stopped it, the
way every declined expression in that file does.

**Three severities onto four levels, and `chat` is the one nothing maps
onto.** That is the right way round: 0051 refuses a fourth severity to
match a consumer, and a schema saying less than a display can show costs
nothing. `refuse` is `expert.group.MALFORMED` because that is what it means
-- the message does not conform -- and the other two are `PROTOCOL`, being
remarks about one that does.

**The expert is registered whether or not the predicate can be read.** A
name a person may type into a filter has to exist: a filter on a name that
was never registered is an error in their filter, where one on a message
this backend cannot evaluate is an honest silence.

**The stub had to learn expert info, and where to put it.** `dissect.lua`
keeps them in their own list rather than among the field rows, because the
differential that runs every dissector over random bytes compares rows
against `walker/report.listing` -- and a remark about the whole message is
not a row. Field abbreviations are `struct.member` and always carry a dot,
so a bare `expert` in the first column cannot be one, which is what lets the
reader split the two out of one stream.

**A message is prose, so the text is escaped.** A `when` carrying a quote
emitted Lua that does not parse, and a dissector that fails to load is worse
than one that divides -- 0021's rule, met by a construct that lets a person
write anything.

**Still to do**: the `messages` sibling in the four backends, which is what
a `refuse` needs before it can change a verdict, and the identity on
existing per-member constraints.

### 26.380 The messages sibling, in C

**0051's `messages` function, and the first of the four backends.** A
struct that states a `when` gains

    void situ_S_messages(situ_view_t view, uint32_t *ids, size_t cap,
    		size_t *count);

beside `validate` rather than inside it, for the reason the record gives:
`validate` short-circuits because for a verdict the first failure is the
answer, and a caller asking what a message SAYS wants all of it. A caller
who wants none never links this.

**`*count` is how many hold, not how many fitted.** The alternative makes
truncation invisible: a caller with a one-element buffer would be told one
message held when two did, with no way to find out. So the count is the
answer and `cap` is what there was room for, and a caller that cares
compares them. That is a two-halves-of-one-result shape -- the buffer and
the count -- written so neither half can be quoted alone.

**The id is always emitted and the text only under `--messages`.** The
split fixed point already takes: the scale is a macro and the conversion is
the caller's, because text needs a locale, a log format and a flash budget
situ cannot choose for a target it has never seen. A default build carries
no sentence at all, which is what the test asserts -- a string that leaked
in would cost flash on every embedded target and nothing would report it.

**The owner rule moved to `traverse`.** Which struct a `when` belongs to is
now `traverse.messages`, beside `obligations`, and the dissector asks it
too. The reason is `obligations`' own: the id a sibling emits, the expert
info a dissector registers and the record an image packs are the same
message keyed on its position in one list, and two backends numbering from
two lists is how the dirty-bit disagreement happened.

**A continuation line in generated C is indented with tabs.** The first
version aligned the second line of the signature under the open paren,
which is right inside a function and wrong at file scope -- there are no
indent tabs for the spaces to follow, and `style_gate` said so about two
lines of `edges.h` and `edges.c`. The generator's own existing wrapped
signature uses two tabs; this now matches it.

**Still to do**: the same sibling in C++, Rust and Python, then the
`refuse` half -- which has to land in all five descriptions at once, since
the walk is held to the four compiled backends over the same bytes -- and
the identity on existing per-member constraints outside C.

### 26.381 The other three backends, and one number in four spellings

**C++, Rust and Python gained the same sibling**, each in its own idiom:
a method and a class constant per id in C++, a method and an associated
constant in Rust, a method and a class attribute in Python. `--messages`
adds the default rendering in all three.

**Python takes a list rather than a buffer and a count, and the departure
is deliberate.** The truncation contract the other three state exists
because a C caller supplies the memory; Python's caller does not, so there
is nothing to truncate and a cap would be ceremony. What agrees across the
four is the ids and their order, which is what 0051 makes the contract.

**The id is the contract, so a test holds the four to one numbering.**
`test_the_four_backends_number_a_message_the_same_way` reads the published
constant out of each of the four and asserts the same integer. That is not
a hypothetical risk: `traverse.obligations` carries the identical rule for
dirty bits and exists because C and Python each numbered them from their
own list and disagreed about a struct carrying a tag and an invariant. Four
walks would repeat it, and nothing else would notice -- an id is a small
integer and a wrong one reads exactly like a right one.

**Rust compiles its `message_text` as a match over associated constants**,
which is legal because a const path is a pattern, and it is run under
`-D warnings` like everything else this backend emits.

**Each sabotage was confirmed to land, and two did not the first time.**
The C short-circuit sabotage matched nothing because the anchor was written
with two tabs where the file has one; the numbering sabotage had to be
aimed at ONE backend, since reversing the shared rule keeps all four in
agreement and the test says nothing about which order is right. A test that
compares descriptions is blind to a change made to the thing they all read,
which is the property that makes it worth saying rather than assuming.

**Still to do**: the `refuse` half, which has to land in all five
descriptions at once because the walk is held to the four compiled backends
over the same bytes, and the identity on existing per-member constraints
outside C.

### 26.382 A refusal, in all six descriptions at once

**0051's last half, and it could not land anywhere on its own.** A
`refuse` makes a message illegal, so `validate` says so -- and `validate`
is what the four compiled backends, the Python walker and the C walker are
all held to over the same bytes. One of them learning it first is a
description disagreeing with five for a reason that is not a defect, which
is why 26.378 recorded the gap with a test rather than closing it early.

**Six, not five.** The walker is two implementations: `walker/report.py`
and `walker/c/situ_walk.c`, which read the same image and are compared
against each other per schema. The C walker already had the expression
evaluator the predicate needs, so what it wanted was the section, the
record layout and a loop -- and the loop sits exactly where the other five
put theirs.

**The order is the contract, not just the verdict.** A `when` is a
predicate over the whole struct, so a member that is wrong is the more
specific answer, and every description evaluates refusals LAST. Five
agreeing about the verdict and not about the order would disagree on any
frame that trips a member check and a message at once, and the code that
comes back would depend on which description was asked.

**`*count` has a sibling problem and `*which` has the same shape.** A
CHECK id names a member and a `when` has none, so C writes the sentinel
rather than a MSG id: the two are different spaces, and an id from one
read as the other resolves to whichever member sits at that index. The
sentinel goes on the line above the refusal, because that is `check`'s
own contract -- and the gate that enforces it caught the first version,
which returned without writing it.

**`failed_check` gained a third answer rather than reusing one.**
`BY_MESSAGE` is not `CANNOT_SAY`: the walk HAS the refusal and ran it.
Saying "cannot say" would have made an answered question look like an
unanswerable one, which is invariant 154's distinction pointed at a new
case.

**The packer had to move a block to make this decidable.** A struct whose
`refuse` the VM cannot encode cannot answer `validate` either -- and the
flag that says so is computed well before the messages were being packed.
So the `when` compilation moved ahead of it: an unencodable refusal now
clears that struct's `validatable` bit, and both walkers answer
`cannot-say` rather than a verdict they cannot support. `validate` is the
one probe that cannot be rendered by halves, and this is that rule
reaching a construct written three days after it.

**0051 is built.** What remains of it is the identity on existing
per-member constraints outside C, which is 26.231's half rather than this
record's construct.

### 26.383 Which member refused, in C++ -- and what comparing two backends found

Moved to `doc/decision/26.383-which-member-refused-in-c-and-what-comparing-two-backends-found.md`.

### 26.384 The same identity in Rust, and a fault the comparison found twice more

**26.231's half in the third backend**, and it needed a restructure first.
Rust built its checks in one flat loop over entries where C and C++ have a
per-entry helper, so there was nothing to group by member. The loop's body
moved into `_member_checks` with `continue` becoming `return`, and nothing
else changed -- **proved by regenerating all 41 schemas and diffing: the
modules are byte-identical.** That is the proof a mechanical change carries,
and it is what made the rest safe to build on.

**The method cannot be called `check`, and the reason is a real schema.**
`cpio_header` has a member named `check`, so the accessor takes that name,
and Rust has no overloading: the module had duplicate definitions and did
not compile. It is `check_which`. The same hazard already reaches
`validate`, `extent` and `required` in this backend and has never fired, no
schema having such a member -- the difference is that this one fired on the
first build of the corpus rather than waiting for somebody's schema.

**Two more naming faults, both caught by rustc rather than by reading.**
`_ident` escapes a keyword as `r#type`, which upper-cased is
`CHECK_R#TYPE`; a constant is SCREAMING_SNAKE_CASE where no keyword can
reach, so the escape is wrong there. And a reserved member's local name is
`<reserved0>`, whose angle brackets rustc reads as chained comparisons.
`c_name` -- what C and C++ already name theirs with -- answers both.

**The comparison has now found the same fault in all three backends, which
is what makes it worth more than any of them.** A member whose check
PROPAGATES rather than naming a refusal was grouped as having no id, so
`check` returned refused while leaving `*which` at the sentinel its own
documentation calls "nothing refused":

    C      return err;                       a nested member's status
    C++    return e;                         the same, one language over
    Rust   self.flags()?.validate()?;        the `?` IS the return

Three spellings of one thing, and none was visible from inside its own
backend: what is missing is a constant nobody named, so the header looks
complete. Only a second description asking the same question found it --
which is `evidence.md`'s corroboration rule earning its place, since the
three were written separately and agree about everything else.

**Python remains.** Its `validate` raises rather than returning a code, so
the identity belongs on the exception, and 52 of its 342 raise statements
span more than one line -- appending an argument means balancing
parentheses forward from the `raise`, with an assertion that the balance
closed.

### 26.385 The identity in Python, and the coincidence that passed a sabotage

**26.231's half, finished.** All four backends now name the member
`validate` refused over. Python's goes on the EXCEPTION -- `SituError`
gains a keyword-only `which`, defaulting to a `NO_CHECK` the runtime
exports -- because that is where a Python caller already has to look: the
other three take an out-parameter and a Python caller does not pass one.

**Editing a raise is text surgery, so it carries a proof rather than a
convention.** The other three write `*which` on the line above the
refusal; Python's refusal IS the raise, so the id becomes a keyword
argument to it -- and 52 of this backend's 342 raises span more than one
line. The tool accumulates each statement until it PARSES, inserts the
argument, and asserts the result parses again as a `raise` whose call now
carries that keyword. A paren count would have done instead, and would
have been wrong inside any message carrying a bracket of its own.

**The propagating case has now appeared in all four backends and Python's
was the worst of them.** A nested member refuses by raising from its own
`validate`, so the exception arrives carrying THE NESTED STRUCT'S id --
not "no id", which is what the other three had, but a number that means
something else. `outer` would have reported `flags`' numbering,
confidently. It is relabelled in an `except SituError` that re-raises,
which is the same act as C setting `*which` before `return err;`.

Python also has a second spelling nobody would guess: `_ = self.ino`, a
statement whose whole purpose is the exception it may raise, which reads
as doing nothing at all. That is how a text number's digits are checked
here, and cpio_header has eleven of them.

**A sabotage passed, and that is the entry's real content.** Breaking the
relabelling left `test_a_nested_refusal_is_relabelled_with_the_parent_s_id`
green -- because the fixture put the nested member FIRST, so the parent's
id for it and the nested struct's own id were both 0. The test was right
by coincidence, which reads exactly like right, and only sabotaging what
had just been built showed it. A member before the nested one separates
them, and the fixture now asserts the two ids differ before relying on the
difference.

**What the comparison has now found, in one sentence per backend:**

    C       return err;                    no id
    C++     return e;                      no id
    Rust    self.flags()?.validate()?;     no id
    Python  raises from the nested validate    the WRONG id

Four backends, written separately, each missing the same case in its own
spelling -- and the fourth failing worse than the other three, because an
absent identity is silent where a wrong one is confident.

### 26.386 `parameter`: the construct, and a guard that guarded nothing

Moved to `doc/decision/26.386-parameter-the-construct-and-a-guard-that-guarded-nothing.md`.

### 26.387 A note would have been worse than a refusal

Moved to `doc/decision/26.387-a-note-would-have-been-worse-than-a-refusal.md`.

### 26.388 The README rule, made mechanical -- and what it found

**25.-1 says a keyword lands in the README too. A test now says so.**
`test_the_readme_names_every_keyword` reads the DECLARATION and MEMBER
keywords off `parser.py`'s own dispatch tables and `ATTRIBUTE_NAMES` off
the parser, and requires each to appear in the README as code -- a fenced
block or an inline span.

**It paid on the first run, and not only for this session's work.** Six
names were in the language and in no table:

    namespace          a file-scope declaration
    pad_random         a member kind (0045)
    [non_canonical]    a weakening the lattice cannot derive
    [require_aligned]  a member that must start on its natural boundary
    [timeout_ms]       a relation's retransmission contract (26.98)
    [retries]          the same

Four of those predate this session by phases. A rule nobody could check
had been followed for whatever was recent and not for anything older,
which is what an unenforced convention looks like from the outside.

**The scan is over code spans, not the whole file, and that is the whole
design.** Matching anywhere would pass `[bits]` on "eight bits with no
numeric reading" and `[version]` on "the version" -- a check inspecting
the wrong thing rather than nothing, which reads identically from the
output. And inline spans have to match ACROSS NEWLINES, because the README
hard-wraps: a first version stopped at the line break and reported
`preamble` missing while it was there, in a span that wrapped.

**No held-out set, deliberately.** Every name the parser dispatches on is
something a schema author can write, so the rule is right for all of them
-- and an exemption list here would be the first place a new keyword went
to avoid the work. The gate carries a control instead: a name no parser
dispatches on must come back missing, or three regexes over a file could
report everything present.

### 26.389 `situ verify` says what the schema says

**0051's fifth consumer, and the last one that record names.** A vector's
report now carries every `when` whose predicate holds:

    warn: frame `warned`: oversized
       --> says.vectors:3
        = longer than any early reader was written to hold

**Every severity, including on vectors that CONFORM.** A `warn` or a `note`
is by definition about a well-formed message, so a report that spoke only
about failures would never print one -- which is most of what the construct
is for. The messages are collected right after the view is acquired and
before `validate` runs, so a vector that `validate` refuses still carries
the `refuse` message naming why.

**The ids come from the module and the words from the schema**, which is
0051's split doing exactly what it was designed for. The generated module
answers WHICH messages hold; `situ verify` holds the schema, so it reads
the severity and the sentence from there. Nothing asks for `--messages`:
the module built in memory carries no strings at all, and the report
carries the schema's own words.

**The id is read back by POSITION, not by name.** `traverse.messages` is
the one list all six descriptions number from, and a name lookup would
agree today and stop agreeing the moment two structs state a `when` of the
same name -- which nothing forbids, since the check that names must be
unique is per schema rather than per struct.

**Three controls, because the assertions are string matches on a report.**
A vector holding no predicate must be spoken about by nothing; a schema
with no `when` must produce no severity line at all; and changing the
schema's sentence must change the report, which is what shows the words
are not the module's.

### 26.390 Refusing four backends left the fifth description emitting it

Moved to `doc/decision/26.390-refusing-four-backends-left-the-fifth-description-emitting-it.md`.

### 26.391 The wire signature names an argument, and one question it raises

Moved to `doc/decision/26.391-the-wire-signature-names-an-argument-and-one-question-it-raises.md`.

### 26.392 The dissector takes its argument from a preference

Moved to `doc/decision/26.392-the-dissector-takes-its-argument-from-a-preference.md`.

### 26.393 A Python view takes its arguments, and the chain that counted them

Moved to `doc/decision/26.393-a-python-view-takes-its-arguments-and-the-chain-that-counted-them.md`.

### 26.394 `situ verify` takes the argument, and where the refusal goes

Moved to `doc/decision/26.394-situ-verify-takes-the-argument-and-where-the-refusal-goes.md`.

### 26.395 A Rust view takes its arguments

Moved to `doc/decision/26.395-a-rust-view-takes-its-arguments.md`.

### 26.396 A C++ view takes its arguments, and a probe that proved half

Moved to `doc/decision/26.396-a-c-view-takes-its-arguments-and-a-probe-that-proved-half.md`.

### 26.397 A C view takes its arguments, by a tail rather than a wrapper

Moved to `doc/decision/26.397-a-c-view-takes-its-arguments-by-a-tail-rather-than-a-wrapper.md`.

### 26.398 A field and a method of one name, where the field is right

Moved to `doc/decision/26.398-a-field-and-a-method-of-one-name-where-the-field-is-right.md`.

### 26.399 A generated header that did not compile, found in passing

**Not a parameter fault, and not this arc's** -- found by the worker
building the C view, reproduced with no `parameter` anywhere, and fixed
separately:

```
struct frame { u8 off; u8 pad[4]; u8 marker at off; }
```

`situc build --target c` SUCCEEDED, and the generated `situ_frame_check`
called `situ_frame_marker_offset(view)` -- a function a located member
never emits, so `cc` refused the output with *implicit declaration of
function*. A build command reporting success and emitting code that does
not compile.

**How it survived is the corpus lesson again.** One schema in the tree
places a member this way -- `example/bmp`'s `u8 pixels[info.image_size] at
file.pixel_offset` -- and it is a NESTED path, which resolves to a
constant offset and takes a different route. The uncovered shape is `at
<bare local member>`, which no schema has. A construct the corpus does not
carry is one no gate can fail on, which is what put the `parameter` struct
on this arc's list in the first place.

**Fixed, and the check is not merely restored -- it is retired, because it
was never askable.** `situ_in_bounds(view, ...)` asks whether the FRAME
contains the member, and a located member's offset is not measured from
the frame; `check` takes a view and no message, so the question is not
expressible in its signature. The accessor asks it instead, against the
message, on every call -- which is what `offset = DataPlaced` in the map
is telling a reader it costs. `check` now carries a note saying so, rather
than a silent absence a reader would have to account for.

**Running the family found an older second fault the report had not.**
`u8 m at 2;` -- located at a CONSTANT -- reads nothing from the frame, so
`view` was an unused parameter and `-Wunused-parameter` under `-Werror`
refused it. The emitter had grown a `(void)view;` for this during the
parameter work (26.397) and guarded it on the struct taking an argument,
which was the conservative choice while that work was in flight and is
exactly what left the older case broken: a schema with no parameter cannot
reach the argument branch. The condition is the rendered text alone now --
an expression that uses the view necessarily spells it.

**Four shapes, and the sabotages separate them.** Disabling the
check-skip fails all four; disabling `(void)view;` fails only the two
constant ones. So the parametrisation is load-bearing rather than
decorative, and the case that was reported is not the case that found the
second fault.

Zero churn on the corpus, measured the same way as 26.397 -- 41 schemas,
164 generations, 328 files, identical against `HEAD` -- with the
comparison controlled by perturbing one captured file.

### 26.404 `situc explain` places a member that occupies nothing

Moved to `doc/decision/26.404-situc-explain-places-a-member-that-occupies-nothing.md`.

### 26.400 A refusal test whose population went empty, and its two detectors

Moved to `doc/decision/26.400-a-refusal-test-whose-population-went-empty-and-its-two-detectors.md`.

### 26.401 A refusal message that outlived what it refused

Moved to `doc/decision/26.401-a-refusal-message-that-outlived-what-it-refused.md`.

### 26.403 Both walkers take the argument, and where each one puts it

**The image carries it as `image_placement.text_flags` bit 6.** `flags` is
full -- all eight bits spoken for -- and `text_flags` had 6 and 7 free,
with `peek` already at bit 5 for exactly this reason: it is the
placement's SECOND flag byte rather than a text number's alone, and `peek`
is `parameter`'s nearest cousin, the other member that occupies nothing.
So the record did not grow, the stride did not move, and `situc map
--check` and `situc wire --check` both report `std/image.situ.map` and
`.wire` current -- verified here rather than taken from the report, and
neither committed file is touched. Which also means the `[stream]`
wire-compatibility question 26.391 leaves open does not arrive through the
image.

**The row still says `offset_bits` and `size_bits`, and that is why the
flag cannot be inferred.** Layout answers `offset = 0` -- where `body`
begins -- and `size = 8`, how wide the argument is. A walker without the
flag spends that byte and reads the next member's first byte as the
argument: the same fault as the accessors, arriving at the walker by the
same route.

**Python keys arguments by placement index; C keys them by ordinal, and
that difference is a hole in one of them.** `acquire(image, buffer,
struct, args=())` stores `{placement index: value}`, so another struct's
parameter is simply not in the map. The C walker has no view -- every
entry point takes `(image, message, len, shape)` -- so the arguments go on
the `situ_walk_image` binding, keyed by ordinal, and in a schema with two
parameterised top-level structs, acquiring for `one` and then reading
`two` returned ONE's argument. A number, not an error.
`situ_walk_image` records `arg_shape` beside the arguments now and
`argument_of` refuses a mismatch.

**Refused in four places and never defaulted**, including again at the
point of use -- so a `View` built by hand, which the sub-view paths and
the tests both do, cannot get a defaulted answer through the window
either. Python raises a new `walk.Unsupplied` rather than a `Refused`, and
C gets `SITU_WALK_ARGUMENT = 10`, because everything else in those modules
answers a question about BYTES and this is the caller's omission. That is
26.394's distinction: exit 2, not 1.

**Two sabotages first failed through the compiler rather than through the
check** -- `-Werror` on an unused static, then on a tautological compare
-- and were reworked until they failed through the check under test. That
is the rule about a control having to be REACHED, met twice in one file.

**And a `printf` trap cost two false readings.** `printf("%d %llu",
(int)situ_walk_read(..., &v), v)` reads `v` before the call that fills it
as readily as after, and both times it looked exactly like the walker
returning a wrong value. The committed driver sequences the read into its
own statement and says why.

**One deliberate non-change, recorded rather than taken:** `parameter` is
not in `pack.CONSTRUCTS` / `ENCODED`. That table lists side-table
FAMILIES, and `peek` and `scaled` are flags and are not in it either -- so
adding one flag and not the others would be the inconsistency rather than
the fix. Whether the coverage report should count flags is a question for
whoever owns that table.

### 26.402 The corpus struct costs more than 0050 says, and the tree has the precedent

Moved to `doc/decision/26.402-the-corpus-struct-costs-more-than-0050-says-and-the-tree-has-the-precedent.md`.

### 26.407 Skipping the struct rather than the schema, in checks and fuzz

Moved to `doc/decision/26.407-skipping-the-struct-rather-than-the-schema-in-checks-and-fuzz.md`.

### 26.408 A uniform tail is a rule with two halves, and only one was applied

Moved to `doc/decision/26.408-a-uniform-tail-is-a-rule-with-two-halves-and-only-one-was-applied.md`.

### 26.655 The cut, pass one: the tool, its proof, and the rate

Moved to `doc/decision/26.655-the-cut-pass-one-the-tool-its-proof-and-the-rate.md`.

### 26.654 A located accessor refuses an offset field the frame cannot reach

Moved to `doc/decision/26.654-a-located-accessor-refuses-an-offset-field-the-frame-cannot-reach.md`.

### 26.653 What the LOCATED probe found: the walker clamped where four refuse

Moved to `doc/decision/26.653-what-the-located-probe-found-the-walker-clamped-where-four-refuse.md`.

### 26.652 The LOCATED probe, and the control that refused to fire

Moved to `doc/decision/26.652-the-located-probe-and-the-control-that-refused-to-fire.md`.

### 26.651 `required` saturates at the type's maximum, and the cast that would have lied

Moved to `doc/decision/26.651-required-saturates-at-the-type-s-maximum-and-the-cast-that-would-have-lied.md`.

### 26.650 The last unclamped sum, and a reachability claim I got wrong twice

Moved to `doc/decision/26.650-the-last-unclamped-sum-and-a-reachability-claim-i-got-wrong-twice.md`.

### 26.649 The walker reports the declared count, and 26.642's blocker was not real

Moved to `doc/decision/26.649-the-walker-reports-the-declared-count-and-26-642-s-blocker-was-not-real.md`.

### 26.648 ~~The gate is red at the tip~~, and one of the twelve is a design question

Moved to `doc/decision/26.648-the-gate-is-red-at-the-tip-and-one-of-the-twelve-is-a-design-question.md`.

### 26.647 Fold: four clamp-once fixes, and the probe that found three

Moved to `doc/decision/26.647-fold-four-clamp-once-fixes-and-the-probe-that-found-three.md`.

### 26.646 Two cells in seventy-five: the probe pays out twice more

Moved to `doc/decision/26.646-two-cells-in-seventy-five-the-probe-pays-out-twice-more.md`.

### 26.645 The clamp on the count path, and the C cast that hid it

Moved to `doc/decision/26.645-the-clamp-on-the-count-path-and-the-c-cast-that-hid-it.md`.

### 26.644 The count disagreement is not a decision: 14.2b already settled it

Moved to `doc/decision/26.644-the-count-disagreement-is-not-a-decision-14-2b-already-settled-it.md`.

### 26.643 The fifth count, and a field read three times before the right question

Moved to `doc/decision/26.643-the-fifth-count-and-a-field-read-three-times-before-the-right-question.md`.

### 26.642 What the walker needs for the fifth count, measured rather than guessed

Moved to `doc/decision/26.642-what-the-walker-needs-for-the-fifth-count-measured-rather-than-guessed.md`.

### 26.641 The probe that found two things and is held back for both

Moved to `doc/decision/26.641-the-probe-that-found-two-things-and-is-held-back-for-both.md`.

### 26.640 The probe list as a partition, and a detector that could not see the exempt case

Moved to `doc/decision/26.640-the-probe-list-as-a-partition-and-a-detector-that-could-not-see-the-exempt-case.md`.

### 26.639 The differential asked a run of structs nothing at all

Moved to `doc/decision/26.639-the-differential-asked-a-run-of-structs-nothing-at-all.md`.

### 26.638 A `remaining` run of fixed elements counted zero, in four backends

Moved to `doc/decision/26.638-a-remaining-run-of-fixed-elements-counted-zero-in-four-backends.md`.

### 26.637 Fold: the sweep command that cannot answer zero, signalled

Two sweeps in this session read as a leak and both were the sweep's own
shell. `running-code.md` prescribes

    ps -o pid,ppid,stat,etime,rss,cmd --ppid 1 | grep -i <name>

after a run, and the pattern is in the command line of the shell doing
the matching -- so a CLEAN sweep, which is the result the sweep exists
to establish, reads back as one.

Measured here, both read at the time as something left behind:

    ps ... | awk '$3=="funk" && $2==1' | grep -icE "pytest|qemu|make"
                                     counted 1,  alive 0
    ps ... --ppid 1 | grep -iE "pytest|cc1"
                                     counted 2,  alive 0

**The second is why "subtract one" is not the remedy.** It answered TWO
because the eval ran a pipeline whose shell and subshell both carried
the pattern, so the inflation is a function of how many shells the
command happens to spawn. Printing the rows and reading them caught
both.

And 26.634 has the same class pointed at situ's own output rather than
at the process table: `situc build | grep -c "no builder"` printed 0,
which read as "nothing refused" and meant "the pattern does not match a
Python traceback" -- the generator was raising on the next line of the
stream the count had filtered.

**Signalled rather than fixed, and the entry is named**: the sweep
section is shared by every tree, so which form it prescribes is a
document decision. netcfgd had signalled the same thing on 2026-10-13
with four instances of its own; situ's two are appended to that entry
under *Signals from the projects* in `claude-guidelines`' `project.md`,
in `9771f4e`. The guidelines were re-read at that commit and
`~/.claude/tool/rules-seen` acknowledges it, so the marker returns the
next time they move.

#### Where this session stops

**At `b34511c`, gated green and pushed**, with nothing uncommitted but
another session's `tool/style_gate.py`. This paragraph said `b319cbf`
and 94 per cent, and both moved: the holder said to continue with the
weekly reset six hours off, so the deferred reader question was measured
rather than handed over -- which turned it into 26.638's four-backend
fix -- and the window then reached **97 per cent**, past the line where
`CLAUDE.md` stops development. Nothing was started after that.

What the last increment cost is worth knowing for the next one: one
measurement (ten minutes), four emitters each one rule, three backends
verified by running and one by reading, a constructed test because the
differential cannot see the accessor, and one container gate. The
remaining candidate of that shape -- the mixed-signedness loop bound --
was measured as cosmetic for every count this corpus and this
composition space can spell, so it is not the next thing to do.

26.636 has the ten increments and what was measured; the open list above
it is three decisions and one coverage gap. The next session's first
move is `python3 tool/sweep.py --writer --all`, which is green, and the
first thing worth BUILDING is the driver that asks a run for its count.

### 26.636 Fold: rung 2 writes, 160 to 180 structs, and the lens that replaces guessing

Moved to `doc/decision/26.636-fold-rung-2-writes-160-to-180-structs-and-the-lens-that-replaces-guessing.md`.

### 26.635 The writer over the composition space, and what the readers do there

Moved to `doc/decision/26.635-the-writer-over-the-composition-space-and-what-the-readers-do-there.md`.

### 26.634 A struct argument is the writer's parameter

Moved to `doc/decision/26.634-a-struct-argument-is-the-writer-s-parameter.md`.

### 26.633 A nested struct one byte wide is not a byte run

Three refusals said *`flags` has no stated length, so nothing says when
the writer should stop*. Its length is one byte and fixed.

**The branch asked how wide an element is**, and a byte-wide element is
exactly what a byte RUN has -- so `flags flags;` went down the run path,
found no count there, and reached the fall-through refusal. The three are
`message.header.flags`, `telemetry.reading.status` and
`header.header.flags`, each a group of bits that closes a byte exactly,
which is the commonest shape a protocol header has. A nested struct TWO
bytes wide was never affected, which is why the family had three members
and not thirty.

What distinguishes the two is not a width: **it is whether the member
names a struct of this schema and is one of it**, which is the question
the branch asks now. The sixth branch in these increments keyed on a
shape a member usually has rather than on what the member is, after the
packed size field, the varint size field, the element-count ordering,
`_len` on a run measured in elements, and `situ_at_len`.

The composition is what the fix buys, and it is the ordinary one:
`situ_flags_build` writes the byte and `situ_header_build` takes it as
bytes, like every other nested member. `A8` is `1010 1000` -- urgent
set, ack clear, priority five, and the reserved three bits zero because
the schema says so -- and the header comes out
`01 02 A8 00 10 00 03 11 22 33 44` with the reader finding the bits
inside the byte again.

**And the message's only corpus instances were all three of those
misdiagnoses**, which is a new way for a claim to go quiet: the gate
reported the row unemitted the moment the fault was fixed. What would
still reach it is `u8 x[]` with nothing stating its length, and the
parser refuses that outright -- *an array needs a size here* -- so it is
listed as constructed, a net for a shape nobody has found, like the
reserved fall-through 26.628 left in the same position.

**179 of 242 structs build**, 36 of 45 schemas, 96 of 112 arms.

#### What is left is mostly correct refusals

80 refusals in 35 families before this, 77 after, and the top of the
list is things a writer at this rung SHOULD decline: nine sealed
regions, whose interior is written through the seal and needs a key that
is rung 3's; eight tags whose schema names no codec, which situ cannot
invent; seven coded regions, where the bytes are a codec's output; five
registers, which are bus transactions rather than bytes in a buffer.

Of the rest, one is a decision rather than a gap and belongs to the
copyright holder: **four structs are refused as "entirely literal, so
there is nothing to build"** -- json's `true`, `false` and `null` among
them. The refusal is accurate and the rule behind it was deliberate:
`_encode` of a constant is not a builder. But an adopter composing a
JSON document then hand-writes those four bytes, which is the
duplication this compiler exists to remove, and situ emits no constant
for them to use either. Whether a struct with nothing for a caller to
supply should get a builder is a question about what a builder IS, so it
is recorded here rather than answered in passing.

### 26.632 Escaping is forced, not invented -- and three copies of one cap

Three refusals said a run *"escapes its delimiter, and escaping would
write bytes other than the ones it was given"*. That is true and it is
not a reason. **The caller hands over CONTENT and the wire form is the
schema's**, which is the division every other member here already
follows: a size field is computed, a pad is computed, a checksum is
computed, and these bytes are encoded.

What makes it not an invention is that the encoding is **forced**.
`situ_scan_relaxed` takes the byte after an escape as content whatever it
is, so exactly two kinds of byte must carry one:

  - the delimiter, or the run reads back shorter than it was written;
  - the escape byte itself, or it swallows the byte after it -- and a
    content byte ending in one swallows the DELIMITER, leaving the member
    unterminated, which is the fault `sexpr`'s own comment describes from
    the reading side.

**Nothing else is escaped**, and that is the line: a JSON encoder also
escapes control bytes, and situ's `[escape = ...]` does not say so. It
says which byte escapes, and the reader accepts any byte after it.

So json's and sexpr's strings and `edges.escaped_run` build:
`a"b` comes out `"a\"b"`, `ab\` comes out `"ab\\"`, and the reader's own
scan finds its delimiter in both.

**The sabotage separates the two rules, and only one of them is caught by
`_validate`.** Dropping the escape-the-escape rule makes `ab\` fail the
build -- the closing quote is escaped, the member is unterminated, and
the struct's own validation refuses it. But `a\b` comes out `"a\b"`,
which the reader accepts **at the same length** while meaning something
else under the escape rule: `\b` is one escaped byte rather than two
content ones. That half is invisible to every check situ has, which is
the argument for the rule rather than for the validation, and the reason
the test asserts bytes.

#### The cap was three copies of one rule, all short by one

`[max = N]` bounds the member's bytes, and the reader's scan window is
`min(N, remaining)` -- so the delimiter has to land INSIDE the cap and
the rule is "content and delimiter together". **This writer checked it in
three places and every one read it as "content"**: a delimited run, an
escaped run, and minimal digits. Each accepted exactly one length that
`_validate` then refused.

Measured, with `max 8` and `max 4`: seven content bytes build and eight
return CONSTRAINT having agreed to them; three digits build and four do
the same. A builder that reports CONSTRAINT for an input it accepted at
plan time is the honest-but-useless state a refusal by name exists to
avoid, and here it was produced by arithmetic rather than by a missing
branch.

**They are gone rather than corrected**, which is 26.628's answer to a
pad's bounds and is this file's own stated rule: two checks of one schema
is how they come to disagree, and these had. The view states the cap, the
builder validates against the view before returning, and that catches
every over-cap run by construction -- an over-long run is precisely one
whose delimiter is not in the window. Behaviour measured identical with
the checks in and out, at the boundary and either side of it.

What the cap means is now visible in the escaped case:
`edges.escaped_run` is `until "," max 64 [escape]`, so a comma in the
content costs two of the sixty-four, and 62 bytes with one in them take
the same budget as 63 without. That is the one place the three-way
relationship -- content, encoding, cap -- can be seen at all, which is
why the test is written over it.

#### Two more things the loop had to get right

The length is accumulated **against the room left** rather than summed
and compared afterwards: a content length near `UINT32_MAX` doubles past
it, and a wrapped total passes a capacity check it should fail.
`situ_cap - situ_at - situ_need` cannot wrap, because the running total
is at most the capacity by induction.

And the null check goes **before** the loops rather than inside the copy,
unlike every other caller-measured run here: those skip a `memcpy` at
zero length, where these loops read bytes one at a time. A length with a
null pointer is the caller's mistake and it gets BOUNDS, not a crash.

**176 of 242 structs build**, 36 of 45 schemas, 96 of 112 arms.

### 26.631 Two writing primitives the runtime did not have

Seven refusals were one family: the runtime could not write the bytes.
Four were big-endian base-128 varints -- SQLite's record varints, and
three in `edges` -- and three were text numbers the schema declares
signed. Both are runtime gaps rather than generator ones, which is the
distinction that decides where the fix goes: a second implementation in
the generator is the fault `owned.py` paid for in BCD, where an encode
and a decode were self-consistent and wrong together.

**`situ_varint_be_put` is the decoder backwards, beside it.** The runtime
had `situ_varint_be_get` and `situ_varint_be_len` and no encoder, so the
length comes from that same `_len` -- the function the minimality check
reads -- rather than from arithmetic done twice. SQLite's ninth byte is
the case worth stating: `terminal_bits == 8` means it carries all eight
of its bits and ends the value whatever its high bit says, which the
decoder does unconditionally at `max_bytes - 1`, so the encoder must too.
A value too wide is refused rather than truncated, because `_len` clamps
its answer to `max_bytes` and the write would otherwise drop the high
bits silently.

Round-tripped at every boundary against the decoder, which is the only
check that catches a sign or shift error: 0, 127, 128, 2^14-1, 2^56-1,
2^56 and UINT64_MAX through SQLite's nine-byte form, and MIDI's
four-byte one beside it. 2^56 comes out `80 C0 80 80 80 80 80 80 00`, and
a cell with a 200-byte payload writes its size as `81 48` and a rowid of
300 as `82 2C` -- SQLite's own spelling.

**`situ_format_int` is `situ_parse_int` backwards**: `-` and never `+`,
no `-0`, and `len` is the whole field including the sign, as
`situ_format_uint`'s is including its leading zeros. The magnitude is
`-(value + 1) + 1` rather than `-value`, which is undefined at
INT64_MIN -- and INT16_MIN is in the test for the same reason, since a
writer that negates the value rather than its predecessor is wrong at
exactly one input.

For `[minimal]` the sign is a byte of width on top of the magnitude's
digits, counted in the generated code because that is where the digit
count already lives. All six values round-trip through the reader's own
getter and `_validate`.

#### A varint can be the size field, and that is three for three

`payload_size` was in the signature. SQLite's cell is
`payload_size`, `rowid`, `payload[payload_size]`, so the caller was
passing the size AND the payload's length -- two numbers for one fact,
with nothing to stop a message disagreeing with itself.

**The third branch in two days to take a value for a member the schema
says the writer computes.** A packed field was the first (26.630), a
varint the second, and the element-count branch ordering was the third.
The cause is structural and worth stating where the next branch will be
written: the size-field check sits AFTER every branch keyed on what a
member IS, so each of those branches has to ask the question itself or
silently win. Three instances is a pattern rather than three bugs.

`VARINT` was also missing from `CALLER_SUPPLIED`, which came out when the
signature guard dropped every varint rather than the computed one: a
struct whose only caller-supplied member is a varint would have been
reported as "entirely literal, so there is nothing to build".

#### The guard that had to arrive with the signed support

`_radix_refusal` returns before `_scalar_refusal`'s own `scaled` check,
so a `scaled i64` written in digits was being refused for its SIGN.
Handle the sign and it falls through to a writer that formats an
integer: **1.5 written as 1**, which is `f32 x[2]` from the previous day
in a second construct. The scaled check now lives in the radix table
where it is reachable, and json's numbers and edges' `measured` say what
is actually in the way.

That is twice now that adding a capability would have un-refused
something that needed a different refusal. The habit that catches it is
reading the branch ORDER rather than the branch: a refusal table ordered
by what a member lacks answers for whatever it reaches first.

#### An out-parameter read in its own call, for the fourth time

`printf("%d %llu", f(view, &size), size)` prints the value `size` had
before the call, because the arguments are unsequenced. It has now
happened four times in these increments, every time in a throwaway C
probe, and every time the symptom was a plausible wrong number rather
than a crash -- here `0 0` for a writer that was correct.

The rule is in the probes as a comment now, where the next one gets
written: **call it, then print it.** A probe is not a test and gets
written fast, which is exactly why the same hazard keeps finding it.

#### Three tests pinned refusals this removed, and one had moved twice

`test_a_nested_struct_whose_extent_its_bytes_do_not_state_is_refused`
asserted a misdiagnosis for a day, was corrected to pin the LEB128-only
refusal, and that is gone as well. A message pinned twice is a message
the test should not be pinning, so it checks the property underneath --
a varint type has no `_required`, read from the artifact -- and that the
planner reaches its varint branch at all.

The BE128 refusal test became a statement about which encoder the writer
calls and that it passes the declaration's own `max_bytes` and
`terminal_bits`, the same properties `emit.py` hands the reader. And the
signed digits case left the refusal table for a test that asserts bytes.

**173 of 242 structs build**, 36 of 45 schemas, 96 of 112 arms.

### 26.630 Running a size expression backwards, and the canonical IPv4 header

Moved to `doc/decision/26.630-running-a-size-expression-backwards-and-the-canonical-ipv4-header.md`.

### 26.629 The internet checksums, and a refusal overturned by its own argument

Moved to `doc/decision/26.629-the-internet-checksums-and-a-refusal-overturned-by-its-own-argument.md`.

### 26.628 A reserved region has a content policy, and the writer ignored it

Moved to `doc/decision/26.628-a-reserved-region-has-a-content-policy-and-the-writer-ignored-it.md`.

### 26.627 Elements wider than a byte, and asking what an element IS

Moved to `doc/decision/26.627-elements-wider-than-a-byte-and-asking-what-an-element-is.md`.

### 26.626 A `while` run is proved by the reader's own count, not by its predicate

Moved to `doc/decision/26.626-a-while-run-is-proved-by-the-reader-s-own-count-not-by-its-predicate.md`.

### 26.625 Every refusal carries a claim, and the gate catches the half that is false

Moved to `doc/decision/26.625-every-refusal-carries-a-claim-and-the-gate-catches-the-half-that-is-false.md`.

### 26.624 `\r\n` is one delimiter, and the control I wrote first tested the bound

Eleven members ended at a multi-byte delimiter and every one was a
single, consumed, two-byte sequence -- `\r\n` in http, smtp and edges,
`: ` in one. So there was nothing to decide: the single-byte and
multi-byte forms are **one emitter**, because the one-byte case is the
other with N = 1, and a second comparison written for it is a second
thing to be wrong.

Three sites use it: the scan that refuses a byte run containing its own
delimiter, the boundary check in a struct run, and the write. All three
now take the sequence.

**A real HTTP request head builds, every byte from generated code:**

    GET / HTTP/1.1\r\nHost: example.invalid\r\n\r\n

41 bytes -- the line from `request_line_build`, the field handed over as
bytes because a `header_field` ends at TWO delimiters and is refused for
that, and **the blank line written by the run's own delimiter**. It
acquires a view.

**The control I wrote first tested the loop bound rather than the
comparison**, and that is the part worth keeping. A sequence scan must
accept a CR *not* followed by LF, since that is not the delimiter -- so
the fixture is a version carrying one. My first was `HTTP/1.1\r`, with
the CR last, and it passed with the needle cut to its first byte:
`scan + 2 <= len` never reaches the final byte, so the bound excluded it
either way. The fixture that discriminates is `HTTP\r1.1`, with the CR in
the middle where the scan looks: correct, it is accepted; first byte
only, it is refused.

That is `evidence.md`'s *a control has to be reached* for the fourth time
in two days, and the first three were upstream checks intercepting the
case. This one was intercepted by **the loop bound of the very check
under test** -- which is nearer to home and harder to see, because the
sabotage and the interception live in the same four lines.

**And a parametrized case was removed rather than reworded.** The table
of delimiters the writer cannot place had a `\r\n` row asserting the
refusal this entry deletes. There is no refusal left to assert, so the
row is gone; what remains is one ambiguity the schema has -- several
delimiters -- and two transformations of the caller's bytes, escaping and
trimming.

Re-measured, same method -- the 45 schemas `every_schema` holds: **141 of
242 structs have a builder, in 34 of the 45 schemas**, up eight structs
and two schemas. What leads the refusals now is a `while` run (14), a
sealed region (9), the eight tags naming no codec, a codec (7), and a
`u8` run whose length nothing states (7).

### 26.623 A varint, and the fourth time a branch ordered by absence misread a member

The largest refusal after the checksum named a construct that does not
exist: **fifteen refusals said "is a nested `remaining_length`"**, and
`remaining_length` is mqtt's varint type. A varint has no `scalar` and a
`type_name` naming no struct, so it fell through to the nested-struct
branch, which is reached by what a member LACKS.

**That is the fourth misdiagnosis of this family in two days** -- after
the `before` run told about its delimiter count (26.612), the nested
field called a run (26.614), and the arm parameter chopped out of a local
(26.617). Every one had a correct verdict and a reason about something
else, and every one came from a branch ordered by absence. The remedy
each time was to ask first what the member IS.

**And a test of mine was holding this one in place.** 26.614's test
asserted the refusal text *"nested `edge_varint`"* -- so the suite pinned
the misdiagnosis, including the wrong half, and would have gone red if
the message were corrected without the test. A test that pins a message
pins whatever the message says. It now checks the refusal that applies.

**Writing one needs no new arithmetic**: `situ_varint_put` encodes
minimally and returns the bytes written or zero, and `situ_varint_len` is
beside it. So the writer bounds the value by the schema's `max_bits`,
calls the runtime, and advances by what it reports. Against LEB128's own
vectors, with 321 being MQTT 2.2.3's worked example:

    0          -> 00
    127        -> 7F
    128        -> 8001
    321        -> C102
    268435455  -> FFFFFF7F
    268435456  -> SITU_ERR_CONSTRAINT

The last is the standard's *"the receiver MUST close the network
connection"* seen from the writing side: one past 28 bits is refused
rather than written in five bytes.

**Two of the corpus's four varint types stay refused**, and by name:
sqlite's and edges' are BE128, which has a reader and a length function
and **no encoder**. Writing one here would be a second implementation of
a conversion whose first lives in the runtime -- `owned.py`'s BCD, where
an encode and a decode were self-consistent and wrong together.

**The parameter the writer needed was the fourth, so the four became
one.** `plan` had grown `header`, `prefix`, `structs` and `arm`, and the
varint declarations would have made a sixth positional argument. They are
a `Shape` now, and what makes it more than tidiness is that every field
is read from something the compilation already produced -- the emitted
header for `_required` and `_compute`, the resolved schema for a nested
type, the declarations for an encoding -- rather than derived a second
way. An absent field means the writer refuses more rather than guesses,
which is the direction 26.614 established.

Re-measured, same method -- the 45 schemas `every_schema` holds: **133 of
242 structs have a builder, in 32 of the 45 schemas.** Only one more
struct than before, which is the honest result: the fifteen refusals were
fifteen ARMS of one struct, and fixing what they said revealed the next
real reason in fourteen of them. What leads now is a `while` run (14), a
multi-byte delimiter (10), a sealed region (9), the eight tags naming no
codec, and a codec (7).

### 26.622 A trailing checksum, computed where the caller has the whole message

Moved to `doc/decision/26.622-a-trailing-checksum-computed-where-the-caller-has-the-whole-message.md`.

### 26.621 The cut has no outlier to lead with, and that decides how it can be done

26.608 measured this document at 88 per cent log and called that the
number to act on. The holder's instruction was to cut it: keep each
entry's rule and its load-bearing number, drop the narration, **one
commit per pass rather than by attrition** -- the shape `evidence.md`
took on 2026-09-04, from 3044 lines to 1989.

**Measured before starting, and the measurement says the pass cannot be
done the way the instruction asks.** 620 entries, 38,974 lines between
them, **mean 62 and median 55**, longest 543:

    top  50 entries     7,194 lines   18% of the entry text
    top 100 entries    11,779 lines   30%
    top 200 entries    19,099 lines   49%
    top 400 entries    30,465 lines   78%

So there is no fat tail. Cutting the fifty longest entries perfectly
removes under a fifth of the text, and reaching three quarters of it
means touching **four hundred entries**, each wanting a judgement about
which of its numbers is load-bearing. That is not one session's work,
and it is work whose quality depends on having written the entry: of the
620, this session wrote eleven.

**A twenty-entry nibble is the thing the instruction rules out**, and for
a reason this document can check: `git log -S` on a rule's own words
recovers the case from the commit that removed it, which works only while
the removals are deliberate commits rather than a trickle nobody can
name. So the choice is a real pass or none, and a real pass is a
different piece of work from the one that was authorised.

**And the measurement went wrong twice in the same way first**, which is
worth more than the figures. Both earlier attempts charged
`### Invariants to hold across all phases` -- 1,391 lines of standing
rules, the most load-bearing prose in the file -- to the entry above it,
reporting a 1,432-line `26.115` whose real length is 41. An entry's span
ends at the next heading of ANY depth, not at the next entry heading,
and a document with non-entry blocks inside its log section breaks the
simpler rule silently. **Twice, after the first correction was recorded**
-- so the record of a measurement error is not a guard against repeating
it, and the figures above are the first ones taken with the invariants
block excluded.

**What is left is a structural option, and it is not this session's to
take.** Section 26 is 84 per cent of the file, and its entries are cited
by number from everywhere -- which is exactly the justification
`CLAUDE.md` gives `doc/decision/`: a single file cannot give a stable
anchor per record, and a record that gets referenced needs one. Moving
the log out of `project.md` would leave the design document readable in
one window without any editorial judgement being spent, and it would
cost a rewrite of every path that reads `project.md` -- the docs gate,
the dangling-citation test, the conventions test -- plus a convention
other projects would then look at. **The option, its cost, and whose it
is: the copyright holder's**, per *Describing a thing thoroughly is a way
of proposing it*.

Until then the cut stays authorised and undone, and this entry is what a
session picking it up should read first, so that the measurement is not
taken a fourth time.

### 26.620 Measuring an element is not validating it

Moved to `doc/decision/26.620-measuring-an-element-is-not-validating-it.md`.

### 26.619 The peek minimum counted peeks the data may never reach

Moved to `doc/decision/26.619-the-peek-minimum-counted-peeks-the-data-may-never-reach.md`.

### 26.618 A byte several members share is written once, and refused when it cannot hold them

Moved to `doc/decision/26.618-a-byte-several-members-share-is-written-once-and-refused-when-it-cannot-hold-them.md`.

### 26.617 A variant is one builder per arm, and the arm says which it is

Moved to `doc/decision/26.617-a-variant-is-one-builder-per-arm-and-the-arm-says-which-it-is.md`.

### 26.616 hull's findings 13 to 15, verified: one fixed, two measured and left

Moved to `doc/decision/26.616-hull-s-findings-13-to-15-verified-one-fixed-two-measured-and-left.md`.

### 26.615 A run of structs is one walk, and the deferral that said otherwise

Moved to `doc/decision/26.615-a-run-of-structs-is-one-walk-and-the-deferral-that-said-otherwise.md`.

### 26.614 A nested struct is written as bytes, and asked its own extent

Moved to `doc/decision/26.614-a-nested-struct-is-written-as-bytes-and-asked-its-own-extent.md`.

### 26.613 The frame reader hands out a message its schema forbids

**hull reports, measured at `9d66b6d` and appended to
`suggestion/hull.md` as finding 12**, that the frame layer's `next`
neither applies a length field's `[max]` nor validates what it hands
out. Their schema and their three outcomes, in their words:

    struct message { u32 length [max = 1048576]; u8 body[length]; }

    a length of 1048577, buffer exactly SIZE_MAX (1048580)
        next = SITU_ERR_BOUNDS, a capacity error rather than a
        malformed message
    buffer 64 bytes larger
        next = SITU_ERR_TRUNCATED, waiting for a body that should
        never be read
    that body then pushed whole
        next = SITU_OK, and validate on the same view =
        SITU_ERR_CONSTRAINT

**Verified here from the generated code rather than from their probes**,
by building that schema at `--layer frame` in this tree. `_required`
reads the length with `situ_get_be32` into `situ_need_u32` and consults
no bound; `_reader_next` calls `_required`, then `_view`, and never
`_validate`. The number it would need is in the same header --
`SITU_MESSAGE_LENGTH_VALUE_MAX 1048576u` -- and the check that uses it
is in the `.c`, inside `_validate`.

**The verdict is not in doubt and situ's own code says why.** The
existing BOUNDS branch in `_reader_next` is justified in a comment: a
whole message needing more than the buffer can hold *"never becomes true
by waiting, so it is reported rather than left to spin on a stream that
will never satisfy it."* A length beyond `[max]` is that same class --
no amount of further reading makes it admissible -- so the principle for
refusing it early is already written down beside the code that fails to.

**What makes it a deliberate pass rather than a one-line fix is the
contract.** `_required` currently has two outcomes, SITU_OK and
SITU_ERR_TRUNCATED, and a caller reads `*need` on the second; refusing a
length means a third, SITU_ERR_CONSTRAINT, with `*need` meaningless.
Measured: **all four backends emit a `required`** -- `codegen/c`,
`codegen/cpp`, `codegen/python` and `codegen/rust` each have one in
`frame.py` and `emit.py` -- and `test_backends_refuse_the_same_members`
holds them to each other. So changing one is a divergence the parity
test exists to catch, and changing all four is a change to a public
function in every language situ emits.

**Two options, their cost, and whose they are.** Per
*Describing a thing thoroughly is a way of proposing it*, this is
recorded rather than chosen:

  - **`_required` refuses the length**, which stops the spin and the
    hand-out at the earliest point the fault is knowable, and costs the
    third outcome in four backends.
  - **`_reader_next` also calls `_validate`**, which closes the general
    case rather than the length one -- any constraint, not just a bound
    -- and costs a full validate per message on the framing path, which
    is the hot path for a stream reader. It also changes what `next`
    can return for a reason unrelated to framing.

They are not alternatives: the first is about a value that can never
become valid, the second about everything else. **Which to do, and
whether to do the second at all, is the copyright holder's** -- it is a
cost on every consumer of the frame layer, not a defect to be fixed by
whoever happened to read the report.

**Nothing waits on it.** hull states they check the length themselves as
soon as it is whole and validate every view, so their reader is not
exposed; the exposure is to a caller who follows the generated header's
own instruction, *call `next` until it answers SITU_ERR_TRUNCATED*, and
trusts what comes back.

**The reporting is worth noting separately.** This is the third finding
hull has produced by reading situ's generated output or its emitters
rather than by using the result -- after 26.606's four and the `before`
diagnosis in 26.612 -- and all three are things situ's own tests could
not have asked, because each is a case where the code does exactly what
some test asserts and the assertion is about the wrong question.

### 26.612 A run ended by its delimiter, and a count that counted the wrong thing

Moved to `doc/decision/26.612-a-run-ended-by-its-delimiter-and-a-count-that-counted-the-wrong-thing.md`.

### 26.611 Digits, and an absence manufactured by the pattern that looked for it

Moved to `doc/decision/26.611-digits-and-an-absence-manufactured-by-the-pattern-that-looked-for-it.md`.

### 26.610 Rung 2 writes: an append-only builder, and what it refuses by name

Moved to `doc/decision/26.610-rung-2-writes-an-append-only-builder-and-what-it-refuses-by-name.md`.

### 26.609 The coverage span moves to `traverse`, and the proof earned its keep twice

Moved to `doc/decision/26.609-the-coverage-span-moves-to-traverse-and-the-proof-earned-its-keep-twice.md`.

### 26.608 This document is 88 per cent log, and that is the number to act on

Moved to `doc/decision/26.608-this-document-is-88-per-cent-log-and-that-is-the-number-to-act-on.md`.

### 26.607 Three reproduced codegen faults, a rung nothing compiles, and 26.604 qualified

hull sent a reduced reproduction for their finding 6 and two more findings,
sections 10 and 11 of `suggestion/hull.md`. Chasing them found a third
fault they had not seen, in situ's own corpus, and made an entry written
earlier today wrong.

**Finding 6, diagnosed from the generated source rather than guessed.**
Their guess was right and here is the reading. For their `b1.situ` the
Python module holds `class bytes(View)` at module level, and `class list`'s
run-walker calls what it believes is the builtin:

	if bytes(self._msg.buffer[self._at + at:self._at + at + 1]) == b')':

`bytes` is the struct, `View.__init__` wants `(msg, at, length)`, and it
gets one argument -- hence their TypeError.

**The mechanism already exists for the other half and its population is
annotations.** `emit.py`'s `_unshadow` emits `_situ_list = list` and
rewrites annotations, and its comment explains why it is done at the point
of writing: *a bare `list` in a user annotation means the STRUCT and the
same word in a generated one means the builtin, and nothing in the text
tells them apart.* A CALL is the same sentence and the pass does not reach
it.

**I tried a post-pass and the file's own comment is why it failed.** Having
the AST looked like a discriminator the regex lacked, so the first attempt
rewrote every `Call(Name(n))` where `n` is both a module-level class and a
builtin. It broke immediately: `bytes(self._msg, self._at + start, size)`
is a legitimate construction OF THE STRUCT, and in the AST it is the same
node shape as `bytes(slice)`. **A parse does not know what the emitter
meant any more than a regex does**, so the fix belongs at each emission
point, spelled `_situ_bytes(...)` as `_situ_list` already is. Reverted
rather than half-landed.

**And the exposure is wider than one name.** Measured over the corpus by
generating every schema's Python and reading the AST: this emitter calls
**fifteen** builtins -- `len` 305, `max` 222, `min` 105, `bytes` 97,
`bytearray` 93, then `IndexError`, `bool`, `type`, `range`, `any`,
`object`, `list`, `frozenset`, `str`, `sum`. Three corpus schemas already
shadow one:

	json.situ       struct object   builtin called 4 times
	sexpr.situ      struct list     builtin called 4 times
	importer.situ   struct str      builtin called 2 times

All three latent, because no vector reaches the colliding call -- hull's
`bytes` had one. **Which settles the fix hull offered as an alternative:
refusing the name is wrong.** `object` in a JSON schema and `list` in an
s-expression schema are the names those formats have, and 26.562's C
precedent does not transfer: there the colliding names were situ's own
runtime vocabulary, `bounds` and `view`.

**Finding 10, reproduced, and it is in `example/http`.** `--layer edit
--target c` emits a decode that assigns every scalar from its `_get`:

	out->code = zz_status_line_code_get(view);

A text-converted member's getter is `situ_err_t _get(view, uint32_t *out)`,
because a decimal parse can fail. `http_edit.h` therefore does not compile,
and neither does hull's canonical atom. Whether the decode should read the
`_value` accessor beside it or propagate `_get`'s status is a convention
question -- the decode returns `situ_err_t` and could do either, and
discarding a parse failure into an owned copy is the half worth deciding
deliberately.

**A third fault, which nobody reported: `example/cpio`'s edit header emits
a placeholder as an identifier.**

	uint8_t <reserved0>;
	out-><reserved0> = zz_cpio_entry_reserved0_get(view);

Four errors. The ordinary header compiles, so the C backend has a spelling
for a reserved member and the edit emitter is not using it.

**Nothing compiles that rung, which is why both survived.**
`test_codegen_c.py` sweeps every schema at `view` and again at `frame`.
There is no edit-layer sweep, so two corpus schemas have been emitting C
that does not compile.

**And that makes 26.604 wrong where it was most confident.** That entry
argued the four absent per-rung sweeps cost nothing, because `layers.reach`
says 44 of 45 schemas reach only `view` and the one that reaches further is
the schema nine driver tests compile at the top rung. The reasoning covers
what a rung emits BEYOND `view` **where the schema has content for that
rung** -- and `--layer edit` does not work that way: it emits a header for
any struct needing backing, whatever the schema's reach. `http` and `cpio`
both have `reach=view` and both emit a broken `_edit.h`.

So the partition I asserted is true and answers a narrower question than
the sentence around it claimed. The guard added there still holds and is
still worth having; what was wrong is the paragraph saying the absent
sweeps cost nothing. They cost two broken headers.

**The gate is deliberately not added yet.** An edit-layer compile sweep is
the obvious remedy and it would go red on `http` and `cpio` the moment it
landed -- and `harmonization.md` is explicit that a red gate is when
somebody reaches for an ignore rule, so the fix comes first and the sweep
with it. Recorded here so the next pass has the population: build every
corpus schema at `--layer edit --target c` and compile the header.

**Finding 11 is hull's and is the holder's.** They asked for a canonical
writer from the edit layer and found that no rung builds a message: C's
`edit` emits 0031's owned decode, `edit.py` refuses a variant as *a shape
rather than a length*, and nothing in `situc/codegen` builds, appends or
resizes. README and 0032 both describe `edit` as *build or resize a message
whose extent is not fixed*. Whether a builder belongs at that rung or the
ladder's description should change is not something to settle while fixing
a decode, and hull said so too.

### 26.606 hull adopted situ and found four faults in situ's own example

Moved to `doc/decision/26.606-hull-adopted-situ-and-found-four-faults-in-situ-s-own-example.md`.

### 26.605 0034's fourth row is answered by two refusals meeting, and nothing said so

Moved to `doc/decision/26.605-0034-s-fourth-row-is-answered-by-two-refusals-meeting-and-nothing-said-so.md`.

### 26.604 Coverage by arithmetic with one term in it

Moved to `doc/decision/26.604-coverage-by-arithmetic-with-one-term-in-it.md`.

### 26.603 The frontend the fix did not reach, warned about in that file's own comment

Moved to `doc/decision/26.603-the-frontend-the-fix-did-not-reach-warned-about-in-that-file-s-own-comment.md`.

### 26.602 Two status documents telling a reader that built features do not exist

Moved to `doc/decision/26.602-two-status-documents-telling-a-reader-that-built-features-do-not-exist.md`.

### 26.601 The other six enums, and a silence that was right for an unwritten reason

Moved to `doc/decision/26.601-the-other-six-enums-and-a-silence-that-was-right-for-an-unwritten-reason.md`.

### 26.600 The other verdict crashed too, and the question asked once instead of four times

Moved to `doc/decision/26.600-the-other-verdict-crashed-too-and-the-question-asked-once-instead-of-four-times.md`.

### 26.599 Three kinds of region read as one, and the interior that is the holder's

Moved to `doc/decision/26.599-three-kinds-of-region-read-as-one-and-the-interior-that-is-the-holder-s.md`.

### 26.598 The argument nobody could supply, and a test that passed for decode's reason

Moved to `doc/decision/26.598-the-argument-nobody-could-supply-and-a-test-that-passed-for-decode-s-reason.md`.

### 26.597 Four of eight kinds, and the allow-list that followed from a short table

Moved to `doc/decision/26.597-four-of-eight-kinds-and-the-allow-list-that-followed-from-a-short-table.md`.

### 26.596 Every schema a compilation read, and the copy of our grammar it retires

Moved to `doc/decision/26.596-every-schema-a-compilation-read-and-the-copy-of-our-grammar-it-retires.md`.

### 26.595 Which arm is present, and the verdict that makes the answer safe

Moved to `doc/decision/26.595-which-arm-is-present-and-the-verdict-that-makes-the-answer-safe.md`.

### 26.594 A predicate built, tested, and called by nothing

Moved to `doc/decision/26.594-a-predicate-built-tested-and-called-by-nothing.md`.

### 26.593 Two generators of checks, and three sabotages that found my tests

Moved to `doc/decision/26.593-two-generators-of-checks-and-three-sabotages-that-found-my-tests.md`.

### 26.592 Two preconditions the compiler keeps and the shipped runtime did not state

Moved to `doc/decision/26.592-two-preconditions-the-compiler-keeps-and-the-shipped-runtime-did-not-state.md`.

### 26.591 A nested write, and two sabotages that proved nothing

Moved to `doc/decision/26.591-a-nested-write-and-two-sabotages-that-proved-nothing.md`.

### 26.590 The write differential's ceiling, and a mechanism that bought 0.3%

Moved to `doc/decision/26.590-the-write-differential-s-ceiling-and-a-mechanism-that-bought-0-3.md`.

### 26.589 Five readers were compared and four writers, and eight probes were wrong

Moved to `doc/decision/26.589-five-readers-were-compared-and-four-writers-and-eight-probes-were-wrong.md`.

### 26.588 A floor decays by exactly as much as the corpus grows

Moved to `doc/decision/26.588-a-floor-decays-by-exactly-as-much-as-the-corpus-grows.md`.

### 26.587 The one declaration keyword no schema exercised

Moved to `doc/decision/26.587-the-one-declaration-keyword-no-schema-exercised.md`.

### 26.586 The editor printed a `[secret]`, and carried no effect axis at all

Moved to `doc/decision/26.586-the-editor-printed-a-secret-and-carried-no-effect-axis-at-all.md`.

### 26.585 A RecursionError on every valid keystore, and the draw that cannot reach it

Moved to `doc/decision/26.585-a-recursionerror-on-every-valid-keystore-and-the-draw-that-cannot-reach-it.md`.

### 26.584 Five values on the mutate axis, and the marker named three

Moved to `doc/decision/26.584-five-values-on-the-mutate-axis-and-the-marker-named-three.md`.

### 26.583 `at expr` is the buffer's zero, and the note that could not be read

Moved to `doc/decision/26.583-at-expr-is-the-buffer-s-zero-and-the-note-that-could-not-be-read.md`.

### 26.582 The window is what the caller gave; the extent is what the struct is

Moved to `doc/decision/26.582-the-window-is-what-the-caller-gave-the-extent-is-what-the-struct-is.md`.

### 26.581 The tool told the reader to do something it did not support

Moved to `doc/decision/26.581-the-tool-told-the-reader-to-do-something-it-did-not-support.md`.

### 26.580 ID3v2.4, and the scalar kind it did not need

`example/id3` describes the tag an MP3 carries: the header, the extended
header, the frames, and the three encodings the real file uses. 85 tests in
the corpus sweeps exercise it, the four-backend differential among them.

**THE FINDING IS WHAT DID NOT GET BUILT.** ID3's defining oddity is the
synchsafe integer -- 28 bits of length in four bytes, seven to a byte, so
that no length can contain a run an MPEG decoder would read as a frame
sync. The first reading of the format concluded situ could not express one
and that a scalar kind had to be added beside `bcd`: `ScalarKind.SYNCHSAFE`
with a groups count, a decode and an encode in the walker, the four
backends, the capability map, the differ, the dissector. **Thirty-eight
sites across fourteen files, scoped from `bcd`'s own footprint before
anything was written.**

It is four `u1`/`u7` pairs with the length an expression across them:

	reserved u1  [must_be_zero];
	u7       size_0;
	...
	u8  body[(size_0 << 21) | (size_1 << 14) | (size_2 << 7) | size_3];

Every field lands at a static offset, the solver derives `Bounded(0,
268435455)` from the expression unaided, and **the stuffing bits become a
claim `validate` checks rather than a comment.** That last part is why this
is better than the type would have been and not merely cheaper: a
`synchsafe4` scalar would have masked the high bits silently, and nothing
would refuse a tag that set one -- which 6.2 says is malformed and after
which every byte means something else.

**One `situc map` found that out.** The measurement cost a command and the
feature would have cost a day, which is *Measure the feared cost before
deliberating* pointed the other way: usually it dissolves a question about
breakage, and here it dissolved a question about absence.

**And it is the second time in this corpus.** mqtt's header records the
same mistake made the same way -- a limitation written down that was not
there, 26.56 -- for a member whose presence depends on a flag, which is a
`variant` over one bit with `nothing` as its other arm. id3 uses that
construct for the extended header. So the pair is now the argument:
**probe the language before extending it, because a schema that records an
absent limitation is worse than one that records nothing -- a reader
designs around it.**

**What the format did cost, honestly.** Three things the schema states
rather than works around:

- **The extended header's CRC cannot be a `checksum`.** It is an ordinary
  CRC-32 stored as a 35-bit synchsafe integer, so the 32 bits the codec
  produces sit in no contiguous span of the message. `checksum` binds a
  codec to the bytes a tag occupies and there are none. Measured rather
  than asserted: the real tag's `04 4b 7f 72 5e` is 0x497FF95E destuffed,
  which is exactly `zlib.crc32` of the bytes from the end of the extended
  header to the end of the tag -- 3.2's coverage, confirmed. So the limit
  is narrow and stated: a checksum whose output is stored bit-stuffed.
- **The frame identifier is a bound, not a set.** 4 says `A-Z0-9`;
  `[min = 0x30, max = 0x5A]` also admits the seven punctuation characters
  between `9` and `A`. The schema says so rather than claiming the set.
- **The frame run carries netlink's caveat**, that situ's run ends after
  the element failing its test. What bounds it is the frame, and the real
  tag is the case that proves it: **it has no padding at all**, its last
  frame ending at byte 209 with the audio at 210, so there is no zero byte
  to stop the run and it stops anyway.

Five positive controls, each refused through its own check: a stuffing bit
set, a v2.3 major version, an extended header claiming 128 bytes, a frame
identifier out of range, and a reserved frame flag set.

**THE ORACLE WAS A CONSTANT, AND THE SABOTAGE IS WHAT SAID SO.** Excusing
id3 in `UNORACLED` would have been false -- two independent readers are
installed -- so it is oracled three-party: ffmpeg writes the tag, mutagen
reads it, situ reads it, and situ is compared against mutagen. Its `why`
claimed the synchsafe decode was on trial, because a group read in the
wrong place shifts every frame after it.

Shifting a frame size's third group by 8 rather than 7 **left the oracle
green.** Every frame ffmpeg wrote was under 128 bytes, so each whole size
sat in the lowest seven bits and `size_2` was always zero: the corpus could
not reach the bits the claim was about. The tag's own size was over 127 and
so the header's decode *was* covered, which is what made the gap
invisible -- one of the two decodes was genuinely checked.

A title long enough to need two groups fixes it, and the sabotage fails
now. **Not a better assertion -- a corpus that reaches the hazard**, which
is the distinction between a control that can fire and one that is
reached.

The first attempt at that used a long COMMENT and found something else:
ffmpeg writes a comment as `TXXX`, whose content is a description and a
value either side of a NUL, which ID3v2.4 gives in its 4.2.6 and is
not the shape this schema
describes. The oracle disagreed -- correctly -- about a frame the schema
makes no claim about, so the corpus uses a title instead. Both readings
had agreed about every boundary including the 200-byte frame, which is how
it was clear the disagreement was the oracle's text decode and not the
decode under test.

And the padding is a checked claim rather than a comment: the run's last
element is the first ten bytes of ffmpeg's padding, and the oracle asserts
`validate` refuses it. netlink's caveat, held to.

### 26.579 situ-edit recomputed no checksum, and said so as though that were 14.1

Moved to `doc/decision/26.579-situ-edit-recomputed-no-checksum-and-said-so-as-though-that-were-14-1.md`.

### 26.578 The draw plants the magics, and the three defects that cost

Moved to `doc/decision/26.578-the-draw-plants-the-magics-and-the-three-defects-that-cost.md`.

### 26.577 A nested struct validated against the parent's frame, not its own extent

Moved to `doc/decision/26.577-a-nested-struct-validated-against-the-parent-s-frame-not-its-own-extent.md`.

### 26.576 The walker ignored a byte-order marker, and every real TIFF read swapped

Moved to `doc/decision/26.576-the-walker-ignored-a-byte-order-marker-and-every-real-tiff-read-swapped.md`.

### 26.575 Three harnesses enumerated the emitter's branches, and it grew one that emits nothing

Moved to `doc/decision/26.575-three-harnesses-enumerated-the-emitter-s-branches-and-it-grew-one-that-emits-nothing.md`.

### 26.574 The four-way comparison was vacuous, and a test said so

Moved to `doc/decision/26.574-the-four-way-comparison-was-vacuous-and-a-test-said-so.md`.

### 26.573 A consumer's name, attributed to the wrong owner

Moved to `doc/decision/26.573-a-consumer-s-name-attributed-to-the-wrong-owner.md`.

### 26.572 A `_ptr` that returned an offset, and the proxy that hid it

Moved to `doc/decision/26.572-a-ptr-that-returned-an-offset-and-the-proxy-that-hid-it.md`.

### 26.571 A sealed interior reachable without the gate, in three member kinds

Moved to `doc/decision/26.571-a-sealed-interior-reachable-without-the-gate-in-three-member-kinds.md`.

### 26.570 A length only a transform can produce, and the three branches 26.440 did not reach

Moved to `doc/decision/26.570-a-length-only-a-transform-can-produce-and-the-three-branches-26-440-did-not-reach.md`.

### 26.569 a member named `at` made the Python module unusable

Moved to `doc/decision/26.569-a-member-named-at-made-the-python-module-unusable.md`.

### 26.568 `import` had no corpus schema, and three shapes were why

Moved to `doc/decision/26.568-import-had-no-corpus-schema-and-three-shapes-were-why.md`.

### 26.567 A test named `every declaration keyword` named 22 of 23

Moved to `doc/decision/26.567-a-test-named-every-declaration-keyword-named-22-of-23.md`.

### 26.566 The same sweep, three backends it had not read

Moved to `doc/decision/26.566-the-same-sweep-three-backends-it-had-not-read.md`.

### 26.565 Five getters each claiming to be the only one

Moved to `doc/decision/26.565-five-getters-each-claiming-to-be-the-only-one.md`.

### 26.564 `validate` read the inside of a sealed region

Moved to `doc/decision/26.564-validate-read-the-inside-of-a-sealed-region.md`.

### 26.563 A bound the type guaranteed, and the corpus case that decided it

Moved to `doc/decision/26.563-a-bound-the-type-guaranteed-and-the-corpus-case-that-decided-it.md`.

### 26.562 A generated name the runtime already had, and a wrong fix

Moved to `doc/decision/26.562-a-generated-name-the-runtime-already-had-and-a-wrong-fix.md`.

### 26.561 `access=Sequential` names a class and reads like a constant

**Settled as 0060, and `raidcfgd` had pointed at the right axis.** Their
ask was *say in the generated header which access pattern costs what. The
map already says `access=Sequential`; it does not say "exponential in k".*
The exponential half was 26.555; what was left turned out to be a real
understatement rather than a wording preference.

**Measured, C at `-O2`, a counted run of four-byte elements, calling
`_at(i)` for every `i` against a cursor the consumer holds:**

    n       _at(i) for every i      a cursor      ratio
    16            0.0022 ms        0.0001 ms        18
    64            0.0256 ms        0.0003 ms        73
    256           0.4017 ms        0.0014 ms       288
    1024          6.5589 ms        0.0056 ms      1177

**6.56 ms to read a four-kilobyte message.** `_at(index)` walks from the
start every time, so reaching element N is O(N) and a loop over every
element is O(N^2). The axis says *cannot reach element N directly*, which
is true, and a reader takes it for a constant factor.

**The member chain is a DIFFERENT axis with the same shape, and separating
them is half the answer.** Reaching member k past variable-length members
is `offset=Dynamic`, not `access=Sequential`. Measured on a struct of k
`str` members, every member once:

    k      every member via _offset    one pass via _extent_from    ratio
    8             0.00036 ms                  0.00005 ms              7
    64            0.02415 ms                  0.00037 ms             65

Identical totals at every k, which is how the two routes were shown to
agree rather than merely to differ in speed. So the chain has a public
one-pass route and a run has none -- which is why `raidcfgd` found the
chain's cost and wrote the run's cursor by hand.

**What the map can say and what it cannot.** `Sequential` is a property of
the FORMAT: element N is not directly reachable. That a loop over all N is
quadratic is a property of the loop the CONSUMER writes, and a capability
vector does not describe the caller. So the number goes in the generated
header beside the accessor that has the property, and the class stays in
the map. A walked element accessor now carries `O(index)`, the `O(n^2)`
consequence, and the cursor to write instead, in all four backends; an
indexed one says nothing, because for a fixed-size element the sentence is
noise.

**Two things recorded for the holder rather than taken, both with their
cost measured.** `offset=Dynamic(k)` would put the chain's length in the
map, which is where 18.1 promises a performance change shows up as a
reviewable diff -- today inserting a variable member lengthens every later
chain with no diff at all. It is about 120 committed map lines, a
numeric-strength rule whose direction is inverted from `Aligned(n)`, and
the normative table in two places. And a run wants an element CURSOR, which
is a new public API in four backends rather than a correction.

**And no capability says whether the generated code achieves the bound,
which is the conclusion this whole question invites.** 26.555's 3^k chain
and today's linear one carry the same vector on every axis. That half is a
gate -- `test_offset_chains_are_linear.py` -- and 0060 says so in as many
words, because a reader who takes a green map for a fast decoder has been
misled by a document that was accurate.

**The controls cost two rounds and the second one is the lesson.** Each
backend's mark sabotaged on its own fails naming that backend; the indexed
case's guard sabotaged makes the note unconditional and fails the vacuity
test. Rust passed both times until the sabotage was looked at: its emitter
carried `O(n^2)` twice, once in a Python comment explaining the note and
once in the emitted line, and `replace(..., 1)` hit the comment. **The
count said the edit landed and the edit established nothing.** The comment
is reworded so the literal mark appears once, which removes the ambiguity
for the next instrument rather than for this one.

**And the note itself failed a gate, which is worth recording because the
change is documentation and the gate is a documentation tool.** The C++
wording named the element class as `::situ::item(raw).extent()`, and
Doxygen reads a backticked qualified call as an explicit link request:
*explicit link request to 'situ::adv_report(raw)' could not be resolved*,
on `ble`, `mqtt` and `edges`. The bare `::situ::item` is an automatic link
with the same problem, so the class is not named at all now -- a reader
needs `extent()` and the accessor's signature already says what it hands
back. **A prose change has a compiler too**, and in this tree it runs over
every schema in the corpus.

**Nothing else moves**: 42 committed maps and 42 wires regenerate
byte-identical, and the four generated outputs still compile -- C and C++
under `-Wall -Wextra -Werror`, Rust under `-D warnings`, Python imported.
The first attempt at that check captured `2>&1` and reported 21 maps
differing; the difference was a `situc map` warning on stderr, in the
instrument rather than the tree.

### 26.560 Six branches stated the rule and two did not

Moved to `doc/decision/26.560-six-branches-stated-the-rule-and-two-did-not.md`.

### 26.559 `import` resolved names and nothing downstream was told

Moved to `doc/decision/26.559-import-resolved-names-and-nothing-downstream-was-told.md`.

### 26.558 A size bound wider than the type carrying it, in six places

Moved to `doc/decision/26.558-a-size-bound-wider-than-the-type-carrying-it-in-six-places.md`.

### 26.557 A counted run stopped at the buffer and called it complete

Moved to `doc/decision/26.557-a-counted-run-stopped-at-the-buffer-and-called-it-complete.md`.

### 26.556 `required` added a length and wrapped to a smaller answer

Moved to `doc/decision/26.556-required-added-a-length-and-wrapped-to-a-smaller-answer.md`.

### 26.555 An offset chain that re-derived itself, in all four backends

Moved to `doc/decision/26.555-an-offset-chain-that-re-derived-itself-in-all-four-backends.md`.

### 26.554 Three of the four installed programs could not start

Moved to `doc/decision/26.554-three-of-the-four-installed-programs-could-not-start.md`.

### 26.553 Half the reserved policy reached the wire signature

Moved to `doc/decision/26.553-half-the-reserved-policy-reached-the-wire-signature.md`.

### 26.552 The dump prints what it was not taught, and cannot stop

Moved to `doc/decision/26.552-the-dump-prints-what-it-was-not-taught-and-cannot-stop.md`.

### 26.551 The lens swept out: four clean, and my own witness was the wrong one

Moved to `doc/decision/26.551-the-lens-swept-out-four-clean-and-my-own-witness-was-the-wrong-one.md`.

### 26.550 One of the Tlv branch's eight, and seven that needed nothing

Moved to `doc/decision/26.550-one-of-the-tlv-branch-s-eight-and-seven-that-needed-nothing.md`.

### 26.549 Five ways a member changed meaning inside a namespace

Moved to `doc/decision/26.549-five-ways-a-member-changed-meaning-inside-a-namespace.md`.

### 26.548 A refusal that could not fire, and the lens that found it

Moved to `doc/decision/26.548-a-refusal-that-could-not-fire-and-the-lens-that-found-it.md`.

### 26.547 The last five cells, and the instrument's fifth fault

Moved to `doc/decision/26.547-the-last-five-cells-and-the-instrument-s-fifth-fault.md`.

### 26.546 The placement matrix, and an instrument wrong four ways

Moved to `doc/decision/26.546-the-placement-matrix-and-an-instrument-wrong-four-ways.md`.

### 26.545 A numeric bound belongs on a field, derived rather than chosen

Moved to `doc/decision/26.545-a-numeric-bound-belongs-on-a-field-derived-rather-than-chosen.md`.

### 26.544 The construct that could not be added, and the walk that enumerated three of thirteen

Moved to `doc/decision/26.544-the-construct-that-could-not-be-added-and-the-walk-that-enumerated-three-of-thirteen.md`.

### 26.543 The EBNF corpus checker, and nine gaps on its first run

Moved to `doc/decision/26.543-the-ebnf-corpus-checker-and-nine-gaps-on-its-first-run.md`.

### 26.542 Two productions the grammar never learned, and four witnesses that could not see them

Moved to `doc/decision/26.542-two-productions-the-grammar-never-learned-and-four-witnesses-that-could-not-see-them.md`.

### 26.541 The reading was settled; the documents were not

Moved to `doc/decision/26.541-the-reading-was-settled-the-documents-were-not.md`.

### 26.540 The one open item was built 25 days ago, over an empty set

Moved to `doc/decision/26.540-the-one-open-item-was-built-25-days-ago-over-an-empty-set.md`.

### 26.539 A cap on a `remaining` run, and the unit nobody asked about

**`u8 content[remaining] max 470` (0059), built.** `fuzznet` reported the
gap from a record body whose real bound lives in the ENCLOSING record, so
a body-only schema has no field to size it from and could only say
`[remaining]`, which reads Unbounded.

They asked for `[max = 470]`, and situc refused it correctly -- `[min]`,
`[max]` and `[must_eq]` are claims about a VALUE that `validate`
compares, an array has no single value, and 14.5's rule is what keeps one
attribute from meaning two things. **The refusal message already named
the spelling the language uses**, a size cap being syntax after the run
form as in `until D max N`, so the language did not need a new idea. It
needed the clause it was already pointing at.

**The holder settled the half the record could not: a declared cap
REFUSES a longer frame rather than truncating the read.** Such a cap
restates a bound the enclosing format guarantees, so a frame exceeding it
is malformed rather than something to read part of. The cost is stated in
0059 rather than discovered later -- a schema that UNDER-states its cap
now rejects valid messages.

**The decision nobody asked about, and the one worth carrying: the cap
counts ELEMENTS, not bytes.** `u8 name[n]` counts elements and the cap
sits exactly where that count sits, so `max 470` is 470 elements;
`until D max N` counts BYTES because a scan counts bytes. **The two are
the same number for the `u8` run either form is usually written on**,
which is precisely why it had to be settled deliberately: every fixture
anyone would reach for agrees, and the disagreement waits for the first
`u16` run. A question whose test cases all agree is not a question
anybody discovers by testing.

Thirteen sites, listed in 0059. The shape is 26.144's checklist and it
held: the AST field, the parser clause, the layout extent, four backends,
the unparser (which drops a field it does not render -- what `Sealed.until`
paid for two entries ago), the image, and both walkers. `traverse.frame_cap`
is the shared predicate, so the four backends ask one question rather than
four -- which is this section's most expensive recurring defect answered
before it could happen rather than after.

**`layout` refuses a cap on a run something already bounds**, which is
14.5 applied to a clause rather than to an attribute: a counted run is
bounded by its count and a delimited one by `until D max N`, so a third
spelling would state what nothing reads.

**Nine sabotages, and two of them were wrong before they were right.**

The first pass over the four backends confirmed nothing for two of them:
the marker it grepped for -- `frame_cap(struct) is not None` -- is absent
by construction in the C++ and Python emitters, which spell it
`capped = frame_cap(struct)` and then test `capped`. The sed matched
nothing, the file was untouched, and the test passed. **It was the
RESTORE check that caught it, not the landing check** -- the landing
check had the wrong polarity for a marker that was never there, and a
harness that only verified the sabotage applied would have reported two
green backends as controlled.

The C walker's first sabotage went red through the C compiler's
`-Werror` rather than through the verdict comparison, so the check under
test never ran. The second inverted the flag and went red on the table's
FIRST entry, every uncapped member now refusing -- right check, wrong
fixture, which proves the guard is reached and not that the new case
discriminates. **The third disables the guard only where it should
fire**, and fails on exactly the 6-byte message of exactly the new entry.
Three attempts for one control, each of them a green-looking result that
established something other than what it appeared to.

**The four backends are sabotaged one at a time on purpose.** A single
control over all four passes while three of them are wrong, which is what
this section has spent fourteen entries on.

Boundaries on both sides everywhere: 471 bytes accepted and 472 refused
in the four backends and the Python walker, 5 and 6 in the C-versus-Python
verdict table. A guard tested only on the bad side passes just as well
when it refuses everything.

**And the nine green tests could not see the defect the gate did.** The
Python walker's guard named its local `held`, which is already a local
further down the same function holding a list of `(check, against)` pairs.
Every test passed -- Python does not care -- and mypy narrowed the whole
scope to `Placement` and reported nine errors on lines this change never
touched. **A name is not a type error until something checks types**, and
the sabotages could not have found it either: the guard was correct, and
what was wrong was what the name did to the rest of the function. It is
`capped` now, which is what the C++ and Python emitters already call it.

### 26.538 `peek` in the C walker, and the comparison that never ran

Moved to `doc/decision/26.538-peek-in-the-c-walker-and-the-comparison-that-never-ran.md`.

### 26.537 The region's extent, back where the arithmetic is not a partition

Moved to `doc/decision/26.537-the-region-s-extent-back-where-the-arithmetic-is-not-a-partition.md`.

### 26.536 The register's field: built, measured, and not kept

**Answered NO, with the code written rather than reasoned about.**
26.144 asked whether a member should record the PLACEMENT its region
resolves to, computed per layout where the prefix is known, instead of
a bare name. It can be done. It buys too little to keep, and three red
gates is what established that.

**26.534's blocking claim was wrong and is corrected.** That entry said
the resolution cannot be qualified at all; it is right about
`tag_covers` and wrong in general. `absorb_nested` rewrites a nested
struct's paths into the parent during `place_members`, so a pass in
`_layout_of` sees final paths for every placement --
`outer_body.nested.body` already spelled from the parent. `tag_covers`
has no such moment, being set in `resolve_coverage`, which the parent
deliberately skips for a nested tag. **The obstacle was specific to one
field and was generalised without checking the other.**

**What sank it is that the two consumers ask different questions.**
`pack._region_owners` wants the region that OWNS some bytes, `coded`
included -- and a `coded` region does not stamp itself into `regions`,
by design, so no name records one. Wiring pack to the field put
`nest.body.inner.deep` under `nest.body` instead of the `coded inner`
that gates it. Two derivations that agree on most shapes are still two
questions, and consolidating them would have been this family's
fifteenth instance rather than its cure.

**And the surviving consumer needs a walk the field cannot shortcut.**
Three structs nested three deep, each with an `authenticated body`,
give a member `regions = ('body', 'body', 'body')`: it is inside all
three and all three tags stale when it is written. Resolving each name
against the containing scopes picks the innermost every time, so the
two outer tags vanish -- under-claiming, which this function's own
comment calls worse than the bug. Telling the cases apart is exactly
`reaches`' ancestor walk: `outer_body.nested` is NOT inside
`outer_body.body` while `top.held` IS inside `top.body`, and only
walking the steps between says which. A field filled BY that walk is a
cache, not a replacement, and nothing here is slow.

**Three red gates, and the corpus was silent for all three.** 1429
coverage rows and 1036 image rows stayed byte-identical at every stage,
because no corpus schema nests a `coded` region inside a `sealed` one
and none nests three same-named regions. Both cases live in unit
fixtures -- 26.525's and this file's own -- and both are what caught
it. **The lesson that this corpus is a weak detector for region shapes
is written three times this week, and its silence was still read as a
result.**

**Two traps worth keeping, whatever happens to the field.** A sabotage
that swapped `max(` for `min(` left the file the SAME SIZE, and Python
invalidates a `.pyc` on source size and mtime -- so the cached
bytecode was reused within the second and two consecutive readings of
one input disagreed. Four commands went on reasoning about a
contradiction that was a stale cache. And a surgical cut that removed
`by_path` and the old `reaches` also took the loop that BUILDS
`covering`, stripping coverage from 272 rows.

**What remains is the register item, answered.** The name stays
relative because inlining needs it to be, the resolution stays at the
point of use because the two uses differ, and the four resolvers are
four questions rather than one spelled four ways -- which 26.533's
sweep had already found to be true of the three it examined.

### 26.535 `sealed ... until`, and a trap predicted then hit anyway

Moved to `doc/decision/26.535-sealed-until-and-a-trap-predicted-then-hit-anyway.md`.

### 26.534 The root fix is not implementable, and the tree said so first

Moved to `doc/decision/26.534-the-root-fix-is-not-implementable-and-the-tree-said-so-first.md`.

### 26.533 Three lenses that found nothing, and what that cost to learn

Moved to `doc/decision/26.533-three-lenses-that-found-nothing-and-what-that-cost-to-learn.md`.

### 26.532 Two spellings that agreed, and a red gate read as green

Moved to `doc/decision/26.532-two-spellings-that-agreed-and-a-red-gate-read-as-green.md`.

### 26.531 A different rule, one place over: the own-member predicate

Moved to `doc/decision/26.531-a-different-rule-one-place-over-the-own-member-predicate.md`.

### 26.530 The thirteenth, pointing the other way, in the same module

Moved to `doc/decision/26.530-the-thirteenth-pointing-the-other-way-in-the-same-module.md`.

### 26.529 The twelfth, and this one turned a guard off

Moved to `doc/decision/26.529-the-twelfth-and-this-one-turned-a-guard-off.md`.

### 26.528 The gate found the eleventh instance while being built

Moved to `doc/decision/26.528-the-gate-found-the-eleventh-instance-while-being-built.md`.

### 26.527 The same bare name, and this one reached the wire

Moved to `doc/decision/26.527-the-same-bare-name-and-this-one-reached-the-wire.md`.

### 26.526 Both halves of one question, and a fixture that missed

Moved to `doc/decision/26.526-both-halves-of-one-question-and-a-fixture-that-missed.md`.

### 26.525 The region that opens no namespace, and the row it never had

Moved to `doc/decision/26.525-the-region-that-opens-no-namespace-and-the-row-it-never-had.md`.

### 26.524 Structure, not spelling -- and two more faults under it

Moved to `doc/decision/26.524-structure-not-spelling-and-two-more-faults-under-it.md`.

### 26.523 Two region lookups key on a bare name, and both are wrong

Moved to `doc/decision/26.523-two-region-lookups-key-on-a-bare-name-and-both-are-wrong.md`.

### 26.522 The literal rule emptied the axis, and the exemption existed

Moved to `doc/decision/26.522-the-literal-rule-emptied-the-axis-and-the-exemption-existed.md`.

### 26.521 A version field written into every file and read by nothing

Moved to `doc/decision/26.521-a-version-field-written-into-every-file-and-read-by-nothing.md`.

### 26.520 The third layer of one fact, and the fixture that showed it

Moved to `doc/decision/26.520-the-third-layer-of-one-fact-and-the-fixture-that-showed-it.md`.

### 26.519 The same key, one dict over, and a repair worse than the bug

Moved to `doc/decision/26.519-the-same-key-one-dict-over-and-a-repair-worse-than-the-bug.md`.

### 26.518 The corpus schema hit Lua's ceiling, which nothing had measured

Moved to `doc/decision/26.518-the-corpus-schema-hit-lua-s-ceiling-which-nothing-had-measured.md`.

### 26.517 The third copy of one question, and the order that made it safe

Moved to `doc/decision/26.517-the-third-copy-of-one-question-and-the-order-that-made-it-safe.md`.

### 26.516 The rule, broken three times by the session that wrote it

Moved to `doc/decision/26.516-the-rule-broken-three-times-by-the-session-that-wrote-it.md`.

### 26.515 The corpus schema found a second defect on its way in

Moved to `doc/decision/26.515-the-corpus-schema-found-a-second-defect-on-its-way-in.md`.

### 26.514 The same argument, one list over, three months apart

Moved to `doc/decision/26.514-the-same-argument-one-list-over-three-months-apart.md`.

### 26.513 Thirty-one candidates, seven claims, three that had moved

Moved to `doc/decision/26.513-thirty-one-candidates-seven-claims-three-that-had-moved.md`.

### 26.512 The register named its own method, and it was 23 days old

Moved to `doc/decision/26.512-the-register-named-its-own-method-and-it-was-23-days-old.md`.

### 26.511 The readers took an arm all along; nothing could name one

Moved to `doc/decision/26.511-the-readers-took-an-arm-all-along-nothing-could-name-one.md`.

### 26.510 The reason was a guess, and it read as a decision

Moved to `doc/decision/26.510-the-reason-was-a-guess-and-it-read-as-a-decision.md`.

### 26.509 An intersection is a filter with two directions, and one was argued for

Moved to `doc/decision/26.509-an-intersection-is-a-filter-with-two-directions-and-one-was-argued-for.md`.

### 26.508 One case name, three structs, and two explanations that failed

Moved to `doc/decision/26.508-one-case-name-three-structs-and-two-explanations-that-failed.md`.

### 26.507 Four of six closures sat within three entries of the claim

Moved to `doc/decision/26.507-four-of-six-closures-sat-within-three-entries-of-the-claim.md`.

### 26.506 The three ways to refuse it, and the one where it was already legal

Moved to `doc/decision/26.506-the-three-ways-to-refuse-it-and-the-one-where-it-was-already-legal.md`.

### 26.505 Six open items swept, five of them already closed

Moved to `doc/decision/26.505-six-open-items-swept-five-of-them-already-closed.md`.

### 26.504 A hand-written vector would have agreed with the bug

**`example/dns` described RFC 1035 exactly and refused traffic every
current resolver emits.** `reserved u3 [must_be_zero]` is that RFC's Z
field, three bits reserved for future use. The future arrived: RFC 2535
took two of them and RFC 4035 section 4.9.3 settled them as AD and CD, and
`dig` sets AD by default. So BIND's flags are 0x0120, those three bits
carry 010, and `situc verify` answered

    ConstraintError: dns_header.<reserved0> is reserved [must_be_zero]

correctly, about a message nothing is wrong with.

**The whole of 23's independent-source rule is in how this was found.** A
vector written by hand would have been written from RFC 1035 -- the same
document the schema was written from -- and it would have set the Z field
to zero and passed. The bug is not reachable from the specification at
all: it is only reachable from bytes, because only an implementation knows
which year it is. **Two readings of one document are one witness however
carefully either is made**, and that is the argument for the rule stated
as a case rather than as a principle.

**What changed is scope, not the argument.** 14.5 rejects reserved bits
because an ignored bit is a malleability surface and DNS is a known covert
channel, and that reasoning is untouched -- it applies to a bit nobody has
assigned a meaning to, which is one here rather than three. AD and CD are
fields a receiver READS, so refusing them was never malleability control;
it was refusing the protocol. `require canonical(dns_header)` survives for
the same reason: a named bit carrying its own value is not a second
spelling of anything.

**Nothing moved.** Every member keeps its offset, the packed byte is still
eight bits, `require size(dns_header) == 12` still holds, and the wire
signature records three lines at the same bit positions where there was
one. It is a WIDENING -- the schema accepts what it used to refuse and
refuses nothing it used to accept -- which is the rare direction for a
contract change and the reason this one is cheap.

**And the corpus came from a permission this machine does not grant.**
Packet capture is refused here, which had been recorded as "no independent
source for dns" and was wrong: a UDP relay between `dig` and the resolver
on 127.0.0.1 records both datagrams in userspace, and `localhost` is
answered without forwarding so nothing goes on the network. The same shape
had already worked for `example/dtls` against two OpenSSL processes.
**A missing PERMISSION is not a missing SOURCE**, and the difference is
one relay.

Worth keeping because the wrong classification was made confidently and
twice: nine schemas were sorted as unclosable, the list was reported, and
the list was repeated unchanged when the question came round again.
`evidence.md` names that -- a frame that has just been right is the hardest
one to drop -- and what broke it was asking what the DTLS entry had
actually done rather than re-reading the classification.

**The reply carried the other half.** Its answer section names its owner
`C0 0C`, a pointer whose low fourteen bits are 12, which is where the
question's name starts in that message. So `example/dnsname` holds a real
resolver's compressed name beside the same name written out: both
spellings of one name, from one exchange, which is exactly what the
`non_canonical` attribute on that run asserts and what nothing in the tree
had demonstrated.

### 26.503 A green suite is not a green gate, and a skip is not a gap

Two findings about the gates rather than the code, from a session that ran
the suite five times and `make check` never.

**`make test` is `test-py test-c`. `make check` is `style typecheck lint
test cross-test`.** So a session that runs the suite has run two of five,
and the three it skipped are the ones that answer different questions:
`typecheck` reads the source mypy-strict, `lint` and `style` read its
shape, and `cross-test` runs the generated accessors on aarch64 under
emulation. 24.0 already says `check` is "what has to pass before a
commit"; what this records is what the other four commits cost.

**A dead branch shipped, in four commits, behind a green suite.**
`_element_guard` was written once and copied into all four backends, and
its `count is None` arm cannot be taken in C, whose `_count_expression`
returns `str` where the other three return `str | None`. mypy strict said
`Statement is unreachable`; the suite said nothing, because there is
nothing to say -- a branch that cannot be taken breaks no test. It was
fixed in `81695c6`, and the emitted code is byte-identical either way,
which is precisely why no behavioural gate could have found it.

**The shape: a type checker and a test suite fail on different axes**, so
one standing in for the other is a population error rather than a
shortcut. The floor gates of 22 already record this from the other side --
`sys.monitoring` is syntactically valid, so only the type checker could
refuse it -- and this is the same division met while writing ordinary
code. Run `check`, not `test`.

**And a skip is not a gap, which took getting wrong to notice.** Reading
the suite's 64 skips by reason, three stood out as checks that inspect
nothing: `test_future_examples_*`, parametrised over examples marked
`// STATUS: needs phase N.`, of which there are none. They were reported
as inert.

They are not. `test_every_example_builds` asserts
`FUTURE == []` and `len(CURRENT) >= 12` -- the partition, not the cell --
and its docstring names what pins the phase-gating machinery while the
group is empty. That is `evidence.md`'s remedy for an empty population,
already in place, and an empty parametrisation sitting beside an assertion
that the population IS empty is covered rather than silent.

**What made the misreading easy is that the skip line looks identical
either way.** `got empty parameter set ['path']` is pytest's, not this
tree's, so it carries no reason and cannot say whether the emptiness is
asserted somewhere. The question to ask of an empty parametrisation is
therefore not "what does this test cover" but **"what asserts that this
population is empty"** -- and if nothing does, that is the finding rather
than the skip.

### 26.502 Nine corpora, and what committing them found

**`gen-tests` emits nothing for a schema with no committed vectors, and
twenty-seven schemas were in that state.** Eight are not, now, and the
rule that governed every one of them is 23's: a `<name>.vectors` beside a
schema is bytes SOME OTHER IMPLEMENTATION wrote. A corpus situ produced
would be the known-answer test `evidence.md` calls the same failure
wearing a lab coat.

| schema | who wrote the bytes |
|---|---|
| `png` | the first eight bytes of a real PNG on this machine |
| `json` | Python's `json`, for `true`, `false` and `null` |
| `mqtt` | paho-mqtt's own packet writer, through a stand-in socket |
| `modbus` | pymodbus 3.8.6, `pdu.encode()` and `FramerSocket` |
| `protobuf` | `protoc --encode=User` over the example's own `.proto` |
| `http` | `http.client` for the request, `http.server` for the reply |
| `smtp` | `smtplib.SMTP.putcmd` |
| `dtls` | OpenSSL 3.5.7, both ends of a relayed DTLS 1.2 session |
| `padded` | nobody -- see below |

**A corpus can be present and still not reach the thing under test.**
`json` had twenty-two vectors and skipped anyway, because every one is for
`value` and `situc build --owned` owns the three fixed-size literals
instead. The skip said "its constraints need committed vectors to
exercise" and the committed vectors were there, for a struct the test
cannot use. **Asking whether a corpus EXISTS is not asking whether it
covers what is being checked**, and only the driver's own `names` list
answers the second.

**A committed vector the decoder REFUSES produced the same skip as no
vector at all**, under a message naming the wrong remedy. That is the one
state that can go quiet after somebody has done this work: corrupt a byte
of `png.vectors` and the count moves from `refused=64` to `refused=1`,
which is a disagreement between a vector and a decoder rather than an
absence of either. It fails now, and the skip is kept for the case it was
written for. Without that, every closure recorded here could have rotted
back into a skip silently.

**Committing a corpus for a delimited schema found a defect in
`gen-tests`.** Its round trip reads every settable field and writes it
back, and it decided which those are by skipping reserved, nested,
located, covered and data-sized members. A run the DELIMITER sizes is none
of those -- `traverse.data_sized` asks whether the extent comes from an
expression over the data, and a scan is not one -- so `command.verb`
looked like a scalar and the harness called `situ_command_verb_get`, which
the header does not declare. It has `_ptr`, `_len`, `_span`, `_eq` and
`_which`.

It was invisible because **no schema with a delimited member had committed
vectors**, which is the shape worth keeping: a generator's blind spot can
be held open by the absence of the very corpus that would close it, and
adding the corpus is what makes the generator wrong out loud.
`test_generated_c_calls_only_accessors_the_headers_declare` caught it,
which is the test written for exactly this -- a build failure `make test`
finds three quarters of an hour later and the fast gate reads past.

**`padded` is the honest exception and its header says so.** It describes
no format anybody else implements, so there is no second implementation to
take bytes from; its vector is computed by hand from 8.4, which is a
second reading of the rule rather than a second implementation of it, and
the weaker evidence. It leans on the schema's own
`require size(aligned_header) == 8`, which fails the build before the
vector is reached. Saying which kind of evidence a corpus is costs a
paragraph and is the difference between a fixture and a claim.

**`smtp` carries only the commands that take an argument**, and that is the
schema's own documented boundary rather than a discovery: `command.verb`
scans `until " "`, and `DATA`, `QUIT` and `RSET` have no space. Running
`situc verify` over all five is the note beside `struct command` behaving
as written -- three conform, two are refused with *command.verb has no
b' ': the frame stops first*. The two were left out rather than wished in.

**`dtls` needed a way round a missing permission.** Packet capture is not
allowed here, so the bytes were taken in userspace: a UDP relay between
`openssl s_client` and `openssl s_server`, cipher forced to
AES128-GCM-SHA256 so the AEAD is the one the schema names. The record
closes exactly -- `length` 0x2B, thirteen of header, eight of explicit
nonce, `length - 24` of ciphertext, sixteen of tag, 56 bytes. Its interior
is not read and cannot be, `require verify_gated(record.sealed)` being the
whole point.

**And recording it left an orphan, which is this workspace's own rule
failing in the direction it warns about.** The server ran under `timeout`
and was killed THROUGH it, so the SIGKILL landed on the wrapper and
openssl was reparented to init, still holding its port. `running-code.md`
records that shape for containers; a `timeout` around any process that
outlives the signal is the same thing. It was found by the sweep that
section asks for afterwards and killed by name, `/proc/<pid>/cmdline`
re-read before signalling.

**What is left is nineteen, and none of it is a gate gone quiet.** Checked
per schema rather than counted: three have no struct to carry bytes at all
(`std/codecs.situ` and `std/kernels.situ` declare none, and a `register`
is a bus transaction); six are situ's own inventions, where no other
implementation exists by construction -- `telemetry`'s first line says
"designed rather than described"; one is `image`, whose agreement with the
packer `test_pack.py` already holds more precisely than a corpus would;
and nine are real formats with no independent source ON THIS MACHINE --
no DNS library, no capture permission, no stored `.pcap`, no Lisp, and
`pickle` is Chromium's rather than Python's. Those nine close the day a
library or a capture appears, and nothing about them is wrong.

**Measured 2026-09-24 and 2026-09-25**, by `pytest test/unit -q -rs`
grouped by reason: the suite went from 75 skips to 64 across four runs,
`commits no golden vectors` from 27 to 19, and the three
`no draw validated` rows to none. Each run was predicted before it was
started and matched after it, which is the only reason the counts are
quoted at all -- rule 7's objection is to a number nobody re-derives, and
the command that re-derives this one is in this sentence.

### 26.501 Two mechanisms for one problem, composing into a third

Moved to `doc/decision/26.501-two-mechanisms-for-one-problem-composing-into-a-third.md`.

### 26.500 A bound may name an element, and the walkers abstain rather than guess

Moved to `doc/decision/26.500-a-bound-may-name-an-element-and-the-walkers-abstain-rather-than-guess.md`.

### 26.499 C++ is the only backend a local can capture a member in, and a bound found it

Moved to `doc/decision/26.499-c-is-the-only-backend-a-local-can-capture-a-member-in-and-a-bound-found-it.md`.

### 26.498 The log is 85% of the document, and the first measure of it was 30% high

Moved to `doc/decision/26.498-the-log-is-85-of-the-document-and-the-first-measure-of-it-was-30-high.md`.

### 26.497 A subscript is invisible to the walker every gate is built on

Moved to `doc/decision/26.497-a-subscript-is-invisible-to-the-walker-every-gate-is-built-on.md`.

### 26.496 Rust renders Reed-Solomon, and the blocker had already gone

**Rust generated 40 of the 42 derived codecs in `std/kernels.situ`; it
generates all 42 now.** The two it declined were the Reed-Solomon pair.
C and Python already generated both.

**Nothing about Reed-Solomon was ported.** The encoder and decoder are
one statement tree in `codegen/kernel_program.py`; what was missing was
a renderer. `_RustRenderer` is 165 lines against C's 160 and Python's
88, and holds no more of the algorithm than either -- which is the claim
26.462 made for the arrangement, now tested by a third backend rather
than argued.

**The recorded blocker did not survive measurement, and the reason it
went is this session's own earlier fix.** 26.457 and this project's
notes both carried "`no_std` and no-panic indexing" as the obstacle,
sized at seven unprovable subscripts. That record predates 9319ca9,
which moved Berlekamp-Massey's `degree > half` guard from after the loop
to inside it. With the guard where it belongs the invariant holds, and
asserting `degree <= at` **inside the Rust** never fired across 20,000
decodes -- with a sabotage of the degree update firing it immediately,
so the assertion was alive.

**Plain `[i]` everywhere, and `.get(i).copied().unwrap_or(0)` is the
wrong instrument for four reasons rather than one.** `kernel_program`
says every index is provably in range and a renderer may rely on it. The
fallback never sees a bad index anyway -- the underflow is in the
SUBTRACTION, which evaluates first, so a sabotaged build panics on
"attempt to subtract with overflow" in debug and on the index in
release. Eight of the 31 index sites are assignment TARGETS, and a
sabotage substituting the fallback fails to compile at all:
`error[E0070]: invalid left-hand side of assignment`. And substituting
zero in an error-correcting decoder is a MISCORRECTION, which this
program ranks as worse than a refusal.

**Three things Rust needed that neither sibling did.** `mut` is earned
-- `unused_mut` is a hard error under `-D warnings`, five of nineteen
declarations must not carry it, and the set is walked out of the tree
rather than listed, because a list is a thing to be wrong.
Parenthesisation follows Python's split and not C's: C brackets every
`Binary` on the grounds that its precedence table is not worth
checking, and doing that here produced **22 hard `unused_parens`**. And
`position` is `[usize; half]` rather than `[u8; half]`, because it holds
indices; that removes three casts and one mixed-type subtraction Rust
would reject outright where C promotes silently.

**One behavioural divergence, decided rather than defaulted.** `parity`
is the caller's slice. C writes through a bare pointer and a short
buffer overflows silently; Rust's `&mut [u8]` panics, which aborts in
`no_std`. The generated code now guards it and returns 0 -- which
already means *the caller passed something wrong* in this program --
rather than inventing a second code or letting the panic stand.

**Verified against a witness that is not situ.** 3,120 encode/decode
cases across both codecs and corruption from 0 to t+2, one PRNG and one
draw order in both languages: **byte-identical to the generated Python,
`diff` empty**, run at `-Os` with `-C overflow-checks=yes`, which never
fired. Then 300 parity blocks against `reedsolo` at `prim=0x11D`: all
300 agree. The first link proves the renderer; only the second proves
the code, and 26.462 is explicit about why -- two backends rendering one
program agree by construction.

**The gate that would have caught the 22 parenthesis errors did not
exist, and now does.** Every test compiling DERIVED Rust used
`-A warnings`; every test using `-D warnings` compiled ACCESSOR Rust.
Disjoint sets -- while `test_codegen_rust.py` says in as many words that
the flag matters *"because a great deal of CI does and generated code
that only compiles without it is generated code that fails for the
user"*. Measured before writing it: the module was already clean, so the
gate cost nothing on the day and exists for the next renderer. Sabotaged
by making every binding `mut`, it goes red through `unused_mut`.

**Two coverage sentences are now wrong and I cannot reconcile the
older one.** 26.457's *"Coverage is 40 of 42 and stops there
deliberately"* is falsified outright. 26.462's *"Coverage is 41 of 42"*
does not match anything measurable today -- C, Rust and Python each
generate 42 of the 42 kernel-bearing codecs, and before this change
Rust generated 40 while the other two generated 42, so no backend was
ever at 41. Rather than guess what the figure counted, the measurement
is recorded and the sentence left for whoever wrote it: **the method is
`_for_kernel(decl, "situ") is not None` per backend over
`std/kernels.situ`'s codecs**, which is the emitters' own question.

### 26.495 Keyword collisions are handled; the lens that found 26.490 is not

**An empty sweep, recorded because it says where the next fault is
not.** A field named after a target language's own keyword survives in
all four backends, checked by compiling rather than by reading:

    C++     class int new delete template operator   g++ -fsyntax-only rc=0
    Rust    match fn impl type move ref unsafe       rustc --emit=metadata rc=0
    Python  class def lambda None import global pass import succeeds

Each backend has its own answer and each is deliberate. C and C++ share
`CPP_KEYWORDS` and append an underscore. Rust emits raw identifiers --
*"the whole reason they exist"*. Python asks `keyword.iskeyword` rather
than restating a list, *"because the interpreter that will read this file
is the authority on what it can parse"*, and leaves SOFT keywords alone
on purpose, since `match` and `type` parse fine as names and mangling
them would rename a member for nothing.

**So the interesting question is why 26.490 exists at all beside that.**
Both are name collisions in generated code and only one is handled,
because they have different sources. A keyword is a name the LANGUAGE
reserves -- knowable in advance, listable, and every backend lists it. The
collision in 26.490 is between a generated LOCAL and the getter the same
expression calls: `const std::uint32_t n = ... leaf_u(n()) + 1`. That name
comes from the EMITTER, and no list of the language's words can contain
it, because the language never reserved `n`.

**Which is why the fix shapes differ too.** A keyword collision is fixed
by a table. `26.490`'s is fixed the way `cpp/names.py check_collisions`
already fixes a class name a member has taken: detect and rename. A
lookup cannot solve a collision whose other half is chosen by the
generator.

**Three harness failures, all mine, before this sweep said anything.**
`mod match;` in my own `lib.rs` is invalid Rust, so the first run blamed
the generated file for an error on line 2 of a file I wrote. The second
failed on `rustc` having nowhere to put a temp dir. The third needed
`runtime/python` on `PYTHONPATH`. **Every one exited non-zero with a
plausible-looking message**, and the only thing that separated them from
findings was reading where the error pointed. That is the third time in
this arc that a harness error nearly became a defect report, and the
remedy has not changed: read the error, not the status.

### 26.494 A ranking that had to be total, and was not

**26.492's crash is fixed and its open question is untouched, which is
the whole shape of the change.** `_weakest_seekable` ranked three of
`Seekable`'s four members and is used as a total function, so composing
a `blockwise` stage reached `order.index` and raised
`ValueError: tuple.index(x): x not in tuple` -- an uncaught Python
traceback out of `situc map`, no span, no diagnostic. It now says:

    error: `both` composes `inter`, whose `seekable = blockwise` has no
           place in the ordering
      = `seekable` composes to the weakest stage, and where `blockwise`
        sits between `permuted` and `none` is not settled

**Refused rather than ranked, and that is the point rather than
timidity.** Where `blockwise` belongs between `permuted` and `none` is a
question about what the value MEANS, and nothing in the compiler reads
it -- `Seekable.BLOCKWISE` has no reference outside its own definition.
Giving it a rank here would settle an open language question silently,
inside a helper, in the one place nobody would look for the decision. A
refusal naming the stage removes the crash and leaves the question where
it belongs.

**Scoped to a pipeline, deliberately.** A standalone
`seekable = blockwise` is accepted today and stays accepted: the crash
is composition's and so is the ordering question, and widening the
refusal to the VALUE would decide the same thing from the other side
while refusing schemas that compile now. There is a test for each half,
and the standalone one is the one that would catch an over-broad fix.

**The control is the test that passes in both states.** Same schema, one
word changed -- `permuted` for `blockwise` -- composing to `PERMUTED`
before and after. A refusal keyed on something broader would take it too
and the defect test would still be green.

**`not_yet_implemented` was the tempting helper and the wrong one.** It
exists for exactly this shape and requires a phase number, which
`project.md` does not give for this value; supplying one would have been
an invented fact in a diagnostic a user reads. `error()` says what is
true without claiming a schedule.

### 26.491 A line code that declares itself authenticated, and is believed

**`authenticated` is not in `DERIVED_PROPERTIES`, so no kernel can
contradict it.** Every other declarable property on a kernel-bearing
codec is computed and a disagreement is refused. This one is trusted --
and a codec WITH a kernel is exactly the case where trust is not the
model.

    codec manch {
        authenticated;
        kernel = table(input_bits = 1, output_bits = 2,
                       code = manchester_802_3);
    }
    struct msg { u8 kind; u8 nonce[12]; tag u8[16];
                 sealed(manch, nonce = nonce) { u32 secret_seq; } }

`situc map` rc=0:

    codec manch unbound ... granularity=bit(1) authenticated invertible
    struct msg ... stage=VerifyGated auth=Covered(tag)

and `situc build` rc=0, emitting
`situ_msg_sealed_open(situ_view_t view, int verified, ...)` -- a real
verify gate. **A Manchester line code has no key, no MAC and nothing in
its output but the code**, and the lattice carries `VerifyGated` out into
generated C on its word.

**The gate that should stop it is `wellformed.py:3542`,
`if not codec.authenticated`.** Its sibling at `:3555` -- a derived impl
may not seal -- does stop it, but only when an `impl` is declared. With
none, `impl is None` and the check is skipped, and `std/codecs.situ:16`
calls a signature with no binding *"the normal case for a protocol under
design"*.

**Amendment: the consequence is narrower than this entry first put it,
and one third of the claim is withdrawn.**

**The generated code is IDENTICAL.** `situ_msg_sealed_open` for the
Manchester codec and for a real extern AEAD are byte-for-byte the same
function -- 187 bytes each, `diff` empty:

    static inline situ_err_t situ_msg_sealed_open(situ_view_t view,
                                                  int verified, ...)
    {
        if (!verified) { return SITU_ERR_TAG; }
        out->view = view;
        return SITU_OK;
    }

**`verified` is the caller's parameter and situ reads no tag.** It
performs no cryptography for either codec. So what the generated code
guarantees is a reachability property of the API surface -- no interior
accessor without a gate object, and the only producer refuses on zero --
and that is true and unchanged.

**What the hole costs is the one refusal that protects what `verified`
MEANS.** `wellformed.py:3532` says it in its own words: *"the caller
passes `verified`, nothing checked anything, and the type system carries
a promise the cryptography never made."* A kernel codec walks past
exactly that. The map then prints `stage=VerifyGated auth=Covered(tag)`
over a line code and the generated prose says parsing before
authenticating *"does not compile"*. **Nobody forges anything; an
assertion is made that nothing is positioned to keep.**

**And `tag_bytes`/`nonce_bytes` are withdrawn from this finding.** They
are outside `DERIVED_PROPERTIES`, and **no kernel could contradict them
anyway**: none of the seven families is a MAC or an AEAD, so `Derived`
has no left-hand side for a tag width. They also reach no generated code
-- three schemas differing only in those declarations produce
byte-identical `.c` and headers differing only in the include guard --
and the cross-check that matters, declared width against the `tag u8[N]`
field, fires in all three directions.

**That asymmetry is what makes `authenticated` the real one.** All seven
families imply **not** authenticated, so *"this kernel implies `not
authenticated`"* is a sentence `Derived` could carry and does not. A tag
width is a gap with nothing behind it; this is a gap with a fact behind
it that nobody wired up.

**Two smaller things came with it.** `_authenticated_note`
(`c/codectests.py:541`) emits a comment claiming *"`tag_bytes` is checked
against that field by the compiler"* for a codec that declares none and
is not length-preserving -- two false clauses, in a note whose stated job
is that *"silence would read as coverage"*. And with no `impl` at all,
`gen-codec-tests` emits `no suite` and not even that note, so the claim
passes the falsification harness in silence.

**Nothing in the tree does this**: 42 schemas, 54 kernel-bearing codecs,
6 `authenticated` codecs, intersection empty. And `grep -rn
DERIVED_PROPERTIES test/` returns nothing, so no gate asserts the tuple
covers what it should -- which is why an omission is invisible.

### 26.492 `seekable = blockwise` does not merely misprice, it crashes

Moved to `doc/decision/26.492-seekable-blockwise-does-not-merely-misprice-it-crashes.md`.

### 26.493 Four axes blind at the default, and the base rates behind them

**26.489 recorded `deterministic` as the vacuous axis. It is four**, and
`length_preserving` is the one that matters most: it is a load-bearing
claim about offsets, and it is silently discarded over a CRC kernel that
adds 32 bits.

    not deterministic  + table       rc=0   declaration dropped
    not seekable       + table       rc=0   dropped, kernel's LINEAR used
    seekable = none    + table       rc=0   dropped
    granularity = stream + table     rc=0   dropped, kernel's bit(1) used
    length_preserving  + polynomial  rc=0   dropped, kernel's +4 used

Against seven live controls on the same axes, all rc=1. And the
declaration IS recorded -- `not seekable; seekable = linear;` is refused
with *"seekable is given twice"* -- so it is the check that cannot see
it, not the parser discarding it.

**The base rates are the part worth keeping.** 379 `raise error(` sites;
of the 154 in `wellformed.py` all were read and 42 executed against
purpose-written schemas -- **40 alive, 2 dead**. Ten codec properties,
all ten reproduced: **six alive, four vacuous at the default**. And the
enum axis: 27 classes, 145 members, 23 flagged by a reference sweep and
**all 23 false positives** once parameterised construction
(`{x.value: x for x in Enum}`) and `.value` consumers were accounted for.

**That 23-of-23 is the instrument lesson.** A sweep counting
`EnumClass.MEMBER` references cannot see a member built from schema text
by a table keyed on `.value`, nor one compared as a string after
`layout.py` stores `member.unknown.value`. Two corrections, each killing
a batch, before anything survived -- and what survived did so on a
reproduced crash rather than on the sweep's say-so.

**One control handed to this sweep did not hold up, and the correction is
recorded rather than buried.** `c/emit.py:6791`'s assert message misstates
its guarantee -- `_has_length` and `_struct_extent` are different
predicates -- but 1,102 reaches across the corpus and a purpose-built
fixture never made it fail. It is **undecided, leaning alive**, not the
confirmed-bad control it was offered as. 26.488's claim stands
separately: that assert fires once `_fits_check` declines, which is a
state the tree does not reach today.

### 26.490 Five ordinary field names produce C++ that does not compile

Moved to `doc/decision/26.490-five-ordinary-field-names-produce-c-that-does-not-compile.md`.

### 26.489 A cross-check that cannot fire on the axis it matters most for

Moved to `doc/decision/26.489-a-cross-check-that-cannot-fire-on-the-axis-it-matters-most-for.md`.

### 26.488 The crash is not C's, and the narrow fix unmasks a worse one

Moved to `doc/decision/26.488-the-crash-is-not-c-s-and-the-narrow-fix-unmasks-a-worse-one.md`.

### 26.487 An image that claimed it could measure what it had not encoded

Moved to `doc/decision/26.487-an-image-that-claimed-it-could-measure-what-it-had-not-encoded.md`.

### 26.486 `at += None;` -- generated C++ that does not compile

Moved to `doc/decision/26.486-at-none-generated-c-that-does-not-compile.md`.

### 26.485 A Python object, shipped in three languages out of four

Moved to `doc/decision/26.485-a-python-object-shipped-in-three-languages-out-of-four.md`.

### 26.484 An enum member inside a size expression, four ways wrong

Moved to `doc/decision/26.484-an-enum-member-inside-a-size-expression-four-ways-wrong.md`.

### 26.483 The contract recorded a spelling, in both directions at once

Moved to `doc/decision/26.483-the-contract-recorded-a-spelling-in-both-directions-at-once.md`.

### 26.482 The encoder's worst case, inverted into a decode buffer

Moved to `doc/decision/26.482-the-encoder-s-worst-case-inverted-into-a-decode-buffer.md`.

### 26.481 Two ordinals a later insertion falsified, and one overclaim

**Three comment claims in `std/kernels.situ` were wrong; two of them were
wrong by arithmetic nobody performed and one by a universal nobody
tested.** Every CRC comment in the file was checked by computing the value
from the declared parameters rather than by recalling a catalogue.

  - **`crc24_ble` called itself "the first entry here whose width is not a
    C word".** It is the fourth. In file order the widths that are not
    8, 16, 32 or 64 run 5, 7, 15, 24, 40.
  - **`crc40_gsm` called itself "the second".** It is the fifth.
  - **`crc32_bzip2` claimed "a different answer for every input" against
    reflected CRC-32.** They agree on the empty input, where both are
    zero: all-ones reverses to itself and `xorout` cancels it either way,
    so reflection has nothing to act on. Confirmed here, and no collision
    in any of the 256 one-byte inputs.

**Both ordinals were true on the day and neither line was ever edited.**
`ca4b9e3` (2026-08-02) wrote them when 24 and 40 genuinely were the only
two; `2cc3407` (2026-09-14) added `crc5_usb`, `crc7_mmc` and `crc15_can`
*above* them and falsified both without touching either. Nothing failed,
because nothing compares a comment to the file it sits in.

**So the fix is to delete the ordinals rather than correct them.** An
ordinal about the file's own shape is falsified by an insertion anywhere
above it, silently, by somebody who never read the line -- which is
exactly what happened, and correcting "first" to "fourth" only resets the
clock. What replaced them is the property that made the ordinal worth
stating: `crc24_ble`'s width is not a C word, `crc40_gsm`'s is the one
above thirty-two. Both survive an insertion.

**The universal beside them was kept, and the difference is the point.**
"Every other reflected CRC in this file starts all-ones or all-zeros" is
also a claim about the file's shape, and it stays -- checked, and true of
all eight others. A universal is falsified by a counter-example that
arrives *with* the thing that breaks it, so the next codec to violate it
makes the sentence wrong in front of whoever added it. An ordinal is
falsified by an insertion that has nothing to do with it. **The two
failure modes are not comparable, and only one of them has a reader.**

**What the verification could not reach is enumerated rather than
passed.** 23 claims in the file are not settleable by computing a CRC --
catalogue provenance, standards attributions, statements about generated
register widths, and the CCSDS parameters -- and they are listed in the
sweep rather than counted as clean. A separate note: the file's assertion
that CRC-5/USB is the catalogue's only `confirmed` entry of its three
appears verbatim in `test_differential_oracle.py` as well, so the two are
one witness, not two.

**One control worth copying.** The check routine was proved able to be
wrong before anything was believed: with reflection removed it returns
`0xFC891918` rather than `0xCBF43926`, so a run agreeing with the
catalogue is agreement rather than a constant. The sixteen values in
`CRC_CHECK_VALUES` were transcribed from the catalogue by hand and all
sixteen match computation from the declarations -- independent in the one
way that matters, since a mistyped parameter breaks the agreement. It is
not evidence that a declaration names the right catalogue entry.

### 26.480 An accepted record the tree overruled by building the thing

**0002 is `Status: accepted`, says "No `CMakeLists.txt` is written", and
declares that it supersedes section 24. There is a `CMakeLists.txt`, it is
126 lines, and `test/unit/test_cmake.py` exercises it against a real cmake
-- 3.31.6 on this machine, so the gate runs rather than skips.** Section 24
has meanwhile been rewritten to assert CMake as current reality and names
0002 nowhere.

**The order is the finding.** 0002 is dated 2026-07-26. `dd7d74f` added the
CMake entry point on 2026-07-31, five days later, and its message says why:

> "Both CMake and GNU Make, maintained in parallel, as separate and
> independently usable entry points" has been section 24's opening line for
> a long time, and `git log --all` had never seen a `CMakeLists.txt`.

That session read section 24, found a promise the tree did not keep, and
kept it. It had no reason to think section 24 was not authoritative,
because **nothing in section 24 said it had been superseded.** 0002
recorded the supersession in 0002. The text it superseded was left
unmarked, and the next reader to arrive followed it.

**So the defect is not that somebody ignored a record.** It is the shape
this document already knows from the other direction: a claim lives in more
than one place, and the correction has to go where a reader will next look
for it. A supersession written only into the superseding record is a
correction filed where nobody who needs it is reading.

**0002's own reconsideration trigger has fired, and not in the way it
imagined.** The record says to reconsider "when someone outside this
repository needs CMake". `suggestion/hydra.md` is such a project and says
so in its own words -- *Hydra's build is CMake plus Qt's own `moc`, with no
other code generation and no Python.* But what hydra asked for is not the
CMake entry point. Their largest objection is the build-time Python
dependency, and the remedy they name is documentation:

> The alternative -- commit the generated `.h`/`.c` and regenerate by hand
> -- is viable but wants a documented, blessed workflow, including how
> `situc map --check` fits into CI when the generator is not run at build
> time.

`situ_generate()` is the opposite of that: it runs the compiler as a build
step, which is the thing hydra said it would rather avoid. So the consumer
exists, the trigger has fired, and the artifact it fired for is not the
artifact that was built.

**Three further statements are stale behind 0002 and are listed rather than
fixed**, because each cites the record: the decision index row at section 5
still reads "GNU Make only; CMake deferred"; the phase-0 acceptance
criterion says CMake "is deferred and recorded in
`doc/decision/0002-build-system.md`, so the acceptance criterion above
stands against `make test` alone until CMake lands"; and CMake has landed.

**Not resolved here, and the reason is not caution.** A record is not
edited once accepted -- a later one supersedes it and says so -- so the
only instrument is a new record, and writing it decides something real:
whether CMake is a supported entry point carrying a maintenance
commitment, or an artifact built against a superseded requirement that
should go. Both branches are live. The second is cheaper than it looks --
one file and one test -- and the first is what the tree currently behaves
as if it had chosen, without anybody having chosen it. That is the
holder's, and the evidence above is assembled so it can be taken once
rather than rediscovered per session.

### 26.479 The floor above the reach, in a line the map publishes

**`situc map` prints `# layers: floor=edit reach=view` for a schema whose
only above-`view` construct is an unbounded codec.** Measured on a
four-line schema -- a `u16` and one `coded body(squeeze)` with
`expansion = unbounded`:

    # layers: floor=edit reach=view

Read against `layers.reach`'s own docstring -- *the highest rung this
schema has content for* -- that line is false. Rung 2 is exactly what
emits storage for an unbounded expansion, per 0032's own table, so the
schema plainly has content for `edit`. `reach()` never looks: it asks
only whether a relation exists, and returns one of `view`, `relate` or
`drive`. Three of the six rungs can never be the reach, and
`FUTURE_LAYERS` is empty, so all six are built.

**The code matches 0032 and 0032's gloss does not match 0032's rule.**
The record says "the highest rung this schema has content for" and then
gives the rule as *a `relation` gives it `relate`; a declared retry
policy gives it `drive`* -- two sources, named exhaustively. The
implementation is that rule exactly. So this is not code drifting from a
record; it is a definition sentence wider than the rule beneath it, and
the artifact prints the definition's shape.

**Nothing is wrong today, and that is why it is recorded rather than
fixed.** Nothing compares the two scalars: no code indexes `LAYERS` with
`reach`, and the floor is separately enforced -- `cli.py` refuses a build
below it, naming the construct. A consumer who read `reach=view` and
concluded that `frame` emits nothing for them would be wrong, since
`framed_structs` has content for nearly every struct with a frameable
one; but no consumer does that, and `reach` is advisory by 0032's own
words -- *building below the reach is ordinary*.

**It is the holder's, because every available fix is a change to a
committed artifact.** Widening `reach` to see allocation or framing
rewrites the `layers:` line in every committed map that has one, under
`map --check`; narrowing the docstring to the rule leaves an artifact
whose two scalars are not comparable and does not say so. 0032 is
accepted. Flagged rather than resolved, per the rule that a document and
its code disagreeing is not a discrepancy to spend in either direction.

### 26.478 A count that was right twice, over two different populations

**README said six projects had evaluated situ and `project.md` said
five, and neither was wrong when written.** Both were dated 2026-08-05.
The README's six counts every project that produced a verdict; the
`project.md` five counts those that wrote it up in `suggestion/`, and
`hydra` is the difference -- its file is there and says in its own words
that the verdict lives in hydra's `project.md`. Two denominators, not a
contradiction, and a session reconciling them to one number would have
destroyed a real distinction.

**Three evaluations have landed since, all yes**, which is what made the
README's line stale: `ossacli` 2026-09-05, `openmlx4` 2026-09-06,
`raidcfgd` 2026-09-17. Nine now, and the README says nine.

**Their verdicts are relayed, not summarised.** The line now reads *five
said yes* where it read *two adopted*. Adoption is a claim about the
current state of five trees that this tree cannot check; a verdict is
what each file states in its own first paragraph and a reader can
confirm by opening it. The weaker word is the honest one, and it is also
the one that stops rotting -- a verdict written in 2026 stays written,
where an adoption can be reversed without anybody here hearing.

**26.66 and item 104 were left as history, and 104 needed two words.**
26.66 is an episode with a date in its title and reads correctly. Item
104 opened *Five sibling projects evaluated situ* in the present tense,
which is the shape `evidence.md` names as the one that rots: a countable
claim about the tree's own shape. It now says *had evaluated situ when
this was written*. The lesson underneath -- that three noes produced
more change than the adoptions did -- is untouched and was never the
thing that went stale.

**The instrument note, because it cost a measurement.** `git log -S"Six
projects evaluated it"` returned nothing, and the claim is in the file:
the phrase wraps at column 75, so `Six` and `projects` are on different
lines. An empty `-S` result over prose is not evidence of absence until
the pattern has been checked against the wrapping. `git log -S"projects
evaluated it against real trees"` -- one line, no wrap -- dated it in one
command.

### 26.477 Three claims in accepted records, corrected the tree's own way

Moved to `doc/decision/26.477-three-claims-in-accepted-records-corrected-the-tree-s-own-way.md`.

### 26.476 The conformance row that promised a test nobody generated

**13.1's table said `seekable = permuted` gets "the position map is a
bijection over a full block", and `codectests.py` contained zero
occurrences of the word.** `_seekable_test` returned early unless the
codec was `LINEAR`, so `interleave_16` -- the only permuted codec in the
tree -- got `invertible`, `deterministic` and `length` and nothing about
the property its signature is about. A conformance table is a list of
guarantees, and one row of it was a guarantee nobody made.

**It is generated now, and the case for it is one sabotage.** Making
`interleave_16` the identity in BOTH directions is what a port that
quietly stopped permuting produces:

    sabotage                    invertible  deterministic  length  permuted
    identity, both directions   OK          OK             OK      FAIL
    encode duplicates a source  FAIL        OK             OK      FAIL
    encode drops block offset   FAIL        OK             OK      FAIL

The identity row is the whole argument: it is a bijection, it round
trips, it preserves length, and every existing check passes it.

**Three assertions, because a map can fail to be a bijection three
ways**: nothing leaves its own block, no output position is written
twice -- which over a full block is onto as well as one-to-one -- and at
least one position moves.

**What it does NOT catch is in the table row now.** A DIFFERENT
permutation of the right shape passes. The test asserts the property the
signature states, not the specific map; the map is the kernel's and
`gen-derived`'s vectors are where that lives. `evidence.md` asks that a
gate's limits be pinned in the same breath as the gate, so the row says
it rather than leaving a reader to assume more.

**The block width comes from the kernel, not the granularity, and that
distinction is the one that would have made this silently do nothing.**
`_step` already returns rows*columns for a `permutation` kernel -- 16
for `interleave_16`. `granularity_size` is None there, because
`kernels.py` derives `granularity = byte` for a permutation, so a
granularity-based lookup yields None and the test is never emitted: a
gate over an empty population, wearing the appearance of coverage.

**Two ABIs, one body.** A tier-1 codec takes a capacity and an
out-parameter and returns a status; a derived one takes three arguments
and returns a count. The bijection being asserted is the same, so the
body is shared and only the call differs.

**And the sabotage had to be forced to land twice.** The first attempt
patched a regex that matched nothing and the assertion caught it; the
second matched encode and decode with one pattern and found only one,
because the decoder is the inverse and reads differently. Both times the
binary would have run the UNMODIFIED codec and reported OK --
`evidence.md`'s "a sabotage that did not apply and a check that cannot
fail are indistinguishable from the output", met twice in five minutes.

### 26.475 A derived codec reached the clause that said it could not

Moved to `doc/decision/26.475-a-derived-codec-reached-the-clause-that-said-it-could-not.md`.

### 26.474 The wire signature recorded a spelling, not a value

Moved to `doc/decision/26.474-the-wire-signature-recorded-a-spelling-not-a-value.md`.

### 26.473 One name for two widths, declined

**Settled by the copyright holder 2026-09-22: a variant arm keeps its own
name, and two arms may not share one.** A protocol that switches a field
between `u8` and `u16` on an earlier flag writes two arms with two names
and the caller branches, which is what situ does today:

    variant len switch (wide) {
        case 0: u8  narrow;
        case 1: u16 broad;
        default: error;
    }

`case 0: u8 value; case 1: u16 value;` is refused -- "field `value` is
declared more than once" -- and stays refused until a real need arrives.

**The reason is the TYPE rather than the branch, and the first statement
of it had that backwards.** The obvious objection to a merged accessor is
that it forces a runtime check; measured, that check is already there and
is deliberate. `situ_frame_len_narrow_get` opens with
`if (situ_frame_wide_get(view) != 0u) return SITU_ERR_VERSION;`, and
`c/emit.py` records why: "A caller who has already dispatched pays for the
branch twice, which is the price of an accessor that cannot be used
wrongly."

So a merged accessor would add no dynamism that is not already paid. What
it would add is a return type covering both widths -- and C, which is the
language situ caters for first, cannot express a type that depends on a
value. One `value_get` would have to return the wider one, so every read
of the narrow arm becomes a widened read, and the member's `repr` goes
from `MemoryIdentical` to `ValueConverted` for both arms. The map would
stop being true of the narrow case in order to make one accessor
possible, which is the trade situ exists to refuse.

**C++ would absorb it and that does not decide anything.** An overload or
a template hides the widening at the call site in one backend out of
four, and the capability map is backend-independent by construction -- so
a convenience only one language can spell would either fracture the map
or be recorded in it as a loss all four pay.

**What the current form costs, stated so the next reader can weigh it.**
It is per FIELD: a message that widens six fields on one flag needs six
variants and twelve names, and a caller branches at each. Nothing
composes them into "this message is the wide flavour". That is the real
friction and the shape a future need would take -- a whole-message
dispatch rather than a merged field.

**The alternative not taken, recorded so it is not re-derived.** A reader
over the variant -- one call returning the widened value and which arm it
came from -- keeps the layout truthful and moves the convenience into the
API rather than into the type. It is the shape to reach for if this comes
back, and it is not built.

### 26.472 A keyword in no grammar, and the gate that could not see it

Moved to `doc/decision/26.472-a-keyword-in-no-grammar-and-the-gate-that-could-not-see-it.md`.

### 26.471 A counted run of a struct nothing can measure

Moved to `doc/decision/26.471-a-counted-run-of-a-struct-nothing-can-measure.md`.

### 26.470 A gap that had half closed, and an unreachable remedy

Moved to `doc/decision/26.470-a-gap-that-had-half-closed-and-an-unreachable-remedy.md`.

### 26.469 What the Rust port is actually blocked on, measured

Moved to `doc/decision/26.469-what-the-rust-port-is-actually-blocked-on-measured.md`.

### 26.468 The fifth generator, and a recorded gap closed

Moved to `doc/decision/26.468-the-fifth-generator-and-a-recorded-gap-closed.md`.

### 26.467 One obligation, one bit -- and the half-fix that was worse

Moved to `doc/decision/26.467-one-obligation-one-bit-and-the-half-fix-that-was-worse.md`.

### 26.466 A generated check that had never run, and was wrong

Moved to `doc/decision/26.466-a-generated-check-that-had-never-run-and-was-wrong.md`.

### 26.465 The decoder read past its locator, and what found it

Moved to `doc/decision/26.465-the-decoder-read-past-its-locator-and-what-found-it.md`.

### 26.464 The second generator with the same fault

Moved to `doc/decision/26.464-the-second-generator-with-the-same-fault.md`.

### 26.463 A harness that names accessors the backend stopped writing

Moved to `doc/decision/26.463-a-harness-that-names-accessors-the-backend-stopped-writing.md`.

### 26.462 The Reed-Solomon decoder, expressed once

Moved to `doc/decision/26.462-the-reed-solomon-decoder-expressed-once.md`.

### 26.461 The Reed-Solomon derivation moves to where it can be shared

Moved to `doc/decision/26.461-the-reed-solomon-derivation-moves-to-where-it-can-be-shared.md`.

### 26.460 An `authenticated` region has to hold something

Moved to `doc/decision/26.460-an-authenticated-region-has-to-hold-something.md`.

### 26.459 `[nul_terminated]` wants a count the schema states

Moved to `doc/decision/26.459-nul-terminated-wants-a-count-the-schema-states.md`.

### 26.458 An exemption whose reason had stopped being true

Moved to `doc/decision/26.458-an-exemption-whose-reason-had-stopped-being-true.md`.

### 26.457 Reed-Solomon: why the last two are not a re-spelling

Moved to `doc/decision/26.457-reed-solomon-why-the-last-two-are-not-a-re-spelling.md`.

### 26.456 Bit stuffing, and two standards that transmit the other way up

Moved to `doc/decision/26.456-bit-stuffing-and-two-standards-that-transmit-the-other-way-up.md`.

### 26.455 Hamming, the interleaver, and a test no square fixture can fail

Moved to `doc/decision/26.455-hamming-the-interleaver-and-a-test-no-square-fixture-can-fail.md`.

### 26.454 COBS and the escape codes, and a test that ran out of bytes

Moved to `doc/decision/26.454-cobs-and-the-escape-codes-and-a-test-that-ran-out-of-bytes.md`.

### 26.453 The shift-register family, and a filter that hid its own test

Moved to `doc/decision/26.453-the-shift-register-family-and-a-filter-that-hid-its-own-test.md`.

### 26.452 The padded base codes, and two sabotages that proved nothing

Moved to `doc/decision/26.452-the-padded-base-codes-and-two-sabotages-that-proved-nothing.md`.

### 26.451 The table family, in the two backends that declined it

Moved to `doc/decision/26.451-the-table-family-in-the-two-backends-that-declined-it.md`.

### 26.450 What a tag is, as well as whether it is there

Moved to `doc/decision/26.450-what-a-tag-is-as-well-as-whether-it-is-there.md`.

### 26.449 A probe with the right name, asking a different question

Moved to `doc/decision/26.449-a-probe-with-the-right-name-asking-a-different-question.md`.

### 26.448 A tag that could not say what it covered

Moved to `doc/decision/26.448-a-tag-that-could-not-say-what-it-covered.md`.

### 26.447 An encoding nobody checks, in three backends and three ways

Moved to `doc/decision/26.447-an-encoding-nobody-checks-in-three-backends-and-three-ways.md`.

### 26.446 A located member reaches past the frame, in five of six readers

Moved to `doc/decision/26.446-a-located-member-reaches-past-the-frame-in-five-of-six-readers.md`.

### 26.445 A located member joins the offset chain it says it is not in

Moved to `doc/decision/26.445-a-located-member-joins-the-offset-chain-it-says-it-is-not-in.md`.

### 26.444 `before` became `until` inside a variant arm, and nowhere else

Moved to `doc/decision/26.444-before-became-until-inside-a-variant-arm-and-nowhere-else.md`.

### 26.443 The paren depth limit: recorded, NOT fixed, and why

**Measured by bisection: 69 nested parentheses parse, 70 raise
`RecursionError` from `situc/parser.py` in `parse_expr`.** It is a
traceback rather than a diagnostic, which is the same class as 26.437
through 26.442 -- and it is the one of the family being left alone.

    u8 d[((((( ... 70 deep ... 1 ... )))))];

`parse_expr` descends one frame per precedence level per paren, so the
budget is about twelve Python frames per `(`. Nothing else in the parser
is this shallow: unary `~` chains 500 deep, 800-term binary chains, 200
nested `positional` blocks and 150 nested `authenticated` regions all
parse.

**Why it is not fixed, which is a judgement and belongs written down.**
Every available fix either narrows the language or rewrites the parser:

  - A depth cap with a real diagnostic has to fire BELOW 70 to beat the
    interpreter, so it turns "parses" into "refused" for anything from
    the cap to 69. That is a language change to defend against input
    nobody writes.
  - Raising the interpreter's recursion limit trades a clean
    `RecursionError` for a possible segfault, which is worse.
  - Making the precedence climb iterative is the honest fix and is a
    parser refactor, which is its own piece of work and not one to do at
    the end of a session spent elsewhere.

**And the bar this file sets is whether acting on it changes anybody's
code, build or decision.** A hand-written schema does not reach 70; a
generator emitting one would be a finding about the generator. So it is
recorded as a known limit with its exact threshold, which is what lets
the next person recognise it in one line instead of bisecting it again.

**The threshold is a property of this interpreter**, not of situ: it
moves with the recursion limit and the frame cost of the Python running
it. Re-measure before quoting the 70.

### 26.442 A constraint the image cannot carry, and the one description that died

Moved to `doc/decision/26.442-a-constraint-the-image-cannot-carry-and-the-one-description-that-died.md`.

### 26.441 An assert that fired while the question it guards was being answered

Moved to `doc/decision/26.441-an-assert-that-fired-while-the-question-it-guards-was-being-answered.md`.

### 26.440 A sealed region's interior length: a wrong parser, and a traceback beside it

Moved to `doc/decision/26.440-a-sealed-region-s-interior-length-a-wrong-parser-and-a-traceback-beside-it.md`.

### 26.439 A `when` over a member no view can read, in four tracebacks

Moved to `doc/decision/26.439-a-when-over-a-member-no-view-can-read-in-four-tracebacks.md`.

### 26.438 Where a nested tag's dirty bit lives: C says the parent, three say the child

Moved to `doc/decision/26.438-where-a-nested-tag-s-dirty-bit-lives-c-says-the-parent-three-say-the-child.md`.

### 26.437 A self-covered struct nested in another: a traceback, and a wrong dirty bit behind it

Moved to `doc/decision/26.437-a-self-covered-struct-nested-in-another-a-traceback-and-a-wrong-dirty-bit-behind-it.md`.

### 26.436 A resolved decision that one backend of four implements

Moved to `doc/decision/26.436-a-resolved-decision-that-one-backend-of-four-implements.md`.

### 26.435 A four-bit arm, and a layout situ cannot express

Moved to `doc/decision/26.435-a-four-bit-arm-and-a-layout-situ-cannot-express.md`.

### 26.434 The sweep at the root cause: five of six, and `floor()` was wrong

Moved to `doc/decision/26.434-the-sweep-at-the-root-cause-five-of-six-and-floor-was-wrong.md`.

### 26.433 A varint arm: every backend calls an accessor none of them emits

Moved to `doc/decision/26.433-a-varint-arm-every-backend-calls-an-accessor-none-of-them-emits.md`.

### 26.432 The lens from the last bug found two more, and one was a disagreement

Moved to `doc/decision/26.432-the-lens-from-the-last-bug-found-two-more-and-one-was-a-disagreement.md`.

### 26.431 The corpus entry could not be written, and then it found the bug

Moved to `doc/decision/26.431-the-corpus-entry-could-not-be-written-and-then-it-found-the-bug.md`.

### 26.430 The nesting refusal had a hole exactly where an arm goes

Moved to `doc/decision/26.430-the-nesting-refusal-had-a-hole-exactly-where-an-arm-goes.md`.

### 26.429 A struct named `list` broke the module that described it

Moved to `doc/decision/26.429-a-struct-named-list-broke-the-module-that-described-it.md`.

### 26.428 The delimited-run emitter never learned about recursion, in three backends

Moved to `doc/decision/26.428-the-delimited-run-emitter-never-learned-about-recursion-in-three-backends.md`.

### 26.427 S-expressions describe in twenty lines, and an f-expr is not a format

**Asked by the copyright holder, who stores data in both**, so this is a
question about a format in use rather than an exercise. `example/sexpr`
is the answer to the first half and this entry is the answer to the
second.

**S-expressions cost twenty lines, because `example/json` already paid.**
`peek` (0057) so an arm owns the byte it was chosen by, recursion through
a variant arm (0054 as amended), a run of structs with a delimiter, and
`before` for a separator that belongs to neither side. Every construct
this schema needs was bought by the harder one, and the second recursive
text format in the tree is a fifth the size of the first. **That is what
a language paying off looks like**, and it is worth recording because the
JSON entry recorded only the cost.

**The subset is stated rather than implied**, which is json.situ's rule:
the core reader grammar is described and the reader MACROS are not --
`;` and `#| |#` comments, `#(...)` vectors, `#\a` characters,
quasiquote. `'x` and `(a . b)` frame exactly and mean nothing: the quote
is a symbol to this schema and so is the dot. Framing is not
interpreting.

**A second omission, found the same way and worth the same warning.**
The symbol terminator was `before ' ' | ')'` -- whitespace or a CLOSING
paren -- and an opening paren also ends a symbol. `(a(b))` is valid, has
no whitespace in it, and measured five bytes of six: the symbol scan
swallowed `a(b`. Every spaced form was already exact, which is what made
it invisible. It is `before ' ' | '(' | ')'` now, and C and Python agree
on all nine probe documents byte for byte.

**The wrong shape is recorded beside the right one, because it was
correct wherever it was easy to check.** The first version gave every
element its own trailing byte, which conflates the separator between
forms with the terminator that ends a list, so a nested list spent a
paren twice. It measured `(a b c)` exactly, and `((((a))))` exactly, and
`(a (b))` one byte short -- **right on flat lists and on pure nesting,
wrong only where nesting had a sibling beside it.** Eight cells across
two shapes are what separated the two, and a fixture carrying only
`((((a))))` would have proved the wrong schema correct.

**Bounded depth is the one place the description is narrower than the
language by construction.** `[depth = 64]` is required; Lisp's grammar
admits arbitrary nesting and a walk spends real stack per level.
Measured: forty levels of parens refuses with `SITU_ERR_DEPTH` rather
than reading past its own frame, which is the right answer and is not the
grammar's answer.

**AN F-EXPRESSION IS NOT A DIFFERENT FORMAT.** An operative is a combiner
that receives its operands unevaluated together with the dynamic
environment -- a property of an interpreter's calling convention, not of
bytes. On disk an f-expr form is s-expression text and `example/sexpr`
describes it exactly. What makes it operative happens in the evaluator,
where situ has nothing to say and no construct that could say it. A
schema can carry a FLAG recording the distinction and situ will describe
the flag precisely while staying silent about what it means. Written into
the schema's header as well, so the question is answered where it will be
asked.

**THE STANDING REQUIREMENT, and it is not built.** The holder's software
uses both as dynamic data stores, and situ will eventually need to do
simple OPERATIONS on these files rather than only describe them. What the
capability map already says about that is in the schema's footer and is
the honest starting point:

- `access=Sequential`, `offset=Scanned`, `size=Unbounded` -- a lookup is
  a scan. There is no length prefix and nothing says where a member
  begins, so the n'th form costs the n-1 before it. That is the format's
  property, not the description's.
- `mutate=Shifting`, `address=Unstable` -- an edit moves everything after
  it. In-place is available only where the replacement is the same
  length, which for text is common and for a symbol rarely is.

So "simple operations" on this format is not the same question as on a
fixed-layout one. Measured against the generated C rather than reasoned
about:

**READS ARE COMPLETE AND WORK TODAY.** `situ_list_items_count`,
`situ_list_items_at`, `situ_sexpr_kind_get`, `situ_sexpr_extent` and the
three `as_*` arm views are a whole walker. Run over `(a bb (c d) "e")`
it indexes all four forms, names each one's kind and measures its extent,
and refuses index 4. `access=Sequential` means a lookup COSTS a scan, not
that it is unavailable -- the accessor is generated and counts from the
front. Worth knowing because the axis name reads like a refusal and is a
price.

An extent includes the form's LEAD: `[1]` of that document is `" bb"` at
three bytes, not `"bb"` at two. A lead belongs to the form it precedes,
which is the same rule the trailing-whitespace edge comes from.

**WRITES ARE NOT GENERATED AT ALL, AND THE HEADER SAYS WHY.**
`situ_text_chars_ptr` returns `const uint8_t *`, and above it:

    /* No setter: mutate is Shifting.
     *   caused by: a member that ends at a delimiter
     *   remedy:    `until D max N` bounds the scan, which makes the
     *              member statically allocatable

**That remedy was tried and it is about allocation, not mutation.**
Bounding both scans at `max 64` moves `size=Unbounded` to
`size=Bounded(1, 64)`, gives `symbol` a `1..64` extent and
`address=FrameStable` -- and `mutate` stays `Shifting`, no setter
appears, and the same comment is still there. The length lives in the
DATA, and a bound on it is still not the schema fixing it. Only a member
whose width the schema states -- `u8 name[16]` -- is writable, and
s-expression text is not that.

**So the honest gap is narrower and more interesting than "no writes":
an equal-length replace is SAFE and situ declines it.** The caller knows
the length of what it is writing; `situ_text_chars_len` tells it the
length of what is there; and overwriting N bytes with N bytes moves
nothing. situ refuses because `mutate=Shifting` is a property of the
MEMBER rather than of the operation, so the const pointer is the same
answer whether the replacement fits or not.

That is the well-scoped candidate for the standing requirement: a checked
`replace(view, bytes, len)` that refuses unless `len` equals the member's
current length. It needs no new layout construct, it cannot shift
anything by construction, and it is the operation a data store performs
most -- updating a value to the same width. An insert, a delete or a
longer replacement remains a tail rewrite, which situ has no construct
for and which is a language decision rather than a backend one.

**Recorded rather than designed. Whether the equal-length replace is
wanted, and whether a tail rewrite belongs in a layout language at all,
is the holder's to decide.**

### 26.426 EVERY value constraint on a varint is accepted, promised, and checked by nobody

Moved to `doc/decision/26.426-every-value-constraint-on-a-varint-is-accepted-promised-and-checked-by-nobody.md`.

### 26.424 A `[since]` arm judges a version-1 message by a version-2 rule

Moved to `doc/decision/26.424-a-since-arm-judges-a-version-1-message-by-a-version-2-rule.md`.

### 26.423 A DELIMITED arm answered one byte, and four copies of one right line dropped its checks

Moved to `doc/decision/26.423-a-delimited-arm-answered-one-byte-and-four-copies-of-one-right-line-dropped-its-checks.md`.

### 26.425 The corpus entry caught the walkers lagging, on the first draw

Moved to `doc/decision/26.425-the-corpus-entry-caught-the-walkers-lagging-on-the-first-draw.md`.

### 26.422 A span constraint on a RUN arm: C refuses the wrong messages, the other three refuse nothing

Moved to `doc/decision/26.422-a-span-constraint-on-a-run-arm-c-refuses-the-wrong-messages-the-other-three-refuse-nothing.md`.

### 26.421 The enum arm's membership, and the gap that ran the other way

Moved to `doc/decision/26.421-the-enum-arm-s-membership-and-the-gap-that-ran-the-other-way.md`.

### 26.420 An enum-typed variant arm answers in the enum's type

Moved to `doc/decision/26.420-an-enum-typed-variant-arm-answers-in-the-enum-s-type.md`.

### 26.418 The sixth description, and the pass that was nearly too wide

Moved to `doc/decision/26.418-the-sixth-description-and-the-pass-that-was-nearly-too-wide.md`.

### 26.417 Four descriptions of six, and the differential said so

Moved to `doc/decision/26.417-four-descriptions-of-six-and-the-differential-said-so.md`.

### 26.415 Arm constraints, gated rather than suppressed

Moved to `doc/decision/26.415-arm-constraints-gated-rather-than-suppressed.md`.

### 26.416 The feature's own bounds hazard, in three flavours

**Emitting the check is what made it reachable.** A struct's `SIZE_MIN` is
its SHORTEST arm, so a fixed-size arm longer than that ends past a frame
the view accepted. An ordinary pinned member is covered by the fits
check, and a `sized_by` arm by the arm-fits check; a FIXED-size arm past
the minimum is the cell neither reaches, and nothing had ever looked
there because the check was not emitted.

Found by the Rust worker, who reproduced it and did NOT fix it -- patching
Rust alone would have made Rust answer bounds where C read garbage, which
is the divergence the four-way differential exists to catch. Then measured
in each backend, and the three failure modes are not equally loud:

    Rust    panics on the slice                     loud
    C       reads past the buffer                   loud only under ASan
    C++     reads past the buffer                   loud only under ASan
    Python  clamps, and answers from the WRONG      silent
            bytes

**Python's is the one worth remembering.** It does not overread, so there
is no crash to find: a two-byte VIEW inside a longer buffer read the two
bytes AFTER the frame and answered OK. The verdict came from bytes the
caller had declared were not part of the message -- the shape 26.32 rates
worst, and the only one of the four with nothing to announce it.

All four now answer the bounds error, and C's is pinned by a test that
builds under `-fsanitize=address` against an exact-length `malloc`, so
the sanitizer can see the read at all. Sabotaging the guard reproduces
`heap-buffer-overflow ... in situ_S_check`.

**And a vacuous test of my own, caught by reading.** The first C test
handed its probe to `compile_generated`, which passes `-c`: the probe was
compiled, never linked, never executed, and every assertion in its `main`
was inert. It passed. It builds, links and runs now, and its exit code
names which case answered wrongly.

### 26.414 A constraint on a non-struct variant arm is enforced by nobody

Moved to `doc/decision/26.414-a-constraint-on-a-non-struct-variant-arm-is-enforced-by-nobody.md`.

### 26.413 One line of recursion, six descriptions wrong, no cross-check able to see it

Moved to `doc/decision/26.413-one-line-of-recursion-six-descriptions-wrong-no-cross-check-able-to-see-it.md`.

### 26.412 The C++ backend crashed on a variant arm at a dynamic offset

Moved to `doc/decision/26.412-the-c-backend-crashed-on-a-variant-arm-at-a-dynamic-offset.md`.

### 26.411 A byte-run enum as a variant arm does not compile

Moved to `doc/decision/26.411-a-byte-run-enum-as-a-variant-arm-does-not-compile.md`.

### 26.410 The fourth shape, and why the half-fix had to be reverted first

Moved to `doc/decision/26.410-the-fourth-shape-and-why-the-half-fix-had-to-be-reverted-first.md`.

### 26.409 The construct is in the corpus, and what it took to get there

**`edges.situ` carries a `parameter` now**, which is what that file is
for: a construct the corpus does not hold is one no gate can fail on.

**The cost of its absence, measured rather than argued.** Between 0050's
view work landing and this struct arriving, the C backend alone had FOUR
shapes where a parameter reached a function that did not take it --
`required`'s fixed-size branch, the offset functions' missing `(void)`,
the nested sub-view accessor, and a variant arm. Three were found in one
afternoon, by hand, by people constructing fixtures. None could have been
found by a gate. `_argument_tail`'s docstring named two unplumbed shapes
and said no schema in this tree reaches either, which was true and was
offered as reassurance; the list doubled the first time anybody looked.

**Both spellings, deliberately.** `payload[block]` resolves through
`sized_by`; `trailer[block + 1]` goes through the expression renderer.
They are different code paths in every backend, and every backend that
built this feature got one and missed the other -- so a fixture carrying
only the bare form is a proof about half the code. A `u16` after both
means a wrong width for either shows up as a wrong BYTE rather than as a
length nobody reads back.

**Getting it in took four generators changed first, and that was not
0050's estimate.** The record said the corpus struct "lands with the
backends". Measured, it lands with every generator that SWEEPS the
corpus: `c/checks` and `c/fuzz` through the conventions sweep, `differ`
through `fourway`, and the three `derived` emitters through the
refuse-the-same-members sweep. Six, of which three needed a per-struct
skip and three needed their refusal deleted as unnecessary (26.405,
26.407). The number moved from four to six to three across this arc, each
time because somebody re-derived it instead of quoting it.

**The map and the wire signature name it rather than placing it**, which
is the committed contract working: `parameter -  u8  block [stream]` and
`negotiated.block  parameter`, against `payload` at `@0x0000`. Both
artifacts were regenerated and the wire diff reports the change as
compatible.

**One assertion in the tree was waiting for this and is now live.**
`test_every_fuzzable_struct_reaches_the_harness` requires an argued
struct to carry a "No harness" note instead of a `fuzz_` entry; until
now no corpus schema could exercise that branch, so it was a population
assertion rather than a measurement (26.407). It measures something as
of this commit.

**And it went red within the hour, which is the point.** `c/tamper` kept
its whole-schema refusal on the stated ground that nothing sweeps it over
the corpus -- measured against `test_tamper.py` and `test/generated/`,
and missing `test_cli.py::test_every_subcommand_runs_on_every_schema`,
which runs EVERY subcommand over every schema. The struct landed and that
sweep failed. Tamper skips the argued struct now, refusing only what the
skip would empty, which is what its three siblings already do.

**That is the third time in this arc a "which generators sweep the
corpus" count has been wrong** -- four, then six, then three, then a
seventh nobody had counted -- and every correction came from measuring
again rather than from anybody re-reading the claim. The struct is what
turned the last one from a claim into a red test.

**A fixture that cannot make a generator act does not test it.** Moving
tamper into the skipping cell failed three tests whose fixture had no
`tag`, and tamper emits nothing at all for a struct without one: it had
been "passing" for tamper only while tamper declined the file outright.
The shared fixture carries an `authenticated` region and a tag now. The
same shape as a gate over an empty file list, one level up -- the test
ran, and there was nothing for it to run against.

**And then it found two real defects in the PYTHON view**, which had been
built, tested, gated and committed a day earlier:

- **`at` assigned an attribute the class declared nowhere.** `view.<name>
  = <name>`, and nothing else. `mypy --strict` runs over every generated
  module in this suite and says *"negotiated" has no attribute "block"* --
  a hard failure, available from the moment any corpus schema declared a
  parameter, and not before. Rust says it with `pub n: u8` and C++ with a
  data member; Python's way is an annotation, and it is the same
  statement.
- **`required` took no argument at all**, while its body reads
  `probe.<name>` -- so it raised `AttributeError` naming a field the
  class does declare. The probe is built directly rather than through
  `at`, because `at`'s minimum-length check would refuse the very short
  buffer `required` is measuring, so the one thing `at` does for free had
  to be done again here.

The second was predicted, in writing, by the worker converting the
per-layer generators: it could not run the Python stream reader end to
end and said so under *what I did not verify*, naming this as the cause.
**A worker's honest "I could not check this" is a finding**, and it was
right.

**And a seventh, in a gate rather than in a backend.**
`test_every_writable_member_has_a_setter` reads each member's MUTATE axis
and requires a setter from every backend for anything `InPlaceFixed`. A
parameter's vector answers `InPlaceFixed` -- describing a byte that is not
there -- so the gate demanded a setter that no backend emits and none can,
one storing at the offset of the member AFTER it (26.398).

The check was the thing at fault, and the file's own name says why: it is
*the map is writable where it says*, and the rendered map says
`negotiated.block   parameter` with no axes at all. It names the member
and places nothing, so it promises nothing to hold a backend to. The gate
was reading the vector underneath instead of the promise the map makes.
That is 26.404 again -- `situc explain` had the same reading and gained a
sentence saying what its axes describe -- and it is *suspect the check
before the code* paying out: changing four backends to satisfy it would
have been a defect propagated rather than fixed.

**An eighth, and the guard that caught it was written for exactly this.**
`walk.Unsupplied` is deliberately NOT a `Refused` -- everything else in
that module answers a question about BYTES and this is the caller's
omission -- and the two-walker identity sweep catches `(Refused,
Unplaceable)`. So the new exception fell through and aborted the whole
`edges` comparison rather than skipping one struct.

What said so was `test_the_identity_comparison_asked_something`:
*only 117 refusals were compared ... it was 197 when written*. Its
docstring gives the reason it exists -- "a comparison that quietly
stopped having anything to compare would pass exactly as loudly as one
that agreed" -- and losing 80 comparisons is precisely the silence it
was built against. **A population guard earned its keep on a shrink
nobody predicted.**

The walker's own author could not have found it: their tests exercise
`Unsupplied` directly and pass, and no corpus schema declared a parameter
for the sweep to trip over. Same shape as the two Python faults -- code
that passed its own gates, with the gap reachable only once the corpus
carried the construct.

It is an explicit skip now. Comparing the two walkers under a SUPPLIED
argument is the better test and is its own piece of work, since the C
driver takes its arguments as trailing argv and both sides would have to
agree how one is passed -- recorded beside the differ's version of the
same question rather than decided in passing.

**There were two such sites, and the second was found by consequence
rather than by looking.** `_verdict_pair` catches `(AssertionError,
IndexError, Refused, Unplaceable)` and `Unsupplied` escaped that too.
The general shape is worth more than either instance: **adding an
exception type to a module means auditing every `except` clause that
catches its siblings** -- and `Unsupplied` was added deliberately NOT to
be a `Refused`, so every such clause in the tree predates it and none
mentions it.

Swept for the rest afterwards rather than waiting for a third to fail:
the corpus sweeps that reach a walker are these two and
`test_dissector`, which goes through `report.listing` -- already taught
to answer `needs-arguments` by the walker's own author. The remaining
`except Refused` sites are inside `walk.py` and `report.py`, where
`Unsupplied` propagating is the intended behaviour.

That makes EIGHT defects this construct's absence was hiding -- four in C
(26.408), two in Python, two in gates -- against a corpus struct that
took an afternoon to make landable.

### 26.405 Three refusals that were never needed, and a tripwire that proved it

Moved to `doc/decision/26.405-three-refusals-that-were-never-needed-and-a-tripwire-that-proved-it.md`.

### 26.406 Two written lists of one fact, and they had already drifted

**`test_wellformed.py` enumerated the refusing generators twice**, 130
lines apart: a `REFUSING` set added on 2026-09-17 that reads the source
and compares, and an older `GENERATORS` list that imports each one and
watches it raise. Two written enumerations of one fact are two things to
keep true.

**They had drifted before anybody noticed.** `differ` called
`refuse_parameters` and was in `GENERATORS` not at all -- so the test
that proves a refuser actually refuses had never run against it. The
source-scan set is what exposed that, which is the argument for deriving
a population rather than carrying one, made by the carried one being
wrong.

**The fix is one written list and two questions asked of it.** `REFUSING`
holds module names; the scan compares against it, and the parametrize
reads it. What stays separate is `SHAPES` -- how each generator's
`generate` is called -- because a signature is not something a scan can
answer, and a call shape does not change when a generator stops
refusing, so those rows are allowed to outlive a refusal.

**A parametrize over a name it cannot call is not a test that passed.**
So a guard asserts `SHAPES` covers `REFUSING`: without it, adding a
refuser with no call shape would have produced a `KeyError` at collection
or, worse, a silently absent case. Sabotaged by adding a module in
neither -- three tests fired, and the guard is the one that says what to
do about it.

Found by the worker sent to remove the `derived` refusals, reading the
file it had been told not to touch in order to report what its change
would break. **The instruction not to edit a shared file is what made it
read that file carefully**, which is not why the instruction was given.

## 27. Questions, and how they were settled

Recorded rather than resolved. Each needs a decision record before the phase
that depends on it.

**All twelve are settled.** Four were answered by building the thing they were
about, five by a decision record, two by concluding the question was not one,
and one by deciding deliberately not to answer it yet. Where a resolution rests
on something a reader can check, the check is named.

Where settling one produced a rule, the rule lives in the normative section and
this is the pointer: 11 and 12 are stated as language rules in 13.1 and 14.3,
because a reader looking up what may seal a region should not have to find it
in a log of questions.

Kept in full rather than deleted. What a question turned out *not* to be is
often the useful part -- 12 asked about ordering and the ordering was already
fine, while the thing actually broken was two lines away and nobody had asked
about it.

1. ~~**`until`-delimited arrays.**~~ **RESOLVED: the lattice models delimited
   data, and the earlier answer was wrong.** The construct was never the
   question -- that much held. Underneath it was whether the lattice should
   model delimiter-framed data at all, and the position taken was that it
   should not, because "no field has an offset, so `offset`, `access` and
   `address` have nothing to say and eleven of thirteen axes are vacuous".

   That claim was false, and the tree disproved it. A struct of variable-sized
   elements -- where element N cannot be found without walking the N-1 before
   it -- already produced `access=Sequential mutate=Shifting address=Unstable`,
   from a rule whose blame reads "element N cannot be found without walking the
   N-1 elements before it". That sentence was already about delimited data;
   it had been written for something else.

   The stronger argument runs the other way. `canonical` exists to say that
   several byte sequences encode one value, and text is where that is most
   often true. The axis was built for this and had never met the case it fits
   best. See 8.6.1 and `doc/decision/0020-delimited-data.md`.
2. ~~**Multiple tags with nested coverage.**~~ **RESOLVED.** Nested coverage is
   permitted and recomputes innermost first, which is the only order that
   terminates: an outer tag covers the inner tag's own bytes, so writing the
   inner one afterwards would leave the outer one stale again. Innermost is
   narrowest, since coverage is disjoint or nested by 14.1, so the sizes of two
   coverage sets order them. `doc/decision/0011-nested-tag-coverage.md`.
3. ~~**Cross-field invariants.**~~ **RESOLVED: the obligation goes where
   coverage already puts it.** The question was where the maintenance
   obligation lives in the generated API, and the tag machinery had already
   answered it for a harder case. A field a tag covers gets no plain setter;
   it gets `set_x(msg, value)`, which marks the tag stale, and the message
   refuses to be transmittable until it is recomputed (14.2).

   An invariant is that shape exactly. `invariant len == size(payload)` makes
   `len` a derived value; writing `payload` leaves it stale in the same way a
   covered write leaves a tag stale. So a field an invariant reads is covered
   by that invariant, loses its plain setter, and a `recompute` is generated
   beside the ones tags get. Nothing new is needed on the lattice: `auth` is
   already set-valued and already means "these bytes have an obligation
   attached", and the dirty bit is already per-obligation rather than per-tag.

   **Implemented.** `invariant s.total == size(s.hdr) + size(s.body);` is a
   top-level declaration beside `require`. The left side names one field and
   nothing else, because an invariant whose left side were an expression would
   say what must be true without saying which field situ is to maintain -- and
   a checked-but-unmaintained equality is what `require` already is. See 16.1.
4. ~~**Slack tracking.**~~ **RESOLVED.** No field is added to the view. `limit`
   *is* the capacity, established once at acquisition; the used extent is read
   from the data rather than stored a second time, so slack is
   `view.limit - used` and both terms are already available. The consequence to
   hold onto: a sub-view of a variable-length region must be acquired with the
   region's *maximum* extent, or a grow-in-place fails its own bounds check.
   `doc/decision/0008-slack-tracking.md`.
5. ~~**Bit-level offsets in the pin syntax.**~~ **RESOLVED: no.** The guess
   was "probably yes for registers", and building the registers showed
   otherwise. A register's fields are declared in order with their widths, and
   a gap is `reserved uN` -- which says how wide the gap is, carries a
   `[preserve]` or `[must_be_zero]` policy, and is checked by the generated
   validator. `@ 0x14:3` says only where the *next* field starts, so two
   fields can silently overlap and nothing accounts for the bits between.

   The capability map prints `0x06:3` as *output*, which is what the notation
   is good for: reporting a position the solver computed. Accepting it as
   input would be accepting a second, weaker way to say what `reserved`
   already says exactly.
6. ~~**Non-power-of-two integer widths above 8 bits.**~~ **RESOLVED.** A width
   divisible by 8 is a byte-aligned scalar, so `u24` and `u48` are ordinary
   scalars. Every other width is bit packed and may sit at any bit offset, so
   `u12` is legal; because it cannot fit inside one byte it always straddles,
   and the straddle rule of Section 8.2 does the gating rather than a width
   restriction. Section 8.1 amended to state the rule once instead of twice.
   `doc/decision/0005-integer-widths.md`.
7. ~~**Whether `native` endian should be permitted at all.**~~ **RESOLVED.**
   Split into two constructs (Section 8.3): `endian native` for genuinely
   host-order formats, gated behind `[allow_host_dependent]` and non-canonical;
   and `endian_marker` for runtime-resolved byte order, which is
   `CanonicalGiven(marker)` and costs nothing on the offset or size axes.
   `doc/decision/0014-positional-directives.md` carries the harder half of
   it: `native` is resolved by the *C* compiler, not by situc, because the
   machine running the generator is not the machine running the output.
8. ~~**Compact-versus-mutable tension.**~~ **NOT A QUESTION, and it should
   not have been listed as one.** It is the thing situ is for. Compactness
   wants varints, bit packing and elided optionals; every one of them destroys
   a fixed offset; and the whole point of the lattice is that the schema says
   which was chosen and the map says what it cost. There is nothing to decide
   because there is no right answer to decide on -- only a tradeoff to make
   visible per field, which is what the thirteen axes do.

   The varint sub-question was real and is resolved: required (8.1.1), because
   describing protobuf is impossible without them, with `minimal` as the
   canonical-encoding rule and mandatory for `require canonical`.

9. ~~**Kernel pipeline property composition.**~~ **RESOLVED by building it:
   conservatism does not collapse it.** The named case is in
   `std/kernels.situ` and the map prints its answer:

   ```
   codec framed expansion=ratio_exact(2,1) seekable=permuted
                granularity=block deterministic
   ```

   An exact ratio survives three stages, which is the property that matters --
   interior offsets stay a linear function of input offsets, so a field under
   the pipeline keeps a computable position. What the composition *did* need
   was a wider vocabulary rather than weaker rules: the addend and the ratio
   have to travel together, since parity appended before a doubling gets
   doubled. `doc/decision/0016-composed-expansion.md`.
10. ~~**Bit phase in the public API.**~~ **RESOLVED: forbidden at the
   surface**, which was the option the question guessed at. A struct whose
   size is not a whole number of bytes gets no accessors in any backend, and
   `gen-checks` says so in the emitted file rather than skipping quietly:
   `s: not a whole number of bytes, so it has no accessors`.

   **Measured 2026-09-19: only C does this.** C++, Rust and Python emit
   accessors for a sub-byte struct, so the sentence above is true of one
   backend of four. The corpus holds no such struct, which is why no gate
   says so. 26.436 has the measurement and the three ways it could be
   settled; it is not settled here.

   Bit *fields* are another matter and are fully supported: they are read by
   value through a shift and a mask, never by pointer, which is what
   `repr = ValueConverted` on them means. What is refused is a bit-phase
   *boundary* -- a region or struct that starts or ends mid-byte -- because a
   caller has no way to hold one. The layout solver refuses a bit-packed field
   after a dynamically sized member for the same reason, and all four backends
   assert that rule rather than handling it (invariant 12).
11. ~~**Whether tier-2 codecs need a constant-time mode.**~~ **RESOLVED: the
   obligation extends, situ cannot discharge it, so situ refuses.** A table
   indexed by plaintext leaks that plaintext through the cache whether the
   table was written by hand or emitted by situ, and the emitted one is
   table-driven by construction. Whether a constant-time Reed-Solomon is
   realistic turned out not to matter, because situ does not generate one.

   So a codec with a `derived` implementation may not seal a region. Sealing
   takes a tier-1 `extern` implementation, where the timing properties are the
   supplier's to state -- the same move decision 0017 makes about codecs
   generally. `doc/decision/0019-sealing-requires-authentication.md`.
12. ~~**`systematic` and authentication interaction.**~~ **RESOLVED: the
   ordering was already explicit, and the real gap was elsewhere.** Both
   orders are expressible and neither is inferred, because a pipeline says
   which stage runs first: `aead |> rs` is encrypt-then-code and `rs |> aead`
   is code-then-encrypt, and the composed signature differs accordingly.

   What the question did not ask, and what was actually wrong, is that
   `sealed(C)` accepted any codec at all -- including one that authenticates
   nothing. `sealed(crc32)` built, and the stage gate would have handed out
   the interior on a `verified` flag nothing had checked. A codec must declare
   `authenticated` before it may seal.
   `doc/decision/0019-sealing-requires-authentication.md` carries both
   refusals, since this question and 11 turned out to be the same one asked
   from different ends.
## 28. Glossary

| Term | Meaning |
|---|---|
| capability vector | the per-field set of axis values (Section 11.1) |
| frame | a region whose base address is resolved at runtime; interior offsets are static relative to it |
| island of staticness | a fully static struct positioned inside a dynamic frame |
| absolute static | offset known at compile time from message base |
| frame static | offset known at compile time from frame base |
| blame chain | the transitive list of constructs that caused a capability weakening |
| stage | a gate that must be passed before a region's capabilities become available |
| tag dirty | state in which an authentication tag no longer matches its covered bytes |
| property signature | the declared, semantics-free description of a transform |
| capability map | the committed, diffable record of every field's capability vector |
| tier 1 codec | extern; property signature declared and trusted |
| tier 2 codec | derived; implementation generated and properties proven from a kernel description |
| systematic code | one whose input data appears verbatim in the output at computable offsets |
| bit phase | the bit offset of a region boundary within a byte, non-zero for sub-byte codecs |
| canonical given f | exactly one encoding of a value once field f is known, though the format admits more |
| codec signature | the contract: what a codec does conceptually, independent of any implementation |
| impl binding | the association of a signature with a concrete implementation, derived or extern |
| conformance harness | generated property tests that falsify a signature an implementation does not satisfy |
| fidelity report | the importer's enumeration of source constructs it could not represent |
| extent | how many bytes one instance of a struct occupies, read from its own bytes; distinct from its *size*, which is what the layout knows without looking |
| measurable | of a struct: its extent can be computed from its own bytes, so a run of them can be walked and a member after one can be placed |
| walk | traversing a run by adding each element's extent, there being no count to index by |
| wire signature | the committed, diffable record of the byte-level contract; distinct from the capability map, which records cost (19.3) |
| declared weakening | a capability weakening a schema asserts because the layout does not imply it (11.5) |
| located member | one placed where a field says, from the start of the message rather than after the member before it; a reference, contributing nothing to the enclosing extent (9.8) |
| framing | deciding from a prefix whether a whole message is present, and if not how many bytes are needed (20.3) |
| accessor family | one of the ways a schema's bytes can be reached -- zero-copy views, or a materialized parse -- chosen by the consumer rather than the schema (decision 0022) |
| materialized | parsed once into owned storage, trading RAM for random access and cheap editing; the opposite of zero-copy, and worth generating only where the zero-copy vector is weak |
