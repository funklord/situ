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
| Capability map file | `*.situ.map`|

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

enum MsgType : u8 {
    hello = 0x01,
    data  = 0x02,
    close = 0x03,
}

struct Flags {
    bit  urgent;
    bit  ack;
    u3   priority;
    reserved u3 [must_be_zero];
}

struct Header {
    u8       version  [must_eq = 1];
    MsgType  type;
    Flags    flags;
    u16      length   [max = MAX_PAYLOAD];
    u32      seq      @ 0x06;          // pin: assert solver agrees
}

require size(Header) == 10;
require absolute_static(Header);
require in_place(Header.seq);
```

### 5.2 Dynamic frame with static islands

```situ
struct Record {
    u32  id;
    u16  kind;
    u16  value;
}

struct Message {
    Header          hdr;
    u8              opts[hdr.length];   // dynamic: shifts everything after
    Record          recs[hdr.rec_count];// frame-relative static elements
    u8              trailer[remaining];
}

// Record is fully static once its base is resolved. Assert that:
require frame_static(Message.recs[]);
require in_place(Message.recs[].value);
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

struct Packet {
    authenticated {
        Header  hdr;
        u8      nonce[12];
    }

    sealed(aes_gcm_128, nonce = nonce) {
        u16  inner_kind;
        u32  inner_seq;
        u8   body[remaining];
    }

    tag u8[16];                 // coverage inferred: all authenticated
                                // and sealed regions in this struct
}

require canonical(Packet);
require in_place(Packet.hdr.seq);              // outside tag coverage? no --
                                               // will fail; see 14.2
require verify_gated(Packet.sealed);           // no parse before verify
```

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

directive     = "target"    target_kind ";"
              | "endian"    endian ";"
              | "bit_order" bitorder ";"
              | "import"    string ";" ;

target_kind   = "buffer" | "mmio" ;
endian        = "big" | "little" | "native" ;
bitorder      = "msb_first" | "lsb_first" ;

decl          = const_decl | enum_decl | struct_decl | codec_decl
              | register_decl | requirement ;

const_decl    = "const" ident "=" expr ";" ;

enum_decl     = "enum" ident ":" scalar_type "{" enum_body "}" ;
enum_body     = { ident "=" expr "," } [ "default" "=" ("error"|"pass") ] ;

struct_decl   = "struct" ident [ attrs ] "{" { member } "}" ;

member        = field | reserved | block | variant | tag_field ;

field         = type_ref ident [ array_spec ] [ pin ] [ attrs ] ";" ;
reserved      = "reserved" scalar_type [ array_spec ] [ attrs ] ";" ;
tag_field     = "tag" scalar_type array_spec [ "covers" "(" ref_list ")" ]
                [ attrs ] ";" ;

block         = "positional"    "{" { member } "}"
              | "authenticated" "{" { member } "}"
              | "sealed" "(" codec_args ")" "{" { member } "}"
              | "indexed" "(" index_args ")" "{" { member } "}"
              | "opaque" ident "[" size_expr "]" [ attrs ] ";"
              | "tlv" ident "(" tlv_args ")" [ attrs ] ";" ;

variant       = "variant" ident "switch" "(" expr ")" "{"
                { "case" expr ":" member }
                [ "default" ":" ( member | "error" | "opaque" ) ]
                "}" ;

array_spec    = "[" [ size_expr ] "]" ;
size_expr     = expr | "remaining" ;
pin           = "@" expr ;

attrs         = "[" attr { "," attr } "]" ;
attr          = ident [ "=" expr ] ;

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
```

`register_decl` is defined in Section 15.

## 8. Type system

### 8.1 Scalars

- `u1` .. `u64`, `i1` .. `i64`. Non-power-of-two widths are legal and imply
  bit packing (`u3`, `u12`, `u48`).
- `f16`, `f32`, `f64` (IEEE 754).
- `bool` is `u1` with value constraint; `byte` is an alias for `u8` with
  `endian` irrelevant.
- Widths >= 8 that are multiples of 8 are byte-aligned by default; others
  participate in bit packing with the surrounding fields.
