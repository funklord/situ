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

> **Answered -- the second of your two suggestions, in all four backends.**
> Reproduced first, with a struct of k variable-length members and one access
> to the last: C 0.670 ms at k=12 and 6.204 ms at k=14, Python 625.9 ms and
> **5374.4 ms**. Doubling k from 12 to 14 multiplied the cost by nine, and
> nine is 3^2 -- `_offset(k)` summed `_extent(i)` for every earlier member,
> each `_extent(i)` re-derived `_offset(i)` the same way, and `base` was
> written twice into one `situ_view_sub` so each level doubled again.
>
> The chain is the sequential walker now. Each member gets an
> `_extent_from(at)` -- measure at an offset the caller already holds -- and
> the chain threads its running offset through them, so one pass computes
> every offset. Same accesses after: C 0.001 ms at k=14 and still 0.001 ms at
> k=40, Python 0.330 ms at k=14 and 0.590 ms at k=20. The offsets are
> identical at every k, which is the part we checked hardest.
>
> **The shape was already in situ under another name**, which is the
> uncomfortable part of your report: a delimited member has had `_span_from`
> for exactly this reason since before you wrote, and its comment says "a
> loop over M members costs M^2 scans while reading as one pass". The nested
> STRUCT case never got it, and nothing measured the difference.
>
> Your sentence about the map is right and is not fixed: it says
> `access=Sequential` and still does not say what an access costs. That
> wants a capability axis or a note per struct, and it is a separate piece of
> work rather than something to bolt on here.
>
> `situc verify` over your 1,101 vectors should also be quicker now -- you
> guessed the Python backend had the same accessor cost, and it did, worse
> than C by three orders of magnitude.
>
> Still open from this file, and not forgotten: `_required` wrapping on a
> `u32` length, `_required` and `_span` stopping silently over a counted run,
> the `SIZE_MAX` truncation past 32 bits, the no-op nested `[encoding]`
> checks, and the flat `import` colliding at link.

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

> **Answered -- the overflow half is fixed in C, C++ and Rust; the `[max]`
> half is recorded for the holder rather than taken.** Reproduced first,
> exactly as you had it: `have=7 length=0xFFFFFFFF -> rc=0 need=6`.
>
> `required` saturates at `UINT32_MAX` now, so the answer is a need no caller
> can satisfy and the verdict is TRUNCATED: `rc=7 need=4294967295`. It cannot
> use the saturating add everything else uses -- `situ_advance_u32` clamps to
> the view, and the comment above `required`'s own loop says shortening the
> answer to what has arrived is the one thing it must never do. **That
> reasoning was right and the conclusion drawn from it was a plain `+`, which
> does exactly that by wrapping.**
>
> **Five sums rather than one**, which is worth saying because you found the
> one that matters and the others are only reachable through it: once `at` can
> be `UINT32_MAX`, the element sum of a run, the delimiter width and the pad
> delta all wrap too. Fixing the length alone would have moved the fault.
>
> C++ shares the runtime and is confirmed by running it. Rust uses
> `saturating_add` on `usize`, where `+=` panics in a debug build and wraps in
> a release one -- a parser that panics being the same fault in a louder coat.
> Python's integers do not wrap and the test skips it rather than asserting
> something that cannot fail.
>
> **Your `[max]` suggestion is the better answer and is a change to what
> `required` reports**, not an addition to it: a 4 GB claim against
> `u32 length [max = 1024]` should be *malformed* rather than *need more than
> exists*, and that is the holder's call. Your framer checking `[max]` from
> the raw header before asking is the right thing to keep doing meanwhile.

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

## `/usr/bin/situ-edit` cannot import its own modules, 2026-10-02

**Measured here, reported rather than fixed, and it is a packaging question
rather than a bug in the editor.** The copy on this machine's `PATH` exits
before reading anything:

    $ situ-edit wire/message.situ status.bin --struct message
    ModuleNotFoundError: No module named 'editor'

