# situ

Situ describes the exact byte layout of data that already has a binary
representation -- wire protocols, packet formats, on-disk records,
memory-mapped registers -- and generates accessors for it in C, C++, Rust and
Python from one schema.

What separates it from the other ways to do that is what it does when it
*cannot* generate something. A field whose bytes cannot be written in place
gets no in-place setter, and the compiler says which property of the schema
took it away and what to change to get it back. The absence is derived,
explained, and assertable in the schema itself.

```
$ situc explain example/http/http.situ request_line.version
request_line.version
  size       Bounded(2, 16)  <- weakened
  offset     Scanned  <- weakened
  access     Sequential  <- weakened
  mutate     Shifting  <- weakened
  ...
blame:
  mutate := Shifting
    a member that ends at a delimiter
    the length is wherever the delimiter turns out to be, so a longer value
    needs more room and the bytes after it have to move to make it
    remedy: `until D max N` bounds the scan, which makes the member statically
    allocatable and turns a missing delimiter into an error instead of a read
    to the end of the buffer
```

Nothing about that is a lint. `offset = Scanned` is why `version()` costs a
scan rather than a load, and every backend emits exactly the operations the
vector supports.

## The shape of it

A schema is the format, not a description of a language's objects:

```situ
// example/udp/udp.situ, abridged. RFC 768.
target buffer;
endian big;

struct udp_header {
	u16  source_port;
	u16  destination_port;
	u16  length  [min = 8];
	u16  checksum;
}

require size(udp_header) == 8;
require absolute_static(udp_header);
require canonical(udp_header);
```

`require` is checked at compile time and fails the build. What comes out is a
header of constant-offset accessors --

```c
static inline uint16_t situ_udp_header_length_get(situ_view_t view)
{
	return (uint16_t)(situ_get_be16(view.base + 4u));
}
```

-- plus a **capability map**, which is the schema's cost recorded in a form a
test can compare against:

```
struct udp_header size=8 repr=ValueConverted
  udp_header.source_port       offset=AbsoluteStatic(0x00) size=Fixed(2) align=Aligned(8) ...
  udp_header.length            offset=AbsoluteStatic(0x04) size=Fixed(2) align=Aligned(4) ...
```

A view is a value: a base, a limit and a generation counter. One bounds check
acquires it and every accessor after that is arithmetic. Generated C, C++ and
Rust never allocates, never recurses and uses no VLAs; Python's data model has
no non-allocating spelling and is the stated exception, bounded by the
schema's own `max`. That guarantee belongs to the default layer -- see *How
much of it you take*.

## The capability lattice

Section 11 of `project.md` is the core of the project, and everything else in
the compiler exists to feed it or report its results. A field's **capability
vector** is thirteen axes, each with a domain ordered strongest to weakest.
The layout solver computes them, `require` asserts them, the map commits
them, and every backend emits exactly the operations the vector supports.

| axis | domain, strongest first | what it answers |
|---|---|---|
| `size` | `Fixed(n)` > `Bounded(lo,hi)` > `Unbounded` | how many bytes |
| `offset` | `AbsoluteStatic(n)` > `FrameStatic(n)` > `Dynamic` > `DataPlaced` > `Scanned` | where it is, and what finding it costs |
| `access` | `Random` > `Sequential` | can element N be reached directly |
| `mutate` | `InPlaceFixed` > `InPlaceSlack` > `Shifting` > `RewriteRequired` > `Immutable` | what a write costs |
| `address` | `Stable` > `FrameStable` > `Unstable` | may a pointer to it be held |
| `align` | `Aligned(n)` > `Unaligned` | relative to the message base |
| `repr` | `MemoryIdentical` > `ValueConverted` > `TextConverted` > `ConditionallyConverted(f)` | is the value literally the bytes, and can the conversion fail |
| `atomic` | `AtomicWord` > `NonAtomic` | is single-instruction access possible |
| `canonical` | `Canonical` > `CanonicalGiven(f)` > `NonCanonical` | does one value have exactly one encoding |
| `stage` | `CompileTime` < `ParseTime` < `TransformTime` < `VerifyGated` | when the answer is knowable |
| `auth` | `Uncovered` / `Covered(obligation)` | which obligations cover these bytes |
| `secrecy` | `Public` / `Secret` | may it be printed, and does it get a debug accessor |
| `effect` | `Pure` > `EffectOnRead` / `EffectOnWrite` / `EffectBoth` | does touching it do something |

Four of them are worth a sentence, because they are the ones other systems
do not distinguish:

- **`repr`.** A big-endian `u32` on a little-endian host is
  `ValueConverted`: the value is not the memory. So "in-place mutation" of it
  is a read-swap-write rather than a store, and a caller cannot take a
  pointer to the value. `TextConverted` goes further -- a decimal parse can
  *fail*, where a byte swap cannot.
- **`offset`.** `Dynamic` is arithmetic over values already read; `DataPlaced`
  is one read of an offset the message itself chose, so nothing about the
  frame bounds it; `Scanned` is a search that can fail. Three different costs
  and three different failure modes, where "not a constant" would be one word.
- **`canonical`.** `CanonicalGiven(f)` is the honest answer for a
  byte-order-marked format: more than one encoding exists, exactly one given
  field `f`. The consequence is a rule -- **verify over received bytes, never
  over re-encoded bytes** -- and it is why a schema distinguishes
  `deterministic_writer(X)` from `canonical(X)`.
- **`stage`.** The only axis that increases rather than weakens. `VerifyGated`
  is what makes a sealed region's interior unreachable until a tag verifies,
  and it is a *type* in C++ and Rust rather than a flag.

**A weakening is never silent and never anonymous.** Each carries a blame
chain naming the construct that caused it, and `situc explain` prints it with
the remedy attached, which is what the excerpt at the top of this file is.
`situc advise` ranks those remedies across a whole schema by what they buy.

## Quickstart

