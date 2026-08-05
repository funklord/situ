# situ, from a project that is a near-miss twice over

Written 2026-08-04 from `apt-emerge`, a Portage-flavoured package manager
front-end for Debian: one stdlib-only Python file that parses Debian control
files, `Release` files and OpenPGP ASCII armour. situ was run, not read
about.

The verdict is **no**, for two independent reasons. Neither is "this is not
the sort of program situ is for" -- the first is structural and fixable at
situ's end, and the second is a genuine scope boundary worth having written
down.

## Reason 1: the Python backend cannot reach a single-file program

Hard rule 1 of this project is that the shipped artifact is **one file that
imports nothing outside the stdlib**. That is not tidiness. The dpkg backend
exists for embedded boxes with no `apt-get`, where the deploy story is one
`scp` and the repair story is a text editor on the target. CI enforces it:
one job walks the AST for imports and fails on anything outside
`sys.stdlib_module_names`.

What the backend emits:

```
$ situc build examples/http/http.situ --target python --out /tmp/gen
situc: wrote /tmp/gen/http.py
$ cd /tmp/gen && python3 -c "import http"
ModuleNotFoundError: No module named 'situ_runtime'
```

So adopting situ costs two files, not one: a 36 KB generated module plus
`situ_runtime.py`, itself 608 lines. Either would fail the CI gate on its own,
and the second is a hard import rather than something that can be vendored by
copy-paste without thought.

**The suggestion is an amalgamation mode** -- something like
`--target python --single-file` -- that inlines the parts of `situ_runtime`
the schema actually reaches and emits one self-contained module with stdlib
imports only. The runtime is already `enum`/`sys`/`typing` and nothing else,
so the output would stay stdlib-clean; what is missing is the inlining and
the dead-code trim.

This is worth more than it looks. Single-file-by-constraint is not rare in
exactly situ's problem space: recovery tools, installers, initramfs helpers
and embedded agents all parse binary formats *and* have a hard "one file, no
dependencies" rule, and they are the programs least able to hand-write a
correct parser and most damaged by getting one wrong. They are currently
excluded by packaging rather than by fit.

## Reason 2: it would still be no, and this is the more interesting half

Suppose the single-file problem were solved. The answer here is still no,
and the reason is a clean line between what situ generates and what this
program needs.

**The layer situ would fit is not the layer that is hard.** A Debian
`Packages` index is stanzas separated by blank lines, each a `Key: value`
with space-continued lines. That is almost exactly `examples/http/http.situ`:

```situ
struct header_field {
	u8  name[]   until ":"     [case_insensitive, encoding = ascii];
	u8  value[]  until "\r\n"  [trim];
}
```

deb822 is that with a different terminator and a continuation rule. situ
would describe it well. But `parse_stanzas` here is about 15 lines and has
never had a bug.

What *is* hard is `parse_depends`, which turns

```
libc6 (>= 2.36), libgcc-s1 | libgcc1, python3:any, foo [amd64] <!nocheck>
```

into alternatives with operators and versions. That is an expression grammar
over a field's text, not a byte layout -- repetition, alternation, optional
parenthesised constraints, architecture and build-profile qualifiers. situ
does not generate it and should not try. So adoption would replace the easy
20 lines and leave the hard 15 exactly where they are.

**And the access pattern is wrong for what situ optimises.** situ's value is
byte-exact accessors over a buffer: zero-copy reads, in-place mutation, and
the derived explanation of when those are impossible. This program parses an
index once at startup into plain dicts, never mutates a control file, and
never writes one. It wants a parse, not a view. Everything situ is good at
would be paid for and unused.

That distinction seems worth stating in situ's own README, because "wire
protocols, packet formats, on-disk records" reads as *if you parse a format,
this is for you*. The sharper claim is: **if you hold a buffer and need
fields out of it, especially to write them back**. A program that parses text
once into native objects is outside that, however byte-exact the format is.

## Where we would come back

One place, and it is real. `dearmor()` converts OpenPGP ASCII armour to raw
packets so `gpgv` can be handed a binary keyring. It deliberately does not
interpret those packets -- base64 in, opaque bytes out, gpgv does the rest.

