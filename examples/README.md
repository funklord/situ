# Examples

One directory per protocol, so each can grow generated code, hex vectors and a
build alongside its schema as the phases land.

Each schema is annotated with what it demonstrates and what it costs. They are
meant to be read in roughly this order.

## Buildable now (phase 1 static subset)

| Example | Demonstrates |
|---|---|
| [ethernet](ethernet/) | The control case. Every capability at its strongest value. |
| [udp](udp/) | The smallest header with a length field pointing at a payload it does not describe. |
| [icmp](icmp/) | A field whose meaning depends on an earlier one, and a checksum modelled the wrong way on purpose. |
| [arp](arp/) | A generic format pinned into a static one with `[must_eq]`. |
| [tcp](tcp/) | Dense bit packing where every group closes on a byte boundary. |
| [ipv4](ipv4/) | The same, where one field does not: `u13` forces `[allow_straddle]`. |
| [dns](dns/) | Sub-byte enums, and an honest boundary where the format stops being a layout. |
| [ntp](ntp/) | A format designed with alignment in mind, and where `repr` starts to matter. |
| [bmp](bmp/) | Little endian, and the canonical misaligned layout. |
| [telemetry](telemetry/) | A protocol under design, with its capability budget written in as requirements. |
| [tiff](tiff/) | `endian_marker`: byte order resolved at parse time, and why it costs nothing on the offset axis. |
| [message](message/) | Islands of staticness inside a dynamic frame, and the views that reach them. project.md example 5.2. |

## Waiting on later phases

These are rejected today with a diagnostic naming the phase that will accept
them. They are kept in the tree because they pin that behaviour, and they
become live as each phase lands.

| Example | Needs | Demonstrates |
|---|---|---|
| [packet](packet/) | phase 8 | The doom principle as a stage gate; tag coverage against in-place mutation. project.md example 5.3. |
| [registers](registers/) | phase 10 | MMIO: where a missing setter is the deliverable. project.md example 15.2. |

## Reading them

```
situc dump-ast examples/ipv4/ipv4.situ
situc map      examples/ipv4/ipv4.situ
situc map --format=summary examples/ntp/ntp.situ
```

Each buildable example carries a committed `*.situ.map` beside its schema. That
is the capability vector of every field, and it is the artifact worth reading:
the maps are where these stop being documentation and start being the point.

The map is regenerated and compared by the test suite, so a change to the
compiler that moves a field or weakens an axis shows up as a reviewable diff in
the same commit. Regenerate one with:

```
python3 -m situc.cli map examples/ipv4/ipv4.situ > examples/ipv4/ipv4.situ.map
```

Requirements the current build cannot decide are reported rather than passed
over. Today that means the capability predicates, which need the lattice from
phase 3; `situc map` names them and the phase that will check them.

## What they are for

Three jobs, in order of how much they matter:

1. **Design pressure made visible.** ipv4 against tcp is the clearest pair: the
   same density of bit packing, but one of them straddles and the other does
   not, and that is a property of the wire format rather than of the schema.
2. **Test material.** Every schema here is parsed by the test suite, the
   buildable ones are checked to round-trip, their committed maps are
   regenerated and compared, and their generated C is compiled warning-clean
   on host and aarch64. `tiff` and `message` are exercised end to end under cmocka,
   from their schemas straight out of this directory, so they cannot drift from
   a copy.
3. **A place to put generated code.** Each directory holds its schema and its
   capability map; generated `.h`/`.c` land in the build tree rather than being
   committed, so a codegen change cannot leave a stale copy behind.
