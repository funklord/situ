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
   the decision in `docs/decisions/`. Do not silently invent semantics.
4. **When something in this document is ambiguous or contradictory, stop and
   ask.** Do not resolve ambiguity by guessing; a wrong guess about capability
   propagation is expensive to unwind later.
5. Everything in `docs/decisions/` is append-only. One file per decision,
   numbered, with the alternatives considered.
6. **Where this document can be checked against the code, it is.** The 11.3
   table and `docs/grammar.ebnf` both have tests that fail when they drift
   from the implementation, because 11.3 was hand-maintained for a while and
   fell about twenty rows behind. Prefer adding a check to adding a promise:
   a normative table nobody verifies is a comment.

---

## 1. What Situ is

Situ describes the exact byte layout of data that already has, or is being
designed to have, a specific binary representation: wire protocols, packet
formats, on-disk records, and memory-mapped hardware registers.

From a `.situ` schema, `situc` generates:

- accessor code (C first, Rust later) for reading and writing fields
- a **capability map**: for every field and region, which operations are
  possible and, when an operation is not possible, precisely why
- design advice: concrete, costed schema changes that would restore lost
  capabilities
- test scaffolding: golden-vector tests and a fuzz harness per schema

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
- **No dynamic allocation in generated code.** Ever. No `malloc`, no hidden
  arena, no growable buffers. Callers supply memory.
- **No recursive types in v0.** Recursive schema types make size and capability
  computation non-terminating. Rejected at parse time with a clear error.
- **Not a parser combinator library.** The schema is declarative and the layout
  solver is a compiler pass, not a runtime interpreter.
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
`docs/decisions/0010-region-and-tag-names.md`.

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

directive     = "target"     target_kind ";"
              | "endian"     endian ";"
              | "strictness" "=" strictness ";"
              | "bit_order"  bitorder ";"
              | "import"     string ";" ;

target_kind   = "buffer" | "mmio" ;
endian        = "big" | "little" | "native" ;
bitorder      = "msb_first" | "lsb_first" ;
strictness    = "strict" | "lenient" ;

decl          = const_decl | enum_decl | struct_decl | codec_decl
              | register_decl | requirement | invariant ;

(* One level; nesting is rejected naming its phase. A declaration inside is
   named `outer::header`, and an unqualified reference within the same
   namespace resolves there and nowhere else.
   docs/decisions/0012-namespaces.md *)
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

enum_decl     = "enum" ident ":" scalar_type "{" enum_body "}" ;
enum_body     = { ident "=" expr "," } [ "default" "=" ("error"|"pass") ] ;

struct_decl   = "struct" ident [ attrs ] "{" { member } "}" ;

member        = field | reserved | block | variant | tag_field ;

field         = [ radix ] type_ref ident [ array_spec ] [ until | repeat ]
                [ pin ] [ attrs ] ";" ;
repeat        = "while" "(" expr ")" [ "max" expr ] ;   (* section 8.6.6 *)
radix         = "decimal" | "hex" ;          (* section 8.6.2 *)
reserved      = "reserved" scalar_type [ array_spec ] [ attrs ] ";" ;
tag_field     = ( "tag" | "checksum" ) scalar_type [ ident ] array_spec
                [ "covers" "(" ref_list ")" ] [ attrs ] ";" ;

block         = "positional"    "{" { member } "}"
              | "authenticated" [ ident ] [ attrs ] "{" { member } "}"
              | "sealed" [ ident ] "(" codec_args ")" [ attrs ] "{" { member } "}"
              | "coded"  ident   "(" codec_args ")" [ until ] [ attrs ]
                "{" { member } "}"
              | "indexed" "(" index_args ")" "{" { member } "}"
              | "opaque" ident "[" size_expr "]" [ attrs ] ";"
              | "tlv" ident "(" tlv_args ")" [ attrs ] ";" ;

variant       = "variant" ident "switch" "(" expr ")" "{"
                { "case" expr ":" member }
                [ "default" ":" ( member | "error" | "opaque" ) ]
                "}" ;

array_spec    = "[" [ size_expr ] "]" ;
size_expr     = expr | "remaining" ;

(* Section 8.6.1. Where a member ends when no length says so. The
   delimiter is a string literal rather than an expression: it would
   otherwise have to be evaluated against the data it is being looked
   for in. `max` bounds the scan. *)
until         = "until" string [ "max" expr ] ;
pin           = "@" expr ;

attrs         = "[" attr { "," attr } "]" ;
attr          = ident [ "=" expr ] ;

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
   each use. See docs/decisions/0006-bracket-disambiguation.md. *)

type_ref      = scalar_type | ident ;
scalar_type   = uint | sint | float | bitfield | "bool" | "byte" ;
uint          = "u" digits ;          (* u1..u64, non-power-of-2 allowed *)
sint          = "i" digits ;
float         = "f16" | "f32" | "f64" ;
bitfield      = "bit" ;               (* single bit; uN < 8 also bit-packed *)

codec_decl    = "codec" ident "{" { codec_prop ";" } "}" ;
codec_prop    = "length_preserving"
              | "expansion" "=" ( "+" digits | "unbounded" | ratio )
              | "granularity" "=" ( "byte" | "block" "(" digits ")" | "stream" )
              | [ "not" ] "seekable"
              | "authenticated" | "invertible" | "deterministic" ;

requirement   = ( "require" | "assert" ) capability_expr ";" ;

(* One field, and what it derives from. The left side is a path rather
   than an expression: an invariant says which field situ maintains, and
   an equality nobody maintains is what `require` already is. *)
invariant     = "invariant" path "==" expr ";" ;
path          = ident { "." ident } ;
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
  width being refused. See `docs/decisions/0005-integer-widths.md`.
- **Fixed point**: `q<int>_<frac>` signed, `uq<int>_<frac>` unsigned. `q16_16`
  is 32 bits, sixteen of them fractional; `q1_15` is the audio convention. The
  width is the sum, so the bit-packing rule above applies unchanged -- `q4_4`
  is a byte and `q2_3` packs.
- **BCD**: `bcd<digits>`, a nibble to a digit, most significant first. `bcd2`
  is one byte and holds 0 to 99, which is what an RTC puts in a seconds
  register. Twelve bits for `bcd3`, so an odd digit count packs.
- Both cost `repr = ValueConverted`, for different reasons the map states: a
  fixed-point field's stored integer is the value scaled by a power of two, and
  a BCD field's nibbles are digits. Neither generates floating point -- the
  target may have none, and the scale is exact, so the header carries
  `_SCALE` and `_FRAC_BITS` and the caller does the arithmetic in whatever type
  it has.
- A BCD field can hold a bit pattern that is not a number: a nibble above nine.
  The getter cannot report that, so the generated validator checks it, which is
  the only type where reading and validating disagree about what is possible.

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

The advisor's varint suggestion (18.2) applies here: if a varint field carries a
`max` constraint, report the fixed-width alternative and its true cost. For
`max = 1500` a varint costs two bytes across most of its range anyway, so `u16`
is free and restores static offsets for everything after it.

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

Four forms, with sharply different capability consequences:

| Form | Size | Consequence |
|---|---|---|
| `T x[N]` | compile-time constant | fully static |
| `T x[expr]` | parse-time from a prior field | frame-relative static elements; everything after becomes dynamic |
| `T x[remaining]` | to end of enclosing frame | must be last in its frame |
| `T x[] until (cond)` | delimited | sequential access only; OPEN, defer to phase 6 |

Element type must have a fixed size for indexed access to be O(1). If the
element size is dynamic, the array is sequential-access only and the compiler
says so.

### 8.6 Strings and bytes

There is no string type. Text is `u8 name[N]` with an optional
`[encoding = ascii | utf8]` attribute that is validated on parse if
strictness is enabled, and a `[nul_terminated]` flag. Length-prefixed text is
just a `u8[]` with a size expression. This avoids inventing string semantics
the underlying protocol may not have.

**Both attributes are implemented.** `[encoding = ascii | utf8]` is validated
on parse, strictly in the sense RFC 3629 requires -- an overlong form or a
surrogate half is refused, because either is a second spelling of a character
that already has one. An encoding situ cannot check is an error rather than a
silent nod.

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
`until` made those unwriteable.

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

Signed and fractional text formats are refused. A sign or a point is a grammar
rather than a number, which is the same line drawn below.

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
`docs/decisions/0020-delimited-data.md`.

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
  to be compared. Each emits an `is_known` predicate over the declared members
  and calls it from `validate`, so a field declared to admit seven protocol
  numbers no longer accepts all 256. `default = pass` still emits the
  predicate, because a caller may want to ask, and does not call it -- a schema
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
proto_message.fields    access=Sequential  address=Unstable  mutate=RewriteRequired
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
| `offset` | `AbsoluteStatic(n)` > `FrameStatic(n)` > `Dynamic` > `Scanned` | position knowledge, and what it costs to get: `Dynamic` is arithmetic over values already read, `Scanned` is a search that can fail (8.6.1) |
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
message. Every construct in `examples/dnsname/dnsname.situ` is canonical taken
alone: a `u2`, a `u6`, a byte run, a `while`. And the format still admits many
encodings of one name, because the redundancy is not inside the name at all --
it is between the name and bytes elsewhere in the message that no per-member
rule ever sees together.

So a schema may say so:

```situ
struct name {
    label labels[] while (form == 0 && rest != 0) max 128
        [non_canonical = "a name may be spelled uncompressed, or as a pointer
                          to any earlier occurrence of any suffix of it"];
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
  `docs/decisions/0019-sealing-requires-authentication.md`.
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
    kernel = polynomial(0x04C11DB7, reflect_in, reflect_out, init = 0xFFFFFFFF);
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
| `seekable = permuted` | the position map is a bijection over a full block |
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

Three of these are new relative to the first draft and each was forced by a real
codec class:

**`expansion = ratio_padded(a,b)`** is the exact ratio applied at group
granularity: the output is always a whole number of groups, so a partial final
group is filled out rather than truncated. The group follows from the ratio --
`lcm(8, b)` bits, the smallest run of input that is both whole bytes and whole
symbols -- rather than being declared, because the two numbers are not
independent. base64 and base32 are the cases; base16 is not, because four bits
divide a byte and the question never arises. See
`docs/decisions/0018-padded-expansion.md`.

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
FEC, scrambler, and framing code in practical use is expressible as one of five
kernel families, or a pipeline of them. This bounds the tier-2 design.

| Family | Description form | Covers | Derived properties |
|---|---|---|---|
| **table** | input symbol -> output symbol map, optionally padded to whole groups | Manchester, 4b5b, 8b10b, NRZI, Gray, BCD, base16/32/64 | `ratio_exact` from symbol widths, or `ratio_padded` with `granularity = block(g)` where the code pads; `seekable = linear`; `deterministic`; not `systematic` |
| **polynomial** | generator polynomial over GF(2) or GF(2^m), plus init/reflect/xorout | CRC (all variants), Reed-Solomon, BCH | `expansion = +N`; `systematic` for appended-parity forms; `seekable = linear`; parity recompute scope = block |
| **linear block** | generator or parity-check matrix over GF(2) | Hamming, extended Hamming, LDPC, arbitrary block codes | `ratio_exact(n,k)`; `systematic` iff the matrix is in standard form; `seekable = blockwise(n)` |
| **shift register** | taps, feedback source, initial state | convolutional codes, additive and multiplicative scramblers, Miller | `length_preserving` or `ratio_exact`; `seekable = linear` iff feedback is from input only; `not seekable` and `error_propagating` if feedback is from output |
| **permutation** | index mapping, closed form or table | block and convolutional interleavers | `length_preserving`; `seekable = permuted`; `deterministic` |
| **stuffing** | trigger predicate plus insertion rule | HDLC bit stuffing, COBS, SLIP, byte stuffing | `expansion = ratio_bounded`; `not seekable`; interior addressing lost |

Pipelines compose: `codec framed = rs_255_223 |> interleave(16) |> manchester;`
Property composition is pointwise and conservative -- the pipeline is seekable
only if every stage is, systematic only if every stage is, and the expansion is
the product of the stages' expansions.

That last one needs the vocabulary above widened, and this example is what
shows it: Reed-Solomon appends 32 bytes and Manchester then doubles all of it,
which is `ratio_exact(2, 1)` *and* `+64` at once. A composed signature may
carry both; a hand-written one still gives one form
(`docs/decisions/0016-composed-expansion.md`).

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

## 14. Cryptographic model

The first real use case is compact encrypted protocols, so this is not an
add-on. It is a first-class part of the capability system.

### 14.1 Constructs

| Construct | Meaning |
|---|---|
| `authenticated { ... }` | plaintext, covered by a tag (AEAD associated data) |
| `sealed(codec, nonce = ref) { ... }` | encrypted and covered by a tag |
| `tag T[N] [covers(...)]` | authentication tag; coverage inferred if omitted |
| `nonce` attribute | marks a field as a nonce for a codec |
| `secret` attribute | marks a field as key material or plaintext secret |
| `checksum T[N] [covers(...)]` | non-cryptographic integrity field (CRC); shares the entire tag mechanism |

Coverage inference: an omitted `covers` clause means "every `authenticated` and
`sealed` region in the enclosing struct, in declaration order". Explicit
`covers(a, b)` overrides. Multiple tags in one struct are permitted; each
covers a disjoint or nested set of regions, and overlapping non-nested coverage
is an error (it makes recomputation order ambiguous).

Nested coverage recomputes **innermost first**, which is the only order that
terminates: an outer tag covers the inner tag's own bytes, so writing the inner
one afterwards would leave the outer one stale again
(`docs/decisions/0011-nested-tag-coverage.md`).

A region no tag covers is an error. `authenticated { }` states that these bytes
are covered; with no tag in the struct that statement is false, and a construct
whose meaning is silently nothing is what 14.5 refuses.

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

`docs/decisions/0019-sealing-requires-authentication.md`. The second refusal is
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
- emits zeroization on scope exit or via an explicit `situ_zeroize()` (using a
  compiler-barrier-protected memset, not plain `memset`)
- forbids the field from being used in any expression that controls layout
  (no secret-dependent lengths or discriminants -- that is a length-based side
  channel)
- generated accessors avoid data-dependent branching and data-dependent
  memory access patterns; where that is not possible for a construct, the
  compiler refuses to generate the accessor and says why

### 14.7 Padding and length hiding

`pad_to(n)` and `pad_random(min, max)` for traffic-analysis resistance. Padding
content policy is explicit (`zero`, `random`, `preserve`), and `zero` is
required for `require canonical`.

Not in phase 8: 26.8's acceptance criteria do not reach these constructs, and
the canonicity item they contribute -- padding whose content is unconstrained
-- is already covered by `reserved [unknown]`, which is the same freedom under
a name that exists. They belong with the schema-evolution work of section 19,
whichever phase takes that.

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

**Four of these the compiler names and cannot decide**, and it says which and
why rather than passing them. They were once recorded against the phase that
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
   = remedy: pin `opts` to a fixed size with [size = 1500]
             cost: +1500 bytes typical, +0 worst case
```

Also emit machine-readable diagnostics (`--diagnostics=json`) so the advisor,
editors, and CI can consume them without parsing prose.

---

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
implementation through its own FFI. A per-language plugin slot exists for a
native implementation where somebody eventually wants one, and is empty:
`impl crc32 derived for rust;`. See
`docs/decisions/0017-codec-implementation-sourcing.md`, which also records what
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

### 20.2 Generated C API shape

Per schema, emit `<name>.h` and `<name>.c`.

Naming: `<prefix>_<namespace>_<Type>_<field>_get`, `_set`, `_view`. The prefix
defaults to `situ` and stacks *outside* any namespace the schema declares, so
`--prefix` prefixes rather than replaces. Generated types carry `_t`, which
keeps them clear of the accessor namespace.

Two constructs whose paths flatten to one C identifier are an error naming
both, because the flattening is not injective -- `A.b_c` and `A_b.c` reach the
same name -- and the alternative is a redefinition error in generated code with
no schema location in it (`docs/decisions/0013-identifier-conventions.md`).

Principles:

- **Views are values, not pointers.** A view is a small struct
  `{ uint8_t *base; uint32_t limit; uint32_t generation; }` passed by value.
  No allocation, no lifetime management.
- **Field access within a view compiles to `base + K`.** Verify this in tests
  by inspecting generated code for constant offsets.
- **Errors are return codes**, never `errno`, never longjmp. A single
  `situ_err_t` enum with distinct codes per failure class (bounds, constraint,
  version, tag, stage).
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

### 20.3 Additional generated artifacts

These are cheap once the layout solver exists and they carry outsized practical
value:

| Artifact | Command | Why |
|---|---|---|
| golden-vector tests | `situc gen-tests` | schema + hex vectors -> cmocka test cases; the only reliable way to know a layout change broke the wire format |
| capability conformance | `situc gen-checks` | schema alone -> cmocka test cases holding the accessors to what the map claims; a map the generated code contradicts is worse than no map |
| fuzz harness | `situc gen-fuzz` | libFuzzer/AFL entry point per parseable struct; parse safety is the top risk in a protocol parser |
| byte-layout diagram | `situc doc --format=ascii` | RFC-style packet diagrams straight from the schema; what protocol documentation always needs |
| Wireshark dissector | `situc gen-dissector` | debugging an encrypted protocol without one is painful; large practical payoff |
| capability map | `situc map` | Section 18.1 |

---

## 21. CLI surface

```
situc build   <schema>            generate code
situc map     <schema>            emit capability map
situc map --check <schema>        compare against committed map, fail on diff
situc gen-checks <schema>         tests holding the accessors to the map
situc gen-derived <schema>        implementations from kernel descriptions
situc advise  <schema>            ranked design suggestions with costs
situc explain <schema> <path>     one field's capability vector and blame chains
situc diff    <old> <new>         capability regressions between revisions
situc wire    <schema>            the byte-level contract [--check] (19.3)
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

Global flags: `--target=c|cpp|python|rust`, `--out=DIR`,
`--diagnostics=text|json`,
`--strict`, `--prefix=NAME`.

---

## 22. Testing strategy

| Layer | Tool | What |
|---|---|---|
| compiler unit | pytest | lexer, parser, expression intervals, lattice meet, propagation table |
| propagation table | pytest, table-driven | one test per row of the 11.3 table; this is the highest-value test suite in the project |
| requirement discharge | pytest | every predicate in Section 16, passing and failing, with expected blame chain |
| canonicity | pytest | one test per source of non-canonicity in 14.4 |
| codegen golden | pytest + file compare | generated C compared against committed expected output |
| capability conformance | generated cmocka | the accessors are held to the map they were generated beside: a field must occupy exactly the bytes claimed, a write must move nothing else, a constraint must refuse what it forbids. Generated from the schema alone, so it costs nothing to run over every example and a new construct is covered the day it lands |
| generated code behavior | cmocka | compile generated C and exercise it; use `--wrap` for syscall-level mocking where needed |
| offset constancy | cmocka + disassembly check | verify view field access compiles to constant offsets |
| round-trip | pytest + hex vectors | parse then re-emit must be byte-identical for canonical schemas |
| fuzz | libFuzzer | generated harnesses run in CI for a bounded time |
| diagnostics | pytest | snapshot-test the exact diagnostic text; regressions in message quality are real regressions |
| backend agreement | pytest + each toolchain | every backend's output compiled and compared against the C on the same buffer, field by field. Four backends that disagreed would mean a schema means four things, and this is the only test that would notice |
| compiler mutation | by hand, recorded in 26.13a | deliberate bugs in the generator, judged by what a *user's* suite catches rather than situ's own. Not automated: choosing the mutation is the work, and a mutation nobody thought of is the gap that survives |
| test mutation | by hand, at the point of writing | a probe that walks generated code is run once against a deliberately wrong expectation, to find out whether it is a test or a compile check. Three of mine were the latter -- `-fsyntax-only`, a `main` nobody executed, an `assert!` in an unrun binary -- and each passed identically before and after the fix it was written for (invariant 35) |

**What the suite does not do**, stated because a reader would otherwise assume
it: the emitted Lua is never executed (no Wireshark, no interpreter in the
build environment), so `gen-dissector` is checked structurally and against the
layout rather than against Wireshark's acceptance. And the aarch64 big-endian
target is compile-only; only the little-endian one runs under emulation.

Implementation language for `situc`: **Python 3.11+, standard library only**,
with full type annotations checked by mypy in strict mode. Rationale: the
compiler is a symbolic solver and a text generator, both of which Python does
well; no dependencies means the toolchain vendors trivially into an embedded
build environment; and rewriting in Rust later is a known, bounded option once
the semantics are settled. Do not add a dependency without recording a decision.

---

## 23. Repository layout

```
situ/
  project.md                  this document
  bin/
    situc                     the command: a launcher that finds its own
                              package, in the tree or under an install prefix
  docs/
    decisions/                append-only numbered decision records
    grammar.ebnf              extracted, kept in sync with Section 7
    capability-axes.md        normative axis definitions
  situc/                      the compiler; Python 3.11+, stdlib only, mypy strict
    __init__.py
    __main__.py               so `python3 -m situc` works
    lexer.py
    parser.py
    ast.py
    wellformed.py             whole-schema checks: duplicate names, unresolvable
                              types, recursion, constant/attribute collision
    types.py                  scalar type table: integers, floats, fixed point, BCD
    expr.py                   expression evaluation and interval arithmetic
    layout.py                 the layout solver
    capability.py             axes, lattice, meet
    propagate.py              the 11.3 table, data-driven
    resolve.py                the seam joining layout to the table
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
    names.py                  ways of writing a schema fact that no target
                              language decides: how a delimiter reads, and
                              which field names an expression rewrites
    wire.py                   the byte-level contract and its comparison; 19.3
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
    dissector.py              `situc gen-dissector`: a Wireshark dissector in Lua
    lsp.py                    `situc lsp`: diagnostics, hover, symbols, code
                              actions and definitions, over JSON-RPC on stdio
    codegen/
      c/                      emit, checks, vectors, fuzz harnesses, codec
                              tests, derived codec implementations, MMIO
      cpp/                    the second backend
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
  tools/
    lint_conventions.py       the formatting enforcer; `make lint`
  tests/
    unit/                     compiler tests (pytest), one file per module
    generated/                cmocka tests over generated C
    cross/                    behavioural checks on aarch64 under emulation
    golden/                   pinned diagnostic text; message quality is the
                              product, so a regression in it is a regression
    propagation/              the 11.3 table's own fixtures
    schemas/                  header.situ, the three in Section 5, and
                              edges.situ -- constructs no protocol here uses,
                              which exists so their generated code runs at all
  examples/                   one directory per protocol, name matching its
                              `.situ` file, each with at least one `require`