If this program ever has to check a key itself -- expiry, fingerprint, which
subkey signed a `Release` -- without shelling out to `gpgv`, then it is
parsing OpenPGP packet headers: length encodings with three forms, tag bits,
subpacket areas. That is byte-exact layout of the kind that is genuinely
dangerous hand-written, on data fetched from the network, in a program that
runs as root. situ would be the right answer, and the single-file constraint
above is what would decide whether it could be taken.

## The suggestion worth more than either verdict: a differential oracle

This is the one thing from this project I would most want situ to take.

The two most valuable test suites here are not hand-written -- they are
**differential against an independent implementation**:

- `vercmp` reimplements Debian policy 5.6.12. Every version pair in the
  suite is *also* run through `dpkg --compare-versions`, and the two must
  agree.
- The 3-way config merge documents itself as `diff3 -m` equivalent, so its
  output is compared against real `diff3`.

Both found bugs that hand-written expectations did not, and the reason is
structural rather than lucky. A hand-written vector encodes **what the author
believed the format says**. When the author misreads the spec, the
implementation and the test are wrong in the same direction and agree
perfectly forever. An independent implementation is wrong in a *different*
direction, so disagreement is visible.

situ's generated vector tests are good -- I read the C for `arp.vectors` and
they assert concrete field values, so a wrong offset fails loudly. But the
vectors are still authored alongside the schema, by the person who read the
RFC. That is exactly the failure mode above.

**The pieces to do better are already in the tool.** `gen-dissector` and
`gen-fuzz` exist, and a large share of `examples/` are formats with an
authoritative third-party decoder:

| example | independent oracle |
|---|---|
| `arp`, `ethernet`, `ipv4`, `icmp`, `dns`, `modbus`, `mqtt`, `ble` | `tshark -T json` |
| `bmp` | `file`, ImageMagick `identify -verbose` |
| `cpio` | `cpio -tv` |
| `sqlite` | `sqlite3` |
| `protobuf` | `protoc --decode_raw` |

So: decode a corpus with situ's accessors and with `tshark -T json`, compare
the fields both name, fail on divergence. That is a far stronger claim than a
hand-authored vector, it needs no expected values written by hand at all, and
it scales to however many capture files exist -- including whatever `gen-fuzz`
produces, which turns fuzzing from crash-finding into correctness-finding.
A fuzzer with a differential oracle finds *wrong answers*, not just crashes,
and wrong answers are what a schema compiler risks shipping.

The honest caveat: this only covers formats someone else already implements,
which is a minority of what situ is for. But for that minority it is close to
free, and those are the examples people read first to decide whether to trust
the tool.

## One check worth running on the generated tests

Related, and cheap. Break the generator deliberately -- shift one field's
offset by a byte, drop a bounds check -- and confirm the generated suite goes
**red**. If any generated test still passes, that test is not testing what
its name says.

This is not a hypothetical worry. Two tests written *in this project this
week* asserted nothing, and both looked completely reasonable: one exercised
a function that returns early unless run as root, so it passed whatever the
code did; another restored a monkeypatch over itself in cleanup, so it
verified a patched function rather than the real one. Neither was found by
review. Both were found by deliberately breaking the thing under test and
noticing the test stayed green.

Generated tests deserve that treatment more than hand-written ones, not
less: they are produced in bulk, they all look alike, and nobody reads the
hundredth one.

## What was good, from outside

`situc explain` is the thing that would make this adoptable rather than
merely usable. A generated accessor that is *missing* is normally a silent
gap you discover at the call site; here it comes with the property that took
it away and the schema change that gets it back. That is the feature to lead
with -- it is what separates situ from every other "describe the format,
generate the reader" tool, more than the backend count does.

**The `deb` target is better than the one I wrote this week, in a specific
way worth keeping.** It stages through the same `install` rule a user runs,
so a packaging bug and an install bug cannot be different bugs -- and then it
treats anything left in the staging tree that neither package claimed as a
packaging bug and reports it. That second check is the good part: the normal
failure is a file that silently ships in no package at all, and nothing about
a successful build hints at it. I am adopting the idea in `apt-emerge`, which
currently has no equivalent.

The gap next to it is the one in the fmake file too: `situ` has no
`.github/workflows/`, so nothing builds the package, **installs** it and runs
the installed binary. Building a `.deb` proves very little on its own. In
fmake's case that exact job would have caught a live bug -- its package
declares `python3 (>= 3.11)` and the program needs 3.12, so on Debian
bookworm it installs cleanly and then refuses to start.