```sh
git clone <this repository> && cd situ
make                                    # build the C runtime

# The part worth seeing first: what a schema costs, and why.
./bin/situc explain example/http/http.situ request_line.version
./bin/situc advise example/http/http.situ

# The part every IDL has.
./bin/situc build --target c --out /tmp/gen example/udp/udp.situ
./bin/situc map example/udp/udp.situ
./bin/situc doc example/udp/udp.situ    # RFC-style byte diagrams
```

`explain` answers "what did this field cost me and why"; `advise` answers
"what should I change", ranked by what it buys and priced by what it costs:

```
6 suggestion(s), highest yield first.

request_head.start: move this variable-length member after the fixed ones
    its extent is not fixed, so 1 member behind it are Dynamic: `fields`
    cost: nothing (reordering moves no bytes, and every deployed peer reads
    the old order)
    yields: 1 member return to AbsoluteStatic, and their accessors to base + K
```

`situc` needs Python 3.11 or later and nothing else -- no third-party packages,
by policy, so the toolchain vendors into an embedded build environment as a
directory copy. `bin/situc` works in place or symlinked onto `PATH`;
`python3 -m situc` does the same thing.

## What it generates

| Command | Artifact |
|---|---|
| `situc build` | accessors: C, C++, Rust or Python (`--target`), how much of the schema becomes code (`--layer`, defaulting to `view`), the shape they take (`--owned`, `--materialize`, `--single-file`), what pumps the rung-6 state machine (`--driver`), and whether a declaration reaching no code fails the build (`--refuse-ungenerated`) |
| `situc map` | the capability map; `--check` compares against a committed one and fails on a diff |
| `situc explain` | one field's capability vector and the blame chain behind every weakening |
| `situc advise` | ranked, costed schema changes that would restore what was lost |
| `situc diff` | capability regressions between two revisions of a schema |
| `situc wire` | the byte-level contract, and `--check` against the committed one |
| `situc verify` | whether real bytes conform to the schema, generating nothing -- the way to adopt one without taking the codegen |
| `situc pack` | the packed layout image: placements, kinds, offsets, sizes, endianness and a bytecode for the expression language, for a walker to read a format it was not compiled against (`--metadata` adds names and capability vectors) |
| `situc doc` | RFC-style byte-layout diagrams and a field reference |
| `situc gen-checks` | tests holding the generated accessors to the map they were generated beside |
| `situc gen-tests` | golden-vector tests from a schema and hex vectors |
| `situc gen-fuzz` | a libFuzzer harness per parseable struct |
| `situc gen-tamper` | the harness that watches a tag's gate refuse: every covered byte and every tag byte flipped one at a time, with refusal required |
| `situc gen-dissector` | a Wireshark dissector in Lua, over the same traversal the code backends use |
| `situc gen-derived` | codec implementations from kernel descriptions |
| `situc gen-codec-tests` | property tests that would falsify a lying codec signature, against the ABI an `impl` binds |
| `situc dump-ast` | the parsed schema, for when a layout surprises you |
| `situc import-proto` | a `.proto` read as a description of its wire format, reporting what it could not represent |
| `situc lsp` | a language server over stdio: diagnostics with the blame chain intact, hover, symbols, code actions carrying the advisor's costs, and go-to-definition |

`situc --help` lists the rest, and `man situc` is the full command reference --
every subcommand, its flags and its exit behaviour. It installs with the rest
of the tool, and reads from `packaging/situc.1` in a source tree. A test holds
it to the parser, so a subcommand that exists and is undocumented fails the
suite rather than going quiet. Section 21 of `project.md` is the authority
above both.

### The other tools

`situc` is the compiler, and three more programs read bytes against a
description at run time rather than generating code from it. None is a
`situc` subcommand and none is meant to be: decision 0026 keeps the compiler
and the interpreter apart, so that under the compiler an offset stays a
constant and an operation stays *absent* rather than refused. The separation
is the point rather than a packaging detail.

```sh
situ-edit      proto.situ capture.bin    # the fields these bytes hold
situ-edit-tui  proto.situ capture.bin    # the same, on a terminal

situc pack     proto.situ -o proto.image # the description a walker reads
situ-walk      proto.image <hex>         # every struct, every member it can read
make walk-c                              # situ-walk-c: the same walk, in C
```

`situ-edit` takes a `.situ` or a packed image; given a schema it runs
`situc pack` first, across a process boundary rather than a link-time one,
which is how 0026's separation survives the convenience.

```
$ situ-edit example/udp/udp.situ capture.bin
udp_header  8 bytes
     0 +  2  source_port              4660
     2 +  2  destination_port         53
     4 +  2  length                   8
     6 +  2  checksum                 abcd
     8 +  0  payload
```

**`situ-walk`** is the interpreter: a table walk over live bytes, which also
serves as a fifth column in the differential check, answering the same
questions as four compiled backends about the same hostile bytes. It has no
manual page yet, which is a gap and is named here rather than left for
somebody to notice.

**`situ-walk-c`** is the same walk in C, for the case decision 0026 was
argued from -- a device whose framing must change without a firmware rebuild,
reading a description out of a fixed arena. `make walk-c` builds it, and a
differential test holds it to the Python walker: every probe the fifth column
makes, this answers. What it declines it declines *by name*.

**`situ-edit` and `situ-edit-tui`** open a capture and a schema and show the
fields, which sounds like every template-driven hex editor and is not one.
010 Editor, Kaitai's IDE and Wireshark all do "open bytes, open a
description, see fields"; none of them carries **capability reasoning**, so
none can grey out a setter that does not exist, say that the field you just
looked at cannot be written in place, or show the blame chain for why
(decision 0034). Read-only for now: writing a field that shifts the layout
drags in the invalidation model, a covered field goes stale, and an invariant
must be maintained rather than checked -- which is its own piece of work. The
CLI can do everything the TUI can, deliberately, because an interactive
frontend is hard to test and a scriptable one is not.

