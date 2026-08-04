# Examples

One directory per protocol, so each can grow generated code, hex vectors and a
build alongside its schema as the phases land.

Each schema is annotated with what it demonstrates and what it costs. They are
meant to be read in roughly this order.

Type names here are snake_case, which is a house style and not a rule: the
compiler reads no casing at all, and a schema may use snake_case, PascalCase or
a mix. What it does check is that two constructs never generate the same C
identifier -- a property of flattening a path, not of how either name is spelled
(`docs/decisions/0013-identifier-conventions.md`).

## Buildable now

| Example | Demonstrates |
|---|---|
| [ethernet](ethernet/) | The control case. Every capability at its strongest value. |
| [udp](udp/) | The smallest header with a length field pointing at a payload it does not describe -- and the payload, which costs nothing because nothing follows it. |
| [icmp](icmp/) | A field whose meaning depends on an earlier one -- a variant whose arms are all four bytes, so it costs nothing -- and a checksum that covers its own bytes. `[self_as = 0]` is RFC 1071 said in the schema; `tests/generated/test_icmp.c` computes it and compares against a number the kernel wrote. |
| [arp](arp/) | A generic format pinned into a static one with `[must_eq]`, and the same packet unpinned beside it: two capability maps for one format, which is what pinning a length buys, in one file. |
| [tcp](tcp/) | Dense bit packing where every group closes on a byte boundary. |
| [ipv4](ipv4/) | The same, where one field does not: `u13` forces `[allow_straddle]`. Also a checksum covering a span the data decides -- the options -- and what covering everything costs a router that rewrites `time_to_live` on every hop. |
| [dns](dns/) | Sub-byte enums, and an honest boundary where the format stops being a layout. |
| [ntp](ntp/) | A format designed with alignment in mind, and where `repr` starts to matter. |
| [bmp](bmp/) | Little endian, and the canonical misaligned layout. |
| [telemetry](telemetry/) | A protocol under design, with its capability budget written in as requirements. |
| [tiff](tiff/) | `endian_marker`: byte order resolved at parse time, and why it costs nothing on the offset axis. |
| [message](message/) | Islands of staticness inside a dynamic frame, and the views that reach them. project.md example 5.2. |
| [protobuf](protobuf/) | The language's hardest conformance test: the worst case on every axis, and five independent causes of non-canonicity. project.md section 9.7. |
| [sqlite](sqlite/) | An offset table: pointers in key order, cells filling the page backwards, and the two things a real one asked for that situ has not got. project.md section 9.3. |
| [packet](packet/) | The security position: the doom principle as a stage gate, and tag coverage against in-place mutation. project.md example 5.3. |
| [registers](registers/) | The MMIO target, where a missing setter is the deliverable: `access_width` plus `no_rmw` makes setting one bit a compile error. project.md example 15.2. |
| [dnsname](dnsname/) | The boundary, in one file: situ describes a compressed name completely and walks it, and cannot follow the pointer -- the difference is the view model rather than a gap. Walking it is what turned a variant's extent from "unknowable" into a switch on the discriminant. Also the one weakening a schema has to declare because the layout does not imply it. |
| [ipv6ext](ipv6ext/) | A self-describing chain: each header names what follows, and the run ends after the one that names something else. The construct SMTP asked for first and did not get, because one protocol is not evidence. |
| [smtp](smtp/) | A fixed-width number written in digits, a body ending at a dot on its own line -- and the one framing rule in this directory that situ cannot express, said in the schema rather than in a footnote. |
| [http](http/) | Text, which situ said for a long time it would not describe: delimited fields, a header block ending at a blank line, and a status code written in digits. Read the three warnings it produces as the bill for framing in text. project.md section 8.6. |
| [ble](ble/) | A count of records that each say how long they are, which is the construct this example added: `num` reports with no stride to index by, so the run is walked. Written from the Linux Bluetooth stack, and the two facts a header alone cannot give -- an RSSI trailing a flexible array, a zero length ending a run -- come from its parsers. |
| [netlink](netlink/) | Host byte order: the wire is whatever the sending machine uses, which costs `canonical` on every field and is why `endian native` has to be declared. Attributes padded to four bytes with `align_up`, and a variant on the message type. Written from the kernel's own netlink walk, and its vector is bytes a `NETLINK_ROUTE` socket handed back. |
| [cpio](cpio/) | Every number written in ASCII hexadecimal, eight digits wide: `hex` is the radix nothing had used, and a cpio header is thirteen of them in a row -- one of which drives the length of the name that follows. Written from `init/initramfs.c`, and its vectors are an archive GNU cpio wrote, padding and all. |
| [modbus](modbus/) | The same field meaning two layouts, with nothing in the bytes to say which: function code 03 is an address and a count going one way and a byte count coming back. Two structs over one enum, and the direction is the caller's -- which is the protocol having no field for it rather than a gap in situ. The exception response needs no construct at all: section 5 makes it a different function code, so it is an arm like any other. Its boundary is at the serial line, where RTU separates frames by 3.5 character times of silence. |
| [mqtt](mqtt/) | The protocol behind most of what is called IoT, and four constructs it puts together that this directory had only apart: a varint that is the frame's own length, a discriminated union over a four-bit type, a run of records ending where the packet does, and three packets whose body is *nothing* -- which is a zero-byte struct, so they stay in the same switch as everything else. Its boundary is a member whose presence depends on a flag, which situ has no construct for and which MQTT needs twice. |
| [rtc](rtc/) | BCD and fixed point, and the register file that made `bcd2 [bits = 7]` exist: a control bit sits above seven bits of packed decimal, which a nibble-per-digit type could not describe. Decision 0027. |

## Waiting on later phases

None: every example that was gated on a phase has had its phase land. The
`// STATUS: needs phase N.` convention stays documented here because the next
construct to be gated will use it, and the test suite still enforces it for any
example that carries the marker.

## Reading them

```
situc dump-ast examples/ipv4/ipv4.situ
situc map      examples/ipv4/ipv4.situ
situc map --format=summary examples/ntp/ntp.situ
situc map --check examples/ipv4/ipv4.situ    # fails if the committed map drifted
situc advise   examples/message/message.situ # ranked, costed suggestions
situc gen-checks examples/tcp/tcp.situ       # tests that hold the accessors to the map
situc explain  examples/message/message.situ message.recs[].value
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
over, with the phase that will decide them. A failing `assert` is reported as a
warning and the build continues, which is what `packet` uses to demonstrate the
conflict between in-place mutation and tag coverage without failing its own
build; the `require` form of the same check, and the exact text of its
diagnostic, are pinned in `tests/unit/test_crypto.py`.

## What they are for

Three jobs, in order of how much they matter:

1. **Design pressure made visible.** ipv4 against tcp is the clearest pair: the
   same density of bit packing, but one of them straddles and the other does
   not, and that is a property of the wire format rather than of the schema.
   `protobuf` is the other end of the range: a format that gives up almost
   every capability, described faithfully and with each loss accounted for.
2. **Test material.** Every schema here is parsed by the test suite, the
   buildable ones are checked to round-trip, their committed maps are
   regenerated and compared, and their generated C is compiled warning-clean
   on host and aarch64. Every one of them then has a `gen-checks` suite built
   and run against its own accessors, so every construct in the language has
   generated code that is executed rather than merely compiled -- which was not
   true before that command existed.
3. **A place to put generated code.** Each directory holds its schema and its
   capability map; generated `.h`/`.c` land in the build tree rather than being
   committed, so a codegen change cannot leave a stale copy behind.