```

Every example is parsed by the test suite, and every buildable one has a
committed `.situ.map` that the suite regenerates and compares. An example
marked `// STATUS: needs phase N.` is asserted to be *rejected*, naming a phase
no later than N; those pin the phase-gating behaviour and go live as phases
land.

### 23.1 Decision records

Append-only, in `docs/decisions/`. A decision goes here when the reasoning
would otherwise be lost -- when the obvious reading of this document is not
what the code does, or when an alternative was rejected for a reason worth
keeping. They are referenced from the sections they bear on; this is the index.

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

0004 and 0007 look contradictory and are not. 0004 made aarch64 compile-only
and deferred a revisit; 0007 closes that revisit once user-mode emulation was
available. Both stay, because the record of why the weaker position was taken
first is part of what a decision log is for.

## 24. Build system

Both CMake and GNU Make, maintained in parallel, as separate and independently
usable entry points.

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
make test       pytest, mypy strict, lint, cmocka, cross
make cross      aarch64 build of the runtime
make cross-test generated accessors run on aarch64 under emulation
make install    situc, the runtime and its header under PREFIX
make help       everything else
```

`situc` itself is `bin/situc`: a launcher that finds its own package, works in
place or symlinked into a PATH directory, and works again from
`<prefix>/lib/situc` once installed. It is not a console entry point, because
generating one needs an installer and the requirement below rules that out.
`python3 -m situc` and `python3 -m situc.cli` both still work.

`pytest` and `mypy` come from the system (`python3-pytest`, `python3-mypy`) and
are used only to check the compiler. `situc` itself must run from a bare
interpreter with nothing installed, which is what makes the toolchain vendor
into an embedded build environment (Section 22).

## 25. Conventions

- **ASCII only** in all source, comments, and docstrings. Non-ASCII belongs
  only in intentional runtime data values, and there are none in this project.
- **Tabs carry structural indent level; spaces carry alignment within a level.**
  Continuation lines use one tab for indent, then spaces to the alignment
  column. If lines are short enough to merge, merge rather than align.
- No prescriptive tab width anywhere in the codebase or in generated output.
  Elastic tabstops are the model: the viewer decides width.
- Generated C follows the same tab/space rule, and so does the Python.
- **No autoformatter.** `black` and `ruff format` rewrite tabs to spaces
  unconditionally and cannot be configured out of it, so `tools/lint_conventions.py`
  under `make lint` is the enforcement instead. See
  `docs/decisions/0003-source-formatting.md`.
- Lowercase filenames unless there is a reason otherwise, and `snake_case` over
  `camelCase` in every language.
- Line length: soft 100 columns; do not sacrifice clarity to it.
- Every module has a docstring stating its single responsibility. If a module
  needs two sentences joined by "and", split the module.
- **Identifier casing in a schema is the author's.** snake_case and PascalCase
  are both first-class and may be mixed; nothing in the compiler reads casing.
  What the compiler does check is that two constructs never generate the same C
  identifier, which is a property of flattening a path rather than of how
  either name is spelled (`docs/decisions/0013-identifier-conventions.md`).
  `examples/telemetry/` is snake_case throughout, as the working proof.
- Single source of truth: the AST is built once from the source text and all
  passes read it. Never re-parse generated output. Never mutate a file
  in a second pass without full knowledge of the first pass's state.

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
**Nothing on the roadmap is outstanding.** What situ deliberately does not
cover is named where the construct that would cover it would go -- 8.6.6 for
a grammar, 8.6.2 for signed text -- rather than in a list of absences, which
goes stale the moment one of them lands.

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
| compiler tests | ~1280 | pytest over `situc/`, one file per module |
| generated C checks | ~670 | cmocka, `gen-checks` output for every schema |
| schemas exercised | 20 | 16 examples, `header.situ`, `edges.situ`, two std |
| targets built | 3 | host, aarch64 under emulation, aarch64 big endian |
| backends compiled | 3 | every schema, as C++, Python and Rust, in the suite |

Every one of those numbers is a floor rather than a target. The generated
checks are derived from the schemas, so adding an example adds coverage without
anybody writing a test. Diagnostics are snapshot-tested in `tests/golden/`,
because section 17 makes message quality the product rather than a finish: a
regression in the exact text of a blame chain fails the build.

Phases 0 through 8 are ordered by dependency, and 9 onward are largely
independent of each other. Do not implement ahead of the plan.

### 26.0 Phase 0: scaffolding

**Status: complete.**

- Repo layout per Section 23; CMake and Make both building an empty target.
- pytest and mypy strict running in CI; cmocka wired up and running one trivial
  test over hand-written C.
- `docs/decisions/0001-implementation-language.md` recording the Python choice.

**Acceptance:** `make test` and `cmake --build . --target test` both pass with
zero warnings on host and aarch64 cross.

Delivered with GNU Make only; CMake is deferred and recorded in
`docs/decisions/0002-build-system.md`, so the acceptance criterion above stands
against `make test` alone until CMake lands.

### 26.1 Phase 1: front end, static subset

**Status: complete.** `situc/wellformed.py` holds the whole-schema checks --
duplicate names, unresolvable types, recursion, and the constant/attribute
collision that decision 0006 left open.

Lexer, parser, AST for: directives, `const`, `enum`, `struct`, scalar fields,
fixed arrays, `reserved`, bit fields, endian/bit_order scoping, offset pins,
attribute lists. Reject everything else with "not yet implemented" plus the
phase number that will add it.

**Acceptance:** `situc dump-ast` round-trips example 5.1 exactly. Recursive
type declarations are rejected with a clear error. 40+ parser tests including
malformed input.

### 26.2 Phase 2: layout solver, static subset

**Status: complete.** `situc map` emits the capability map, and every buildable
example carries a committed `.situ.map` the test suite regenerates and compares.

Compute size, offset, alignment, bit positions for the static subset. Evaluate
offset pins and `size()` requirements as assertions. Emit the capability map for
the static subset (offset, size, align, repr, atomic axes only).

Offsets are tracked internally in **bits**, not bytes, from this phase onward,
and reported in bytes only where byte-aligned. Sub-byte codecs (Section 13.4)
will require bit phase later, and retrofitting it is a rewrite.

**Acceptance:** example 5.1 produces the correct map; a deliberately wrong pin
produces the diagnostic format of Section 17; bit-field packing verified against
hand-computed layouts for both bit orders, including a straddle rejection test;
a test asserts that internal offsets are bit-valued and that a non-byte-aligned
offset is reported as such rather than silently truncated.

### 26.3 Phase 3: capability lattice

**Status: complete.** All thirteen axes with their domains as data
(`capability.py`), the 11.3 table as rows rather than conditionals
(`propagate.py`), the seam joining layout to table (`resolve.py`), blame chains
through to a costed remedy, `situc explain`, and `--diagnostics=json`.

`requirements.PREDICATES` is the predicate table, and `DEFERRED_PREDICATES`
lists the rest against the phase that will discharge them. A deferred
requirement is reported on every run rather than silently passing, and that
must stay true: a predicate that quietly succeeds because nothing implements it
yet is worse than one that fails.

Implement axes, meet, and the propagation table as data. Implement `require`
and `assert` for the static subset predicates. Implement blame chains and the
full diagnostic renderer plus JSON output.

**Acceptance:** one passing test per row of the 11.3 table that is reachable in
the static subset; every failing requirement produces a blame chain that names
the correct root cause; `--diagnostics=json` output validates against a
committed schema.

### 26.4 Phase 4: C backend, static subset

**Status: complete.** Accessors are `static inline` and compile to `base + K`,
verified by disassembly rather than assumed; `gen-tests` and `gen-fuzz` derive
from the layout rather than a hand-maintained list.

One caveat to carry forward, recorded in
`docs/decisions/0007-aarch64-testing.md`: the big-endian aarch64 build is
compile-only, because no big-endian glibc is available. That is sound only
while every multi-byte access is explicit byte indexing rather than a cast. A
`memcpy` fast path for `MemoryIdentical` fields -- the obvious future
optimisation -- would break the argument and needs the decision revisited
first.

Generate `.h`/`.c` for the static subset: getters, setters where permitted,
endianness conversion, bit-field RMW, size constants, `SITU_CHECKED` checks.
Generate golden-vector tests and a fuzz harness.

**Acceptance:** generated C for example 5.1 compiles with
`-Wall -Wextra -Werror -Wconversion` on host and aarch64; cmocka tests
round-trip a hex vector byte-identically; a field whose `mutate` axis forbids
in-place setting has no setter and the header explains why; disassembly check
confirms constant-offset access.

Also in this phase: `endian_marker` (Section 8.3), including the generated
host-order constant and accessor, and `ConditionallyConverted` propagation.

**Additional acceptance:** a byte-order-marked struct parses both orders from
hex vectors; the generated host constant matches the build's endianness on both
a little-endian host and an aarch64 big-endian cross build;
`require canonical(X)` on such a struct reports `CanonicalGiven(byte_order)`
rather than passing or failing outright; `require deterministic_writer(X)`
passes.

### 26.5 Phase 5: expressions and dynamic layout

**Status: complete.** `examples/message/` reproduces the 11.4 map exactly.

Expression language with interval arithmetic. Counted arrays, length-driven
sizes, `remaining`. Frame detection and frame-relative staticness. Views,
generation counters, invalidation rules. Capability propagation for the dynamic
constructs.

**Acceptance:** example 5.2 produces the map in Section 11.4 exactly;
`require frame_static(message.recs[])` passes and
`require absolute_static(message.recs)` fails with blame on `opts`; a cmocka
test proves a stale view is caught in `SITU_CHECKED` and that mutation of a
preceding length field increments the generation.

### 26.6 Phase 6: variants, opaque, TLV, indexed, varints

**Status: complete**, including both halves of the protobuf gate below and the
17.0 ambiguity table audited row by row -- which is where `[require_aligned]`
turned out to be in the attribute vocabulary while checking nothing.

Two checks belong to a later phase, now that expressions evaluate: an enum
value that does not fit its backing type, and a duplicate enum value.

`variant switch`, exhaustiveness checking, `opaque`, `tlv` in both the simple and
general forms (composite `tag_decode`, dispatched `value_size`,
`duplicate_tags`), `indexed` regions, and `varint_type` including zigzag.

Also in this phase: the ambiguity table of Section 17.0 must be fully enforced.
Every row is a test.

**Acceptance:** a variant with unequal arm sizes correctly marks following
members dynamic and the advisor reports the equalization cost; TLV iteration and
same-size item mutation work under cmocka; `unknown = preserve` correctly sets
`canonical = NonCanonical` and `require canonical` fails naming it; a
non-`minimal` varint sets `NonCanonical` with the right reason; every row of the
17.0 ambiguity table produces an error when unresolved and compiles when resolved.

**Gate acceptance -- the protobuf conformance test.** The schema in Section 9.7
must compile, parse real protobuf-encoded hex vectors correctly (generate them
with `protoc` and a reference implementation), and `situc explain proto_message`
must enumerate all five causes of non-canonicity with source locations. Do not
proceed to phase 7 until this passes. It is the sharpest single test of whether
the capability system is real.

### 26.7 Phase 7: transforms, tier 1 (extern codecs)

**Status: complete.** `situc gen-codec-tests` output was checked against four
deliberately lying implementations. `coded(C) { ... }` is the general transform
region this table needed and this document did not name;
`docs/decisions/0009-coded-regions.md` records why, and phase 8's `sealed`
becomes `coded` plus authentication rather than a second mechanism.

The property held throughout: **the lattice reads property signatures and
nothing else** (13.1). No rule in `propagate.py` knows what an algorithm does.

`codec` signatures with the full property set of 13.2, separated from `impl`
bindings. Staging. Propagation per the 13.5 table. Enforce the three prohibitions
in 13.3. Mark extern codecs `trusted` in the capability map. Ship
`std/codecs.situ`. Implement `situc gen-codec-tests`.

A `codec` with no bound `impl` must analyze cleanly and fail only at codegen.

**Acceptance:** one test per row of 13.5; a schema referencing transform output
in an expression is rejected; a `length_preserving + seekable = linear` codec
yields in-place interior mutation while `seekable = none` yields
`mutate = RewriteRequired`, both verified in the generated API surface; an
`expansion = ratio_exact(2,1)` codec preserves static interior offsets and a
`ratio_bounded` one does not; a `systematic` codec permits interior reads with
no decode; swapping `impl X derived` for `impl X extern "..."` changes not one
byte of the capability map; a deliberately mismatched implementation is caught by
the generated conformance tests for every property in the 13.1 table.

### 26.8 Phase 8: cryptographic model

**Status: complete.** The transform half already existed and was tested
(decision 0009), so this phase added the tag, the coverage inference and the
stage gate rather than a second transform mechanism.

The stage gate is a generated view type: interior accessors take
`situ_<Struct>_<region>_t`, nothing produces one but the open function, and the
open function demands the verification result. A caller holding an ordinary
view cannot reach an interior field because the program does not compile, which
is what "unrepresentable rather than discouraged" has to mean in C. Both halves
are tested, the compile-refusal one by compiling the attempt and requiring it
to fail.

Two decisions came out of it: `docs/decisions/0010-region-and-tag-names.md` for
the surface syntax the grammar left out, and
`docs/decisions/0011-nested-tag-coverage.md`, which resolves open question 2.

Two things this phase found in earlier work, both fixed here: a region
placement was counted twice in the offset arithmetic of anything after it, and
a coded region had no runtime length expression at all, so a member following
one landed on the wrong bytes.

`authenticated`, `sealed`, `tag` with coverage inference, `nonce`, `secret`.
Tag-dirty tracking. `VerifyGated` staging with no unverified interior access.
`require canonical` covering all of 14.4. Strictness policy.

**Acceptance:** example 5.3 compiles; the interior of a sealed region is
unreachable before verification and a test attempting it fails to compile;
mutating a covered field marks the tag dirty and finalize recomputes it;
`[secret]` fields have no debug accessor and are zeroized; every item in the
14.4 checklist has a test.

### 26.9 Phase 9: advisor

**Status: complete.** `situc/advise.py` holds the 18.2 catalog as data and
`situc/revision.py` the 18.3 diff. `situc explain` already existed from phase 3.

Two rows needed reinterpreting, and the reinterpretation is in 18.2 above:
"frequent mutation of a covered field" is not a property of a schema, and the
alignment row triggers off the weakening the lattice recorded rather than
recomputing the offset arithmetic beside it.

The diff found a bug in the propagation table on its first real run: a
multi-byte scalar with a dynamic offset kept `atomic = AtomicWord`, because the
alignment row skipped anything whose offset it could not see. An offset that is
not known is not an aligned one, and the row says so now.

Suggestion catalog, cost model with typical and worst case, `situc advise`,
`situc explain`, `situc map --check`, `situc diff`.

**Acceptance:** `advise` on a deliberately badly-ordered schema produces the
field-reordering suggestion with a correct byte cost; `map --check` fails on an
uncommitted capability change; `diff` correctly identifies a regression from
`InPlaceFixed` to `Shifting`.

### 26.10 Phase 10: MMIO target

**Status: complete.** A register lowers to a `StructDecl` carrying a
`RegisterInfo`, so the solver places it, the lattice costs it and the map
renders it with no change at all -- which is the unification 15 exists to
demonstrate. Only the backend knows it is emitting bus transactions, in
`situc/codegen/c/mmio.py`.

Three things the section did not spell out and the implementation had to
decide, all recorded in `docs/decisions/0015-register-access-modes.md`:

- **A field getter takes a word, never the register.** A read is an event, so
  it happens once and is decoded as many times as there are fields. An API that
  read per field would drain a FIFO to decode a status word under
  `on_read = pop`, which is a correctness question rather than a performance
  one.
- **The byte is not the unit inside a register.** The alignment and straddle
  rules of 8.2 and 8.4 are about buffers; a register is one access of
  `access_width` bits and every field in it is a bit range within that word, so
  a `u8` three bits in is ordinary rather than an error.
- **An access mode shapes which operations exist; the lattice costs the ones
  that do.** `wo` generating no getter is not a fourteenth axis.

`target mmio`, `register`, `register_block`, access modes, side effects, scoped
defaults, and the capability interactions in 15.3.

**Acceptance:** example 15.2 generates an API with no `set_enable()` and a
diagnostic explaining why; `w1c` generates `clear_error()`; volatile access is
emitted correctly and verified by disassembly; `ro`/`wo` asymmetry holds.

### 26.11 Phase 11: Rust backend

**Superseded by 26.18.** This number was allocated to the Rust backend before
the backend order was decided; section 20.1 now puts C++ and Python ahead of
it. The number stays rather than being reused, so that a reference to "phase
11" written earlier still lands somewhere true.

### 26.12 Phase 12: transforms, tier 2 (derived codecs)

**Status: complete.** All six kernel families derive their signatures and
generate implementations, and pipelines compose; `situc/kernels.py` holds the
derivation and `situc/codegen/c/derived.py` the generation.

Every family is exercised against published vectors rather than against situ's
own output: the CRC catalogue's check values, IEEE 802.3's Manchester
encoding, Cheshire and Baker's COBS examples, Hamming(7, 4)'s single-error
correction, HDLC's five-ones rule. Generating COBS turned up a real bug on the
254-byte boundary -- a full group opened a second one at end of input, spending
the extra overhead byte the code is named for not spending.

The invariant held: no propagation rule changed, and
`test_no_propagation_rule_reads_a_kernel` fails if one ever reaches past the
signature to a kernel.

`std/kernels.situ` describes as kernels the same codes `std/codecs.situ`
declares by hand, and a test requires all nine properties to agree for every
code in both. That is the acceptance criterion, and it earned its keep
immediately: it caught three families deriving the wrong granularity and one
claiming an error propagation a block code does not have.

Composing the section 13.4 example needed the expansion vocabulary widened --
`rs |> interleave |> manchester` is a ratio *and* an addend, which 13.2 offers
only as alternatives. See `docs/decisions/0016-composed-expansion.md`.

Generate codec implementations from kernel descriptions and derive property
signatures from them (Section 13.4). Because the property signature is the only
interface to the lattice, **no propagation rule changes in this phase.**

Recommended order within the phase, cheapest and highest-value first:

1. **polynomial / CRC.** Universally needed, simplest kernel, and it lands as
   `checksum` reusing the tag machinery rather than as a codec. Existing
   generators (pycrc, crcany) are good references for the table-generation and
   reflection handling.
2. **table codes.** Manchester, NRZI, 4b5b, 8b10b. Exercises `ratio_exact`,
   symbol granularity, and bit phase end to end.
3. **permutation.** Interleavers; exercises `seekable = permuted`.
4. **linear block.** Hamming, then general GF(2) matrices; exercises
   `systematic` derivation from matrix form.
5. **shift register.** Scramblers, then convolutional codes; exercises
   feedback-source analysis for seekability.
6. **stuffing.** COBS and HDLC; exercises `ratio_bounded` and the advisor rule
   about applying stuffing only at the outermost layer.
7. **Reed-Solomon / BCH over GF(2^m).** The largest single item. Needs field
   arithmetic, table generation, and real performance work. Consult libfec and
   ISA-L before writing anything.

   Done for RS. The polynomial kernel dispatches on `field`: a `width` is a CRC
   over GF(2), a `field` is a code over GF(2^m), and the derivation says how
   they differ -- RS is `invertible` because the message comes back where a
   digest does not, and `error_propagating` because a burst past the capacity
   spoils the whole block. `codec c { kernel = polynomial(field = 256, n = 255,
   k = 223); }` derives exactly the hand-written `reed_solomon_255_223`
   signature.

   The generated decoder is the standard chain -- syndromes, Berlekamp-Massey,
   Chien search, Forney -- over tables computed at generation time from the
   primitive polynomial. Nothing here is specific to the famous code: the tests
   cover RS(255, 223), RS(255, 239) and a shortened RS(64, 56), all from the
   same generator.

   No performance work yet. The arithmetic is table-driven but scalar, and
   The constant-time question is open question 11, and is still open.

**Acceptance per family:** derived properties match a hand-written signature for
a known code; generated implementation passes vectors from an independent
reference implementation; a pipeline of two families composes properties
correctly and conservatively.

### 26.13 Phase 13: `.proto` importer

**Status: complete.** `situc/proto.py` reads the subset of `.proto` with a
wire-format meaning and refuses the rest by name. The fidelity report is the
feature, and the three acceptance criteria are tested: a translatable `.proto`
produces a schema whose field numbers and wire types agree with what `protoc`
actually emits, an untranslatable one exits non-zero with each construct and
its line, and `--accept-lossy` downgrades to warnings and still writes a schema
that compiles -- checked by parsing the output back before reporting success.

Two things the section did not settle. A protobuf enum imports with an `i32`
backing, since that is what the value is even though the wire encoding is a
varint. And the scanner reads statements rather than lines, because
`message M { optional int32 a = 1; }` is legal on one line and a line-based
scanner would silently import an empty message -- which is exactly the silent
partial success 26.13 warns about.

Per Section 19.2. Deliberately after tier 2, because the importer is only useful
once the language it targets is complete, and because it is the one component
where a silent partial success does real harm.

**Acceptance:** a `.proto` file exercising every translatable construct produces
a schema that parses vectors generated by `protoc`; a `.proto` using `map`,
`Any`, or groups exits non-zero with a fidelity report naming each construct and
its source location; `--accept-lossy` downgrades to warnings and the resulting
schema still compiles.

### 26.13a Auditing the tests by breaking the compiler

A suite is only worth what it refuses to pass, so the generator was mutated on
purpose -- one deliberate bug at a time -- and the suite was asked to notice.
Each mutation was judged twice: against situ's own tests, and against what a
*user* gets, which is the generated accessors plus `situc gen-checks` and
nothing else.

Four of the first seven survived the user-visible suite, and the diagnosis
was the same for all of them. Every generated check was symmetric.
`occupies_its_claimed_bytes` asks which bytes a field reaches; a byte-swapped
accessor reaches the same ones. `round_trips_in_place` asks whether the setter
and the getter agree with each other; a swap in both keeps them agreeing. So a
backend emitting little-endian loads for big-endian fields passed the entire
generated suite -- for a language whose whole subject is byte order.

Five check families closed it, all working from outside the accessor and all
deriving their expectations from the map rather than from the emitter:

- `decodes_a_known_encoding` writes a byte pattern computed from the declared
  offset and order, and demands the value back. Byte order, bit order and sign
  extension are all asymmetric under it.
- `starts_where_the_map_says` measures a nested struct's base against its
  parent's. Its interior is checked against its own base, so every field inside
  it can agree with every other and the whole thing still sit a byte late.
- `elements_are_one_stride_apart` and `element_lands_on_its_stride` measure
  element spacing. A drifting stride leaves element zero right and every other
  element quietly wrong.
- `places_its_members_in_one_instance` synthesises a concrete instance of a
  struct that has no worst case, and asks every member where it starts.
- `covers_what_it_claims` bounds a tag's span by the members the map says are
  inside it.

All seven mutations are now caught by the user-visible suite alone.

The array check found a real bug on its first run: `_at` bounded the index
against the view rather than against the element count, so indexing one past
the end of an array that did not run to the end of its struct returned
`SITU_OK` and a view over whatever followed. In `telemetry_frame` that was the
checksum.

Two mutations survived because no schema reached the code at all -- signed
bit-packed fields and arrays of converted scalars were generated and had never
run. `tests/schemas/edges.situ` exists to reach them; it describes no protocol.

A struct with no bounded size got no checks at all, which left the offset
functions -- the whole reason such a struct is unbounded -- checked by nothing.
No buffer fits every instance, but nothing forces the general case:
`_instance_checks` gives every variable member two elements, walks the members
adding up the sizes that implies, and asserts each one starts where that walk
says. The accessors reach the same numbers by summing length fields at run
time, so the two routes are independent and a disagreement means something.
`message` is the example: `hdr` at 0, `opts` at 11, `recs` at 13, `trailer` at
29.

The last survivor was a coded region's extent. It is deliberately not
recomputed -- a region's extent is its interior put through a codec's
expansion, and reconstructing that would be a second solver to get wrong, which
is why the emitter takes the region's end from where the next member starts.
It can still be bounded from outside without any solver: every member the map
calls covered has to be inside the span. A region that ended at its own start
fails that whatever the expansion is.

**One skip is left, and it is correct.** `proto_message` is a single TLV
stream. It has no fixed layout at any instance, so there is no offset to
assert, and the emitted file says so.

**A second round found the constructs no example reached.** Mutating the
reserved-field and tag-mask paths showed both surviving, not because the checks
were weak but because nothing in the tree exercised them: `must_be_one` was
supported and unused, and `DIRTY_MASK` only means anything with more than one
tag in a struct. Adding both to `tests/schemas/edges.situ` made the gaps
visible, and closing them turned up two real defects.

Reserved fields had no generated check at all -- section 8.8 calls them
malleability control, and a receiver ignoring them lets a sender vary bytes the
format calls fixed without disturbing a tag. Worse, `reserved u8[3]` was not
validated even by the emitter: arrays were skipped, and `packet.situ` puts
exactly that inside an authenticated region.

Checking a reserved policy needs a buffer that is otherwise valid, or the check
is vacuous -- a zeroed buffer already breaks `must_be_one`, so asserting that a
wrong value is refused would pass against a validator that did nothing. The
baseline satisfies every `must_eq`, `min` and reserved policy first and asserts
the struct validates, then breaks one field.

**The baseline has since been wrong twice more, both times the same way.** It
left enum fields at zero once membership was enforced, and it does not satisfy
constraints on fields it cannot place. A baseline that is not actually valid
makes every check built on it assert the wrong thing, and it fails loudly -- so
the pattern to watch for is a new constraint kind landing without the baseline
learning to satisfy it.

### 26.14 Delivered after the plan ran out

The plan ran to phase 13. Three things landed after it, and they are numbered
here rather than left in a "beyond" list: a delivered thing is a phase whatever
the plan called it at the time.

**Fixed-point and BCD are done.** Section 8.1 called for the type table to be
extensible and it was: both landed by adding kinds to `situc/types.py` and a
row each to the propagation table, with no parser change at all. That is
invariant 1 working as intended.

`examples/rtc/rtc.situ` is the worked example, and it is the honest one -- a
real-time clock holds BCD because the part drives a display, and its
calibration is fixed point because the correction is fractional and the part
has no FPU. Asking `memory_identical` of the trim field produces a blame chain
naming both causes at once, the byte swap and the scale, which is what a
multi-cause diagnostic is for.

`memory_identical` is new. `repr` was one of the thirteen axes with no
predicate, which left the question a caller most often has -- can I point at
these bytes and read them as they are? -- unaskable.

**The Wireshark dissector is done.** `situc gen-dissector` emits Lua: a `Proto`
per struct, a `ProtoField` per member with the bitmask where it is bit-packed
and the value string where its type is an enum, and a dissector that walks the
members in order. Nested structs get their own subtree; struct arrays are
dissected element by element rather than shown as a run of bytes; a
variable-length member's length is read back out of the buffer with the same
arithmetic the generated C offset functions use.

Lua rather than C because a Lua dissector is a file a user drops in a plugins
directory, where a C one has to be compiled against the headers of whatever
Wireshark they happen to run.

Registers are deliberately not dissected -- a register is a bus transaction and
does not appear on a wire -- and the emitted file says so. Nothing is bound to
a port or EtherType automatically, because the schema does not say which one
and guessing would be wrong; the file shows how to bind it and the hint names
the outermost struct rather than an inner one.

**Not executed by the test suite.** There is no Lua interpreter or Wireshark in
the build environment, so the tests check that the emitted Lua balances its
blocks, uses valid identifiers, and that every `tvb(first, count)` in it is a
span some member actually occupies. That last one is the claim worth holding: a
dissector that loads and shows the wrong bytes is worse than one that fails to
load.

**Documentation generation is done.** `situc doc` renders RFC-style byte-layout
diagrams and a field reference, in plain text or markdown. The diagrams come
from the same placements the accessors are generated from, which is the whole
argument for generating them: a drawn diagram is a second description of the
layout, and second descriptions drift. `situc doc examples/udp/udp.situ`
reproduces RFC 768, and the IPv4 header reproduces RFC 791 including its
bit-packed first row.

Rows are 32 bits by the RFC convention, narrowing to 16 or 8 for a struct that
is smaller than that -- a one-byte register drawn across four bytes of row
reads as a four-byte one. Nested structs are drawn as a single box under their
own name, the way RFC 791 draws `Source Address` rather than four octets, and
get their own diagram further down. Variable-length members take the rest of
the row they start in and continue in the slanted form RFCs use.

### 26.15 The built-in codec set

**Status: partly done.** The checksums, base16 and text validation have
landed. base32 and base64 have not, and the reason is a decision rather than
effort -- see the end of this section.

**Checksums are runtime primitives, not kernels.** A `checksum` field says
which bytes are covered and when the value went stale; it does not compute
anything, because the algorithm is not something the layout knows. So the four
small enough to ship are `static inline` in `situ.h`, where a schema naming
none of them emits none of them: `situ_checksum_internet` (RFC 1071),
`situ_fletcher16`, `situ_fletcher32` and `situ_adler32`.

Each is held to somebody else's answer rather than its own. Adler-32 is checked
against Python's `zlib`, which is an independent implementation of RFC 1950.
Fletcher's vectors are the published ones -- and pinned the word order, which
is not a free choice: Fletcher-32 reads its words little-endian, and the
big-endian reading gives a byte-swapped near-miss that looks entirely plausible
until compared with somebody else's answer. The internet checksum is tested
against RFC 1071's worked example and, more usefully, against the property the
RFC states: a block carrying its own checksum sums to zero. A property holds
for every input where a constant holds for one.

**base16 needed no new machinery at all.** It is 4 bits in, 8 bits out, so it
is a table code and the existing kernel generates it from an alphabet in
`NAMED_CODES`. It needs no padding at any input length -- every byte is exactly
two nibbles -- which is precisely what separates it from base32 and base64.
`base16_lower` is a separate code rather than an alias: a protocol that
specifies one and receives the other has received something it did not specify.

**Text validation makes `[encoding]` mean something.** Section 8.6 offered
`ascii` and `utf8` and both were parsed and dropped on the floor, so a schema
could call a field ASCII and the generated code would neither check it nor
record it. `situ_utf8_valid` is strict in the way RFC 3629 requires: overlong
forms, surrogate halves, values above U+10FFFF and sequences running off the
end of the field are all refused. That strictness is section 8.8's argument in
different clothes -- an overlong encoding is a second spelling of a character
that already has one, and a receiver accepting both accepts two byte sequences
for one value. Every expectation is checked against Python's strict decoder.
An encoding situ cannot validate is now an error rather than a silent nod.

**base32 and base64 needed a sixth expansion form**, and got one. Their ratios
are exact at the bit level but the output is always whole groups, so one byte
of base64 input produces four characters rather than the two an exact ratio
predicts. `ratio_padded(a, b)` says `ceil(input / group) * group_out`, where
the group follows from the ratio rather than being declared -- `lcm(8, b)`
bits, which is three bytes for base64 and five for base32. See
`docs/decisions/0018-padded-expansion.md`, which also records why no
propagation row changed.

The encoders were written against Python's `base64` module and agree with it at
every input length from 0 to 20, which matters more than usual here: an encoder
wrong only for inputs of length 3n+1 looks right in casual testing. The
committed tests are RFC 4648's own vectors, which cover every length mod 3 and
mod 5 precisely because that is what decides the padding.

`base64url` is a separate code rather than a flag. The alphabets differ only in
their last two characters, so text encoded with one and decoded with the other
is wrong in exactly the bytes that made somebody reach for it.

**`nul_terminated` is done, and the declared size is the capacity.** That was
the decision the feature was waiting on, and it is the one that keeps the
layout static: a nul-terminated field is its declared width whatever it holds,
so nothing after it moves. Section 8.6 records what follows -- a `_len()`
accessor, a validator that demands the terminator, and `NonCanonical` on the
canonical axis, since the bytes past the terminator are not part of the value.

`tests/schemas/edges.situ` carries a struct using both text attributes, because
no protocol in the tree does and generated code that never runs is the thing
that file exists to prevent.

**26.15 is complete.**

**Parsing most protocols should need nothing installed.** The long-term aim is
that a schema for an ordinary protocol builds and runs against situ alone --
that is what "batteries included" has to mean for a description language, since
a user who must go and find three libraries before their schema compiles will
write the parser by hand instead.

The line between what ships and what does not is not popularity but whether
situ can produce it honestly:

- **Built in** -- anything a kernel description derives, or that is a few
  hundred lines of dependency-free C. The six kernel families are already here,
  and Reed-Solomon with them. Still missing and worth adding: the internet
  checksum (RFC 1071), Fletcher and Adler-32, base64/base32/hex, UTF-8
  validation. All small, all common, none of them needing anybody's library.
- **Optional** -- anything needing a real implementation behind it. AEAD
  ciphers, hashes, deflate, zstd, LZ4. These stay tier-1: declared by property
  signature, trusted, supplied by the user.

**And optional has to mean optional.** A schema pays for what it names and
nothing else, or every user links a crypto library so that the few who seal a
region can. This already holds and is now tested rather than assumed: generated
code includes `situ.h` and its own header, and a schema that seals a region
with `aes_gcm_128` has exactly the dependencies of one that does not -- none.
The stage gate takes the verification result as a parameter, so situ guards the
bytes and the caller runs the cipher.

### 26.16 C++ backend

**Status: complete.** `situc build --target=cpp` emits one header per schema,
covering scalars, bit fields, straddling fields, enums, nested structs, byte
arrays, fixed point, BCD, dynamic layout, sealed regions, registers and every
constraint the C backend validates. A variable-length member inside a sealed region is reachable too: its length
is read through the gate's own view, since the field that sizes it is plaintext
at the same offsets and only the view differs.

**Registers are where the access modes stop being documentation.** A `word` is
a copy of the bits and a register is a place on a bus, and separating them is
what section 15's headline asks for: a partial-width field in a `no_rmw`
register cannot be written alone, and the remedy is to compose the whole word
and write it once. Here that remedy is the only shape the API has:

```cpp
r.write(r.read().with_enable(1).with_mode(5));
```

Each mode then decides which operations exist rather than which are unwise. A
`ro` field has no composer, a `wo` field has no getter, and `w1c` has neither
-- writing a one clears it, which is not an assignment, so it gets
`clear_error()` instead. `on_write` makes the write itself the event and gets
`trigger_start()`. Tests assert each of those by requiring the forbidden
expression to fail to compile; in C they are comments.

**The stage gate is the reason this backend exists.** Section 14.3 asks that a
sealed interior be unreachable before its tag verifies. C gets close: the
accessors take a struct that only `_open` is supposed to fill in, and anybody
determined enough fills it in anyway. C++ closes it. The gate has no public
constructor and no public factory, and the only way to hold one is to be handed
it inside a callback:

```cpp
err e = p.with_sealed(verified, [&](packet::sealed_gate g) {
        kind = g.inner_kind();
});
```

There is no expression a caller can write that names a gate outside that
branch, so parsing attacker-controlled plaintext before authenticating it is
not discouraged -- it does not compile. A test asserts exactly that, by trying
to construct one and requiring the compile to fail. `[secret]` fields get no
accessor even inside the gate (section 14.6).

**Dynamic layout works the same way it does in C**, because it is the same
walk: constants for the fixed members and a runtime read for each variable one.
Element access is bounded by the count as well as the extent -- bytes after an
array are inside the view and are not elements, which the C backend learned
from a mutation that survived and this one started with.

**There is no second runtime.** `runtime/cpp/situ.hpp` is a header over
`situ.h`, whose functions are already `extern "C"`. A second implementation of
the same arithmetic is a second thing to get wrong, which is decision 0017's
argument about codecs applied to the runtime.

**What C++ enforces that C documents.** This is the whole reason the backend
is worth having, and it came to three things:

- A byte array is a `situ::rt::bytes`, so its length travels with its pointer.
  The C backend hands out a bare pointer and a `_COUNT` macro and nothing makes
  a caller use the second with the first.
- Every fallible operation is `[[nodiscard]]`. In C the return of `validate` is
  as ignorable as any other `int`.
- A `enum class` cannot be confused with its backing width.

The stage gate is the fourth and is not built yet: a class whose constructor is
private, so a sealed region's view cannot be made except by the function that
verifies the tag. In C the struct is there for anybody determined enough to
fill in. That is the headline and it waits on codec binding.

**The design decisions, and why.** A view is a value, as in C: it owns nothing,
so a destructor would be a lie about what it is, and section 12.3's
invalidation rule is a generation check rather than a lifetime. Errors are
return codes rather than exceptions because the target may have neither
unwinding nor a heap; the generated headers compile clean under
`-fno-exceptions -fno-rtti`. `situ::rt::span` is twenty lines rather than
`<span>` because a freestanding toolchain may not ship the header and the part
of it worth having is small. C++17 rather than 20, for the same reason.

**Two naming hazards C does not have**, both found by compiling the examples
rather than by thinking about it. A field may share a name with its type --
IPv4 has a `protocol` field of type `protocol` -- and an unqualified use inside
the class changes what that name means partway through it, which C++ rejects
outright; every generated type is therefore fully qualified. And the runtime
lives in `situ::rt` rather than `situ`, because generated code lives in `situ`
and a schema is free to declare `struct message` or `struct view`.

**The test that matters is that the two backends agree.** Two backends over one
layout that disagreed would be worse than one, because the schema would then
mean two things. Both headers are compiled into one program over one buffer and
compared field by field, including a write through the C++ side read back
through C.

Codecs bind the C implementation (decision 0017), which for C++ costs nothing:
`extern "C"` is idiomatic and the generated C header already emits the guard.

### 26.17 Python backend

**Status: complete.** Registers are there too, though not as a driver: Python
cannot promise `volatile`, so the generated class composes words exactly and
leaves the transport to the caller -- an `mmap` of `/dev/mem`, a probe, a
simulator. That is the shape section 15 asks for anyway, since a partial-width
field in a `no_rmw` register cannot be written alone. The access modes still
decide which operations exist: a `ro` field has no composer and a `wo` field
has no getter, checked the way Python can check it, by the attribute simply not
being there. `situc build --target=python` emits one module per
schema over `runtime/python/situ_runtime.py`.

**The surface is properties**, not `get_x()` methods. A caller who has to write
`packet.version()` writes the parser by hand instead, and a backend nobody uses
enforces nothing at all. `validate()` raises rather than returning a code for
the same reason: idiom is not a capability, and a return value a Python caller
silently drops is worse than an exception they have to catch.

**What Python does enforce**, which is more than expected going in:

- Zero copy. A view is a `memoryview` over the caller's buffer; a write through
  one is visible to whoever owns the bytes.
- Bounds, once at acquisition, as in C.
- **Invalidation (section 12.3), which is the one place Python is stronger than
  release-build C.** Every access checks the generation, where the C check
  compiles out. It costs an integer compare, which is not a cost worth avoiding
  here.
- A covered write is not spelled as an assignment. It leaves a tag stale, so
  the plain setter is refused and `set_x(msg, value)` marks the tag dirty --
  the same refusal C makes, because a schema that means one thing in C must not
  mean another here.

**What it does not**, said in the module header rather than left to be found:
`atomic` means nothing, because Python has no single-instruction access and the
GIL is not a statement about bus transactions. `repr` costs what the map says
even though a property makes it look free.

**The stage gate is weakest here.** It refuses to construct without a token
only a verified open produces, which is a real run-time check -- but not the
C++ guarantee, where forging one does not compile. Python has no access control
and `object.__new__` will make a gate whatever the class says. The generated
docstring states that outright, because a reader who has seen the C++ backend
would otherwise assume the strength carried over.

Codecs bind the C implementation, which costs Python a build step -- the
friction Python users least expect. Decision 0017 records why a pure-Python
second implementation is the wrong answer, and what the plugin slot is for if
it proves untenable.

### 26.18 Rust backend

**Status: complete.** `situc build --target=rust` emits one module per schema
over `runtime/rust/situ_rt.rs`, which is `no_std` and allocation-free.
Everything the C backend covers: scalars, bit fields, straddling fields, enums,
nested structs, byte arrays, fixed point, BCD, dynamic layout, sealed regions,
registers and every constraint. This section supersedes 26.11.

**A slice carries its own length**, so a variable-length struct needs no second
parameter saying where the frame ends -- the one place Rust's model is simpler
than C's and C++'s rather than stricter.

A member after a variable-length one is placed at run time and read like any
other: its arithmetic is the struct's own walk, and only where the read is
measured from differs. Its constraints are checked too, which is what the C
backend always did and what the other three had quietly skipped.

A *bit-packed* field cannot land there at all: section 8.2's solver refuses one
that follows a dynamically sized member, because a bit phase across a dynamic
boundary is not something it computes and a wrong bit offset is undetectable at
run time. Every backend asserts that rule rather than handling it, so relaxing
the rule fires an assertion instead of emitting wrong bytes.

**The gate is a struct with a private field.** Rust's privacy is module-scoped,
so no code outside the generated module can construct one, and the verified
open is the only thing that does. A test asserts it by building the forgery and
requiring `error[E0451]: field is private`.

**A register's `unsafe` is marked where it happens.** The bus access is
`read_volatile`/`write_volatile` through a raw pointer, `new` carries a
`# Safety` contract saying what the caller must promise, and every block has a
`// SAFETY:` note. Decision 0017 puts situ's unsafe surface at the bus and the
codec calls; a reader auditing a Rust codebase needs to see it rather than find
it.