## How much of it you take

A schema may describe more of a protocol than you want generated, so how much
becomes code is a choice you make at the command line rather than in the
schema. `situc build --layer` takes it, defaulting to `view`. **All six rungs
are built**, in all four backends:

| `--layer` | what it emits | the new "yes" |
|---|---|---|
| `view` | accessors over bytes you own | *(baseline)* |
| `edit` | build or resize a message whose extent is not fixed | may it allocate? |
| `relate` | predicates over two messages | may it look at two messages? |
| `frame` | a byte stream in, whole messages out | may it hold bytes between calls? |
| `converse` | match a reply to its request | may it hold messages between calls? |
| `drive` | send, receive, retransmit, time out | may it own I/O? |

The ladder was written down before the rungs existed, and that is the point:
"should situ do X" stops being asked once per adopter and becomes "at which
rung does X live" (`doc/decision/0032-the-layer-ladder.md`). A rung emits
every file the rung below emits, byte-identical, plus new ones, so moving up
leaves you no already-reviewed file to review again. The rung you pick is the
invariant you get: `view` guarantees the allocation-free property above, and
`edit` is where it is spent.

A relation is a pure predicate over two views, read through the generated
getters rather than the bytes -- so a big-endian `u16` compares correctly
against a little-endian `u32` without the author thinking about it:

```c
situ_err_t situ_rel_response_to(situ_view_t request, situ_view_t response);
```

**What a relation may say is decided once, not four times.** Python's integers
are arbitrary precision and would compare a `u64` against an `i8` happily; C,
C++ and Rust cannot. It is refused in all four, because a schema one backend
accepts and another does not is a schema that means two things. The walker
answers relations out of the packed image too, so a schema's predicates can be
exercised without generating or compiling anything.

**The ladder is one axis of three.** `--layer` says which invariants the
output holds to; `--owned`, `--materialize` and `--single-file` say what shape
it takes; and `--driver` says what pumps the top rung. The first two compose,
and the ladder explains refusals that used to stand alone: `--materialize`
turns down an uncapped run at `view` because the index would have to be
allocated, and `--layer edit --materialize` is how you ask for it anyway. That rung is sans-I/O
-- it never opens a socket and never reads a clock -- so `--driver` adds the
adapter that does, as an extra file over the same schema: `epoll`, `poll`,
`select`, `ppoll`, `blocking` and `io_uring` in C, `qt` in C++, `tokio` in
Rust, `asyncio` in Python. An unavailable pair is refused naming both, because
`--driver epoll --target python` is a worse asyncio (decision 0033).

Above `relate` a schema has to say more than bytes -- which relation pairs a
request with its reply, what the retry policy is -- because both endpoints
must agree and a command-line flag cannot make them agree. **None of it is
ever inferred.** There is no default timeout and no implicit retransmission;
where a schema states no policy the generated code has none. A deployment may
override a declared *value* at the command line and may never introduce a
*shape*. Rung 6 owns I/O and never owns the clock: time enters as a parameter,
so a timeout bug reproduces every run instead of racing a deadline nobody
wrote down.

## The language

`doc/grammar.ebnf` is the extracted grammar and section 7 of `project.md` is
the authority; what follows is the working vocabulary.

### Types and structure

**Directives** set how the rest of the file is read:

```situ
target buffer;          // or `mmio`, for memory-mapped registers
endian big;             // or `little`, `native`
bit_order msb_first;    // or `lsb_first`
strictness = strict;    // or `lenient`
import "std/codecs.situ";
```

**Scalars** are spelled by width, and the width need not be a whole byte:

| spelling | meaning |
|---|---|
| `u1` .. `u64`, `i8` .. `i64` | unsigned and signed integers; any width that is not a whole number of bytes is bit-packed |
| `f16`, `f32`, `f64` | IEEE floats |
| `bit`, `bool` | a single bit |
| `byte` | eight bits with no numeric reading |
| `q16_16`, `uq8_8` | fixed point, signed and unsigned; the width is the sum of the halves |
| `bcd4` | packed binary-coded decimal, four bits per digit |

**Structure.** `struct` is the ordinary case; the rest exist for shapes a
struct cannot describe:

| construct | for |
|---|---|
| `struct name { ... }` | members in declaration order |
| `positional { ... }` | a group whose offsets are asserted rather than accumulated |
| `variant v switch (e) { case 1: ... default: error }` | one of several layouts, chosen by a field already read |
| `indexed (base = region) { ... }` | members reached through an offset table |
| `tlv name (tag_decode = ..., value_size = ...)` | a run of tag-length-value items, with the item grammar declared |
| `opaque name [ n ]` | bytes with no described interior |
| `authenticated { ... }` | a region covered by a tag |
| `sealed name (codec) { ... }` | a region nothing may read before the tag verifies |
| `coded name (codec) { ... }` | a region transformed before it is read |
| `register` / `register_block` | an MMIO register with its bus width and access rules |

### Where a member ends

This is the question the language spends most of itself on, because it is what
decides the capability vector:

```situ
u8   fixed[4];                        // a count
u8   rest[remaining];                 // to the end of the frame
u8   name[]    until ":";             // to a delimiter
u8   method[]  until " " max 16;      // bounded, so it stays allocatable
nlattr attrs[] while (nla_len >= 4);  // while a predicate over each element holds
u8   pixels[n] at file.pixel_offset;  // placed where the data says
u32  crc @ 0x1c;                      // assert the offset the solver computed
```

### Choosing between layouts, and describing a run of items

A `variant` is one of several layouts chosen by a field already read, and it
is where a format stops being a struct:

```situ
// example/icmp/icmp.situ.
variant body switch (type) {
	case icmp_type.echo_request:            icmp_echo_id  echo;
	case icmp_type.echo_reply:              icmp_echo_id  reply;
	case icmp_type.redirect:                u8            gateway[4];
	case icmp_type.destination_unreachable: icmp_frag     frag;
	case icmp_type.time_exceeded:           u8            unused[4];
	case icmp_type.parameter_problem:       u8            pointer[4];
}
```

