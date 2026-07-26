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

## Waiting on later phases

These are rejected today with a diagnostic naming the phase that will accept
them. They are kept in the tree because they pin that behaviour, and they
become live as each phase lands.

| Example | Needs | Demonstrates |
|---|---|---|
| [tiff](tiff/) | phase 4 | `endian_marker`: byte order resolved at parse time, and why it costs nothing on the offset axis. |
| [message](message/) | phase 5 | Islands of staticness inside a dynamic frame. project.md example 5.2. |
| [packet](packet/) | phase 8 | The doom principle as a stage gate; tag coverage against in-place mutation. project.md example 5.3. |
| [registers](registers/) | phase 10 | MMIO: where a missing setter is the deliverable. project.md example 15.2. |

## Reading them

```
situc dump-ast examples/ipv4/ipv4.situ
```

From phase 2, `situc map` will print the capability vector for every field,
which is where these stop being documentation and start being the point.

## What they are for

Three jobs, in order of how much they matter:

1. **Design pressure made visible.** ipv4 against tcp is the clearest pair: the
   same density of bit packing, but one of them straddles and the other does
   not, and that is a property of the wire format rather than of the schema.
2. **Test material.** Every schema here is parsed by the test suite, and the
   static ones are checked to round-trip.
3. **A place to put generated code.** Each directory will hold its `.h`/`.c`,
   its golden vectors and its capability map once phase 4 arrives.