**Invalidation is the borrow checker.** Section 12.3's rule is a generation
counter checked at run time in C and compiled out of a release build. Here a
write through `&mut` while a read view is outstanding does not compile, so the
counter is not carried at all -- and a test asserts exactly that by requiring
the offending program to fail to build.

Reads and writes are separate types, `Foo` and `FooMut`, which is what the
ecosystem does and means a reader who only parses never holds a `&mut` they do
not need. An enum field reads as `Option<T>`, because a field may hold a value
no member names and section 8.7 rejects those on parse rather than on read.

**`unused_parens` is a hard error under `-D warnings`, and it fires wherever a
generated expression composes.** The other three backends parenthesise freely,
which is how a generator stays composable: every sub-expression wraps itself
and the caller never has to know precedence. Rust rejects that on the tail
expression of a block and on a match arm alike, so a variant's extent -- an
`if`/`else` chain whose branches carry length expressions built elsewhere --
cannot simply nest them. The backend strips a fully-enclosing pair before
placing one in a branch, and builds the chain unparenthesised, wrapping once at
the end where it sits in a sum. Worth stating because the reflex is to reach
for `#[allow]`, and the lint is right: the parentheses really are redundant,
and the generator was the thing being sloppy.

**A schema is free to name a field `type`; Rust is not.** Raw identifiers carry
it -- `r#type` -- which is what they exist for, and decision 0013 says the
naming is the author's. `set_type` needs no escape, and `set_r#type` would not
be an identifier at all.