`default:` takes `error`, `opaque` or a member -- three different positions
on an unknown discriminant, stated rather than implied.

A `tlv` region describes a run of tag-length-value items by declaring the
item grammar, which is what lets the compiler reason about a self-describing
format without a parser being written for it:

```situ
// example/protobuf/protobuf.situ, abridged.
tlv fields (
	tag_type       = pb_varint,
	tag_decode     = { field = tag >> 3, wire = tag & 0x7 },
	tag_identity   = field,          // which half of the tag names an item
	value_size     = switch (wire) {
		case 0: self_delimiting,
		case 1: 8,
		case 2: prefixed(pb_varint),
		case 5: 4,
		default: error,              // groups are not supported
	},
	duplicate_tags = allowed,
	known = { 1 : { name = user_id, wire = 0, type = pb_varint }, ... },
)
```

`indexed (base = region)` is the other shape a struct cannot describe:
members reached through an offset table rather than laid out in order, which
is what a page format like SQLite's does. `opaque name [n]` is the honest
answer where the interior is not described at all.

### The expression language

Small on purpose, because everything in it has to be evaluable by the layout
solver *and* by four backends *and* by a table walk. Field references,
arithmetic, comparisons, and six builtins: `min`, `max` and `align_up` over
values; `size`, `offset` and `count` over the layout.

```situ
u8       body[length - 8];              // arithmetic over a field already read
reserved u8 [align_up(nla_len, 4) - nla_len] [must_be_zero];
require  size(udp_header) == 8;
require  offset(tcp_header.options) == 20;
```

`remaining` is the one keyword that is not an expression: it means "to the
end of the frame", and it is what makes a member's size depend on the buffer
rather than on the message.

That second line is netlink's alignment padding written by hand, and it is
also what `pad_to(4);` says in one member -- `base::Pickle`, cpio and most
RPC framings all spell it the long way. The construct exists because the
long way is a `reserved` run the map labels `<reserved0>`, where `pad_to`
labels it `<pad>` and the wire signature carries it as padding rather than as
bytes that happen to be zero.

### Strictness

`strictness = strict` or `lenient`, at file level, and it is section 14.5's
answer about malleability rather than a parser mood. Lenient makes
`canonical` `NonCanonical` throughout, so the generated code does not hold a
message to one canonical form -- and the map records that, which is the
point. A schema that accepts what the format permits and the author would
rather refuse has said so in the artifact a reviewer diffs, instead of in a
comment.

### Attributes, requirements and invariants

**Attributes** are a closed vocabulary in brackets, and a table says which
member kinds each may appear on -- an attribute in the wrong place is refused
rather than ignored. `[min = 8]`, `[max = N]`, `[must_eq = 0x1f]`,
`[must_be_zero]`, `[preserve]`, `[since = 2]` for a member a later version
added, `[secret]`, `[self_as = 0]` for what a checksum's own bytes read as
while it is computed, `[allow_straddle]` for a bit field that crosses a byte
boundary on purpose.

**The rest of the declarations:**

```situ
const header_bytes = 8;
enum operation : u16 { request = 1, reply = 2, }
checksum u8 header_checksum[2] covers(header) [self_as = 0];
require absolute_static(arp_packet);   // fails the build if it does not hold
invariant derived.total == size(derived.a) + size(derived.b);
```

`require` is a compile-time assertion about the capability vector; `invariant`
names a field situ *maintains* rather than one it merely checks. Writing a
field an invariant reads leaves the derived one stale in exactly the way a
covered write leaves a tag stale, and the generated API says so.

### Codecs, in two tiers

A codec is a transform over a region -- a checksum, a line code, a cipher, a
framing rule. What the capability lattice consumes is never an implementation
but a **property signature**: how the length changes, whether an interior
position is still computable, what unit the transform works in, whether the
input survives verbatim, whether it is invertible, deterministic, error
propagating, authenticating. The two tiers differ only in where that signature
comes from.

**Tier 1 is `extern`.** The signature is declared and situ trusts it, because
the implementation is somebody else's:

```situ
// std/codecs.situ, verbatim.
codec aes_gcm_128 {
	tag_bytes   = 16;
	nonce_bytes = 12;
	length_preserving;
	seekable = linear;
	granularity = byte;
	authenticated;
	invertible;
	deterministic;
}

impl aes_gcm_128 extern "my_gcm";   // yours, called as my_gcm_encode/_decode
```

The map marks such a codec `trusted` for exactly that reason, and
`situc gen-codec-tests` emits the property tests that would falsify a lying
one, against the ABI the `impl` binds. `std/codecs.situ` carries 19 such
signatures -- AES-CTR and AES-CBC, ChaCha20, AES-GCM, ChaCha20-Poly1305,
HMAC-SHA256, deflate, lz4 -- and deliberately binds none of them: they are
contracts, and which implementation satisfies one is the adopter's to name.

**Tier 2 is `derived`,** and it is the interesting half. The schema gives a
*kernel description* rather than a signature, and situ computes the signature
and generates the implementation from the same description -- so the two
cannot disagree, which is the whole argument for the tier:

```situ
// Feedback from the input: startable anywhere, and a corrupt bit spoils only
// itself. Change one word to `output` and both answers flip.
codec scrambler_additive {
	kernel = shift_register(taps = 0xB400, width = 16, seed = 0xACE1,
	                        feedback = input);
}
impl scrambler_additive derived;
```

Six kernel families cover essentially every line code, FEC, scrambler and
framing code in practical use, which is what bounds the design:

| family | described by | covers |
|---|---|---|
| `table` | input symbol to output symbol, optionally padded to whole groups | both Manchesters, 4b5b, base16/32/64 |
| `polynomial` | a generator polynomial over GF(2) or GF(2^m), plus init, reflection and xorout | every CRC variant, Reed-Solomon, BCH |
| `linear_block` | a generator or parity-check matrix over GF(2) | Hamming and other block codes |
| `shift_register` | taps, a feedback source, an initial state, and whether the feedback is complemented | additive and multiplicative scramblers, both NRZI conventions |
| `permutation` | an index mapping | block and convolutional interleavers |
| `stuffing` | a trigger predicate and an insertion rule | COBS, bit stuffing, SLIP, PPP, SMTP dot-stuffing |

`std/kernels.situ` carries 38 of them: 15 polynomial (13 CRCs and 2
Reed-Solomon codes), 8 table, 7 shift_register (two scramblers, the two NRZI
conventions, SONET, USB 3.0, PRBS23), 6 stuffing, 1 linear_block -- a
Hamming(7, 4) -- and 1 permutation. Those counts are held to the schema by a
test, because a number in a README is a claim like any other. Stages compose
into a pipeline, whose properties are the product taken conservatively:

```situ
codec framed   = crc32 |> interleave_16 |> manchester_802_3;
codec usb_line = usb_bit_stuffing |> nrzi_transition_on_zero;
```

**None of it is believed on the strength of having compiled.** The CRCs are
held to the catalogue's published check values, SLIP and PPP to their RFCs'
own vectors, the scramblers to the maximal period a primitive polynomial must
have, and the two NRZI conventions to the two facts a standard states rather
than to the rule a generator would be written from. Beyond the per-codec
oracles, three guards read the generated object file rather than the
declaration: every codec's declared expansion is measured against what its
encoder writes, every stuffing code's `ratio_bounded` against the input its
own comment calls the worst case, and the `error_propagating` axis by
flipping one coded bit and counting how much of the decode it takes with it.

### Authentication and sealing

`authenticated { }` names a region a tag covers, and the tag is a member like
any other:

```situ
// example/packet/packet.situ, abridged.
struct packet {
	authenticated {
		header  hdr;
		u8      nonce[12];
	}

	sealed(aes_gcm_128, nonce = nonce) {
		u16  inner_kind;
		u32  inner_seq;
		u8   session_key[16]  [secret];
		u8   body[hdr.length];
	}

	tag u8[16];        // coverage inferred: every authenticated and
}                      // sealed region in this struct

require verify_gated(packet.sealed);
require in_place_dirty(packet.sealed.inner_seq);
```

Three things follow, and the generated API is where they show up.

**A covered field loses its plain setter.** It gets `set_x()`, which marks the
tag stale, and the message is not transmittable until a `recompute` puts it
right. `require in_place_dirty(...)` is how a schema asserts it noticed. A
checksum inside its own coverage says what its bytes read as while the
algorithm runs (`[self_as = 0]`, which is RFC 1071's rule), and one that
covers bytes the message does not contain names them (`prefix(...)`, which is
UDP's and TCP's pseudo-header).

**The interior of a sealed region does not exist until the tag verifies.**
That is the doom principle as a typestate: parsing attacker-controlled
plaintext before authenticating it is the mistake, so situ makes it
unspellable. C++ gives the interior view no public constructor and Rust a
private field, so in those two the compiler refuses; C and Python check at run
time.

**A codec must earn the right to seal.** It has to declare `authenticated`,
so `sealed(crc32)` is refused rather than handing out the interior on a flag
nothing checked; and a `derived` implementation may not seal at all, because a
table indexed by plaintext leaks it through the cache and situ cannot
discharge that obligation (decision 0019). Which key and which nonce are
fields, not configuration -- `sealed(codec, nonce = n, key = epoch)` names
them, and a conversation key wider than a word is the exact bytes of a field.

`situc gen-tamper` is the harness that watches the gate refuse: it takes the
caller's verifier as a callback and drives it across the schema's own coverage
geometry, flipping every covered byte and every tag byte one at a time with
refusal required. A gate nobody has watched fail is not evidence.

### Text

Situ describes text formats as layouts, which is the part of them that is one:

```situ
// example/http/http.situ, three of its structs.
struct request_line {
	u8  method[]   until " "     max 16   [encoding = ascii];
	u8  target[]   until " "     max 8192;
	u8  version[]  until "\r\n"  max 16   [encoding = ascii];
}

struct status_line {
	u8       version[]  until " "     max 16  [encoding = ascii];
	decimal  u16        code until " " max 4  [minimal];
	u8       reason[]   until "\r\n"  max 256;
}

struct header_field {
	u8  name[]   until ":"     [case_insensitive, encoding = ascii];
	u8  value[]  until "\r\n"  [trim];
}
```

A radix prefix -- `decimal` or `hex` -- says a number is written as digits
rather than laid down as bytes, which is what `cpio`'s eight-character hex
fields and HTTP's status codes need. `[encoding = ascii | utf8 | utf16le |
leb128]` says what a run holds and gets a validity check that rejects a lone
surrogate the way the UTF-8 one rejects an overlong form. `[trim]`,
`[case_insensitive]`, `[nul_terminated]`, `[quoted = "\""]` and
`[escape = "\\"]` describe the rest, and each is a real claim: a
case-insensitive token is `NonCanonical` on the lattice, because
`Content-Length` and `content-length` are one value with two spellings, and
the generated comparison folds case the way the schema says.

String literals know `\n \t \r \0 \\ \"` and `\xNN`, so a frame delimiter
outside ASCII -- SLIP's 0xC0 -- can be written.

**Where situ stops is a grammar.** A field whose *text* contains an expression
language is not a layout, and situ describes the layout around it rather than
generating a parser for it. Section 8.6.6 is where that line is drawn.

### Registers

`target mmio` changes what a schema means: offsets are bus addresses, reads
and writes may have side effects, and a partial access may be forbidden.

