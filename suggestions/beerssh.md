# situ, evaluated against beerssh

Written 2026-08-04. Per `build-and-commit.md`'s standing instruction that
projects hand-writing wire-format encoders, parsers or layout assertions
should say whether they would adopt situ.

## The short answer

**No, and not because of anything situ does.** beerssh's entire byte-exact
surface is one 33-byte header, in one file, parsed in 24 lines. There is no
second format to keep consistent with it, no other language reading the same
bytes, and no version skew to manage yet. A schema, a generator and a build
dependency would be more machinery than the thing they describe.

Reported anyway, because "we looked and the answer is no" is worth more than
silence, and because *why* it is no may be useful for sizing where situ
starts to pay.

## What beerssh is

An SSH client with a terminal emulator, Qt Widgets, Linux desktop and
Android from one codebase. It moves a great deal of bytes around, which is
what made it a plausible candidate -- but almost none of them are a *layout*
it defines.

## Everything in it that is byte-exact

**The passphrase-derived secret store** (`src/platform/secret_store_file.cpp`).
The whole format:

```
magic       4 bytes   "BSSH"
version     1 byte    currently 1
salt       16 bytes   PBKDF2
nonce      12 bytes   AES-256-GCM
ciphertext  rest      with a trailing 16-byte GCM tag
```

Written in one function, read in another, 24 lines of offset arithmetic
between them. The plaintext inside is JSON, so the interesting structure is
not in the layout at all.

**The Android Keystore store** (`src/platform/secret_store_keystore.cpp`):
`iv (12) || ciphertext+tag`. Two fields, and the split happens in Java on
one side and C++ on the other -- which is the one place in this project
where situ's "one schema, several languages" argument would apply. It
applies to twelve bytes.

That is the complete list.

## What looked like situ's domain and is not

Worth writing down, because from outside the tree this project looks full of
wire formats:

- **Terminal escape sequences** -- a byte protocol, but a *streaming* one
  with unbounded scanning, and libvterm parses it. Not a layout.
- **Sixel and Kitty graphics payloads** -- parsed by libsixel and by a
  hand-written key/value reader. Kitty's is `key=value` pairs plus base64;
  text, not layout.
- **`known_hosts` entries** -- `|1|<base64 salt>|<base64 HMAC-SHA1>`. A text
  format with base64 fields, defined by OpenSSH.
- **The host list, presets and layout** -- INI files, by an explicit
  decision in the project's own design notes.
- **The screen and scrollback** -- in memory only, never serialised.

So the pattern is: where this project touches a binary format, somebody
else's library owns it; where it owns a format, it chose text.

## What would change the answer

Concretely, any one of these:

- **A second implementation reading the same bytes.** The Keystore blob is
  already split across Java and C++, and if that grew past two fields the
  argument would start. Today the Java side does two `System.arraycopy`
  calls.
- **A format that has to survive a version bump**, and this is the strongest
  one. The store carries a version byte precisely because a planned move
  from PBKDF2 to Argon2id is meant to be a bump rather than a rewrite. When
  that lands there will be two layouts and one reader that must accept both.
  Section 19 is exactly that -- `variant` on the version discriminant for a
  revision that re-lays the bytes, `[since = N]` for one that appends -- and
  it is a better answer than the `if (version != 1) return false;` that is
  there now, which is a check that will become a branch and then two
  branches. That is the point at which "generate the accessors" stops being
  tidier and starts being safer, and it is the trigger to re-run this
  evaluation.
- **Session persistence across restarts.** Currently out of scope, and the
  design notes say plain SSH cannot do it. If it ever arrives it would mean
  serialising real structure.

None of those is near.

## The examples, as an adoption question

All 27 directories under `examples/` are either public formats -- ARP, DNS,
HTTP, TIFF, SQLite, MQTT, netlink, protobuf -- or design exercises named for
the property they demonstrate (`message`, `packet`, `registers`,
`telemetry`). Every one of them is a format somebody else specified, or one
built to show a capability.

None is the shape most applications actually have: a small private layout
the program invented for itself, of the order of five fields, with a version
byte and an encrypted tail. That is beerssh's, and it is probably
fuzzypickles' and netcfgd's too.

The effect from outside is that situ reads as a tool for implementing
somebody else's protocol. That is a fair description of the hardest thing it
does, and it is also the reason a project with a 33-byte header does not
recognise itself in the documentation and never runs the trial. An example
of a small private record -- explicitly labelled as being near the floor,
and honest about the fact that the hand-written version is only twenty-odd
lines -- would do more for adoption among application projects than another
public format, precisely because it would let a reader conclude *no* quickly
and for the right reason.

## One observation, offered rather than requested

The bar in `build-and-commit.md` is *materially better, not merely tidier*,
and applying it honestly here meant answering no to a tool that would
genuinely produce nicer code than 24 lines of `blob.mid(offset, N)` and
`offset += N`. If situ has a stated view on where its floor is -- a field
count, a language count, a version count below which it is not worth it --
saying so in its own docs would let a project like this reach the same
answer without a trial. The README argues its case from what it does when it
*cannot* generate something, which is a strong argument to somebody already
holding a complicated format, and no help at all in deciding whether the
format you have is complicated enough.