- OPEN: fixed-point (`q16_16`) and BCD. Deferred to a later phase; do not
  implement in v0 but leave the type table extensible.

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

- Both are scoped: file-level directive, overridable per struct via
  `[endian = little]`, overridable per field.
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

**3. Runtime-resolved endianness via a byte-order marker.** This is the TIFF
`II`/`MM` pattern, and it is a first-class construct:

```situ
endian_marker byte_order : u16 {
    little = 0x4949,        // "II"
    big    = 0x4D4D,        // "MM"
}

struct TiffHeader [endian = from(byte_order)] {
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

### 8.7 Enums

- Backing type mandatory: `enum E : u8 { ... }`.
- `default = error` (reject unknown values on parse) or `default = pass`
  (accept and preserve). Default is `error`, deliberately: see 14.5.
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
    Record entries[];
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
    case MsgType.hello: Hello hello;
    case MsgType.data:  Data  data;
    case MsgType.close: Close close;
    default: error;
}
```

The discriminant must be parsed strictly before the variant in layout order;
forward references are an error. Size of the variant is the size of the
selected arm, so unless all arms are the same size the variant makes everything
after it dynamic -- which the advisor will point out, along with the padding
cost of equalizing them.

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

struct ProtoMessage {
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
ProtoMessage           size=Unbounded  offset=AbsoluteStatic(0)
ProtoMessage.fields    access=Sequential  address=Unstable  mutate=RewriteRequired
ProtoMessage.fields[*] offset=Dynamic
canonical = NonCanonical, five independent causes:
  - non-minimal varint encodings accepted (pb_varint has no `minimal`)
  - duplicate_tags = allowed with no ordering rule
  - unknown = preserve
  - field order is unconstrained
  - packed and unpacked repeated encodings both legal
```

**This is the acceptance test.** `situc explain ProtoMessage` must enumerate all
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
| `offset` | `AbsoluteStatic(n)` > `FrameStatic(n)` > `Dynamic` | position knowledge |
| `access` | `Random` > `Sequential` | can reach element N directly |
| `mutate` | `InPlaceFixed` > `InPlaceSlack` > `Shifting` > `RewriteRequired` > `Immutable` | write cost |
| `address` | `Stable` > `FrameStable` > `Unstable` | can a pointer be held |
| `align` | `Aligned(n)` > `Unaligned` | relative to message base |
| `repr` | `MemoryIdentical` > `ValueConverted` > `ConditionallyConverted(f)` | is the value literally the bytes |
| `atomic` | `AtomicWord` > `NonAtomic` | single-instruction access possible |
| `canonical` | `Canonical` > `CanonicalGiven(f)` > `NonCanonical` | exactly one valid encoding |
| `stage` | `CompileTime` < `ParseTime` < `TransformTime` < `VerifyGated` | when resolvable (later = more gated) |
| `auth` | `Uncovered` / `Covered(tag)` | which tag covers these bytes |
| `secrecy` | `Public` / `Secret` | affects generated API |
| `effect` | `Pure` > `EffectOnRead` / `EffectOnWrite` / `EffectBoth` | MMIO side effects |

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

These are normative. Implement them as a table, not as scattered conditionals.

| Construct | Effect |
|---|---|
| fixed-size scalar, byte-aligned, native endian | identity (all strongest) |
| non-native endian scalar | `repr := ValueConverted` |
| bit field | `repr := ValueConverted`, `atomic := NonAtomic`, `mutate := max(mutate, InPlaceFixed)` but write is RMW |
| straddling bit field | as above, plus `atomic := NonAtomic` and a warning |
| unaligned multi-byte scalar | `align := Unaligned` |
| array `[N]` const | element vectors preserved; `offset` of element k is base + k*size |
| array `[expr]` | elements `offset := FrameStatic`; **all following members** `offset := Dynamic`, `address := Unstable` |
| array `[remaining]` | `size := Bounded(0, frame_remaining)`; must be last in frame |
| dynamic-size element type | `access := Sequential` |
| variant with unequal arm sizes | following members `offset := Dynamic` |
| `opaque` | interior: none; region: `access := Sequential`, no interior addressing |
| `tlv` | `access := Sequential`, items `address := Unstable`, `offset := Dynamic` |
| `indexed` | elements `offset := FrameStatic` after one indirection; `access := Random`; insertion unsupported |
| entering a frame with dynamic base | all interior `offset := FrameStatic` (not Absolute), `address := FrameStable` |
| `endian native` | enclosing struct `canonical := NonCanonical`; requires `[allow_host_dependent]` |
| `endian = from(marker)` | fields in scope `repr := ConditionallyConverted(marker)`, `canonical := CanonicalGiven(marker)`; `offset` and `size` unaffected |
| `reserved [unknown]` | enclosing struct `canonical := NonCanonical` |
| `tlv unknown = preserve` | enclosing struct `canonical := NonCanonical` |
| enum `default = pass` | enclosing struct `canonical := NonCanonical` |
| `authenticated { }` | members `auth := Covered(t)` for enclosing tag t |
| `sealed(codec) { }` | see Section 13.3 |
| `secret` attribute | `secrecy := Secret`; suppresses debug accessors, adds zeroization |
| register with `no_rmw` | single-bit/partial fields `mutate := RewriteRequired` |
| register with `EffectOnRead` | any field needing RMW `mutate := RewriteRequired` |

**The critical rule, stated once:** a construct with dynamic size weakens the
`offset` and `address` axes of every *subsequent* member of its enclosing
frame, and of nothing else. It does not weaken members of parent frames before
it, and it does not weaken its own interior. This locality is what makes
islands of staticness work.

### 11.4 Worked example

For 5.2 `Message`:

```
Message.hdr              offset=AbsoluteStatic(0)  size=Fixed(10)   mutate=InPlaceFixed
Message.hdr.seq          offset=AbsoluteStatic(6)  repr=ValueConverted (big endian)
Message.opts             offset=AbsoluteStatic(10) size=Bounded(0,1500) mutate=Shifting
Message.recs             offset=Dynamic            access=Random
Message.recs[]           offset=FrameStatic(0)     size=Fixed(8)    address=FrameStable
Message.recs[].value     offset=FrameStatic(6)     mutate=InPlaceFixed
Message.trailer          offset=Dynamic            size=Bounded(0,...)
```

`require in_place(Message.recs[].value)` passes. `require absolute_static(Message.recs)`
fails with blame on `Message.opts`.

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
| length | `length_preserving` \| `expansion = +N` \| `expansion = ratio_exact(a,b)` \| `expansion = ratio_bounded(a,b)` \| `expansion = unbounded` | output extent as a function of input extent |
| `seekable` | `linear` \| `permuted` \| `blockwise(N)` \| `none` | class of the output-position function |
| `granularity` | `bit(N)` \| `symbol(N)` \| `byte` \| `block(N)` \| `stream` | minimum independently-transformable unit |
| `systematic` | flag | input data appears verbatim in the output at computable positions |
| `authenticated` | flag | produces or consumes a tag over a declared range |
| `invertible` | flag | inverse exists |
| `deterministic` | flag | same input, same output, always |
| `error_propagating` | flag | a corrupted input unit damages more than its own output unit |

Three of these are new relative to the first draft and each was forced by a real
codec class:

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
| **table** | input symbol -> output symbol map | Manchester, 4b5b, 8b10b, NRZI, Gray, BCD | `ratio_exact` from symbol widths; `seekable = linear`; `granularity = symbol(N)`; `deterministic`; not `systematic` |
| **polynomial** | generator polynomial over GF(2) or GF(2^m), plus init/reflect/xorout | CRC (all variants), Reed-Solomon, BCH | `expansion = +N`; `systematic` for appended-parity forms; `seekable = linear`; parity recompute scope = block |
| **linear block** | generator or parity-check matrix over GF(2) | Hamming, extended Hamming, LDPC, arbitrary block codes | `ratio_exact(n,k)`; `systematic` iff the matrix is in standard form; `seekable = blockwise(n)` |
| **shift register** | taps, feedback source, initial state | convolutional codes, additive and multiplicative scramblers, Miller | `length_preserving` or `ratio_exact`; `seekable = linear` iff feedback is from input only; `not seekable` and `error_propagating` if feedback is from output |
| **permutation** | index mapping, closed form or table | block and convolutional interleavers | `length_preserving`; `seekable = permuted`; `deterministic` |
| **stuffing** | trigger predicate plus insertion rule | HDLC bit stuffing, COBS, SLIP, byte stuffing | `expansion = ratio_bounded`; `not seekable`; interior addressing lost |

Pipelines compose: `codec framed = rs_255_223 |> interleave(16) |> manchester;`
Property composition is pointwise and conservative -- the pipeline is seekable
only if every stage is, systematic only if every stage is, and the expansion is
the product of the stages' expansions.

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

The example in 5.3 deliberately contains a failing requirement:
`require in_place(Packet.hdr.seq)` where `hdr` is inside `authenticated { }`.
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
canonical(X)            deterministic(X)
aligned(X, n)           atomic(X)
no_tag_invalidation(X)  verify_gated(X)         uncovered(X)
no_alloc(X)             bounded_stack(X, n)
```

Runtime-checked variants exist where the property depends on runtime values:
`no_realloc(expr)` cannot be decided statically when the new value's size is
dynamic, so it compiles to a runtime check in `SITU_CHECKED` builds. The
compiler must report, per requirement, whether it was discharged statically or
deferred to runtime. Silently downgrading a static check to a runtime one is
not acceptable.

---

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
41 | require absolute_static(Message.recs);
   | ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
   |
   = offset(Message.recs) is Dynamic, required AbsoluteStatic
   = caused by: Message.opts has size Bounded(0,1500)
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

`situc advise schema.situ` runs the catalog and prints ranked suggestions.
`situc explain Message.recs[].value` prints one field's full vector plus the
blame chain for every axis that is not at its strongest value.

### 18.3 Revision diff

`situc diff old.situ new.situ` reports capability regressions and improvements
between two schema revisions: fields that lost in-place mutability, size bounds
that grew, canonicity that was lost. Intended for code review and CI.

---

## 19. Schema evolution

Situ has no field numbers, so evolution must be explicit. This is the one place
where protobuf's design bought something real, and the replacement must be
deliberate rather than accidental.

The model:

1. **Version is a field, not metadata.** Schemas that need to evolve carry an
   explicit version discriminant and use `variant` to select the layout.
2. **Old revisions are kept in the schema**, not deleted:

```situ
variant body switch (hdr.version) {
    case 1: BodyV1 v1;
    case 2: BodyV2 v2;
    default: error;
}
```

3. **`situc diff` is the compatibility linter.** It reports whether a new
   revision changed the layout of an existing version arm (a wire break) versus
   only added a new arm (compatible).
4. **Never silently accept unknown versions.** `default: error` is required
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

## 20. Code generation

### 20.1 Backends

- **C (C11) is the primary backend.** Target is embedded: no allocation, no
  libc dependency beyond `<stdint.h>` and `<string.h>`, no recursion, bounded
  stack, no VLAs, MISRA-friendly where it does not conflict with clarity.
- **Rust is the second backend.** It expresses the capability system far better
  (typestate as distinct types, invalidation as borrows) and should be built
  once the C backend has proven the lattice. Do not start it before phase 11.

### 20.2 Generated C API shape

Per schema, emit `<name>.h` and `<name>.c`.

Naming: `situ_<Struct>_<field>_get`, `_set`, `_view`. Prefix configurable.

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
situc advise  <schema>            ranked design suggestions with costs
situc explain <schema> <path>     one field's capability vector and blame chains
situc diff    <old> <new>         capability regressions between revisions
situc doc     <schema>            byte-layout documentation
situc gen-tests   <schema> <vectors>
situc gen-fuzz    <schema>
situc gen-dissector <schema>
situc dump-ast <schema>           debugging aid, phase 1 deliverable
situc gen-codec-tests <schema>    property tests from codec signatures
situc import-proto <proto> -o <schema>
```

Global flags: `--target=c|rust`, `--out=DIR`, `--diagnostics=text|json`,
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
| generated code behavior | cmocka | compile generated C and exercise it; use `--wrap` for syscall-level mocking where needed |
| offset constancy | cmocka + disassembly check | verify view field access compiles to constant offsets |
| round-trip | pytest + hex vectors | parse then re-emit must be byte-identical for canonical schemas |
| fuzz | libFuzzer | generated harnesses run in CI for a bounded time |
| diagnostics | pytest | snapshot-test the exact diagnostic text; regressions in message quality are real regressions |

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
  docs/
    decisions/                append-only numbered decision records
    grammar.ebnf              extracted, kept in sync with Section 7
    capability-axes.md        normative axis definitions
  situc/
    __init__.py
    lexer.py
    parser.py
    ast.py
    types.py                  scalar type table
    expr.py                   expression evaluation and interval arithmetic
    layout.py                 the layout solver
    capability.py             axes, lattice, meet
    propagate.py              the 11.3 table, data-driven
    requirements.py           predicate evaluation and discharge
    diagnostics.py            diagnostic construction, blame chains, rendering
    advise.py                 suggestion catalog and cost model
    codegen/
      c/
      rust/                   phase 11
    cli.py
  runtime/
    c/
      situ.h                  minimal runtime: views, bounds, generation
      situ.c
  tests/
    unit/
    propagation/
    golden/
    generated/                cmocka tests over generated C
    schemas/                  example schemas including the three in Section 5
  examples/
    packet.situ               the encrypted protocol from 5.3
    registers.situ            the MMIO example from 15.2
```

## 24. Build system

Both CMake and GNU Make, maintained in parallel, as separate and independently
usable entry points.

- Sub-projects (compiler, runtime, generated-code tests) must be fully
  self-contained. The parent injects values via environment export for Make and
  via cache variables for CMake. **No shared include files between
  sub-projects.**
- Beware the GNU Make `LD` quirk: the built-in default `LD=ld` means `?=` never
  fires. Use `$(origin LD)` to distinguish a built-in default from an explicit
  user choice.
- Cross-compilation must work out of the box for aarch64; the generated code
  and runtime must build clean for a Cortex-A55 target with
  `-Wall -Wextra -Werror -Wconversion -Wsign-conversion`.

## 25. Conventions

- **ASCII only** in all source, comments, and docstrings. Non-ASCII belongs
  only in intentional runtime data values, and there are none in this project.
- **Tabs carry structural indent level; spaces carry alignment within a level.**
  Continuation lines use one tab for indent, then spaces to the alignment
  column. If lines are short enough to merge, merge rather than align.
- No prescriptive tab width anywhere in the codebase or in generated output.
  Elastic tabstops are the model: the viewer decides width.
- Generated C follows the same tab/space rule.
- Line length: soft 100 columns; do not sacrifice clarity to it.
- Every module has a docstring stating its single responsibility. If a module
  needs two sentences joined by "and", split the module.
- Single source of truth: the AST is built once from the source text and all
  passes read it. Never re-parse generated output. Never mutate a file
  in a second pass without full knowledge of the first pass's state.

---

## 26. Implementation plan

### 26.0 Phase 0: scaffolding

- Repo layout per Section 23; CMake and Make both building an empty target.
- pytest and mypy strict running in CI; cmocka wired up and running one trivial
  test over hand-written C.
- `docs/decisions/0001-implementation-language.md` recording the Python choice.

**Acceptance:** `make test` and `cmake --build . --target test` both pass with
zero warnings on host and aarch64 cross.

### 26.1 Phase 1: front end, static subset

Lexer, parser, AST for: directives, `const`, `enum`, `struct`, scalar fields,
fixed arrays, `reserved`, bit fields, endian/bit_order scoping, offset pins,
attribute lists. Reject everything else with "not yet implemented" plus the
phase number that will add it.

**Acceptance:** `situc dump-ast` round-trips example 5.1 exactly. Recursive
type declarations are rejected with a clear error. 40+ parser tests including
malformed input.

### 26.2 Phase 2: layout solver, static subset

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

Implement axes, meet, and the propagation table as data. Implement `require`
and `assert` for the static subset predicates. Implement blame chains and the
full diagnostic renderer plus JSON output.

**Acceptance:** one passing test per row of the 11.3 table that is reachable in
the static subset; every failing requirement produces a blame chain that names
the correct root cause; `--diagnostics=json` output validates against a
committed schema.

### 26.4 Phase 4: C backend, static subset

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

Expression language with interval arithmetic. Counted arrays, length-driven
sizes, `remaining`. Frame detection and frame-relative staticness. Views,
generation counters, invalidation rules. Capability propagation for the dynamic
constructs.

**Acceptance:** example 5.2 produces the map in Section 11.4 exactly;
`require frame_static(Message.recs[])` passes and
`require absolute_static(Message.recs)` fails with blame on `opts`; a cmocka
test proves a stale view is caught in `SITU_CHECKED` and that mutation of a
preceding length field increments the generation.

### 26.6 Phase 6: variants, opaque, TLV, indexed, varints

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
with `protoc` and a reference implementation), and `situc explain ProtoMessage`
must enumerate all five causes of non-canonicity with source locations. Do not
proceed to phase 7 until this passes. It is the sharpest single test of whether
the capability system is real.