Codecs bind the C implementation (decision 0017), so a Rust program calling one
goes through `extern "C"` and therefore through `unsafe`, in the backend whose
argument is that the capability system becomes compile-time. Nothing generated
does that yet; when it does, the `unsafe` belongs at the call site rather than
buried.

### 26.19 Language server

**Status: diagnostics, hover and symbols work.** `situc lsp` speaks JSON-RPC
over stdio, standard library only: the framing is a length header and a JSON
body, and a dependency for that would be a poor trade against section 22's
rule about vendoring.

What it carries that an editor cannot get elsewhere is not syntax colouring:

- **Diagnostics**, with the blame chain intact. Section 17 makes the chain the
  product, so it travels as `relatedInformation` rather than being flattened
  into the message and lost at the first newline.
- **Hover**, giving the capability vector of the field under the cursor, with
  the weakened axes first and the rest listed compactly. This is `situc
  explain` with a cursor instead of a path, and it is the reason to run a
  server rather than a linter: thirteen axes of consequence the source text
  does not show.
- **Symbols**, outlining structs, their fields, and enums.

**It never raises.** An editor asks about a document in whatever state the user
has left it, and half-written is the normal state rather than the exceptional
one. Every failure becomes a diagnostic; a document that will not parse still
answers, with nothing to hover over rather than an exception.