```situ
register ctrl_reg @ 0x00 {
	width        = 32;
	access_width = 32;          // partial access forbidden implicitly
	volatile;
	no_rmw;                     // reads have side effects; RMW unsafe

	bit       enable  [rw];
	bit       start   [wo, on_write = trigger];
	u3        mode    [rw];
	bit       busy    [ro];
	bit       error   [w1c];
	reserved  u25     [preserve];
}
```

The access attributes are the hardware's vocabulary -- `[ro]`, `[rw]`, `[wo]`,
`[wo_once]`, `[w1c]`, `[w0c]`, `[w1s]`, `[w0s]`, `[rc]`, `[rs]` -- and a
`reserved` run carries the policy the datasheet states, `[preserve]` or
`[must_be_zero]`, which the generated validator enforces. `register_block`
groups registers that share bus settings.

### Versions

A version byte in a header is a promise, and section 19 is where keeping it
becomes a compile error rather than an `if`. `[since = N]` marks a member a
later revision added; the wire signature and the capability map are the two
committed artifacts that make a change reviewable:

```sh
situc wire --check  proto.situ   # did a field move?
situc map  --check  proto.situ   # did a field get more expensive to reach?
situc diff old.situ new.situ     # what changed between two revisions
```

`wire` records the byte-level contract and `map` records the cost; they are
different questions, which is why there are two files (decision 0041).

### Documentation, generated from the layout

`situc doc` emits RFC-style byte diagrams and a field reference from the same
layout the accessors come from, so the picture cannot drift from the code:

```
struct udp_header
-----------------

Size: 8 to 65535 bytes.

 0                   1                   2                   3
 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|          source_port          |        destination_port       |
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|             length            |          checksum[2]          |
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
/                      payload[length - 8]                      /
/                           (variable)                          /
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+

Field                Offset  Size          Type           Notes
-------------------  ------  ------------  -------------  --------------------
summed               0       8 bytes       authenticated  covered by checksum
source_port          0       2 bytes       u16            big endian; covered..
length               4       2 bytes       u16            big endian; min = 8..
checksum[2]          6       2 bytes       u8             self_as = 0
payload[length - 8]  8       [length - 8]  u8             covered by checksum
```

(The Notes column is cut here to fit; it runs to 98 columns in the real
output, and it is where `[min]`, `[must_eq]`, `[self_as]`, the byte order and
the covering tag all end up.)

The Wireshark dissector (`situc gen-dissector`) comes off the same traversal,
which is why a schema that parses in C parses in Wireshark.

### The tests you do not write

Five of the subcommands generate test code, and the argument for each is that
it asks a question a hand-written test would have to be remembered to ask:

- `gen-checks` holds the accessors to the capability map they were generated
  beside -- every member the map calls writable has a setter, and every
  member it calls unwritable says why.
- `gen-tests` turns hex vectors into golden tests, so a corpus somebody else
  produced becomes a build failure when the schema stops describing it.
- `gen-fuzz` emits a libFuzzer harness per parseable struct. `make fuzz` runs
  them under ASan.
- `gen-tamper` drives the caller's verifier across the schema's own coverage
  geometry, flipping every covered byte and every tag byte with refusal
  required. A gate nobody has watched fail is not evidence.
- `gen-codec-tests` emits the property tests that would falsify a codec
  signature that lies, against the ABI its `impl` binds.

All five derive from the schema, so adding a worked example adds coverage
without anybody writing a test.

**Diagnostics are a product here, not a finish.** Section 17 makes message
quality part of what situ is for, and `test/unit/test_diagnostics.py`
compares the rendered text against exactly what it should say: a regression
in the wording of a blame chain fails the build. That is why a refusal in this tree usually names what *would* have
been accepted -- a message that only says no leaves the reader to infer the
boundary, and they infer it in the direction of "nothing works".

## Four backends, one layout

C is the reference; C++, Rust and Python emit a different surface over
identical bytes. The layout solver, the capability lattice and the traversal
are shared, and the runtime arithmetic lives once, in C:

- **C** -- header-only accessors, C11, warning-clean under
  `-Wall -Wextra -Werror -Wconversion -Wsign-conversion`, cross-built for
  aarch64 and run under emulation (little-endian; the big-endian target is
  compile-only, for the reason in decision 0004).
- **C++** -- what C can only document, this enforces: a byte run carries its
  length, an error is `[[nodiscard]]`, and a sealed region's view has no public
  constructor, so it cannot be made except by the function that verifies the
  tag.
- **Rust** -- `no_std`, allocation-free. Invalidation is the borrow checker
  rather than a generation counter.
- **Python** -- views over `memoryview`, with the generation check of section
  12.3 that a release build of C cannot afford.

What differs between them is not the bytes but how much of the lattice each
language can *enforce* rather than document:

| | C | C++ | Python | Rust |
|---|---|---|---|---|
| bounds | run time | run time | run time | run time |
| invalidation | generation, checked in `SITU_CHECKED` | as C | generation, always | **borrow checker** |
| a length that cannot be lost | `_COUNT` macro | `span` | `memoryview` | slice |
| an error that cannot be dropped | no | `[[nodiscard]]` | exception | `Result` |
| the stage gate | a struct anyone can fill in | **no public constructor** | a run-time token | **private field** |

The two in bold are refused by a compiler rather than reported by a runtime,
and they are the argument for having written those backends.

Four backends are worth nothing if they disagree, so several checks exist to
make disagreement fail rather than ship. The differential one generates a
driver per backend *from the layout*, feeds all four the same pseudo-random
buffers -- drawn from several alphabets and mostly short, because uniform
noise never enters a text protocol's parse paths -- and diffs the answers. It
writes as well as reads: every writable scalar takes a pattern, and the buffer
afterwards is the assertion, because a byte order reversed in a setter is
invisible to a read pass over bytes nobody wrote. `situ-walk` joins it as a
fifth column, answering out of the packed image whether a table walk agrees
with four compiled backends about hostile bytes.

