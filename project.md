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
  (Section 19), not implicit. Situ never preserves unknown fields *silently*:
  they are rejected by default, and Section 14.5 says why that is a security
  position rather than an oversight.

  **Retaining them is on the roadmap, at the lowest priority** -- the
  copyright holder's decision 2026-10-29, which replaced "never". A format
  that carries fields its reader does not understand has a smell, so this is
  opt-in and discouraged rather than refused outright. `unknown = preserve`
  already parses and carries an unknown TLV item. What is missing is
  re-serialising it byte-identically in its original position, which needs
  an ordering rule as well (9.7).
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
- **~~Not a replacement for protobuf semantics.~~ Superseded: situ aims to
  handle the protobuf use case completely** -- the copyright holder's
  decision 2026-10-29. Situ describes the protobuf wire format (Section 9.7)
  and imports `.proto` files to produce such a description (Section 19.2). A
  workflow that breaks where protobuf's works is an implementation fault to
  fix, not a limit of scope.

  The earlier wording listed what situ "does not adopt": protobuf's identity
  model, its unknown-field retention and its evolution story. Read those now
  as **gaps, not refusals**. Unknown-field retention is the lowest-priority
  item on the roadmap (the bullet above). Evolution stays explicit (Section
  19) rather than implicit.

  **Weigh protobuf-specific work against the chance that it stops
  mattering.** Also the holder's, the same day. Situ may quickly supplant
  protobuf, and work spent reproducing protobuf's own conventions is wasted
  once people stop using it. So prefer fixes that serve every format of the
  same shape. A `tlv` region that validates nothing hurts CoAP as much as
  protobuf (26.737). Build a protobuf-only convention only when a real
  workflow needs it.

  More broadly, situ is **for most purposes already a superset** of the
  format descriptions it sits beside: RFC layouts, schema languages and
  register description languages. Once situ is mature, it will be one
  outright. What it leaves out on purpose -- silent retention of unknown
  fields, nesting with no declared bound -- the holder counts as poor design
  better left behind rather than as ground to make up. The exact parts of a
  specification belong in the schema, and its abstract parts -- rationale,
  meaning, receiver behaviour -- stay as comments in the same file. Each
  known gap is an item to close rather than a boundary.

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
tlv_args      = tlv_arg { "," tlv_arg } [ "," ] ;
tlv_arg       = "tag_decode" "=" tag_decode
              | "tag_running" "=" tag_decode   (* same shape, other
                                       * scope: `previous` and the
                                       * decoded parts *)
              | "tag_identity" "=" ident    (* decision 0023 *)
              | "value_size" "=" value_size
              | "known"      "=" known_tags
              | attr ;
tag_decode    = "{" tag_part { "," tag_part } [ "," ] "}" ;
tag_part      = ident "=" expr ;      (* over `tag`, the raw tag, alone *)
value_size    = "switch" "(" ident ")" "{" value_case { "," value_case }
                [ "," ] "}" ;
value_case    = ( "case" digits | "default" ) ":" value_rule ;
value_rule    = "self_delimiting"     (* the value carries its own extent *)
              | "prefixed" "(" ident ")"
              | "error"               (* a wire type this schema refuses *)
              | expr ;                (* a byte count over the tag's parts,
                                       * a literal among them. Last, because
                                       * the three keywords above are idents
                                       * and an expression would take them *)
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

**The array form is implemented, as of 26.683**, and that sentence is what
says what it costs: `uvar v[n]` emits a count, a span, a minimum span and
an `_at` that walks, in all four backends and both walkers. It had been
documented and unimplemented, which is worse than either -- six readers
agreed about it by all being wrong the same way.

**Two gaps stood beside it, both in what the IMAGE carried about a varint
rather than in the construct, and both are closed by 26.687.** The flags
byte stated `minimal` and `be128`, `image.py` read only the second, and
the transform had no bit at all -- so both walkers answered a `svar`'s raw
groups where four backends answer the decoded value, and took a padded
encoding all four refuse. `edges.varint_signed` and
`edges.varint_canonical` are the fixtures that were missing, and all six
readers agree on them now.

**And `uvar v[remaining]` crashes the compiler** -- `AssertionError` in
`propagate._parameterise`, on `assert placement.size_max_bits is not
None`, with no diagnostic. Measured 2026-10-23 and confirmed identical in
a worktree pinned at `b186326`, so it predates the array work rather than
following from it. A bare `[remaining]` states BYTES rather than a count
of elements, which is a question the construct has to answer before it can
be compiled: either the run fills the remaining bytes (well defined -- walk
until the frame ends) or the form is refused. Both walkers decline it
already, asking `_sizes_in_bytes` before walking, so whichever answer is
chosen they are ready for it. An assertion is not either answer, and
section 17.0 is about exactly this: a construct situ cannot decide is one
it refuses with a reason.

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

**The expression form is over the tag's own decoded parts**, which is CoAP's
option header -- a four-bit delta and a four-bit length in one byte:

```situ
tlv options (
    tag_type     = u8,
    tag_decode   = { delta = tag >> 4, length = tag & 0x0F },
    tag_identity = delta,
    value_size   = switch (delta) { default: length },
);
```

The parts are the only names in scope, and that is the same rule the
dispatch's selector follows: they are computed from the tag just read and
decide where the value ends, so a size naming a member would be naming
something the item has not reached. A literal is the degenerate case and
stays one -- `4` and `2 * 2` fold to a count, and only an expression that
names a part is the fifth rule.

**A value may be carried from one item to the next**, which is what CoAP's
option NUMBER is: the running sum of every delta ahead of it.

```situ
tlv options (
    tag_type     = u8,
    tag_decode   = { delta = tag >> 4, length = tag & 0x0F },
    tag_running  = { number = previous + delta },
    tag_identity = number,
    value_size   = switch (delta) { default: length },
);
```

`previous` is that value for the item before, and zero before the first --
scoped to the definition it appears in, so two accumulators cannot mean each
other's and nothing has to be said about the order they are evaluated in. In
scope beside it are the tag's decoded parts and nothing else: not the raw
tag, which the decode is for; not another accumulator, for the ordering
question that would open; and nothing read later, which is the rule
`tag_decode` is already held to.

**A carried value may be the identity and may not size a value.** The parts
are bounded by the tag's width and a sum over a region's worth of them is
bounded by the item count, so an extent computed from one would have no
bound the solver could carry. `tag_identity` is what it is for: a `known`
key then matches the number rather than the delta, and the four backends
take the carried value as a parameter to `read` -- which is what says an
item reached by offset alone cannot have a number.

**And a single part stops being the identity by inference once a value is
carried.** 0023 infers the identity from a lone decoded part, which is right
while the tag is the only source of one; a region that also carries a number
has two candidates that rule cannot tell apart, and keying a lookup on the
delta finds an item whose number is something else. It is refused where a
`known` map makes the choice decide what an accessor finds.

**The state situ carries is no longer only inside codecs.** 26.142's
sentence -- "situ carries state inside codecs and nowhere in the layout" --
named the boundary that made 8b10b and CoAP look like one question. A
`shift_register` holds a register and `stuffing` with `unit = stream` holds
line position; this is the first thing in the layout that holds anything,
and it holds exactly one value per name per region.

**A scalar tag type is read as that scalar.** `tag_type = u8` is one byte
read as a byte and `tag_type = u16` two in the region's byte order; a
declared `varint_type` is read by its own encoding. The width came from a
varint declaration alone until 26.664, so a scalar fell back to a 64-bit
leb128 and a tag at or above `0x80` consumed the byte after it -- which is
every CoAP option delta of 8 or more. A `tag_type` that names neither a
declared varint nor a scalar is refused, where it used to take the same
silent fallback.

**It was specified before any code existed and implemented 26.663.** The
sentence above has said "or an expression" since the scaffolding commit of
2026-07-26, and the four forms the parser knew were a literal,
`self_delimiting`, `prefixed(T)` and `error`. So a CoAP option was
inexpressible for a reason 26.142 recorded as a language question and this
document had already answered: *the document wins over the code*, which
made implementing it the only available reading rather than a decision
anybody had to take.

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

What Situ does *not* reproduce yet: field-number-as-identity for schema
evolution (Section 19.1), and re-serialization that preserves unknown fields
in their original positions. These were written as deliberate exclusions; since
2026-10-29 they are gaps (Section 2). A Situ description of protobuf can parse
and report unknown fields; round-tripping them byte-identically requires
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

**Measured from the message, which is what its capability reads off.** A
located member's offset is `DataPlaced` even where the expression is a
literal, and a schema written as a datasheet reads -- `u32 board_id at 26` --
therefore reports less than the same layout padded out with `u8 gap[n]`,
which is `AbsoluteStatic(0x1A)`. That is not the construct under-reporting
itself: `at 26` is 26 bytes from the MESSAGE, a view cannot resolve it, and
every generated accessor takes the message and bounds-checks on each call.
The two numbers coincide only for a struct read at message offset zero.
Reported from `ossacli`, and 26.667 has the question it raises -- whether
the offset axis should distinguish a message-relative constant at all.

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
| `coded-predecessor` | a coded region earlier in the same frame whose codec has no closed-form expansion | computed from the construct |
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