**Full-document sync, deliberately.** A schema is a few hundred lines and
parsing one is microseconds. Incremental sync would be a cache to keep coherent
in exchange for time nobody is waiting on.

**Code actions and go-to-definition** are there too. The actions carry the
advisor's costs and are offered rather than applied: a suggestion like
"reorder the members so this one is last" is a change with a cost the author
has to agree to, and applying it silently would be situ making a design
decision on somebody's behalf.

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
`tests/unit/test_invariants.py` compares the backends instead of reading each.

### 26.21 Text protocols

**Reversing 26.21's earlier position, which was wrong.** It read: text-based
protocols are folded out, because "their fields have no offsets to be static
about, so the capability lattice has nothing to say about them". Kept here
rather than deleted, because what a position got wrong is the useful part.

It was wrong on the facts. Every axis says something about a delimited field,
and two of them -- `canonical` and `access` -- say more about text than about
binary. The machinery was already in the tree, firing for a construct of the
same shape. `docs/decisions/0020-delimited-data.md` has the evidence and takes
the decision; 8.6.1 is the language.

What the old position got right is kept: situ is not becoming a parser
generator. A full grammar stays out, because a parse tree has no offsets to be
static about -- which is the sentence the old position applied one level too
early.

Delivered:

- `T x[] until "D"`, with `max N` to bound the scan (8.6.1).
- `offset = Scanned` and `repr = TextConverted`, two distinctions that were
  being collapsed into `Dynamic` and `ValueConverted`.
