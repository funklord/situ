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
$ situc explain examples/http/http.situ request_line.version
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
// examples/udp/udp.situ, abridged. RFC 768.
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

## Quickstart

```sh
git clone <this repository> && cd situ
make                                    # build the C runtime
./bin/situc build --target c --out /tmp/gen examples/udp/udp.situ
./bin/situc map examples/udp/udp.situ
./bin/situc doc examples/udp/udp.situ   # RFC-style byte diagrams
```

`situc` needs Python 3.11 or later and nothing else -- no third-party packages,
by policy, so the toolchain vendors into an embedded build environment as a
directory copy. `bin/situc` works in place or symlinked onto `PATH`;
`python3 -m situc` does the same thing.

## What it generates

| Command | Artifact |
|---|---|
| `situc build` | accessors: C, C++, Rust or Python (`--target`), how much of the schema becomes code (`--layer`, defaulting to `view`), and the shape they take (`--owned`, `--materialize`, `--single-file`) |
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

**The second binary is `situ-walk`**, which is not a `situc` subcommand and is
not meant to be: decision 0026 keeps the compiler and the interpreter apart,
and the separation is the point rather than a packaging detail. It takes a
packed image and a buffer of hex, and prints what it can read of every struct
in it.

```
situ-walk <image> <hex>
```

It has no manual page yet, which is a gap and is named here rather than left
for somebody to notice: `situc` has one and this ships beside it.

## How much of it you take

A schema may describe more of a protocol than you want generated, so how much
becomes code is a choice you make at the command line rather than in the
schema. `situc build --layer` takes it, defaulting to `view`, which is what
`situc build` has always done. **Two rungs are real and four are decided and
not built**; asking for one of those four says which phase adds it. The ladder
was written down ahead of the rungs on purpose, and the table says where each
one stands:

| `--layer` | what it emits | the new "yes" | status |
|---|---|---|---|
| `view` | accessors over bytes you own | *(baseline)* | **ships** |
| `edit` | build or resize a message whose extent is not fixed | may it allocate? | partly |
| `relate` | predicates over two messages | may it look at two messages? | **ships** |
| `frame` | a byte stream in, whole messages out | may it hold bytes between calls? | decided |
| `converse` | match a reply to its request | may it hold messages between calls? | decided |
| `drive` | send, receive, retransmit, time out | may it own I/O? | decided |

`docs/decisions/0032-the-layer-ladder.md` is the reasoning, and writing it
down before the rungs exist is the point: "should situ do X" stops being asked
once per adopter and becomes "at which rung does X live". Rung 2 exists in
pieces -- `--owned` emits a fixed-size decode today and refuses
variable-length members, which is that refusal seen from below.

A relation is a pure predicate over two views, so `--layer relate` adds one
function per relation and nothing else, in whichever backend you asked for:

```c
situ_err_t situ_rel_response_to(situ_view_t request, situ_view_t response);
```
```rust
pub fn rel_response_to(request: &Frame, response: &Frame) -> Result<()>
```

It reads through the generated getters rather than the bytes, which is what
makes the comparison one of *values* -- a big-endian `u16` against a
little-endian `u32` compares correctly without the author thinking about it.
A failed constraint is `SITU_ERR_CONSTRAINT`, which already meant exactly
that, so no new failure class had to reach four runtimes; Python raises
`ConstraintError` instead, as `validate` already does there.

**What a relation may say is decided once, not four times.** Python's
integers are arbitrary precision and would compare a `u64` against an `i8`
happily; C, C++ and Rust cannot, no 64-bit type holding both ranges. It is
refused in all four, because a schema one backend accepts and another does
not is a schema that means two things. The walker does not read relations
yet -- that needs a section in the packed image, which is the rest of 26.95.