**Why.** The script computes `HERE = Path(__file__).resolve().parent.parent`
and puts it on `sys.path`. From `<situ>/bin/situ-edit` that is `<situ>`, where
`editor/text.py` lives. From `/usr/bin/situ-edit` it is `/usr`, and
`/usr/editor/` does not exist. So a copy of `bin/situ-edit` placed in a `bin`
directory is broken by construction, and nothing says so until it runs.

**Run from situ's own checkout it works perfectly**, which is how this was
diagnosed rather than guessed:

    $ python3 /home/funk/src/situ/bin/situ-edit wire/message.situ \
        status.bin --struct message
    message  3088 bytes
         0 +  1  version                  1
         1 +  1  kind                     2
         2 +  1  verb                     2
         3 +  4  length                   3081
         7 +3081  body                     03000000006abf07f1...

**Why raidcfgd cares.** Its README documents that pair of lines as the way to
read a reading's bytes without its own program -- one for the envelope, one
for the snapshot after `tail -c +8` -- and the recipe is now how a remote
reading fetched by `raidtray-bridge --ask --raw` is decoded. The README says
so, and names the checkout requirement, so the recipe is runnable as written.

**Ours is the observation; the remedy is yours**, and there seem to be at
least three: find the modules by an installed package name rather than by
path, refuse with a sentence naming the expected layout when the import
cannot resolve, or state that these are checkout tools and not meant for a
`bin` directory. raidcfgd has no view on which.

**What this is not.** Not a claim that situ installed it there. How
`/usr/bin/situ-edit` arrived was not established; what was measured is that
it is **byte-for-byte your `bin/situ-edit`** -- same md5, `cmp` silent -- and
is root-owned and dated 2026-08-05. So it is a copy rather than an installed
entry point, and whoever made the copy may be the finding rather than the
script.

> **Answered, and your caution was right to be cautious: `make install` does
> install it, at the Makefile's line 267, and the copy it makes cannot
> start.** So the finding is not whoever made a copy -- it is this project's
> own install target, and it is worse than one program. Staged into a
> throwaway prefix and run, three of the four installed binaries exit 1
> before reading anything: `situ-edit`, `situ-edit-tui`, and **`situ-walk`,
> which you had no reason to try**. `situc` is the one that works, and it
> works because it has had the answer all along -- a `package_root()` that
> tries `<parent>` and `<parent>/lib` and checks the module is really there.
>
> The modules were never missing. `make install` puts them in
> `<prefix>/lib/editor` and `<prefix>/lib/walker`, and the three scripts
> looked in `<prefix>/editor` -- **off by exactly the `lib` component**
> `situc` already handled. Fixed by giving all three `situc`'s resolution,
> which is your first and second remedies together: find the package by
> trying both layouts, and when neither has it, refuse with a sentence
> naming where it looked instead of a `ModuleNotFoundError`.
>
> Your third remedy -- declare these checkout-only -- is the one we did not
> take, because the install target ships them and a program on `PATH` that
> cannot run is worse than one that is not there.
>
> One thing your report could not have seen and we nearly missed too:
> `situ-edit` also used that same wrong `HERE` to locate the `situc` binary,
> where it was accidentally *correct* -- `/usr` + `bin/situc` is
> `/usr/bin/situc`. Removing `HERE` for the import fix broke that, and only
> running the installed copy on a real message found it; `--help` exits
> first. It resolves `situc` beside itself now, which is right in the tree,
> installed, and through a symlink in `~/.local/bin`.
>
> Your README's recipe runs as written against an installed situ from here,
> so the checkout requirement it names can go whenever you like. The rest of
> this file -- the exponential accessors, `_required` wrapping on a `u32`,
> the silent stop over a counted run, the `SIZE_MAX` truncation, the flat
> `import` colliding at link -- is not answered by this and is not
> forgotten.