### 26.7 Phase 7: transforms, tier 1 (extern codecs)

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

`authenticated`, `sealed`, `tag` with coverage inference, `nonce`, `secret`.
Tag-dirty tracking. `VerifyGated` staging with no unverified interior access.
`require canonical` covering all of 14.4. Strictness policy.

**Acceptance:** example 5.3 compiles; the interior of a sealed region is
unreachable before verification and a test attempting it fails to compile;
mutating a covered field marks the tag dirty and finalize recomputes it;
`[secret]` fields have no debug accessor and are zeroized; every item in the
14.4 checklist has a test.

### 26.9 Phase 9: advisor

Suggestion catalog, cost model with typical and worst case, `situc advise`,
`situc explain`, `situc map --check`, `situc diff`.

**Acceptance:** `advise` on a deliberately badly-ordered schema produces the
field-reordering suggestion with a correct byte cost; `map --check` fails on an
uncommitted capability change; `diff` correctly identifies a regression from
`InPlaceFixed` to `Shifting`.

### 26.10 Phase 10: MMIO target

`target mmio`, `register`, `register_block`, access modes, side effects, scoped
defaults, and the capability interactions in 15.3.

**Acceptance:** example 15.2 generates an API with no `set_enable()` and a
diagnostic explaining why; `w1c` generates `clear_error()`; volatile access is
emitted correctly and verified by disassembly; `ro`/`wo` asymmetry holds.

