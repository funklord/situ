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

Generated code never allocates, never recurses, and uses no VLAs. A view is a
value: a base, a limit and a generation counter. One bounds check acquires it
and every accessor after that is arithmetic.

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
| `situc build` | accessors: C, C++, Rust or Python (`--target`), plus a second accessor family with `--materialize` |
| `situc map` | the capability map; `--check` compares against a committed one and fails on a diff |
| `situc explain` | one field's capability vector and the blame chain behind every weakening |
| `situc advise` | ranked, costed schema changes that would restore what was lost |
| `situc diff` | capability regressions between two revisions of a schema |
| `situc wire` | the byte-level contract, and `--check` against the committed one |
| `situc doc` | RFC-style byte-layout diagrams and a field reference |
| `situc gen-checks` | tests holding the generated accessors to the map they were generated beside |
| `situc gen-tests` | golden-vector tests from a schema and hex vectors |
| `situc gen-fuzz` | a libFuzzer harness per parseable struct |
| `situc gen-dissector` | a Wireshark dissector in Lua, over the same traversal the code backends use |
| `situc gen-derived` | codec implementations from kernel descriptions |
| `situc import-proto` | a `.proto` read as a description of its wire format, reporting what it could not represent |
| `situc lsp` | a language server over stdio: diagnostics, hover, symbols, code actions |

`situc --help` lists the rest. Section 21 of `project.md` is the authority, and
a test holds it to the parser.

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

Three checks exist because four backends are worth nothing if they disagree.
Each backend's output is compiled beside the C and compared field by field on
the same buffer. Every schema in the repository is generated and compiled in
every backend -- generating is not compiling, and that check found a header no
C++ compiler would take. And one asks which members each backend *declines* to
give an accessor to, failing where the four disagree: a construct one backend
emits and another refuses means the schema means different things in different
languages.

## What it will not do

- No wire format of its own. Situ describes formats that already exist.
- No serialization of language-native objects: no reflection, no object graphs,
  no interning.
- No implicit schema evolution and no unknown-field retention. Compatibility is
  explicit, and silently preserving bytes nobody validated is a security
  position rather than an oversight (section 14.5).
- No allocation in generated code. Callers supply memory.
- No recursive types: size and capability computation would not terminate.

## Building and testing

```sh
make            # the C runtime
make test       # pytest, mypy strict, lint, generated C, aarch64 under qemu
make bench      # what the offset cache costs and saves, in all four backends
make fuzz       # every generated harness, under libFuzzer and ASan
make help       # everything else
```

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
(every emitted dissector is parsed, and four are executed against a stub of the
Wireshark API), `doxygen` (the emitted C and C++ comments are extracted and
read back), and `aarch64-linux-gnu-gcc` with `qemu-aarch64`. `make test-c` and
`make cross-test` are the two steps that need their toolchain rather than
skipping: they build and run the generated C directly.

There is no autoformatter. Tabs carry indent level and spaces carry alignment,
which `black` and `ruff format` cannot be configured to leave alone, so
`tools/lint_conventions.py` under `make lint` is the enforcement instead
(decision 0003).

## Layout

```
project.md        the specification, and the authority on intent
docs/decisions/   append-only decision records, numbered, with alternatives
docs/grammar.ebnf extracted from section 7, held in sync by a test
situc/            the compiler: Python 3.11+, standard library only, mypy strict
runtime/          one runtime per backend, each thin; the arithmetic lives in C
examples/         one directory per protocol, each with at least one `require`
tests/            unit, generated-C, cross-architecture, golden diagnostics
tools/            the convention linter and the benchmark
```

## Status

The phase plan in section 26 ran out some time ago; what came after it is
recorded in 26.14 onward. Every construct the language offers is reachable in
all four backends, and 26.31 -- the list of what is unfinished, re-derived from
generated output rather than remembered -- has no open gap on it. What remains
there is deliberate and each entry says why.

It is not packaged. There is no release, no wheel and no version number:
`situc` runs from the tree or from a directory copy, which is the only
distribution the no-dependency policy needs.

One feature is planned and deliberately late: a packed layout image (`situc
pack`) and a separate project that walks one at run time, for a device whose
framing has to change without a firmware rebuild. Section 26.33 is the plan and
decision 0026 is the shape -- including what an interpreter cannot do, which is
make an operation absent.

## Reading further

`project.md` is long and is meant to be. Useful entry points:

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
`http` for a text protocol, `sqlite` for an offset table, `packet` for
authenticated and sealed regions.