| **linear block** | parity-check matrix over GF(2): a named code, or one `parity` row per check bit | Hamming(7,4) by name; any `(n,k)` from its matrix, bounded to a codeword that fits the byte the generated pair takes and to rows that locate a single error. Not LDPC, for the reason below the table | `ratio_exact(n,k)`; `systematic` iff the matrix is in standard form, which rows given outright are by construction; `seekable = blockwise(n)` |
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
worth keeping.** A `linear_block` whose matrix nothing states derives a
*correct* `ratio_exact` and generates nothing; a `permutation` with a bare
`span` does the same. Both are honest: the signature is right and the author
is told there is no implementation.

**The dishonest case existed here and was found by closing the gap.** The
emitters gated on the code's NAME and read the dimensions from nowhere, so
`linear_block(n = 15, k = 11, code = hamming_7_4)` passed the gate and
generated Hamming(7, 4)'s tables -- a seven-bit codeword under a signature
saying `ratio_exact(15, 11)`, which is the one thing this paragraph says
does not happen. A named code is a code at its own dimensions now, and
anything else declines with the reason. A convolutional code declared as a shift
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

**`[truncated]` is a tag's and is refused on a `checksum` (0067).** The
comparison is where the two differ: for an authenticated region situ
generates none, handing the caller a `verified` flag and the tag's bytes so
that the codec applies its own convention, while a checksum's comparison is
generated here -- so truncating one would ask situ which end of the value a
format keeps, and 17.0 does not guess that. A checksum field is therefore
exactly the width of its code, read from the kernel rather than from
`tag_bytes`, which is how all thirteen in `std/kernels.situ` state it. 0067
narrows 0038's placement rule rather than reversing it.

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
absolute_static(X)      frame_static(X)         static(X)
in_place(X)             in_place_dirty(X)       immutable(X)
random_access(X)        stable_address(X)
canonical(X)            deterministic(X)        deterministic_writer(X)
aligned(X, n)           atomic(X)               memory_identical(X)
max_size(X, n)          no_tag_invalidation(X)  verify_gated(X)
uncovered(X)            no_alloc(X)             bounded_stack(X, n)
no_realloc(expr)
```

**A predicate's bound is an ARGUMENT, and this list said otherwise about
one of them.** `max_size` was written `max_size(X) <= N` here for as long
as the section has existed, beside `aligned(X, n)` and
`bounded_stack(X, n)` in the same block -- the same kind of thing spelled
two ways, and the compiler has only ever accepted the second.

Of the three, `aligned` is the one this compiler discharges with a bound;
`bounded_stack` is deferred for want of a stack-depth model and reports
as unchecked, which is why its spelling has never been tested by anything
either. bbq-predictor copied the documented form, got "unknown function
`max_size`" with the six expression builtins listed, and reported it. The
copyright holder settled it on 2026-10-25: `max_size(X, n)` is right and
this list was wrong.

A predicate answers yes or no about one axis, so there is no value for a
comparison to be made against -- which is the reason the argument form is
the right one rather than merely the accepted one.

**`size` and `offset` are expression builtins and not predicates**, which
is why they were in this list and why they do not belong in it:

```
require size(X) == N;           require offset(X) == N;
```

Those are `expr` in `capability_expr = ident "(" expr { "," expr } ")" |
expr`, and they take a comparison because they yield a number. `size` and
`max_size` reading so alike is what made one list of two things look like
one thing.

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
  show it, beside the ones already there.

**The README carries no progress section.** It had one, *Recent
additions*, sorting features into built, half built and not yet built, and
it was the part that rotted: 0051 sat under "not yet built" while all six
descriptions carried it, and 0050's entry said `parameter` was not built on
the day it was. A section describing a STATE is stale by construction, and
the holder made that a rule for every README on 2026-10-29
(`harmonization.md`, citing this one). State lives in section 26 and in the
log. Removing it found one keyword, `preamble`, that only it named; it has
a row in the construct table now.

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

### 25.3 A bill carries the date it was verified

**Set by the copyright holder 2026-10-22.** A *bill* is a comment in a
schema claiming the language cannot express something. It carries the date
somebody last checked it, on its own line at the end of the paragraph
making the claim:

    // operator whose operands are a field and a literal of a different kind.
    // (verified 2026-10-22)

The date means **verified on**, so it is not written without re-deriving
the claim. `verified 20` is the lookup path, and is distinguishable from a
decision citation or a source line:

    grep -rn 'verified 20' --include='*.situ' example/ std/

**Anchor where you can, because an anchor outlives a date.** A bill that
names a decision record, a refusal the compiler emits by name, or a line
in the source it was measured from says where to re-derive rather than
only when somebody last did. Six of the nine do.

**Date a claim that the language cannot do something. Do not date a claim
about what a construct does.** A bill is falsified silently by a language
addition -- the schema goes on compiling and nothing brings a reader back
to the sentence -- while a claim about a construct's behaviour is
falsified loudly, because every schema using it stops building. So
`example/ipv6ext`'s "what `while` asks and what `until` cannot" is not a
bill and is not dated, and a format fact never is.

**And when the language gains a construct, re-read the bills that asked
for it.** Not the corpus and not a sample: a bill that asks for something
names what it wants, so the files to check are findable the moment the
thing they asked for lands. 26.672 has the pass that set this, and the
stale bill that was sitting next to its own refutation while an audit
verified the refutation.

There is no gate. A stale bill reads exactly like a sound one, so the only
detector is re-derivation, and an age nag over nine lines would saturate.

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

**What is open as of 2026-10-29**, with the record that carries each
reproduction. **Three are decisions rather than work and are the copyright
holder's** -- whether `required` should read every readable length
(26.693), which walker is right about a nested struct's value (26.721),
and the walker guard-width reconciliation (26.689). Only the second
blocks anything.

**What a session picking this up should take first**, in order: the
`uvar v[remaining]` walk below, which is specified and blocked on nothing;
then rungs 3, 5 and 6 of the layer ladder with 26.99 finishing rung 2,
which is the real goal gap and is sequenced in the phases above rather
than open-ended; then teaching `differ.py` to compare framing, which would
fold 26.725's and 26.732's two files into the corpus differential.

- **`required` has seven records in four days and what is left of it is
  one design question.** The seventh arrived from bbq-predictor after the
  sixth was pushed: a counted run of FIXED-SIZE records was framed as a
  single record, so a 104-byte message of two entries was reported
  COMPLETE at 62 and held it to the last byte (26.734). `is_counted_run`
  excludes a fixed-size element on purpose -- such a run has a stride to
  multiply -- so the shape fell past all four guards in all four
  emitters, and `traverse.nests_one_struct` is the one predicate they
  ask now. The function frames a
  message from bytes a caller has and nothing has validated, and every
  fault in the run was one shape: **two quantities computed in different
  places from different things, where neither bounds the other.**
  - 26.693, the first and the one that named the rule: a floor derived
    from `[min]` constraints against a total computed from unvalidated
    bytes, so bbq-predictor's `head_body` answered truncated-need-11 at
    ten bytes and **complete-need-10 at eleven**. The floor came out of
    `required` in all four backends on the holder's instruction. A bound
    derived from constraints is not a better bound, it is a different
    claim -- about valid messages, asked of a function handed invalid
    ones.
  - 26.723, the half of that floor nobody had written down: it was also
    a **bounds guard**. Reported by bbq-predictor as a regression, an
    ASan heap-buffer-overflow reading a counted run's count out of a
    buffer too short to hold it. C emitted the guard before the run
    branch all along; the other three emitted it after a branch that
    `continue`s, and only C++ is unsafe because only its accessors are
    not lax.
  - 26.724, the same rule in the run walk: each unreached element was
    credited `SIZE_MIN`, which is constraint-derived, and re-counted at
    its own honest bound when reached, so `need` FELL. `framing_floor`
    in `situc/traverse.py` is the structural figure, and the inequality
    it rests on is asserted over the corpus rather than argued.
  - 26.726, the same rule a third time: the varint and delimiter
    branches answered `have + 1`, a bound on one member, discarding the
    total the walk already held. `max(have + 1, at)` now.
  - **The standing advice of 26.693 has been acted on and is spent.** It
    said to find the clamped terms in the offset chain and ask whether a
    larger `have` can make the total smaller. The answer was that the
    clamp was never the mechanism: all three later faults are a figure
    quoted from the wrong authority, and none involves a clamp.
  - **What remains is the holder's**, unchanged from 26.693: whether
    `required` should read every length that is readable, so that the
    first answer is the last. Today it reports a lower bound that rises
    as bytes arrive, which is every variable-length shape in this tree
    rather than a defect in one, and a caller makes several round trips
    for one message. Taking it would turn two round trips into one and
    would make a tighter tail term worth writing; leaving it is sound.
- **The population story was wrong for one of them, and the correction
  is worth more than the fault** (26.725). 26.693 closed with "the
  property was right and the population was not", and 26.723's record
  borrowed that. It is false: `example/ble`'s own
  `le_advertising_report` reproduces 26.723, and has since `ble` was
  added for exactly that construct. **What was missing was ever handing
  `required` a buffer whose end was real.** The one sweep there was
  walks `example/http` out of a `const char req[]` on the stack, where
  an over-read lands inside another object and answers from whatever is
  in it. The general form: **a bound is only tested by an input whose
  edge exists**, and a test that supplies a length from a buffer with
  slack behind it has checked the arithmetic and not the bound.
- **The framing layer had no corpus-wide execution in any backend**
  until 26.725, and `situc/codegen/differ.py` -- the differential over
  all five spellings, and the source the C++ fuzz harness is built from
  -- still does not mention `required` or `framed`. Two files stand in
  its place and ask different questions, which is why there are two:
  - `test/unit/test_required_over_the_corpus.py` owns the PROPERTIES,
    in C and C++: every `required` taking only bytes, every prefix to
    48, four fills, each prefix in a `malloc` of exactly its own length,
    under ASan and UBSan. This is where a missing bound is caught.
  - `test/unit/test_four_backends_frame_alike.py` owns AGREEMENT, in all
    four: the same sweep to 32, with each backend's answer sequence read
    back and compared token for token. 221 structs and 884
    (struct, fill) sequences. **It found Rust saturating at `usize::MAX`
    where the other three saturate at `u32::MAX` deliberately** -- five
    structs, and the divergence depended on the target's pointer width
    (26.732).
  - **Two backends agreeing wrongly is what the second cannot see**, and
    the properties are what see it. Teaching `differ.py` itself would
    fold both into the corpus differential and is still not done.
- **A population is not only which schemas are swept but which bytes
  they are swept with** (26.727). The construct 26.724 needed -- a
  counted run whose element length carries a `[min]` -- was in no schema
  in the tree, and is in `test/schema/edges.situ` now. It took the
  construct AND a fourth fill chosen for it: a uniform fill makes a
  `u16` count either zero or enormous, so the step the fault lives in
  was unreachable under the first three. The same question is worth
  asking of every corpus-driven check here, and the schemas are counted
  where the inputs are not.
- **Rung 4 is emitted four ways and all four are compiled now**
  (26.692, 26.728, 26.729). C's gate has existed since 26.260 and C++'s
  and Python's since 2026-10-26; **Rust's landed 2026-10-29** and is in
  `test_codegen_rust.py` beside its three siblings. The entry here said
  Rust's was unbuilt and was right; a record that said only C's had ever
  been compiled was not, and 26.728 carries that correction.
  - **CLOSED 2026-10-29** (26.731). The last of it was `pickle_string`
    and `byte_run` in `test/schema/padded.situ`, and the three backends
    have C's `pad_to` branch now -- on the UNCLAMPED form 26.730 left
    rather than the clamped one that stood there, which had reported a
    message with a trailing pad COMPLETE three bytes short. Every
    backend's frame holdout set is empty; they are kept rather than
    deleted because the mechanism is live, and the next construct one
    backend gains before the others puts a schema back in them.
  - **The parking was wrong, which is the part worth keeping.** The run
    before deferred this as depending on whether `required` should read
    every readable length, because C's pad term was loose. Section 26's
    own sentence settles it -- four backends that disagreed would mean a
    schema means four things -- and the looseness turned out to be the
    safety fault 26.730 fixed. So the deferral held up a port AND left
    the fault that made the port look unattractive.
  - **Three of the five that gap used to hold were a different fault**
    (26.729), and the worst thing found this week. `coap`,
    `proto_message` and `tagged_options` each hold a `tlv` region, whose
    reach is schema-free, and C's length expression for one is the
    literal zero -- so C framed each at the length of its FIXED members.
    For `proto_message` that is zero: `situ_proto_message_required`
    answered `SITU_OK` with `*need = 0` for every input, and a caller
    doing what the header says -- *"call `next` until it answers
    SITU_ERR_TRUNCATED"* -- **looped for ever handing back zero-length
    views**. `frameable` refuses a `tlv` region now, beside the
    `indexed` branch it parallels, and C's `_required` asks `frameable`
    rather than judging again. C loses exactly three `_required`
    functions and gains none.
  - **A property sweep would have passed that one.** `need` never fell
    and was never below `have`, because it was never anything. What
    caught it is the per-schema non-vacuity floor in 26.725's C arm:
    *this schema answered 196 calls and not one truncated*. Fourth time
    this run that a count of what a check asked found what the check
    could not.
- **`validate` reports a malformed message as valid** where a counted run
  sits in a variant arm (26.689). C's `check` is the struct's minimum
  length and `return SITU_OK`; every other counted run gets a `_fits`
  predicate and a call to it. An absent `validate` or a walk for arm
  members are different decisions, so `edges.situ` carries no such struct
  rather than carrying an unsound verdict silently.
- **No axis says "unreachable"** (26.689). `offset`, `stage`, `mutate` and
  `access` each carry part of it for a member no backend can place, and
  nothing says the whole.
- **`uvar v[remaining]` is unimplemented in all four** (26.691, 26.733).
  It was recorded as three backends short of a construct with C
  answering; **C emitted a count returning the constant `0u`**, so on
  four bytes holding three varints every accessor said the run was empty
  and `validate` returned OK. Four short, with C the one that answered
  confidently. C declines now, in the other three's words, and the
  construct is honestly unsupported rather than wrong in one -- the
  corpus C output is byte-identical, since no committed schema has it.
  `edges.situ` carries one now so the agreement is witnessed.
  **Walking varints to the end of the view is the feature, and none of
  the four does it.**
- **Four walker identity divergences** of one shape (26.689), where the
  guard widths want reconciling rather than listing. **The holder's**, and
  the parking was re-measured and confirmed 2026-10-28 rather than
  weakened: `fits_frame` is the packer's name for an image check, and the
  only readers of the image's check table are the two walkers, so there is
  no third witness for check identity the way a generated backend is one
  for a value.
- **Which walker is right about a nested struct's value, and should either
  answer at all?** (26.721). **The holder's**, and the one question on
  this list that blocks work: it holds `VALUES_APART` open in
  `test/unit/test_walker_c.py`, three `(schema, struct)` pairs, and holds
  back dealing more packets per struct in the walker sweeps, which would
  grow that set before the question is answered.

  C reads the row's `size_bits` and the Python walk reads the measured
  extent, and **both agree about the member's offset and its width** --
  which is why the three older corpus sweeps cannot see it and the values
  sweep found it on its first run. `situ_walk.h` states a contract C
  violates while the Python walk answers a third thing. Best guess:
  refuse in both, as the fifth of `read_scalar`'s recorded refusals. Not
  taken, because it changes BOTH independent readers -- the only
  independent witnesses this project has -- and fixing C alone would turn
  a visible divergence into a silent decline, which is the vacuity 26.718
  cost eleven weeks.

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

Moved to `doc/decision/26.7-phase-7-transforms-tier-1-extern-codecs.md`.

### 26.8 Phase 8: cryptographic model

Moved to `doc/decision/26.8-phase-8-cryptographic-model.md`.

### 26.9 Phase 9: advisor

Moved to `doc/decision/26.9-phase-9-advisor.md`.

### 26.10 Phase 10: MMIO target

Moved to `doc/decision/26.10-phase-10-mmio-target.md`.

### 26.11 Phase 11: Rust backend

Moved to `doc/decision/26.11-phase-11-rust-backend.md`.

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

Moved to `doc/decision/26.20-cross-field-invariants.md`.

### 26.21 Text protocols

Moved to `doc/decision/26.21-text-protocols.md`.

### 26.22 Schema evolution

Moved to `doc/decision/26.22-schema-evolution.md`.

### 26.23 What the worked examples asked for

Moved to `doc/decision/26.23-what-the-worked-examples-asked-for.md`.

### 26.24 DNS name compression, and what it cost

Moved to `doc/decision/26.24-dns-name-compression-and-what-it-cost.md`.

### 26.25 The dissector reads the same layout

Moved to `doc/decision/26.25-the-dissector-reads-the-same-layout.md`.

### 26.26 What the folds keep finding

Moved to `doc/decision/26.26-what-the-folds-keep-finding.md`.

### 26.27 What fuzzing every schema found

Moved to `doc/decision/26.27-what-fuzzing-every-schema-found.md`.

### 26.28 Where a generated artifact cannot be purged

Moved to `doc/decision/26.28-where-a-generated-artifact-cannot-be-purged.md`.

### 26.29 One list, four runtimes

Moved to `doc/decision/26.29-one-list-four-runtimes.md`.

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

Moved to `doc/decision/26.41-the-rest-of-the-list.md`.

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

Moved to `doc/decision/26.48-the-constructs-that-had-never-met-a-run.md`.

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

Moved to `doc/decision/26.63-the-rest-of-the-stack-and-a-filter-that-had-to-be-shared.md`.

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

Moved to `doc/decision/26.77-the-instrument-builds-somewhere-it-does-not-own.md`.

### 26.78 The conventions the log did not follow, and the gate that agreed

Moved to `doc/decision/26.78-the-conventions-the-log-did-not-follow-and-the-gate-that-agreed.md`.

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

Moved to `doc/decision/26.88-a-nested-sub-view-a-tag-and-the-extent-that-was-zero.md`.

### 26.89 `validate`, which cannot be rendered by halves

Moved to `doc/decision/26.89-validate-which-cannot-be-rendered-by-halves.md`.

### 26.90 A member named for a keyword, in four languages

Moved to `doc/decision/26.90-a-member-named-for-a-keyword-in-four-languages.md`.

### 26.91 The two things `validate` deferred, and the six bugs behind them

Moved to `doc/decision/26.91-the-two-things-validate-deferred-and-the-six-bugs-behind-them.md`.

### 26.92 The last twenty-one, and what they were really deferring over

Moved to `doc/decision/26.92-the-last-twenty-one-and-what-they-were-really-deferring-over.md`.

### 26.93 The file nothing referenced, except the tests

Moved to `doc/decision/26.93-the-file-nothing-referenced-except-the-tests.md`.

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

Moved to `doc/decision/26.102-the-table-nobody-cleared-and-the-stack-that-was-zero.md`.

### 26.103 The delimiter scan, and the value a run does not have

Moved to `doc/decision/26.103-the-delimiter-scan-and-the-value-a-run-does-not-have.md`.

### 26.104 The constraint cpio's own comment defends, checked by nobody

Moved to `doc/decision/26.104-the-constraint-cpio-s-own-comment-defends-checked-by-nobody.md`.

### 26.105 The other form, and the schema that had to be written for it

Moved to `doc/decision/26.105-the-other-form-and-the-schema-that-had-to-be-written-for-it.md`.

### 26.106 The last construct, and the width its values could not show

Moved to `doc/decision/26.106-the-last-construct-and-the-width-its-values-could-not-show.md`.

### 26.107 The member that says where it is

Moved to `doc/decision/26.107-the-member-that-says-where-it-is.md`.

### 26.108 The run a device could measure and not read

Moved to `doc/decision/26.108-the-run-a-device-could-measure-and-not-read.md`.

### 26.109 The loop in the arena, and the bound that had a public entrance

Moved to `doc/decision/26.109-the-loop-in-the-arena-and-the-bound-that-had-a-public-entrance.md`.

### 26.110 The variant, and the fifth thing that is not a number

Moved to `doc/decision/26.110-the-variant-and-the-fifth-thing-that-is-not-a-number.md`.

### 26.111 `validate` in C, and the difference between two refusals

Moved to `doc/decision/26.111-validate-in-c-and-the-difference-between-two-refusals.md`.

### 26.112 The span checks, and `remaining` measured from the wrong end

Moved to `doc/decision/26.112-the-span-checks-and-remaining-measured-from-the-wrong-end.md`.

### 26.113 The walker, folded

Moved to `doc/decision/26.113-the-walker-folded.md`.

### 26.114 The ladder nobody had run

Moved to `doc/decision/26.114-the-ladder-nobody-had-run.md`.

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

Moved to `doc/decision/26.121-the-floor-the-generated-code-was-never-held-to.md`.

### 26.122 The tag whose width meant nothing

Moved to `doc/decision/26.122-the-tag-whose-width-meant-nothing.md`.

### 26.123 The attribute that was parsed for eleven phases and read by none

Moved to `doc/decision/26.123-the-attribute-that-was-parsed-for-eleven-phases-and-read-by-none.md`.

### 26.124 An argument parser, as an evaluation of the text machinery

Moved to `doc/decision/26.124-an-argument-parser-as-an-evaluation-of-the-text-machinery.md`.

### 26.125 The bound that was stated once and readable nowhere

Moved to `doc/decision/26.125-the-bound-that-was-stated-once-and-readable-nowhere.md`.

### 26.126 The two defects the argv exercise left behind

Moved to `doc/decision/26.126-the-two-defects-the-argv-exercise-left-behind.md`.

### 26.127 The refusal the survey promised

Moved to `doc/decision/26.127-the-refusal-the-survey-promised.md`.

### 26.128 The field that says which key

Moved to `doc/decision/26.128-the-field-that-says-which-key.md`.

### 26.129 The signature keeps only kept promises

Moved to `doc/decision/26.129-the-signature-keeps-only-kept-promises.md`.

### 26.130 The key that was never allowed to be wrong, made wider

Moved to `doc/decision/26.130-the-key-that-was-never-allowed-to-be-wrong-made-wider.md`.

### 26.131 Three answers to the consumers

Moved to `doc/decision/26.131-three-answers-to-the-consumers.md`.

### 26.132 The floor stated, and the clamp that was checked by nothing

Moved to `doc/decision/26.132-the-floor-stated-and-the-clamp-that-was-checked-by-nothing.md`.

### 26.133 The session, folded

Moved to `doc/decision/26.133-the-session-folded.md`.

### 26.134 pad_to, built across the stack

Moved to `doc/decision/26.134-pad-to-built-across-the-stack.md`.

### 26.135 utf16, and the byte view a wide run never had

Moved to `doc/decision/26.135-utf16-and-the-byte-view-a-wide-run-never-had.md`.

### 26.136 Nine drivers, and the five bugs they found

Moved to `doc/decision/26.136-nine-drivers-and-the-five-bugs-they-found.md`.

### 26.137 Scope: what a protocol needs, situ implements

Moved to `doc/decision/26.137-scope-what-a-protocol-needs-situ-implements.md`.

### 26.138 A delimiter is a byte, and now it can be written

Moved to `doc/decision/26.138-a-delimiter-is-a-byte-and-now-it-can-be-written.md`.

### 26.139 8b10b is not a table code, and was listed as one in four places

Moved to `doc/decision/26.139-8b10b-is-not-a-table-code-and-was-listed-as-one-in-four-places.md`.

### 26.140 NRZI was in the same list, and the family that has it already did

Moved to `doc/decision/26.140-nrzi-was-in-the-same-list-and-the-family-that-has-it-already-did.md`.

### 26.141 `make fuzz` run, and what "no crash in 36 harnesses" is worth

Moved to `doc/decision/26.141-make-fuzz-run-and-what-no-crash-in-36-harnesses-is-worth.md`.

### 26.142 State between items, which three protocols have wanted this week

Moved to `doc/decision/26.142-state-between-items-which-three-protocols-have-wanted-this-week.md`.

### 26.143 What the CI history is evidence of, which is two things

Moved to `doc/decision/26.143-what-the-ci-history-is-evidence-of-which-is-two-things.md`.

### 26.144 What is open, and what was tried and did not work

Moved to `doc/decision/26.144-what-is-open-and-what-was-tried-and-did-not-work.md`.

### 26.145 What stands behind the last thirty commits

Moved to `doc/decision/26.145-what-stands-behind-the-last-thirty-commits.md`.

### 26.146 USB's NRZI, and a gap that was filed as a decision

Moved to `doc/decision/26.146-usb-s-nrzi-and-a-gap-that-was-filed-as-a-decision.md`.

### 26.147 A named code's overhead is the code's, and three declare it short

Moved to `doc/decision/26.147-a-named-code-s-overhead-is-the-code-s-and-three-declare-it-short.md`.

### 26.148 USB's bit stuffing, and the whole of its line coding

Moved to `doc/decision/26.148-usb-s-bit-stuffing-and-the-whole-of-its-line-coding.md`.

### 26.149 The partial symbol three table codes encoded half of

Moved to `doc/decision/26.149-the-partial-symbol-three-table-codes-encoded-half-of.md`.

### 26.150 The axis the scrambler pair was built to demonstrate, measured

Moved to `doc/decision/26.150-the-axis-the-scrambler-pair-was-built-to-demonstrate-measured.md`.

### 26.151 The README, and which direction a front page goes stale in

Moved to `doc/decision/26.151-the-readme-and-which-direction-a-front-page-goes-stale-in.md`.

### 26.152 CI, and the half of the floor gate nobody was holding

Moved to `doc/decision/26.152-ci-and-the-half-of-the-floor-gate-nobody-was-holding.md`.

### 26.153 The first CI run in forty commits, and what it caught

Moved to `doc/decision/26.153-the-first-ci-run-in-forty-commits-and-what-it-caught.md`.

### 26.154 The first green in three weeks, and what it is worth

Moved to `doc/decision/26.154-the-first-green-in-three-weeks-and-what-it-is-worth.md`.

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

Moved to `doc/decision/26.160-one-rule-four-constructs-and-it-was-written-on-one.md`.

### 26.161 The arguments only the emitter reads, and their defaults

Moved to `doc/decision/26.161-the-arguments-only-the-emitter-reads-and-their-defaults.md`.

### 26.162 A caveat in the position of a reason, and the cache that hid a sabotage

Moved to `doc/decision/26.162-a-caveat-in-the-position-of-a-reason-and-the-cache-that-hid-a-sabotage.md`.

### 26.163 A fix that stayed in the rule that produced it

Moved to `doc/decision/26.163-a-fix-that-stayed-in-the-rule-that-produced-it.md`.

### 26.164 A bound that parsed, changed nothing, and asked for itself again

Moved to `doc/decision/26.164-a-bound-that-parsed-changed-nothing-and-asked-for-itself-again.md`.

### 26.165 The cheapest number in the range, under the name of the dearest

Moved to `doc/decision/26.165-the-cheapest-number-in-the-range-under-the-name-of-the-dearest.md`.

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

Moved to `doc/decision/26.171-make-test-was-credited-with-three-things-it-has-never-run.md`.

### 26.172 A struct's size was printed as its minimum, with nothing to say so

Moved to `doc/decision/26.172-a-struct-s-size-was-printed-as-its-minimum-with-nothing-to-say-so.md`.

### 26.173 The fix left the same defect one `if` away from itself

Moved to `doc/decision/26.173-the-fix-left-the-same-defect-one-if-away-from-itself.md`.

### 26.174 What a `file` target would actually change, measured

Moved to `doc/decision/26.174-what-a-file-target-would-actually-change-measured.md`.

### 26.175 The `file` target, built

Moved to `doc/decision/26.175-the-file-target-built.md`.

### 26.176 The axis moved and the header said nothing

Moved to `doc/decision/26.176-the-axis-moved-and-the-header-said-nothing.md`.

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

Moved to `doc/decision/26.183-the-dissector-read-the-located-member-wherever-the-walk-had-reached.md`.

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

Moved to `doc/decision/26.191-the-third-construct-of-a-class-the-signature-knew-about.md`.

### 26.192 Two sections under one tag, and the pool that shadowed the pool

Moved to `doc/decision/26.192-two-sections-under-one-tag-and-the-pool-that-shadowed-the-pool.md`.

### 26.193 The image's own schema said version 2, and nothing had ever asked

Moved to `doc/decision/26.193-the-image-s-own-schema-said-version-2-and-nothing-had-ever-asked.md`.

### 26.194 Half a gate nobody had watched fail

Moved to `doc/decision/26.194-half-a-gate-nobody-had-watched-fail.md`.

### 26.195 Two witnesses, one hand: the round trip that could not see ten fields

Moved to `doc/decision/26.195-two-witnesses-one-hand-the-round-trip-that-could-not-see-ten-fields.md`.

### 26.196 The one attribute whose whole purpose is to give up a guarantee

Moved to `doc/decision/26.196-the-one-attribute-whose-whole-purpose-is-to-give-up-a-guarantee.md`.

### 26.197 The piece 26.188 said was owed

Moved to `doc/decision/26.197-the-piece-26-188-said-was-owed.md`.

### 26.198 Five rows checked, one not, and the sixth is the one that drifted

Moved to `doc/decision/26.198-five-rows-checked-one-not-and-the-sixth-is-the-one-that-drifted.md`.

### 26.199 Two claims the falsifier never attacked, and never said so

Moved to `doc/decision/26.199-two-claims-the-falsifier-never-attacked-and-never-said-so.md`.

### 26.200 One source line, two answers, and the editor picked one

Moved to `doc/decision/26.200-one-source-line-two-answers-and-the-editor-picked-one.md`.

### 26.201 The dissector was the description nobody compared, and it was wrong twice

Moved to `doc/decision/26.201-the-dissector-was-the-description-nobody-compared-and-it-was-wrong-twice.md`.

### 26.202 `pad_to` aligns from a base 0043 does not name, and nothing has asked

Moved to `doc/decision/26.202-pad-to-aligns-from-a-base-0043-does-not-name-and-nothing-has-asked.md`.

### 26.203 Two grammars, one hand, and nine spellings neither knew

Moved to `doc/decision/26.203-two-grammars-one-hand-and-nine-spellings-neither-knew.md`.

### 26.204 The oracle that existed to avoid situ-against-situ, and one family it never reached

Moved to `doc/decision/26.204-the-oracle-that-existed-to-avoid-situ-against-situ-and-one-family-it-never-reached.md`.

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

Moved to `doc/decision/26.218-the-register-settled-five-closed-and-three-that-are-not-mine.md`.

### 26.219 The refusal was not strictness, it was the only rule five can hold

Moved to `doc/decision/26.219-the-refusal-was-not-strictness-it-was-the-only-rule-five-can-hold.md`.

### 26.220 The shift rule, built, and the width that is not the field's

Moved to `doc/decision/26.220-the-shift-rule-built-and-the-width-that-is-not-the-field-s.md`.

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

Moved to `doc/decision/26.226-two-sweeps-that-found-nothing-and-the-description-that-was-right.md`.

### 26.227 A rule stated twice, tested once, and implemented nowhere

Moved to `doc/decision/26.227-a-rule-stated-twice-tested-once-and-implemented-nowhere.md`.

### 26.228 `at expr` measured from the wrong base

Moved to `doc/decision/26.228-at-expr-measured-from-the-wrong-base.md`.

### 26.229 The construct seam, followed to three clean answers

Moved to `doc/decision/26.229-the-construct-seam-followed-to-three-clean-answers.md`.

### 26.230 Two constructs accepted, and what each is waiting on

Moved to `doc/decision/26.230-two-constructs-accepted-and-what-each-is-waiting-on.md`.

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

Moved to `doc/decision/26.239-the-construct-that-seals-names-its-algorithm-the-one-that-summarises-cannot.md`.

### 26.240 A magic is one comparison now, in four backends and not the fifth

Moved to `doc/decision/26.240-a-magic-is-one-comparison-now-in-four-backends-and-not-the-fifth.md`.

### 26.241 The image carries the bytes, and a preamble states its content

Moved to `doc/decision/26.241-the-image-carries-the-bytes-and-a-preamble-states-its-content.md`.

### 26.242 A signature has no byte order, so the enum has no integer

Moved to `doc/decision/26.242-a-signature-has-no-byte-order-so-the-enum-has-no-integer.md`.

### 26.243 The checksum computes itself, in the two backends that can

Moved to `doc/decision/26.243-the-checksum-computes-itself-in-the-two-backends-that-can.md`.

### 26.244 Writing one real example found three bugs nothing else could

Moved to `doc/decision/26.244-writing-one-real-example-found-three-bugs-nothing-else-could.md`.

### 26.245 Every CRC anybody uses, and not the sum on almost every packet

Moved to `doc/decision/26.245-every-crc-anybody-uses-and-not-the-sum-on-almost-every-packet.md`.

### 26.246 Three languages, one polynomial

Moved to `doc/decision/26.246-three-languages-one-polynomial.md`.

### 26.247 The hole becomes a parameter, and IPv4 computes its own checksum

Moved to `doc/decision/26.247-the-hole-becomes-a-parameter-and-ipv4-computes-its-own-checksum.md`.

### 26.248 Two spans, and the last two IP checksums bind

Moved to `doc/decision/26.248-two-spans-and-the-last-two-ip-checksums-bind.md`.

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

Moved to `doc/decision/26.254-one-construct-six-descriptions-and-five-of-them-wrong.md`.

### 26.255 A byte has no byte order

Moved to `doc/decision/26.255-a-byte-has-no-byte-order.md`.

### 26.256 A run that is not unrolled does not move the cursor

Moved to `doc/decision/26.256-a-run-that-is-not-unrolled-does-not-move-the-cursor.md`.

### 26.257 The traceback respec reported, still available under another name

Moved to `doc/decision/26.257-the-traceback-respec-reported-still-available-under-another-name.md`.

### 26.258 Refused, and nothing refused

Moved to `doc/decision/26.258-refused-and-nothing-refused.md`.

### 26.259 One schema, one language building it and three raising

Moved to `doc/decision/26.259-one-schema-one-language-building-it-and-three-raising.md`.

### 26.260 A reader calling the function its own header declined to write

Moved to `doc/decision/26.260-a-reader-calling-the-function-its-own-header-declined-to-write.md`.

### 26.261 The axis was enumerated and the member was always data-sized

Moved to `doc/decision/26.261-the-axis-was-enumerated-and-the-member-was-always-data-sized.md`.

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

Moved to `doc/decision/26.272-generated-code-the-size-of-hand-written-except-where-it-was-3x.md`.

### 26.273 A bound the descriptions disagreed about, behind a subcommand split

Moved to `doc/decision/26.273-a-bound-the-descriptions-disagreed-about-behind-a-subcommand-split.md`.

### 26.274 The conversion the accessor applies and the checker beside it skips

Moved to `doc/decision/26.274-the-conversion-the-accessor-applies-and-the-checker-beside-it-skips.md`.

### 26.275 A round trip cannot see a conversion dropped from both sides

Moved to `doc/decision/26.275-a-round-trip-cannot-see-a-conversion-dropped-from-both-sides.md`.

### 26.276 A capability withheld for a reason that was false

Moved to `doc/decision/26.276-a-capability-withheld-for-a-reason-that-was-false.md`.

### 26.277 The library situ installed and gave nobody a way to name

Moved to `doc/decision/26.277-the-library-situ-installed-and-gave-nobody-a-way-to-name.md`.

### 26.278 The examples ship, and become a library by being importable

Moved to `doc/decision/26.278-the-examples-ship-and-become-a-library-by-being-importable.md`.

### 26.279 Three breakages in `make test-c`, stacked behind each other

Moved to `doc/decision/26.279-three-breakages-in-make-test-c-stacked-behind-each-other.md`.

### 26.280 A guard aimed wider than its subject, and two that hid each other

Moved to `doc/decision/26.280-a-guard-aimed-wider-than-its-subject-and-two-that-hid-each-other.md`.

### 26.281 Bounded recursion, and the one line that was doing the refusing

Moved to `doc/decision/26.281-bounded-recursion-and-the-one-line-that-was-doing-the-refusing.md`.

### 26.282 A proxy that agreed with the thing for every schema in the corpus

Moved to `doc/decision/26.282-a-proxy-that-agreed-with-the-thing-for-every-schema-in-the-corpus.md`.

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

Moved to `doc/decision/26.294-a-check-that-fits-in-one-byte-and-a-corpus-that-does-not.md`.

### 26.295 A directive is a claim about the file it is written in

Moved to `doc/decision/26.295-a-directive-is-a-claim-about-the-file-it-is-written-in.md`.

### 26.296 The packer resolved a character against the wrong file

Moved to `doc/decision/26.296-the-packer-resolved-a-character-against-the-wrong-file.md`.

### 26.297 A text number the schema declared signed, and nothing read

Moved to `doc/decision/26.297-a-text-number-the-schema-declared-signed-and-nothing-read.md`.

### 26.298 The trim set was HTTP's, in a compiler that is not HTTP's

Moved to `doc/decision/26.298-the-trim-set-was-http-s-in-a-compiler-that-is-not-http-s.md`.

### 26.299 The gap named the construct that closed it

Moved to `doc/decision/26.299-the-gap-named-the-construct-that-closed-it.md`.

### 26.300 Two attributes in one file, one question, two answers

Moved to `doc/decision/26.300-two-attributes-in-one-file-one-question-two-answers.md`.

### 26.301 A generated comment that named the wrong one of a pair

Moved to `doc/decision/26.301-a-generated-comment-that-named-the-wrong-one-of-a-pair.md`.

### 26.302 The differential could not see a delimited member's length

Moved to `doc/decision/26.302-the-differential-could-not-see-a-delimited-member-s-length.md`.

### 26.303 A reason that was true when it was written

Moved to `doc/decision/26.303-a-reason-that-was-true-when-it-was-written.md`.

### 26.304 A private copy of a decision the layer owns

Moved to `doc/decision/26.304-a-private-copy-of-a-decision-the-layer-owns.md`.

### 26.305 The members nothing compared were the ones that underflow

Moved to `doc/decision/26.305-the-members-nothing-compared-were-the-ones-that-underflow.md`.

### 26.306 A check on every access that nothing can make fail

Moved to `doc/decision/26.306-a-check-on-every-access-that-nothing-can-make-fail.md`.

### 26.307 The check is real now, and one line is why it was not

Moved to `doc/decision/26.307-the-check-is-real-now-and-one-line-is-why-it-was-not.md`.

### 26.308 A sweep that was wrong five times out of six

Moved to `doc/decision/26.308-a-sweep-that-was-wrong-five-times-out-of-six.md`.

### 26.309 A differential that disagreed about a value nobody disputes

Moved to `doc/decision/26.309-a-differential-that-disagreed-about-a-value-nobody-disputes.md`.

### 26.310 Five more copies of one rule, and a suite I had not run

Moved to `doc/decision/26.310-five-more-copies-of-one-rule-and-a-suite-i-had-not-run.md`.

### 26.311 Two gates in `make check` that nobody had run

Moved to `doc/decision/26.311-two-gates-in-make-check-that-nobody-had-run.md`.

### 26.312 The same omission, one document over

Moved to `doc/decision/26.312-the-same-omission-one-document-over.md`.

### 26.313 An empty sweep, recorded because it is one

Moved to `doc/decision/26.313-an-empty-sweep-recorded-because-it-is-one.md`.

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

Moved to `doc/decision/26.321-the-fix-with-no-test-found-by-listing-them.md`.

### 26.322 What the fuzzers found the moment they could see

Moved to `doc/decision/26.322-what-the-fuzzers-found-the-moment-they-could-see.md`.

### 26.323 CI had been red for five days, and nobody had looked

Moved to `doc/decision/26.323-ci-had-been-red-for-five-days-and-nobody-had-looked.md`.

### 26.324 The other reading of the layout, never fuzzed

Moved to `doc/decision/26.324-the-other-reading-of-the-layout-never-fuzzed.md`.

### 26.325 C++ was the backend nobody fuzzed, and it had two

Moved to `doc/decision/26.325-c-was-the-backend-nobody-fuzzed-and-it-had-two.md`.

### 26.326 Two empty sweeps and a detector worth keeping

Moved to `doc/decision/26.326-two-empty-sweeps-and-a-detector-worth-keeping.md`.

### 26.327 A vocabulary for text protocols, to be designed

Moved to `doc/decision/26.327-a-vocabulary-for-text-protocols-to-be-designed.md`.

### 26.328 The harness that could not call the accessor it was for

Moved to `doc/decision/26.328-the-harness-that-could-not-call-the-accessor-it-was-for.md`.

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

Moved to `doc/decision/26.344-the-c-walker-s-tlv-and-indexed-counts.md`.

### 26.345 A stale sentence that read as a design limit

Moved to `doc/decision/26.345-a-stale-sentence-that-read-as-a-design-limit.md`.

### 26.346 A field that was `none` in every image ever written

Moved to `doc/decision/26.346-a-field-that-was-none-in-every-image-ever-written.md`.

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

Moved to `doc/decision/26.353-tonumber-is-lua-s-rule-not-situ-s-and-the-clamp-was-one-symptom.md`.

### 26.354 The C walker learns which arm a discriminant selects

Moved to `doc/decision/26.354-the-c-walker-learns-which-arm-a-discriminant-selects.md`.

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

Moved to `doc/decision/26.362-define-and-why-it-edits-the-source-rather-than-the-tree.md`.

### 26.363 Asking the two walkers WHY, and the BCD the C walk never had

Moved to `doc/decision/26.363-asking-the-two-walkers-why-and-the-bcd-the-c-walk-never-had.md`.

### 26.364 The editor says which field the schema refuses over

Moved to `doc/decision/26.364-the-editor-says-which-field-the-schema-refuses-over.md`.

### 26.365 Reading the diagnostics nobody produces

Moved to `doc/decision/26.365-reading-the-diagnostics-nobody-produces.md`.

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

Moved to `doc/decision/26.376-example-usb-and-what-three-bytes-took.md`.

### 26.377 `when`: the construct, and the two descriptions that carry it

Moved to `doc/decision/26.377-when-the-construct-and-the-two-descriptions-that-carry-it.md`.

### 26.378 The messages an image carries, and who runs them

Moved to `doc/decision/26.378-the-messages-an-image-carries-and-who-runs-them.md`.

### 26.379 Expert info, which is what a `when` already was

Moved to `doc/decision/26.379-expert-info-which-is-what-a-when-already-was.md`.

### 26.380 The messages sibling, in C

Moved to `doc/decision/26.380-the-messages-sibling-in-c.md`.

### 26.381 The other three backends, and one number in four spellings

Moved to `doc/decision/26.381-the-other-three-backends-and-one-number-in-four-spellings.md`.

### 26.382 A refusal, in all six descriptions at once

Moved to `doc/decision/26.382-a-refusal-in-all-six-descriptions-at-once.md`.

### 26.383 Which member refused, in C++ -- and what comparing two backends found

Moved to `doc/decision/26.383-which-member-refused-in-c-and-what-comparing-two-backends-found.md`.

### 26.384 The same identity in Rust, and a fault the comparison found twice more

Moved to `doc/decision/26.384-the-same-identity-in-rust-and-a-fault-the-comparison-found-twice-more.md`.

### 26.385 The identity in Python, and the coincidence that passed a sabotage

Moved to `doc/decision/26.385-the-identity-in-python-and-the-coincidence-that-passed-a-sabotage.md`.

### 26.386 `parameter`: the construct, and a guard that guarded nothing

Moved to `doc/decision/26.386-parameter-the-construct-and-a-guard-that-guarded-nothing.md`.

### 26.387 A note would have been worse than a refusal

Moved to `doc/decision/26.387-a-note-would-have-been-worse-than-a-refusal.md`.

### 26.388 The README rule, made mechanical -- and what it found

Moved to `doc/decision/26.388-the-readme-rule-made-mechanical-and-what-it-found.md`.

### 26.389 `situ verify` says what the schema says

Moved to `doc/decision/26.389-situ-verify-says-what-the-schema-says.md`.

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

Moved to `doc/decision/26.399-a-generated-header-that-did-not-compile-found-in-passing.md`.

### 26.404 `situc explain` places a member that occupies nothing

Moved to `doc/decision/26.404-situc-explain-places-a-member-that-occupies-nothing.md`.

### 26.400 A refusal test whose population went empty, and its two detectors

Moved to `doc/decision/26.400-a-refusal-test-whose-population-went-empty-and-its-two-detectors.md`.

### 26.401 A refusal message that outlived what it refused

Moved to `doc/decision/26.401-a-refusal-message-that-outlived-what-it-refused.md`.

### 26.403 Both walkers take the argument, and where each one puts it

Moved to `doc/decision/26.403-both-walkers-take-the-argument-and-where-each-one-puts-it.md`.

### 26.402 The corpus struct costs more than 0050 says, and the tree has the precedent

Moved to `doc/decision/26.402-the-corpus-struct-costs-more-than-0050-says-and-the-tree-has-the-precedent.md`.

### 26.407 Skipping the struct rather than the schema, in checks and fuzz

Moved to `doc/decision/26.407-skipping-the-struct-rather-than-the-schema-in-checks-and-fuzz.md`.

### 26.408 A uniform tail is a rule with two halves, and only one was applied

Moved to `doc/decision/26.408-a-uniform-tail-is-a-rule-with-two-halves-and-only-one-was-applied.md`.

### 26.716 A gate that prints a finding and passes

Moved to
`doc/decision/26.716-a-gate-that-prints-a-finding-and-passes.md`.

### 26.717 The thunk that reached one walker of two

Moved to `doc/decision/26.717-the-thunk-that-reached-one-walker-of-two.md`.

### 26.718 The harness overwrote the image it was comparing against

Moved to
`doc/decision/26.718-the-harness-overwrote-the-image-it-was-comparing-against.md`.

### 26.719 The program walked a message nobody gave it

Moved to
`doc/decision/26.719-the-program-walked-a-message-nobody-gave-it.md`.

### 26.720 The sweep compiled a driver for every packet

Moved to
`doc/decision/26.720-the-sweep-compiled-a-driver-for-every-packet.md`.

### 26.721 What the two walkers read, asked over the corpus

Moved to
`doc/decision/26.721-what-the-two-walkers-read-asked-over-the-corpus.md`.

### 26.722 The image ceilings beside the message ones

Moved to
`doc/decision/26.722-the-image-ceilings-beside-the-message-ones.md`.

### 26.723 The floor 26.693 removed was also a bounds guard

Moved to
`doc/decision/26.723-the-floor-was-also-a-bounds-guard.md`.

### 26.724 The run walk credited a constraint it could not enforce

Moved to
`doc/decision/26.724-the-run-walk-credited-a-constraint-it-could-not-enforce.md`.

### 26.725 The framing layer had no corpus-wide execution

Moved to
`doc/decision/26.725-the-framing-layer-had-no-corpus-wide-execution.md`.

### 26.726 `have + 1` discarded the total the walk already had

Moved to
`doc/decision/26.726-have-plus-one-discarded-the-total-the-walk-had.md`.

### 26.727 The counted run the tree did not have

Moved to
`doc/decision/26.727-the-counted-run-the-tree-did-not-have.md`.

### 26.728 Rust's frame layer was the one nothing compiled

Moved to
`doc/decision/26.728-only-c-s-frame-layer-was-ever-compiled.md`.

### 26.729 C framed a TLV region at zero bytes, and the Reader looped

Moved to
`doc/decision/26.729-c-framed-a-tlv-region-at-zero-bytes.md`.

### 26.730 The pad term shortened the answer to what had arrived

Moved to
`doc/decision/26.730-the-pad-term-shortened-the-answer-to-what-arrived.md`.

### 26.731 Three emitters gained C's pad branch

Moved to
`doc/decision/26.731-three-emitters-gained-c-s-pad-branch.md`.

### 26.732 Rust framed to a different ceiling

Moved to
`doc/decision/26.732-rust-framed-to-a-different-ceiling.md`.

### 26.733 C answered zero elements for a run it could not count

Moved to
`doc/decision/26.733-c-answered-zero-elements-for-a-run-it-could-not-count.md`.

### 26.734 A counted run of fixed-size records was framed as one

Moved to
`doc/decision/26.734-a-counted-run-of-fixed-records-was-framed-as-one.md`.

### 26.735 Validate held a varint run against how far it reached

Moved to
`doc/decision/26.735-validate-held-a-varint-run-against-how-far-it-reached.md`.

### 26.736 A `[remaining]` run dropped what it could not divide

Moved to
`doc/decision/26.736-a-remaining-run-dropped-what-it-could-not-divide.md`.

### 26.737 Nothing validated a `tlv` region

Moved to `doc/decision/26.737-nothing-validated-a-tlv-region.md`.

### 26.738 `unknown = error` was the default, and nothing enforced it

Moved to
`doc/decision/26.738-unknown-error-was-the-default-and-nothing-enforced-it.md`.

### 26.739 Rust's `check_which` named a member that passed

Moved to
`doc/decision/26.739-rust-check-which-named-a-member-that-passed.md`.

### 26.740 `duplicate_tags = error` was the default, and nothing enforced it

Moved to
`doc/decision/26.740-duplicate-tags-error-was-the-default-and-nothing-enforced-it.md`.

### 26.715 The same width in the other pointer backend

Moved to
`doc/decision/26.715-the-same-width-in-the-other-pointer-backend.md`.

### 26.714 A declared width is not a width the frame has

Moved to
`doc/decision/26.714-a-declared-width-is-not-a-width-the-frame-has.md`.

### 26.713 The fifth family the lesson did not reach

Moved to
`doc/decision/26.713-the-fifth-family-the-lesson-did-not-reach.md`.

### 26.712 Six hundred and forty-three dates

Moved to `doc/decision/26.712-six-hundred-and-forty-three-dates.md`.

### 26.711 A declaration equal to its default was silence

Moved to
`doc/decision/26.711-a-declaration-equal-to-its-default-was-silence.md`.

### 26.710 The one property no kernel could contradict

Moved to
`doc/decision/26.710-the-one-property-no-kernel-could-contradict.md`.

### 26.709 The class moves and the reference did not

Moved to
`doc/decision/26.709-the-class-moves-and-the-reference-did-not.md`.

### 26.708 A covered span that reaches the argument

Moved to
`doc/decision/26.708-a-covered-span-that-reaches-the-argument.md`.

### 26.707 gen-tests was the fifth in the family and did not refuse

Moved to
`doc/decision/26.707-gen-tests-was-the-fifth-in-the-family-and-did-not-refuse.md`.

### 26.706 One spelling for a parameter's argument

Moved to
`doc/decision/26.706-one-spelling-for-a-parameters-argument.md`.

### 26.705 Two name collisions whose fix is a decision

Moved to
`doc/decision/26.705-two-name-collisions-whose-fix-is-a-decision.md`.

### 26.704 Two Python guards matched by a member's own name

Moved to
`doc/decision/26.704-two-python-guards-matched-by-a-members-own-name.md`.

### 26.703 A type name that begins with a runtime name

Moved to
`doc/decision/26.703-a-type-name-that-begins-with-a-runtime-name.md`.

### 26.702 A member name in a comment declared a local

Moved to
`doc/decision/26.702-a-member-name-in-a-comment-declared-a-local.md`.

### 26.701 The C offset cache took no argument tail

Moved to
`doc/decision/26.701-the-c-offset-cache-took-no-argument-tail.md`.

### 26.700 One accessor, two spellings

Moved to `doc/decision/26.700-one-accessor-two-spellings.md`.

### 26.699 `--materialize` had no corpus-wide compile

Moved to
`doc/decision/26.699-materialize-had-no-corpus-wide-compile.md`.

### 26.698 A rewriter's own exceptions, matched by a member

Moved to
`doc/decision/26.698-a-rewriters-own-exceptions-matched-by-a-member.md`.

### 26.697 The offset cache was outside the warnings gate

Moved to
`doc/decision/26.697-the-offset-cache-was-outside-the-warnings-gate.md`.

### 26.696 A member named for a builtin crashed two backends

Moved to
`doc/decision/26.696-a-member-named-for-a-builtin-crashed-two-backends.md`.

### 26.695 Every name the generator declares, swept

Moved to `doc/decision/26.695-every-name-the-generator-declares.md`.

### 26.694 A member named for the view it is read through

Moved to
`doc/decision/26.694-a-member-named-for-the-view-it-is-read-through.md`.

### 26.693 `required` answered from bytes past the frame it named

Moved to
`doc/decision/26.693-required-answered-from-bytes-past-the-frame-it-named.md`.

### 26.692 Three frame layers nothing ever built

Moved to
`doc/decision/26.692-three-frame-layers-nothing-ever-built.md`.

### 26.691 A row stated Bounded for a member with no maximum

Moved to
`doc/decision/26.691-a-row-stated-bounded-for-a-member-with-no-maximum.md`.

### 26.690 A memory-safety closure nobody had tested

Moved to
`doc/decision/26.690-a-memory-safety-closure-nobody-had-tested.md`.

### 26.689 A member after something nobody can measure

Moved to
`doc/decision/26.689-a-member-after-something-nobody-can-measure.md`.

### 26.688 A coded region's extent landed between bytes

Moved to
`doc/decision/26.688-a-coded-regions-extent-landed-between-bytes.md`.

### 26.687 The image did not say what a varint's bytes mean

Moved to
`doc/decision/26.687-the-image-did-not-say-what-a-varints-bytes-mean.md`.

### 26.686 A varint's length answers zero, and `required` read it

Moved to
`doc/decision/26.686-a-varints-length-answers-zero-and-required-read-it.md`.

### 26.685 The framing total was used as the element offset

Moved to
`doc/decision/26.685-the-framing-total-was-used-as-the-element-offset.md`.

### 26.684 `framed` reads an extent, and an extent shrinks

Moved to
`doc/decision/26.684-framed-reads-an-extent-and-an-extent-shrinks.md`.

### 26.683 A run of varints was one varint, in all six readers

Moved to
`doc/decision/26.683-a-run-of-varints-was-one-varint-in-all-six-readers.md`.

### 26.682 A delegated check was found only one component deep

Moved to
`doc/decision/26.682-a-delegated-check-was-found-only-one-component-deep.md`.

### 26.681 A declared cap was not checked after a dynamic member

Moved to
`doc/decision/26.681-a-declared-cap-was-not-checked-after-a-dynamic-member.md`.

### 26.680 A delimited run of records had no span, so a nested one lost an element

Moved to `doc/decision/26.680-a-delimited-run-of-records-had-no-span.md`.

### 26.679 A cycle across an import emitted its call ahead of its declaration

Moved to `doc/decision/26.679-a-cycle-across-an-import-emitted-its-call-ahead-of-its-declaration.md`.

### 26.678 `framed` and `required` read past the buffer

Moved to `doc/decision/26.678-framed-and-required-read-past-the-buffer.md`.

### 26.677 A wider check field lets undeclared bits decide the verdict

Moved to `doc/decision/26.677-a-wider-check-field-lets-undeclared-bits-decide.md`.

### 26.676 `validate` accepts a run the frame cannot hold

Moved to `doc/decision/26.676-validate-accepts-a-run-the-frame-cannot-hold.md`.

### 26.675 `validate()` read past the buffer, and why nothing caught it

Moved to `doc/decision/26.675-validate-read-past-the-buffer-and-why-nothing-caught-it.md`.

### 26.674 A checksum field holds what its code produces

Moved to `doc/decision/0067-a-checksum-field-holds-what-its-code-produces.md`.

### 26.673 A truncated checksum cannot verify, in any of the four backends

Moved to `doc/decision/26.673-a-truncated-checksum-cannot-verify-in-any-backend.md`.

### 26.672 The bills are dated, and the population is nine rather than 63

Moved to `doc/decision/26.672-the-bills-are-dated-and-the-population-is-nine.md`.

### 26.671 A schema's bill is a dated claim about the compiler

Moved to `doc/decision/26.671-a-schema-s-bill-is-a-dated-claim-about-the-compiler.md`.

### 26.670 An escaped span frames, and where decoding would live

Moved to `doc/decision/26.670-an-escaped-span-frames-and-where-decoding-would-live.md`.

### 26.669 A refusal that named one of three spellings

Moved to `doc/decision/26.669-a-refusal-that-named-one-of-three-spellings.md`.

### 26.668 The adopter path is three gates, and none substitutes

Moved to `doc/decision/26.668-the-adopter-path-is-three-gates.md`.

### 26.667 What `at <literal>` is, and the offset axis has no value for it

Moved to `doc/decision/26.667-what-at-a-literal-is-and-the-offset-axis-has-no-value-for-it.md`.

### 26.666 The CoAP example, and the three faults it found

Moved to `doc/decision/26.666-the-coap-example-and-the-three-faults-it-found.md`.

### 26.665 A tlv region had no byte order, and four backends disagreed

Moved to `doc/decision/26.665-a-tlv-region-had-no-byte-order-and-four-backends-disagreed.md`.

### 26.664 A `u8` tag is read as a ten-byte varint

Moved to `doc/decision/26.664-a-u8-tag-is-read-as-a-ten-byte-varint.md`.

### 26.663 Two absences, one item, and a sentence nobody read

Moved to `doc/decision/26.663-two-absences-one-item-and-a-sentence-nobody-read.md`.

### 26.662 A matrix the schema states, and an item parked on a wrong premise

Moved to `doc/decision/26.662-a-matrix-the-schema-states-and-an-item-parked-on-a-wrong-premise.md`.

### 26.661 A marker decides the order, and the owned form had baked one

Moved to `doc/decision/26.661-a-marker-decides-the-order-and-the-owned-form-had-baked-one.md`.

### 26.660 The literal-only builder, and a writer that could ship what its reader refuses

Moved to `doc/decision/26.660-the-literal-only-builder-and-a-writer-that-could-ship-what-its-reader-refuses.md`.

### 26.658 The reserved policy, spelled once, and the copy nobody counted

Moved to `doc/decision/26.658-the-reserved-policy-spelled-once-and-the-copy-that-was-not-counted.md`.

### 26.657 The temporary trees, and the policy rather than the sweep

Moved to `doc/decision/26.657-the-temporary-trees-and-the-policy-rather-than-the-sweep.md`.

### 26.656 The move, and three checks that did not cover what I cited

Moved to `doc/decision/26.656-the-move-and-three-checks-that-did-not-cover-what-i-cited.md`.

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

Moved to `doc/decision/26.637-fold-the-sweep-command-that-cannot-answer-zero-signalled.md`.

### 26.636 Fold: rung 2 writes, 160 to 180 structs, and the lens that replaces guessing

Moved to `doc/decision/26.636-fold-rung-2-writes-160-to-180-structs-and-the-lens-that-replaces-guessing.md`.

### 26.635 The writer over the composition space, and what the readers do there

Moved to `doc/decision/26.635-the-writer-over-the-composition-space-and-what-the-readers-do-there.md`.

### 26.634 A struct argument is the writer's parameter

Moved to `doc/decision/26.634-a-struct-argument-is-the-writer-s-parameter.md`.

### 26.633 A nested struct one byte wide is not a byte run

Moved to `doc/decision/26.633-a-nested-struct-one-byte-wide-is-not-a-byte-run.md`.

### 26.632 Escaping is forced, not invented -- and three copies of one cap

Moved to `doc/decision/26.632-escaping-is-forced-not-invented-and-three-copies-of-one-cap.md`.

### 26.631 Two writing primitives the runtime did not have

Moved to `doc/decision/26.631-two-writing-primitives-the-runtime-did-not-have.md`.

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

Moved to `doc/decision/26.624-r-n-is-one-delimiter-and-the-control-i-wrote-first-tested-the-bound.md`.

### 26.623 A varint, and the fourth time a branch ordered by absence misread a member

Moved to `doc/decision/26.623-a-varint-and-the-fourth-time-a-branch-ordered-by-absence-misread-a-member.md`.

### 26.622 A trailing checksum, computed where the caller has the whole message

Moved to `doc/decision/26.622-a-trailing-checksum-computed-where-the-caller-has-the-whole-message.md`.

### 26.621 The cut has no outlier to lead with, and that decides how it can be done

Moved to `doc/decision/26.621-the-cut-has-no-outlier-to-lead-with-and-that-decides-how-it-can-be-done.md`.

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

Moved to `doc/decision/26.613-the-frame-reader-hands-out-a-message-its-schema-forbids.md`.

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

Moved to `doc/decision/26.607-three-reproduced-codegen-faults-a-rung-nothing-compiles-and-26-604-qualified.md`.

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

Moved to `doc/decision/26.580-id3v2-4-and-the-scalar-kind-it-did-not-need.md`.

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

Moved to `doc/decision/26.561-access-sequential-names-a-class-and-reads-like-a-constant.md`.

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

Moved to `doc/decision/26.539-a-cap-on-a-remaining-run-and-the-unit-nobody-asked-about.md`.

### 26.538 `peek` in the C walker, and the comparison that never ran

Moved to `doc/decision/26.538-peek-in-the-c-walker-and-the-comparison-that-never-ran.md`.

### 26.537 The region's extent, back where the arithmetic is not a partition

Moved to `doc/decision/26.537-the-region-s-extent-back-where-the-arithmetic-is-not-a-partition.md`.

### 26.536 The register's field: built, measured, and not kept

Moved to `doc/decision/26.536-the-register-s-field-built-measured-and-not-kept.md`.

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

Moved to `doc/decision/26.504-a-hand-written-vector-would-have-agreed-with-the-bug.md`.

### 26.503 A green suite is not a green gate, and a skip is not a gap

Moved to `doc/decision/26.503-a-green-suite-is-not-a-green-gate-and-a-skip-is-not-a-gap.md`.

### 26.502 Nine corpora, and what committing them found

Moved to `doc/decision/26.502-nine-corpora-and-what-committing-them-found.md`.

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

Moved to `doc/decision/26.496-rust-renders-reed-solomon-and-the-blocker-had-already-gone.md`.

### 26.495 Keyword collisions are handled; the lens that found 26.490 is not

Moved to `doc/decision/26.495-keyword-collisions-are-handled-the-lens-that-found-26-490-is-not.md`.

### 26.494 A ranking that had to be total, and was not

Moved to `doc/decision/26.494-a-ranking-that-had-to-be-total-and-was-not.md`.

### 26.491 A line code that declares itself authenticated, and is believed

Moved to `doc/decision/26.491-a-line-code-that-declares-itself-authenticated-and-is-believed.md`.

### 26.492 `seekable = blockwise` does not merely misprice, it crashes

Moved to `doc/decision/26.492-seekable-blockwise-does-not-merely-misprice-it-crashes.md`.

### 26.493 Four axes blind at the default, and the base rates behind them

Moved to `doc/decision/26.493-four-axes-blind-at-the-default-and-the-base-rates-behind-them.md`.

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

Moved to `doc/decision/26.481-two-ordinals-a-later-insertion-falsified-and-one-overclaim.md`.

### 26.480 An accepted record the tree overruled by building the thing

Moved to `doc/decision/26.480-an-accepted-record-the-tree-overruled-by-building-the-thing.md`.

### 26.479 The floor above the reach, in a line the map publishes

Moved to `doc/decision/26.479-the-floor-above-the-reach-in-a-line-the-map-publishes.md`.

### 26.478 A count that was right twice, over two different populations

Moved to `doc/decision/26.478-a-count-that-was-right-twice-over-two-different-populations.md`.

### 26.477 Three claims in accepted records, corrected the tree's own way

Moved to `doc/decision/26.477-three-claims-in-accepted-records-corrected-the-tree-s-own-way.md`.

### 26.476 The conformance row that promised a test nobody generated

Moved to `doc/decision/26.476-the-conformance-row-that-promised-a-test-nobody-generated.md`.

### 26.475 A derived codec reached the clause that said it could not

Moved to `doc/decision/26.475-a-derived-codec-reached-the-clause-that-said-it-could-not.md`.

### 26.474 The wire signature recorded a spelling, not a value

Moved to `doc/decision/26.474-the-wire-signature-recorded-a-spelling-not-a-value.md`.

### 26.473 One name for two widths, declined

Moved to `doc/decision/26.473-one-name-for-two-widths-declined.md`.

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

Moved to `doc/decision/26.443-the-paren-depth-limit-recorded-not-fixed-and-why.md`.

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

Moved to `doc/decision/26.427-s-expressions-describe-in-twenty-lines-and-an-f-expr-is-not-a-format.md`.

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

Moved to `doc/decision/26.416-the-feature-s-own-bounds-hazard-in-three-flavours.md`.

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

Moved to `doc/decision/26.409-the-construct-is-in-the-corpus-and-what-it-took-to-get-there.md`.

### 26.405 Three refusals that were never needed, and a tripwire that proved it

Moved to `doc/decision/26.405-three-refusals-that-were-never-needed-and-a-tripwire-that-proved-it.md`.

### 26.406 Two written lists of one fact, and they had already drifted

Moved to `doc/decision/26.406-two-written-lists-of-one-fact-and-they-had-already-drifted.md`.

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