### 26.11 Phase 11: Rust backend

Typestate for stages, borrows for view invalidation, `zerocopy`-style traits
where the `repr` axis permits. This is where the capability system is expressed
most naturally, and it will expose any place where the C backend papered over a
soundness gap.

### 26.12 Phase 12: transforms, tier 2 (derived codecs)

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

**Acceptance per family:** derived properties match a hand-written signature for
a known code; generated implementation passes vectors from an independent
reference implementation; a pipeline of two families composes properties
correctly and conservatively.

### 26.13 Phase 13: `.proto` importer

Per Section 19.2. Deliberately after tier 2, because the importer is only useful
once the language it targets is complete, and because it is the one component
where a silent partial success does real harm.

**Acceptance:** a `.proto` file exercising every translatable construct produces
a schema that parses vectors generated by `protoc`; a `.proto` using `map`,
`Any`, or groups exits non-zero with a fidelity report naming each construct and
its source location; `--accept-lossy` downgrades to warnings and the resulting
schema still compiles.

### 26.14 Beyond

Documentation generation; Wireshark dissector; `until`-delimited arrays;
fixed-point and BCD types; LSP.

### Invariants to hold across all phases

1. The propagation table (11.3) is data, not code. Adding a construct means
   adding a row and a test, never editing scattered conditionals.