Beside it: every schema is generated *and compiled* in every backend, because
generating is not compiling; every schema's dissector is executed over bytes,
because parsing is not running; and two checks compare each backend against the
capability map rather than against each other -- one asking that every member
the map calls writable has a setter, the other that every member it calls
unwritable says why. A backend that silently omits an operation the map
promises is invisible to a backend-versus-backend check unless the four happen
to disagree.

## Is your format worth a schema?

Situ is worth its cost above a floor, and the floor is not a field count. Six
projects evaluated it against real trees and wrote the result up in
`suggestion/`; two adopted, three said no, and the sixth recorded its verdict
in its own tree. What follows is their reasoning rather than an argument from
this side.

**The wrong axis is size.** A five-field record read by two implementations in
two languages is above the floor. A forty-field format parsed once into native
objects, in one language, by one program, may be below it.

**Four things decide it, and any one is usually enough:**

- **More than one implementation reads the bytes.** Two languages, two teams,
  or a client and a server built separately. What the schema replaces is the
  agreement nobody wrote down, and that agreement is what drifts.
- **The format will be versioned.** If a version byte is already in your
  header, you have promised something you will later have to keep. Section 19
  is where that promise becomes a compile error instead of an `if (version !=
  1) return false;` that turns into a branch, and then two branches.
- **You hold a buffer and take fields out of it -- especially to write them
  back.** This is what situ optimises: zero-copy reads, in-place mutation, and
  a derived account of when those are impossible. A program that parses once
  into native objects and never writes a byte pays for all of that and uses
  none of it.
- **Getting it wrong is dangerous.** Attacker-controlled input, a parser
  running as root, or bytes under a MAC. Situ's bounds and its verify gate are
  worth most exactly where a mistake is worst.

**Below all four, write the twenty lines.** A single private record, read in
one language by one program that never mutates it, is twenty-five lines of
`offset += N` and they work. `example/keystore` is deliberately that shape,
so you can see the floor rather than infer it -- and it shows what changes the
answer, which is the version bump re-laying the bytes and the sealed body
nothing may read before the tag verifies.

**One trap worth naming, because it is invisible from outside.** The layer
situ fits may not be the layer that is hard. A project whose index format is
stanzas of `Key: value` found situ described that layer well -- and that layer
was fifteen lines that had never had a bug, while the difficulty was an
expression grammar inside one field's *text*, which situ does not generate and
should not. Check that the part situ would take is the part costing you
something.

### Using a schema as a specification, generating nothing

Some projects cannot take the usual bargain. A codebase whose callers hold
owned structs that outlive the buffer cannot swap in zero-copy views without
rewriting the callers; a build that must not gain a code generator cannot run
one. Both are real, and both were reported by projects that evaluated situ and
said no.

There is a smaller bargain available, and it is worth more than nothing:

```sh
situc wire --check   proto.situ                 # the layout contract
situc map --check    proto.situ                 # the capability contract
situc verify         proto.situ corpus.vectors  # do real bytes conform?
```

Keep the hand-written codec. Commit the schema, the `.situ.wire` and the
`.situ.map` beside it, and run those three in CI. The first two fail when an
edit moves a field or changes what a field costs to reach. The third is the
one that matters most and the one the other two cannot do: it takes bytes
your existing implementation produced and asks whether the schema actually
describes them, which is the check that catches a schema that was wrong from
the day it was written.

`verify` generates nothing and compiles nothing -- the accessors are built and
run in memory -- so the only thing that enters your tree is the schema and two
text files a reviewer can read.

## What it will not do

- No wire format of its own. Situ describes formats that already exist.
- No serialization of language-native objects: no reflection, no object graphs,
  no interning.
- No implicit schema evolution and no unknown-field retention. Compatibility is
  explicit, and silently preserving bytes nobody validated is a security
  position rather than an oversight (section 14.5).
- No allocation in generated C, C++ or Rust at the default layer. Callers
  supply memory, and `--layer edit` is the explicit way to ask for more.
  Python allocates because its data model gives it no other spelling, bounded
  by the schema's own `max`.
- No recursive types: size and capability computation would not terminate.
- No grammar inside a field. A field whose text holds an expression language
  is not a layout; situ describes the layout around it (section 8.6.6).
- **No transform that has to allocate.** A codec whose output is not a view
  over its input -- deflate, LZ4, anything with overlapping copies into a
  second buffer -- stays tier 1: situ checks the contract and the caller
  supplies the implementation. This follows from the no-allocation rule rather
  than being a separate one, and it is the boundary of the derived tier rather
  than of codecs generally: a transform that keeps its output the size of its
  input, or expands it by a computable amount, situ generates.
- No behaviour the schema did not state. Situ describes conversations where a
  schema says so, and generates the machinery only when `--layer` asks for it,
  but it never supplies a fact nobody declared -- no default timeout, no
  implicit retransmission, no inferred correlation.

## Building and testing

```sh
make            # the C runtime
make test       # pytest and the generated C
make check      # everything before a commit: style, types, test, aarch64
make bench      # what the offset cache costs and saves, in all four backends
make fuzz       # every generated harness, under libFuzzer and ASan
make hooks      # install the commit-message hook from tool/hooks/
make walk-c     # build situ-walk-c, the embedded walker
make deb        # situc and libsitu-dev, with debhelper
make help       # everything else
```

`test` and `check` are not the same target and the difference matters.
`make test` is pytest plus the generated C, which is the fast one to run while
working. `make check` is what has to pass before a commit: it adds `style`
(indentation, ASCII, and project.md's own claims), `typecheck` (mypy over
`situc`, `walker`, `editor`, `tool` and `test`, and `--strict` over
`runtime/python`) and `cross-test` (the generated accessors on aarch64 under
emulation).

CMake is the other entry point, not a wrapper around that one:

```sh
cmake -B build && cmake --build build && cmake --install build
```

A project that already uses CMake can run the compiler as a build step rather
than checking generated code in:

```cmake
add_subdirectory(third_party/situ situ)
situ_generate(packet_schema SCHEMA packet.situ TARGET c)
target_link_libraries(my_app PRIVATE packet_schema)
```

The schema is a dependency of the target, so editing it regenerates what reads
it.

The Python suite needs `pytest` and `mypy`. Everything it drives a toolchain
for is *skipped* rather than failed when that toolchain is absent: `gcc`, `g++`
and `rustc` for compiling and running generated code, `lua5.4` and `luac5.4`
(every emitted dissector is executed against a stub of the Wireshark API, and
four of them over packets whose fields are then compared with the layout),
`doxygen` (the emitted C and C++ comments are extracted and read back), and
`aarch64-linux-gnu-gcc` with `qemu-aarch64`. `make test-c` and
`make cross-test` are the two steps that need their toolchain rather than
skipping: they build and run the generated C directly.

There is no autoformatter. Tabs carry indent level and spaces carry alignment,
which `black` and `ruff format` cannot be configured to leave alone, so
`tool/style_gate.py` under `make style` is the enforcement instead
(decision 0003). `make lint` is kept as an alias for it.

## Layout

```
project.md       the specification, and the authority on intent
doc/decision/    append-only decision records, numbered, with alternatives
doc/grammar.ebnf extracted from section 7, held in sync by a test
situc/           the compiler: Python 3.11+, standard library only, mypy clean
runtime/         one runtime per backend, each thin; the arithmetic lives in C
std/             codec signatures, kernel descriptions, and the packed image's
                 own schema
example/         one directory per protocol, each with at least one `require`
suggestion/      what other projects said when they evaluated situ, and the
                 replies; correspondence rather than a backlog
test/            unit, generated-C, cross-architecture, golden diagnostics,
                 a Wireshark stub, and the schemas written to be awkward
tool/            the style gate, the commit-msg hook, the benchmark, the sweep
walker/          the interpreter behind `situ-walk`, and `walker/c` behind
                 `situ-walk-c`; shares nothing with situc but the image format
editor/          the document model behind `situ-edit`, and its frontends
```

## Status

The phase plan in section 26 ran out some time ago; what came after it is
recorded in 26.14 onward, in folds -- a batch of work, then what it found and
what it left open. Every construct the language offers is reachable in all four
backends, and 26.31, the list of where the frontier is, has no open gap on it.
The latest fold is the place to look for what is open today; each entry there
says why it is, and several say why they are deliberate rather than pending.

There is no wheel and no PyPI release: `situc` runs from the tree or from a
directory copy, which is the distribution the no-dependency policy is for.
There is a version -- `VERSION` at the root, which `situc --version` and the
Debian packaging both read, so the three cannot disagree -- and `make deb`
builds `situc` and `libsitu-dev` packages with debhelper. Treat those as
evaluation output rather than a release: nothing is signed, uploaded, or
carries a stable ABI promise yet.

`situc pack` emits a packed layout image, for a device whose framing has to
change without a firmware rebuild: ship a description of the new format, load
it, parse. The format is itself a situ schema (`std/image.situ`), read through
generated accessors, so everything this repository does to a schema it also
does to the image. `situc pack --coverage` says what any given image contains,
and `walker.report.SUPPORTED` is the subset the walker renders. Note what an
interpreter cannot do, which is make an operation *absent*: under a walker the
capability map stops being the shape of the interface and becomes data a
caller may consult.

## Reading further

`man situc` is the command reference and `doc/grammar.ebnf` is the grammar.
Beyond those, `project.md` is long and is meant to be. Useful entry points:

- **Section 11**, the capability lattice: the core of the project. Every other
  part of the compiler exists to feed it or to report its results.
- **Section 13**, codecs and the kernel families; **section 14**, the
  cryptographic model and the stage gate.
- **Section 26.31**, where the frontier is: what is unfinished, re-derived from
  generated output rather than remembered. It has been wrong four times, and
  says so.
- **Section 0**, on how the document is kept honest: seven claims in it are
  held to the code by tests that fail when they drift. Every one was added
  after finding drift, and each found more than expected.
- `example/README.md` for what each worked example is there to prove.

Thirty worked examples ship, and they are the schemas to read first:
`example/udp` for the smallest complete one, `example/ipv4` for bit packing,
`example/dns` and `example/dnsname` for a variant and a walk, `example/http`
for a text protocol, `example/sqlite` for an offset table, `example/ble` for a
count of records that each say how long they are, `example/netlink` for a
format whose byte order is the sending machine's, `example/cpio` for numbers
written as digits, `example/register` for memory-mapped hardware, and
`example/packet` for authenticated and sealed regions.

Eleven carry a `.vectors` file, and where the bytes came from matters more
than that they exist. Eight are bytes some *other* implementation wrote, with
that implementation named: ImageMagick for `bmp` -- a whole file, header and
pixels -- glibc's `struct arphdr` for `arp`, lwIP's SNTP client for `ntp`,
U-Boot's `bin2bcd` and DS1307 driver for `rtc`, the Linux Bluetooth stack for
`ble`, GNU cpio for `cpio`, libtiff and matplotlib's committed test data for
`tiff`, and for `netlink` the reply a `NETLINK_ROUTE` socket gave to a dump
request. Two more are laid out by hand from an RFC's own field list rather
than by situ (`tcp` and `udp`, both pseudo-headers). The eleventh, `packet`,
is situ's own output and its header says so in the first paragraph, because a
vector file that agrees only with its own compiler has demonstrated nothing
and should not be counted as though it had. Two of the eight disagreed with
the schema on arrival.

## Copyright

Copyright (C) 2026 Nabeel Sowan <nabeel@vibes.se>

This names who wrote situ and grants nothing: authorship vests
automatically, and saying so is a statement of fact rather than a licence.
No licence has been declared for this tree, which is the holder's decision
rather than an oversight -- `packaging/copyright` states it, and says what
follows from it.