- `[quoted]` and `[escape]`, for a protocol that admits the delimiter inside a
  field, at the cost of `canonical = NonCanonical`.
- Text-encoded numbers, so a length written as digits is a typed field that
  drives an array (8.6.2).
- Runs of records, so a header *block* is expressible and not only the
  individual lines of one (8.6.3).
- `[minimal]` on a text number, which is the `canonical` axis finally doing
  the work decision 0020 argued it was built for.
- **All four backends.** A real HTTP header block parses in C, C++, Python and
  Rust, to the same three fields and the same span, and each refuses the same
  malformed frames for the same reason and in the same order.

What each target adds over C, where the language allows it:

| | delimited member | what it enforces that C documents |
|---|---|---|
| C++ | methods on the view | `at()` is `[[nodiscard]] err`, so an out-of-range index cannot be ignored into a use of an uninitialised view |
| Python | properties | the text-number property raises rather than returning a sentinel |
| Rust | `&[u8]` slices | a slice carries its length, so it cannot be paired with the wrong count; `Result` is `#[must_use]`, so ignoring a bad parse does not compile |

- `[trim]` and `[case_insensitive]`, the two canonicity questions text asks
  that binary does not (8.6.4).

And three more that the worked examples asked for afterwards, which 26.23
records:

- A **fixed-width** text number, `decimal u16 code[3]`, which SMTP's reply
  code and HTTP's status both are and neither could be written (8.6.2).
- A region that is **delimited and coded**, which SMTP's dot-stuffed DATA
  body is: two constructs that existed and would not compose (8.6.5).
- `while`, a run ending after the element failing a test, which SMTP asked
  for and IPv6 extension headers asked for second (8.6.6).

Not covered, and worth naming so nobody designs around a limit that is not
there: a grammar -- alternation, repetition and rule references -- which is
out by decision rather than unfinished, because a parse tree has no offsets to
be static about.

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

Three protocols written down as schemas, and what writing them cost. Not a
phase: the roadmap had run out, and these were chosen because each has a
shape the tree did not already have.

**HTTP** (8.6.1--8.6.4). Delimited fields, a header block, a text
Content-Length driving a binary body. Writing it found three latent bugs, none
of them about HTTP: the expression parser did not know decision 0006's bracket
rule, so `max 16 [encoding = ascii]` indexed `16` by the attribute list;
nesting a variable-size struct named a `SIZE_FIXED` macro that is emitted only
for fixed ones, so any schema doing it produced C that did not compile; and
the member *after* such a struct was placed on top of it, because the offset
sum treated it as zero bytes wide.

**SMTP** (8.6.2, 8.6.5). A reply code is three digits with no delimiter, and
8.6.2 required `until` on every text number -- so the commonest fixed-record
shape in existence was unwriteable. Dot-stuffing needed a `coded` region that
is also delimited, which is two existing constructs that refused to compose.

And it asked for something it did not get: a multiline reply ends after the
line whose separator is a space, which no delimiter expresses. The schema said
so, named the construct that would cover it, and did not invent it.

**IPv6 extension headers** (8.6.6). The second protocol wanting a run that
ends after the element failing a test, which is what made `while` worth
having. It also found that `traverse.classify` called a member sized by
arithmetic over a field a SCALAR, so three backends read one byte and called
it the field.

**One protocol is not evidence; two is.** SMTP asked for `while` and was
declined, with the reason written into the schema. IPv6 asked for the same
shape with different fields, and that pair is the whole justification. The
cost of waiting was one example carrying a limitation for a while. The cost of
not waiting would have been a construct shaped around a single format, and
`until` had already been generalised once from `nul_terminated`.

The rule is not that two is a magic number. It is that the second asker is
what tells you which parts of the first were the protocol and which were the
shape.

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
limit and a shrug -- the earlier `examples/dns/dns.situ` had claimed situ could
not describe DNS names "statically at all", which was wrong by a wide margin.

### Invariants to hold across all phases

1. The propagation table (11.3) is data, not code. Adding a construct means
   adding a row and a test, never editing scattered conditionals. The same rule
   now covers backend dispatch: `traverse.classify` says what kind of member a
   placement is, in the one order that is safe, and a backend that grows its
   own gets the same two bugs three backends already shipped.
2. No capability may be strengthened by any construct. If an implementation
   seems to need that, the axis definition is wrong -- stop and ask.
3. Every diagnostic has a blame chain. A diagnostic without one is a bug.
4. Generated code never allocates, never recurses, never uses VLAs.
5. Requirements discharged at runtime rather than compile time must be reported
   as such. Never silently downgrade.
6. The expression language stays total. No construct may make it possible to
   reference a transform result.
7. Offsets are tracked in bits and reported in bytes only where byte-aligned
   (26.2). Retrofitting bit phase into a byte-based solver is a rewrite.
8. **Nothing about the machine running `situc` may reach the generated code.**
   The generator and the target are different machines on every cross build, so
   a decision taken here that belongs there is correct only by coincidence and
   silent when it is not. Byte order is the instance that got this wrong
   (`docs/decisions/0014-positional-directives.md`); it will not be the last.
9. Ambiguity is an error (17.0). Situ never guesses and never takes a silent
   default where the wrong choice is undetectable at runtime. Where a default
   does exist, the safe option is silent and the unsafe option is loud.
10. Diagnostic quality is the product, not a finishing touch. The exact text is
   snapshot-tested, and a regression in message quality is a real regression.
11. **A test that asserts absence has a shelf life; one that asserts behaviour
   does not.** Five tests in this repository asserted that some construct was
   unsupported, and every one of them became a false statement the day it was
   implemented -- each failing for the good reason, but each needing rewriting
   rather than deleting. Where a gap must be recorded, record it in the emitted
   artifact where a *reader* sees it, and test the behaviour that exists.
12. **A declared gap must be a real one.** Saying "this backend cannot do X" is
   more useful than silence and worse than silence when X cannot arise: a
   reader designs around a limit that is not there. Three backends declared
   they could not place a bit-packed field at a dynamic offset; the layout
   solver refuses the construct outright, so the declaration was fiction. They
   assert the rule now, and fire if it is ever relaxed.
13. **A second implementation is an audit of the first.** Enum membership was
   specified in 8.7, emitted as a comment by the C backend since phase 4, and
   enforced by nothing. It surfaced when a third backend made three answers
   comparable. Expect each new target to find something the existing ones
   agreed about only because they shared a code path.
14. **Answering a question means reading the code it points at, even when the
   answer is "nothing to do".** Open question 12 asked whether encrypt-then-code
   and code-then-encrypt were both expressible. They were, and always had been,
   because a pipeline says which stage runs first. But `sealed(C)` two lines
   away had been accepting a codec that authenticates nothing, so the stage
   gate of 14.3 would hand out a sealed interior on a flag nobody had checked.
   The question was adjacent to the bug rather than about it. A settled
   question is worth the read regardless of how it settles.
15. **A new construct lands in a schema the suite compiles, in the same
   commit.** `tests/schemas/edges.situ` exists to say this and its own header
   says it -- "a construct the language offers and nothing exercises is a
   construct whose generated code has never run" -- and the commit that added
   invariants did not follow it. The generated C contained a macro name with a
   space in it, which no compiler would take and no test tried. The narrower
   trap: the schema exercised by hand derived from a nested struct and an
   array, neither of which gets a setter, so the one path that pastes a name
   into an identifier was never reached. Exercising a construct means reaching
   the code it changes, not mentioning it.
16. **A label is not an identifier, and one string cannot be both for long.**
   `covered_by` holds "invariant total" because a diagnostic has to say that;
   generated code needs `total`. They were one string for as long as tags were
   the only obligation, and the day they stopped being one the compiler emitted
   `SITU_S_INVARIANT TOTAL_DIRTY`. Where a name is read by a human and by a
   code generator, carry both from the start.
17. **A derived fact gets one derivation.** Dirty-bit numbering was computed in
   two backends from two different lists, and a struct carrying both a tag and
   an invariant got different bits in C and in Python. This is invariant 1
   again, one level down: the table is data, and so is anything computed from
   it. `traverse.obligations` is the numbering; no backend counts for itself.
18. **A refusal must blame the right thing.** `checksum(s.a)` in an invariant
   reached every backend, and each declined it saying *this build* could not
   evaluate it -- accurate about a dynamic offset on a constrained target, and
   false about a question that exists nowhere. A reader told the build is at
   fault goes looking for a better compiler. Invariant 12 says a declared gap
   must be real; this is its other half -- a real gap must be attributed to
   whatever actually causes it.
19. **Two doors to one answer must open on the same room.** `situc explain`
   printed the blame chain for a weakened axis and LSP hover printed only the
   value, so the editor gave a worse answer than the CLI for the same field --
   while this project's stated reason for the server is that it is "this
   information behind a different door". Adding a construct means checking
   every artifact that describes a field, not the backends alone: `doc`,
   `map`, `explain`, hover and the advisor each answer for their own reader.

   The second pass of this found worse than an omission. `doc` labelled a
   delimited member `name[1]` and gave its size as the delimiter's width --
   an array of one, drawn as a fixed box -- because `array_count = 1` on the
   empty bracket form. Somebody implementing from that diagram writes a
   fixed-width parser. And the advisor told a versioned schema to move a
   member past a `[since]` one, at "cost: nothing": a reordering the compiler
   refuses, and one that would move bytes for every deployed peer if it did
   not. An artifact that says nothing is a gap; one that says something false
   is a defect, and both come from the same missed pass.

   The third pass found the worst of the three, and it had nothing to do with
   the construct that prompted it. `gen-fuzz` declared
   `buf[SITU_X_SIZE_FIXED]` for every struct, and that macro exists only where
   a struct has one size -- so it had never produced compilable C for anything
   with a length field, a `[remaining]` tail or a delimiter. The artifact
   whose entire purpose is to be run against hostile input did not build for
   the structs most likely to have a parsing bug, and had not since variable
   structs arrived. Nothing noticed because the only harness the build
   compiled was over a fixed-size schema. `edges.situ` is in that list now,
   which is invariant 15 applied to an artifact rather than to a construct.
20. **The shared classifier is only shared if the first backend uses it too.**
   `traverse.classify` was written after three backends shipped the same two
   dispatch bugs, and the C backend -- which had not had them -- was left with
   its own walk. So when `until` arrived, the one place the fact "a delimited
   member is not an array" belonged was the one place it was missing, and the
   other three inherited a classifier that called `x[] until "D"` an ARRAY:
   the empty bracket form carries `array_count = 1`, because one *run* is one
   thing to count. All three then emitted a fixed array of one element at a
   static offset. A shared rule with an exempt caller is a rule with a hole
   in the shape of that caller.