2. No capability may be strengthened by any construct. If an implementation
   seems to need that, the axis definition is wrong -- stop and ask.
3. Every diagnostic has a blame chain. A diagnostic without one is a bug.
4. Generated code never allocates, never recurses, never uses VLAs.
5. Requirements discharged at runtime rather than compile time must be reported
   as such. Never silently downgrade.
6. The expression language stays total. No construct may make it possible to
   reference a transform result.

---

## 27. Open questions

Recorded rather than resolved. Each needs a decision record before the phase
that depends on it.

1. **`until`-delimited arrays.** Genuinely useful for existing protocols,
   but sequential-only and awkward to bound. Phase 6 or later; may be dropped.
2. **Multiple tags with nested coverage.** Disjoint coverage is clearly fine.
   Nested (an inner tag over a subrange of an outer tag's range) is coherent but
   the recomputation order matters. Decide before phase 8.
3. **Cross-field invariants.** `invariant len == size(payload);` is attractive
   -- checked on parse, maintained automatically on mutation. Where does the
   maintenance obligation live in the generated API? Deferred.
4. **Slack tracking.** `InPlaceSlack` implies the runtime knows the allocated
   capacity of a region separately from its current size. That needs a place in
   the view struct. Decide during phase 5.
5. **Bit-level offsets in the pin syntax.** `@ 0x14` is a byte offset. Does
   `@ 0x14:3` (byte 0x14, bit 3) earn its keep? Probably yes for registers.
6. **Non-power-of-two integer widths above 8 bits.** `u12` and `u48` are useful
   and unambiguous when byte-aligned, less so when packed. Constrain to
   byte-aligned only, or allow packed? Decide in phase 1.
7. ~~**Whether `native` endian should be permitted at all.**~~ **RESOLVED.**
   Split into two constructs (Section 8.3): `endian native` for genuinely
   host-order formats, gated behind `[allow_host_dependent]` and non-canonical;
   and `endian_marker` for runtime-resolved byte order, which is
   `CanonicalGiven(marker)` and costs nothing on the offset or size axes.
   Write the decision record.
8. **Compact-versus-mutable tension.** Not a question so much as a standing
   tradeoff to surface: compactness wants varints, bit packing, and elided
   optionals, all of which destroy fixed offsets. Situ's value is making that
   choice explicit per field. **The varint sub-question is now resolved:** they
   are required (Section 8.1.1), because describing protobuf is impossible
   without them. `minimal` is the canonical-encoding rule and it is mandatory
   for `require canonical`.

9. **Kernel pipeline property composition.** 13.4 states composition is
   pointwise and conservative. Verify that against a real case: does
   `rs |> interleave |> manchester` compose to something usefully strong, or
   does conservatism collapse it to nothing? If the latter, the composition
   rules need refinement before tier 2 is worth building.
10. **Bit phase in the public API.** The solver tracks bits internally, but what
   does a non-byte-aligned region look like to a C caller? Options: forbid
   non-byte-aligned region boundaries at the API surface (simplest, probably
   right for v0), or expose a bit-offset view type. Decide before phase 12.
11. **Whether tier-2 codecs need a constant-time mode.** A table-driven codec
   over secret data is a cache-timing side channel. `[secret]` regions already
   forbid data-dependent access in generated accessors (14.6); does that
   obligation extend into generated codec implementations, and is a
   constant-time Reed-Solomon realistic? Decide before applying tier 2 to
   sealed regions.
12. **`systematic` and authentication interaction.** A systematic FEC block
   inside a `sealed` region means the ciphertext is FEC-coded and the plaintext
   is not, or vice versa depending on layer order. Encrypt-then-code and
   code-then-encrypt have different capability outcomes and different security
   properties. Both should be expressible; the ordering must be explicit in the
   schema, never inferred.
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