Each rung answers one more question yes, and the rung you pick is the
invariant you get: `--layer view` guarantees the allocation-free property
above and `--layer edit` is where it is spent. A rung emits every file the
rung below emits, byte-identical, plus new ones, so moving up leaves you no
already-reviewed file to review again.

**The ladder is a second axis, not a replacement for the shape flags.**
`--layer` says which invariants the output holds to; `--owned`,
`--materialize` and `--single-file` say what shape it takes. They compose, and
the ladder explains refusals that used to stand alone: `--materialize` turns
down an uncapped run at `view` because the index would have to be allocated,
and `--layer edit --materialize` is how you ask for it anyway.

Above `relate` a schema has to say more than bytes -- which relation pairs a
request with its reply, what the retry policy is -- because both endpoints
must agree on those and a command-line flag cannot make them agree. **None of
it is ever inferred.** There is no default timeout and no implicit
retransmission; where a schema states no policy the generated code has none. A
deployment may override a declared *value* at the command line and may never
introduce a *shape*. Rung 6 owns I/O and never owns the clock: time enters as
a parameter, so a timeout bug reproduces every run instead of racing a
deadline nobody wrote down.

## The language

`docs/grammar.ebnf` is the extracted grammar and section 7 of `project.md` is
the authority; what follows is the working vocabulary.

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

**Where a member ends** is the question the language spends most of itself on,
because it is what decides the capability vector:

```situ
u8   fixed[4];                      // a count
u8   rest[remaining];               // to the end of the frame
u8   name[]    until ":";           // to a delimiter
u8   method[]  until " " max 16;    // bounded, so it stays allocatable
nlattr attrs[] while (nla_len >= 4);  // while a predicate over each element holds
u8   pixels[n] at file.pixel_offset;  // placed where the data says
u32  crc @ 0x1c;                    // assert the offset the solver computed
```

**Attributes** are a closed vocabulary in brackets: `[min = 8]`, `[max = N]`,
`[since = 2]` for a member a later version added, `[encoding = ascii]`,
`[case_insensitive]`, `[trim]`, `[self_as = 0]` for what a checksum's own bytes
read as while it is computed.

**The rest of the declarations:**

```situ
const header_bytes = 8;
enum operation : u16 { request = 1, reply = 2, }
checksum u8 header_checksum[2] covers(header) [self_as = 0];
require absolute_static(arp_packet);         // fails the build if it does not hold
invariant derived.total == size(derived.a) + size(derived.b);
```

`require` is a compile-time assertion about the capability vector; `invariant`
names a field situ maintains rather than one it merely checks.

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

Four backends are worth nothing if they disagree, so several checks exist to
make disagreement fail rather than ship.

The differential one generates a driver per backend *from the layout*, feeds
all four the same pseudo-random buffers -- drawn from several alphabets and
mostly short, because uniform noise never enters a text protocol's parse paths
-- and diffs the answers. It writes as well as reads: every writable scalar
takes a pattern, and the buffer afterwards is the assertion, because a byte
order reversed in a setter is invisible to a read pass over bytes nobody wrote.

Beside it: every schema is generated *and compiled* in every backend, because
generating is not compiling; every schema's dissector is executed over bytes,
because parsing is not running; and two checks compare each backend against the
capability map rather than against each other -- one asking that every member
the map calls writable has a setter, the other that every member it calls
unwritable says why. A backend that silently omits an operation the map
promises is invisible to a backend-versus-backend check unless the four happen
to disagree.

## Is your format worth a schema?

Situ is worth its cost above a floor, and the floor is not a field count. Five
projects evaluated it against real trees and two said no; what follows is
their reasoning rather than an argument from this side.