21. **Agreeing on the answer is not agreeing.** Two backends can return the
   same value and disagree about which of two true things went wrong. Python
   reported "these digits are not a number" where C reported "this frame stops
   early", for a frame that was both; both are `CONSTRAINT`, both are correct,
   and nobody would notice until two people compared notes on the same
   capture. Where more than one check can fire, the order is part of the
   schema's meaning and is fixed in one place.
22. **A test that greps generated source does not run it.** Rust's `validate`
   omitted the delimited case entirely: a frame with `5x` where a length
   belonged passed validation and panicked in the accessor. Nothing that
   inspected the emitted text would have seen it, because the bug was the
   absence of text. Each of these three backends was finished by running the
   same HTTP header block through it and comparing bytes, and each time that
   found something reading would not have.
23. **Generated code that warns teaches a reader to ignore warnings.** The
   Rust backend imported `Dirty` unconditionally, so every schema without a
   tag or an invariant warned on sight. `-D warnings` on generated output is
   worth keeping green for the same reason `-Werror` is on the runtime: the
   first ignorable warning makes the next one ignorable too.
24. **A guard against non-termination is not a comment.** A record whose
   members are all delimited and all empty occupies no bytes, and a walk that
   advanced by that would not return on input somebody chose. That is a denial
   of service rather than a wrong answer, so every generated walk is bounded
   twice: by the view's extent, and by refusing to advance on a zero-length
   element. Generated code never allocates or recurses (invariant 4); it must
   not loop unboundedly either.
25. **Do not record a fact that is not one, however convenient.** The layout
   solver set `array_count = 1` on `x[] until "D"` -- one run counted as one
   element -- and four consumers believed it: the classifier called it an
   ARRAY, `doc` labelled it `x[1]` and drew a one-byte box, the dissector read
   one byte and misaligned the rest of the packet, and `gen-checks` sized a
   synthesised instance from it. Each needed its own code to disbelieve it,
   and every one of those was written as a bug fix without anyone asking why
   four places needed the same one.

   Deleting it then exposed two more sites that had been *right by accident*:
   the dissector and `gen-checks` both reached their delimited branch because
   a check above them failed, and it failed because of the lie. A false fact
   in the model is not contained by the code that works around it -- it
   becomes load-bearing, and the workarounds hold up the wrong thing.

   Removing it also has a second-order form that is harder to see. `size_bits`
   on a delimited member is its *delimiter's* width -- a true lower bound, and
   the one number that is not the answer to "which bytes does this member
   touch". `byte_span` answered with it, so once the count was gone the
   dissector declared `ProtoField.uint8` for an HTTP header name and Wireshark
   would have shown a variable-length text field as a single decimal number. A
   flatly false value gets found; a true value answering a different question
   does not.
26. **An assertion weaker than the property passes on broken code.** The
   cross-backend check that every target refuses a versioned read searched for
   `"version"` and passed on C++ while C++ was still emitting an ungated
   getter -- because that substring is in the doc comment saying which version
   a member arrives in, which an ungated getter carries too. Each refusal is
   spelled out now. Invariant 22 says a test that greps does not run; this is
   the case where it greps for the wrong thing and looks like it ran.
27. **Silence is worse than a crash.** Three backends met `[since]` and
   emitted a plain getter, building without complaint, so a caller reading a
   v2 field from a v1 message got whatever followed in the buffer. The same
   three met `until` and raised an `AssertionError` out of the layout module,
   which is ugly and told somebody immediately. Where a backend cannot do
   something, refusing loudly is the floor; emitting something that compiles
   and is wrong is below it.
28. **Reading the wrong bytes is a wrong answer; writing them is somebody
   else's data.** Every backend gated the getter for a versioned member and
   left the setter open, C included and first. Writing a `[since = 2]` field
   to a v1 message puts those bytes past the end of that message. Accessor
   pairs are not symmetric in consequence, and the write side is the one that
   escapes the object.
29. **Generated code shares a namespace with names the schema chose.** The C++
   factory named its own parameter `msg`, so any schema with a struct called
   `msg` emitted a header where the parameter shadowed the class and the file
   did not compile.

   Renaming that parameter fixed one instance and this invariant then claimed
   the class of them, which was false: a generated *local* called `e` shadowed
   a class called `e` the next time a schema used that name, in the same
   backend. Renaming is the wrong instrument -- a schema may call a struct
   anything, and every rename is the same bet at longer odds. The fix is to
   make the reference unambiguous instead: `::situ::e` cannot be shadowed by
   a local, whatever it is called. Where a language offers no such
   qualification, the compile lists carry the collision, which is the thing
   that actually catches the next one.
30. **A real protocol finds what a synthetic schema does not.** `edges.situ`
   exercises every construct deliberately and had not found any of: a bracket
   ambiguity in the expression parser, a nested variable struct naming a macro
   that does not exist, a member placed on top of the one before it, a
   fixed-width text number being unwriteable, or two constructs that refused
   to compose. Three real formats found all five between them, because a
   protocol combines constructs in the order its designers needed rather than
   the order the compiler's author thought of. Each new example is worth
   writing until one of them stops finding something.
31. **One protocol asking is not evidence; two is.** SMTP wanted a run ending
   on a test over the element just read, and did not get it: the schema
   recorded the limitation and named the construct that would cover it. IPv6
   extension headers wanted the same shape with different fields, and `while`
   was built then. The cost of waiting was one example carrying a limitation;
   the cost of not waiting would have been a construct shaped around one
   format. The second asker is what separates the protocol from the shape.
32. **A language that has two constructs and cannot compose them is the
   anomaly.** The same judgement cut the other way for dot-stuffing. `until`
   and `coded` both existed and a region could not be both, which is not a
   missing feature but a missing edge -- so it was built on one protocol
   asking, where `while` waited for two. What distinguishes them is whether
   anything new is being invented.
33. **C's integer promotion is a guarantee that runs out.** `(len + 1) * 8`
   over a `u8` field is correct in C because the read is widened to `int`
   before the arithmetic, and wrong in Rust, which has no such rule -- 255 + 1
   is 0 there. Rust refusing it is the useful signal: the same expression over
   a `u32` field wraps in both languages, and only one of them said so. Where
   generated arithmetic depends on a width the schema did not state, state it.

34. **Two modules deciding the same thing separately will decide it
   differently.** The C emitter worked out whether a struct's extent could be
   measured from its own bytes, and `gen-checks` worked it out again to decide
   whether a check could call the accessor. They disagreed on a nested struct
   whose only member was an unwalkable run: the emitter declined to write the
   sub-view, the check suite called it anyway, and the generated tests failed
   to compile -- which is the same class of wrong as the header failing to.
   The fix was not to copy the guard across; it was to notice the predicate is
   a fact about the *layout*, not about C, and move it to `traverse.py` where
   both ask one function. Whenever two backends need the same answer, that is
   evidence the question belongs to neither.

35. **A probe that only compiles asserts that the names exist.** The test for
   the bit-packed extent wrote a C probe checking that the second element of a
   run sits three bytes after the first, and compiled it with `-c`. It passed
   before the fix and after it, because `main` was never run. Changing the
   expected offset to a wrong number and watching the test still pass is the
   cheapest way to find out which kind of test you have written -- and I had
   to do it, having got the same thing wrong in the assertion right above it,
   where I guessed the shape of the emitted arithmetic instead of reading it.

36. **Fixing it in one backend finds it; fixing it in one backend is not
   fixing it.** The nested member reaching for an extent that was never
   emitted was found in C, and the same three lines were wrong in C++, Python
   and Rust -- each with a comment recording an *earlier* round of the same
   mistake, patched by tightening the condition on the accessor and never on
   the thing it called. Three sites per backend: the accessor, the offset of
   whatever follows it, and `validate`, which reaches through the accessor on
   the path least likely to be exercised. The lesson is not "check the other
   backends" but the one above it: the condition was duplicated because the
   predicate was, and the predicate had no business being in a backend.

37. **"It cannot be computed" is often "it is not a constant".** A variant's
   extent was refused outright on the grounds that it depends on the
   discriminant -- which is an argument that the extent is a switch, not that
   there is none. The refusal propagated: no extent meant no run over a
   variant, which meant the DNS example could describe a compressed name and
   not walk one, which is the thing it exists to show. When a fact is unknown
   at compile time, ask whether it is knowable at run time before recording
   that it is unknowable.

38. **A false fact is load-bearing by the time you find it.** `array_count =
   1` on a `while` run was the same lie invariant 25 removed from delimited
   members, left behind on the other construct that carries it. Removing it
   broke four things, and every one of them had been right by accident:
   `gen-checks` classified the run as a nested struct and called a `_view` that
   is not emitted for one; the instance walk placed the member after the run at
   an offset the run walks straight past; `doc` labelled it `x[1]`; and the
   nested copy of the placement had been getting `access = Sequential` from the
   generic variable-element row rather than from the run's own. The map came
   out identical before and after, which is the part worth remembering: two
   errors cancelling is indistinguishable from correctness until you fix one.

39. **A hand-maintained list of "which facts to carry" is wrong already.** The
   nested copy of a placement -- the same member seen from its parent --
   enumerated the fields to carry across, and had fallen six behind, among
   them `repeat_while`. Use `dataclasses.replace` and name what *differs*
   instead: for a copy that is the same bytes seen from somewhere else, that
   is the path, the offset, and what it cost to reach. The list of differences
   is short and stable; the list of samenesses grows with every field added.

40. **A capability nothing exercised hid a check nothing emitted.** Making a
   variant walkable immediately showed that `default: error` was enforced by
   no backend, in a language whose spec had said it was since section 14.5 was
   written, with an error code reserved for it and returned by nothing. Dead
   ends hide their own bugs: the reason nobody noticed is that nothing could
   reach a variant closely enough to care what an unmatched discriminant did.

---

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
   best. See 8.6.1 and `docs/decisions/0020-delimited-data.md`.
2. ~~**Multiple tags with nested coverage.**~~ **RESOLVED.** Nested coverage is
   permitted and recomputes innermost first, which is the only order that
   terminates: an outer tag covers the inner tag's own bytes, so writing the
   inner one afterwards would leave the outer one stale again. Innermost is
   narrowest, since coverage is disjoint or nested by 14.1, so the sizes of two
   coverage sets order them. `docs/decisions/0011-nested-tag-coverage.md`.
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
   `docs/decisions/0008-slack-tracking.md`.
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
   `docs/decisions/0005-integer-widths.md`.
7. ~~**Whether `native` endian should be permitted at all.**~~ **RESOLVED.**
   Split into two constructs (Section 8.3): `endian native` for genuinely
   host-order formats, gated behind `[allow_host_dependent]` and non-canonical;
   and `endian_marker` for runtime-resolved byte order, which is
   `CanonicalGiven(marker)` and costs nothing on the offset or size axes.
   `docs/decisions/0014-positional-directives.md` carries the harder half of
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
   doubled. `docs/decisions/0016-composed-expansion.md`.
10. ~~**Bit phase in the public API.**~~ **RESOLVED: forbidden at the
   surface**, which was the option the question guessed at. A struct whose
   size is not a whole number of bytes gets no accessors in any backend, and
   `gen-checks` says so in the emitted file rather than skipping quietly:
   `s: not a whole number of bytes, so it has no accessors`.

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
   generally. `docs/decisions/0019-sealing-requires-authentication.md`.
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
   `docs/decisions/0019-sealing-requires-authentication.md` carries both
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
