# situ, from the first consumer to describe a variable-length tree

Written 2026-09-17 from `raidcfgd`, which that day put its whole socket
payload -- a snapshot of one or more RAID controllers, each holding arrays,
volumes, drives, enclosure components, stacked block layers and free-form
key-value maps -- into three situ schemas (`wire/common.situ`,
`wire/status.situ`, `wire/message.situ`), generated the reader with
`situc build --target c --layer view` from a `git archive` of your `47772fe`,
committed the output with the runtime, and proved a hand-written encoder
against the schema with `situc verify` over 1,101 vectors. fuzznet's
`frame.situ` is nearly fixed-layout with one bounded variable member; this
is the other kind -- counted runs of variable-size structs nested four deep,
with length-prefixed UTF-8 text at every level -- and it is what netcfgd
will bring next after its C rewrite. Everything below was measured on that
schema; the numbers are in `raidcfgd`'s `project.md` under "The wire is
binary", and the schema is public if you want to run any of it yourself.

## What worked, so the rest reads in proportion

Every shape the tree needed exists and composed: `u16 len; u8 v[len]
[encoding = utf8]`, `count [max = N]` then a run of structs that each
measure themselves, runs nested in runs nested in runs, enums with `default
= error`, `[must_eq]` on a version byte, `require canonical` on the whole.
`situc map` accepted nested counted runs of variable elements at every depth
without flattening. `situc advise` moved every variable member after the
fixed ones and was right to; the 54 suggestions that remained were all "a
string after another string", which is unpayable in a struct made of
strings and is recorded in the schema so nobody chases it. `situc verify`
refused a health byte of 9, a version of 2 and an overrunning length with
the member named, which is exactly the independent witness a hand encoder
needs. Wire size against the indented JSON it replaced: four to five times
smaller.

## 1. The generated accessors are exponential in a struct's variable members

For a struct with k variable-length members, member k's generated `_offset`
recomputes every earlier member's `_extent`, and each `_extent` calls its own
`_offset` three times, so the cost is on the order of 4^k per access. Our
`physical_drive` has nine strings and a key-value run. Measured with gprof
on one decode of a 12,344-byte snapshot (34 drives) walked through the
generated accessors: **3,000,000 calls to `rcs_physical_drive_status_text_extent`
and 573 ms.** The struct-level `_validate`, `_extent`, `_at` and `_required`
inherit the cost, since they are built from the same accessors.

What we did: the decoder walks the tree with a cursor of its own -- the
fixed prefix through the generated getters, the first variable member
through its generated `_view`, then sequentially by each leaf's `_extent` --
and uses the generated `_check` only for `str`, `text`, `kv` and the small
standalone messages. Same bytes, 0.54 ms. **The consequence to state
plainly: the generated struct-level validators are not the gate for the
tree.** Every constraint is checked by name in the codec, and `situc verify`
is what makes that independent. A consumer who trusts `situ_X_validate` on
a tree like this has a validator that is correct and unusable.

Suggestion: memoise the extents of preceding members within one accessor
call, or emit a single sequential walker per struct that computes all
offsets in one pass, and say in the generated header which access pattern
costs what. The map already says `access=Sequential`; it does not say
"exponential in k".

## 2. `_required` wraps on a `u32` length

The generated `situ_rcm_message_required` computes `7 + length` in
`uint32_t`. A length of `0xFFFFFFFF` wraps to 6, and the function reports a
complete message from seven bytes that claim four gigabytes. Our framer
checks the length against the schema's `[max]` from the raw header before
asking `_required`, and the test pins it. fuzznet checked its own generated
frame on hearing this and is clear, for the reason you would expect: its
lengths are `u16 [max = 1024]` and its fixed structs get a constant `need`.
The bug is specific to a `u32` length feeding `_required`.

Suggestion: compute `need` in a wider type or check for overflow, and apply
the field's `[max]` before the sum -- a length the schema already bounds
should never reach an addition it can overflow.

## 3. `_required` and `_span` over a counted run stop silently at the limit

Over a run of `count` elements, the generated `_required` and `_span` walk
until the buffer's limit and return what they reached; a buffer holding
fewer elements than `count` declares reads as complete rather than short.
The caller has to walk to the declared count itself. We do; a consumer who
believed `_required` would accept a truncated run as a whole message.

## 4. Smaller things

- Nested `[encoding = utf8]` checks inside a parent's `_check` are emitted
  at `base + 2u` with length 0, which are no-ops; the real check happens in
  the nested `str_validate`. Harmless as long as the nested one runs, and
  misleading to read.
- The `SIZE_MAX` constants overflow `uint32_t` for a bounded tree:
  `RCS_SNAPSHOT_SIZE_MAX 9758327360018u` compiles with a warning-free
  truncation and is wrong. A tree with `[max]` on every count and string
  has a computable maximum and it is not a 32-bit number.
- `import` splices flat, so two schemas that both import `common.situ`,
  generated under one prefix, define `situ_str_check` twice at link. We
  generate under `rcs_` and `rcm_`, which means the envelope's copy of `str`
  is a second definition rather than a shared one. A `--prefix` per import,
  or emitting imported types once under the import's own prefix, would let
  a consumer share `common.situ` the way fuzznet's fourteen layouts and
  ours already spell it.
- A struct member named `kind` beside an enum named `<struct>_kind` flattens
  to one C identifier and situc refuses by name. We renamed the enums; a
  message would have been kinder than a collision.
- `situc verify` over 1,101 vectors takes about three minutes, most likely
  the same accessor cost in the Python backend.

## What we are not asking for

An encoder for variable-length structs. The hand-written one, decoding its
own output before it returns and verified by `situc verify`, is the honest
shape for a consumer that owns its model in C++; what it needs from situ is
a reader it can trust and a verifier it can run, and it has both.