**The wrong axis is size.** A five-field record read by two implementations in
two languages is above the floor. A forty-field format parsed once into native
objects, in one language, by one program, may be below it. Counting fields
predicts almost nothing.

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
`offset += N` and they work. `examples/keystore` is deliberately that shape,
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
make hooks      # install the commit-message hook from tools/hooks/
make help       # everything else
```

`test` and `check` are not the same target and the difference matters:
`make test` is pytest plus the generated C, which is the fast one to run while
working. `make check` is what has to pass before a commit -- it adds `style`
(indentation, ASCII, and project.md's own claims), `typecheck` (mypy strict
over `situc`, `tools` and `tests`) and `cross-test` (the generated accessors on
aarch64 under emulation).

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
`tools/style_gate.py` under `make style` is the enforcement instead
(decision 0003). `make lint` is kept as an alias for it.

## Layout

```
project.md        the specification, and the authority on intent
docs/decisions/   append-only decision records, numbered, with alternatives
docs/grammar.ebnf extracted from section 7, held in sync by a test
situc/            the compiler: Python 3.11+, standard library only, mypy strict
runtime/          one runtime per backend, each thin; the arithmetic lives in C
examples/         one directory per protocol, each with at least one `require`
tests/            unit, generated-C, cross-architecture, golden diagnostics,
                  a Wireshark stub, and the schemas written to be awkward
tools/            the style gate, the commit-msg hook, the benchmark, the sweep
```

## Status

The phase plan in section 26 ran out some time ago; what came after it is
recorded in 26.14 onward, in folds -- a batch of work, then what it found and
what it left open. Every construct the language offers is reachable in all four
backends, and 26.31, the list of where the frontier is, has no open gap on it.
The latest fold is the place to look for what is open today; each entry there
says why it is, and two of them say why they are deliberate rather than
pending.

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
does to the image.

**The walker is `situ-walk`, a separate binary.** `situc` never walks an
image -- decision 0026 keeps the compiler and the interpreter apart, so that
an offset stays a constant and an operation stays *absent* rather than
refused -- and `bin/situ-walk` shares nothing with it but the format (0026,
amended 2026-08-07). That is what lets it join the differential check as a
fifth column, answering whether a table walk says the same thing as four
compiled backends about hostile bytes. It does, for every schema here.

What it renders is a named subset, `walker.report.SUPPORTED`, and `situc
pack --coverage` says what any given image contains. Note also what an
interpreter cannot do, which is make an operation *absent*: under a walker the
capability map stops being the shape of the interface and becomes data a
caller may consult.

## Reading further

`man situc` is the command reference and `docs/grammar.ebnf` is the grammar.
Beyond those, `project.md` is long and is meant to be. Useful entry points:

- **Section 11**, the capability lattice: the core of the project. Every other
  part of the compiler exists to feed it or to report its results.
- **Section 26.31**, where the frontier is: what is unfinished, re-derived from
  generated output rather than remembered. It has been wrong four times, and
  says so.
- **Section 0**, on how the document is kept honest: seven claims in it are
  held to the code by tests that fail when they drift. Every one was added
  after finding drift, and each found more than expected.
- `examples/README.md` for what each worked example is there to prove.

The examples are the schemas to read first: `udp` for the smallest complete
one, `ipv4` for bit packing, `dns` and `dnsname` for a variant and a walk,
`http` for a text protocol, `sqlite` for an offset table, `ble` for a count of
records that each say how long they are, `netlink` for a format whose byte
order is the sending machine's, `cpio` for numbers written as digits, and
`packet` for authenticated and sealed regions.

Eight of them carry a `.vectors` file, and what is in it matters more than that
it exists: bytes some *other* implementation wrote, with that implementation
named. ImageMagick for `bmp` -- a whole file, header and pixels -- glibc's
`struct arphdr` for `arp`, lwIP's SNTP client for `ntp`, U-Boot's `bin2bcd` and
DS1307 driver for `rtc`, the Linux Bluetooth stack for `ble`, GNU cpio for
`cpio`, and for `netlink` the reply a `NETLINK_ROUTE` socket gave to a dump
request. A description that agrees only with its own compiler has demonstrated
nothing, and two of those eight disagreed with the schema on arrival.
