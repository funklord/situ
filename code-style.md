<!-- Copied from ~/.claude/guidelines/code-style.md by tool/sync.py.
     Edit the source, not this part: this project's own rules follow
     the end-of-copy line below. -->

# code-style.md

Code style for private projects. This file is the **source**. Every private
project except the one holding it carries a copy at its repo root, written
by `tool/sync.py` -- see *Keeping the copies in sync* at the end.

Vendored submodules, generated sources and attic/historical material are
exempt: they keep whatever their upstream or generator produces. Each
project names its own exempt paths in its part of the file.

## The three rules

1. **`snake_case`, not `camelCase`,** for identifiers this project defines.
2. **Tabs for indentation, spaces for alignment.**
3. **Lowercase filenames,** unless a tool demands otherwise.

Everything below is these three rules in detail, plus the exceptions that
are already settled. An exception not listed here is not yet settled: raise
it rather than deciding in passing.

## 1. Naming

`snake_case` for functions, variables, type names and fields.

This holds **even inside a toolkit whose own API is `camelCase`**. Call the
foreign API exactly as it is spelled (`setParent`, `addWidget`) -- that is
the API's name, not a violation. But names *you* introduce stay
`snake_case`; do not let the surrounding convention pull them across.

- **Name the thing, not its category**: `plan`, not `plan_struct` or
  `plan_result`.
- **No abbreviations that are not already vocabulary.** `observed`, not
  `obs`; `interface`, not `iff`. This matters most where an internal name
  escapes into something you cannot rename later -- a wire format, a config
  key, a CLI output, an on-disk path.
- **One word per concept, everywhere** -- the same word in the type name,
  the file path, the subcommand and the documentation. A synonym introduced
  for variety reads as a second concept.

### Prefixes, and visibility

Prefixes keep this project's symbols from colliding with a library's, so
they follow **visibility**, and the choice is judgement rather than a
mechanical rule:

- **Anything with more than small visibility carries the project prefix**
  -- the public API, and anything a linker or importer outside its own
  module can reach.
- **Module-private symbols are left unprefixed**, so that the absence of a
  prefix reads as "this does not leave the module."

The middle case decides itself on link safety, not taste. A symbol internal
by intent but still reaching the linker -- cross-file within a library, not
`static`, not part of the API -- is *not* private for this purpose: prefix
it. A deliberate parallel copy of a function in two libraries needs a
**distinct** name; the assumption that nothing will ever link both sides
fails later, at a call site that changed nothing, and names files you did
not touch.

Where a language enforces its own scheme, accept it, and say in the
project's part of this file that the toolchain is doing it:

- **Rust** -- `non_snake_case` and `non_camel_case_types` are on by
  default, so types are `PascalCase` and constants `SCREAMING_SNAKE_CASE`
  -- the toolchain's, not a choice. Package systems that demand kebab-case
  (Cargo crates, Debian packages) read back with their own spelling; do not
  invent a third by naming the directory differently from the package.
- **Python** -- a leading underscore (`_name`) is the language's private
  marker and stands in for "unprefixed" above.

## 2. Indentation and alignment

Indent structural nesting with **tab** characters, one tab per level. When
lining up tokens *within* a line -- continuation parameters under an open
paren, a block comment's `*` column, an aligned trailing comment -- use
**spaces**, after the indent tabs. Alignment is then relative to the shared
leading tabs, so it survives at any tab width. No tab width is prescribed;
the viewer decides.

```c
int thing_do(thing_t *thing, const char *name, size_t name_len,
              uint8_t *out, size_t out_cap) {
>---if (!thing) return ERR_MALFORMED;
>---return thing_write(thing, name, name_len, out, out_cap,
>---                    THING_DEFAULT_FLAGS);
}
```

(`>---` marks a tab; everything lining up under `(` is spaces.)

Never mix tabs and spaces *within* the indent itself. Tabs come first and
spaces after; the reverse, or an alternation, breaks at a different tab
width -- and in Python it is a syntax error.

### Settled exceptions

Divergence needs a technical reason. These are accepted:

- **Makefiles** -- a recipe line requires a literal tab, and the body of an
  `ifeq`/`ifdef` outside a recipe may be space-indented, since after a rule
  a tab-led line is read as a recipe. Both are correct in one file, so the
  gate does not apply the tab rule to Makefiles.
- **YAML** -- the spec forbids tabs for indentation. Use spaces.
- **Markdown** -- list continuation and code fences are space-indented by
  specification. Exempt.
- **Debian packaging files** -- exempt, for two reasons measured against
  dpkg. `debian/changelog` has a fixed layout with no tab in it:
  `dpkg-parsechangelog` calls a tab-indented change line "unrecognized"
  and loses the trailer if a tab precedes `--`. A deb822 continuation in
  `control` or `copyright` may lead with a space *or* a tab, but that
  whitespace is field syntax rather than indentation, and everything past
  it is alignment.
- **Go** -- `gofmt` emits tabs natively. Compliant already.
- **Vendored, generated and attic sources** -- exempt, per the header.

**Python**: PEP 8 prefers spaces, but the language's only hard rule is that
indentation must not be *ambiguous*, and tabs-then-spaces is unambiguous at
every tab width. Continuation lines inside brackets are not
indentation-significant at all. Never a space *before* a tab in leading
whitespace -- that is what raises `TabError`.

Anything else that seems to need spaces: signal it to the list in
`claude-guidelines`' `project.md`, follow the rule meanwhile, and it is
settled here in a pass rather than in whichever project met it first.

## 3. Filenames

**Lowercase, always**, for everything the project names itself:
`main_window.cpp`, not `MainWindow.cpp`.

**The separator follows what the name binds to**, which is a technical
difference rather than taste:

- **`snake_case` where the filename becomes an identifier** -- a source
  file, a header, a module. `desired_state.rs` *is* the module
  `desired_state`, and `desired-state.rs` cannot be a module at all, a
  hyphen not being legal in a Rust path; Python imports are the same. That
  is the language's requirement, and not negotiable where it applies.
- **`kebab-case` for prose** -- documentation, design notes, decision
  records. Nothing imports `code-style.md`, so no identifier is at stake,
  and kebab-case is what markdown and URLs settled on. (When this rule said
  `snake_case` for documentation too, 174 of 197 markdown names across the
  workspace were already kebab-case.)

Settled exceptions:

- **Names a tool will not accept lowercased** -- `Makefile`,
  `CMakeLists.txt`, `AndroidManifest.xml`, `Dockerfile`, `Cargo.toml`.
- **Root files with an established convention** -- `README.md`, `LICENSE`,
  `CHANGELOG.md`, `AUTHORS`, `VERSION`. The last is this workspace's own: a
  project that packages or ships a version tracks one, and the build reads
  it for the package version and for what the program prints, so the
  number lives in one place. `claude-guidelines` packages nothing and has
  none.
- **Package-system spellings** -- kebab-case where Cargo or Debian require
  it, which is the spelling prose uses, so a crate directory and the design
  note beside it agree by construction.

### Singular, unless somebody else standardised the plural

**Prefer the singular for a directory this project names itself**:
`helper/`, `doc/`, `fixture/`. The name says what kind of thing lives
there, not how many, and should not have to change when the count does.

Two exceptions, and they are not equal -- the same shape as the lowercase
rule, which yields first to `Makefile` because make will read nothing else,
and only then to `README.md` because the world settled it.

**First: a name a tool requires is not a name we choose.** It outranks the
singular as it outranks lowercase, needs no measurement, and the test is
whether something breaks when the name changes. It is open-ended: a tool
met tomorrow that demands a name gets the same answer, whether the name is
plural, singular, capitalised or none of those. Present here: **Cargo**
looks for `tests/`, `examples/` and `benches/`, and `cargo-fuzz` for
`fuzz_targets/`; **GitHub** requires `.github/workflows/`; **git** keeps
`hooks/`, hence `tool/hooks/`. **A foreign package's directory names are
the same exception one layer out**: the Android SDK's `platform-tools/` and
`build-tools/` are the SDK's spelling. A word-boundary rewrite of `tools/`
once reached into `platform-tools/`, the hyphen being a boundary, and every
adb target in four projects pointed at a binary that was not there -- so a
sweep skips compound names it does not own. Package-system spellings (Cargo
crates, Debian packages) under *Filenames* above are this same carve-out.

**Second: a plural an ecosystem has settled** -- a convention rather than a
requirement: nothing breaks, but a reader would be surprised. Cargo
workspaces keep members in `crates/`. **These need measuring**, and the
project's part of this file names what it measured against, so the next
reader does not reopen it.

Say which kind is being claimed: confusing them lands on whoever renames a
directory that looked like a convention and finds the build broken. And an
inventory name in `harmonization.md` moves only when its owner says so, in
a deliberate pass with each project's gates run against the result -- as
`tools/`, `docs/` and `docs/decisions/` became `tool/`, `doc/` and
`doc/decision/` -- never as a side effect of a style rule.

## ASCII in source

Source and comments are ASCII. Write `--` where prose would use an em dash,
and "section" for a section sign.

This governs **the text the repository writes about itself**, not the data
the software handles. Three exceptions, which are the rule's shape rather
than holes in it:

- **Documentation.** Markdown may use typographic punctuation.
- **User-facing text in UI software.** A tick a program prints is output,
  not prose -- `GREEN('gpg ')` is correct as it stands.
- **Anything that genuinely requires Unicode**: a fixture for a UTF-8
  parser, a terminal emulator's character tables, a font tool.

Where a project needs the rule enforced, `ascii_only` in `.style-gate.toml`
turns it on. In Python and in C/C++ it enforces exactly that shape -- ASCII
outside string literals, Unicode allowed inside -- reading Python with
`tokenize` and C and C++ with a scanner written for the purpose. Every
other language gets a whole-file byte check, and so does a file in those
two that will not lex: a file nobody can parse is not a file that has been
cleared.

**An exception wider than its reason is how a rule stops being enforced.**
The check was once whole-file for everyone, so a project printing two
status ticks switched it off, and an em dash arrived in a comment; in a Qt
tree that kept it off for its toolbar glyphs, 854 of 1114 non-ASCII
characters turned out to be prose in comments.

## Formatters

A formatter is allowed **only if it can be configured to honour the three
rules completely**. Configuration gaps are disqualifying: a formatter that
gets indentation right and alignment wrong rewrites the tree on somebody's
next save. So the decision is per tool, per project, and a real evaluation:

- If it can be made to comply, use it, and commit the config with a comment
  saying which setting is load-bearing and what happens without it.
- If it cannot, do not run it -- **not even ad hoc on a single file**. The
  failure is a silent conversion of files that were already correct,
  found later as a reverted commit rather than an error.
- If no existing tool fits and the rule is worth mechanising, write our
  own. A checker that only gates indentation is worth more than a formatter
  that reflows everything.

**Record the decision and the finding that produced it** in the project's
part of this file -- which tool, what failed, what would change the answer.
A verdict without its evidence gets re-litigated, and a tool that improves
later is never reconsidered because nobody remembers what was wrong with
it.

Naming and filename rules are review items, not automated ones.

## Precedence

Three layers, and they are not equals:

1. **The global guidelines** (`~/.claude/CLAUDE.md` and the files it
   imports) -- the source, and they win.
2. **The project's `project.md`** -- project-specific design and
   conventions.
3. **The project's `code-style.md`** -- this file, copied, and the
   project's own part after it.

A project file that disagrees with the source is **drift, not an
override**: fix it. A project that genuinely needs to diverge needs a
technical reason, and that is not a decision to make while working on
something else -- signal it to the list in `claude-guidelines`'
`project.md` and keep following the source meanwhile.

**When a conflict between layers actually comes up, stop and ask.** Do not
silently pick a winner, even the global one.

This precedence rule lives here and in the global guidelines only. It does
not belong in a `project.md`.

## Keeping the copies in sync

Each private project keeps `code-style.md` at its root, in two parts: this
source, verbatim, under a header naming it; then, after a line marking the
end of the copy, **the project's own part**, holding only what is genuinely
its own -- its exempt paths, formatter verdicts, language-specific notes
and tooling commands, under its own headings. `claude-guidelines` is the
exception: it holds the source at `guidelines/code-style.md`, and its root
file points here.

**`tool/sync.py` writes the copied part and never touches the project's
part**, the same way it spreads the gate. So a copy cannot drift, and a
project that wants a rule to read differently changes the source, through
the signal list, rather than its copy. Until 2026-11-20 copies were adapted
by hand and audited only for their headings, and most had drifted in prose
no audit could see.

**Keep a census out of this document.** A count written here is copied
into every tree and goes stale in all of them at once; state the rule and
the criterion that decides it, and keep the count where it can be
recounted once.

**This source is plain ASCII**, so that it is valid in a tree whose own
rules restrict the characters its files may contain. Keep it so.

**Re-read this source before writing a project's own part, or anything
about the rules**, rather than working from what was loaded at the start of
the session: text written against a stale source is drift being written.
**A copied part that differs from the source** -- a tree the spread has not
reached, a hand edit -- is put right with `sync.py --commit <tree>`, never
by editing the copy; `sync.py --check` lists any that differ. A project's
`project.md` may state the three rules in brief and point here; it does not
restate the precedence rule.

<!-- End of the copied part. What follows is this project's own. -->

## Additions specific to situ

Code style for this project. Applies to the compiler (`situc/`), the
runtime (`runtime/`), the tooling (`tool/`), the standard library
(`std/`), the tests and the build files.

**Exempt paths: none.** This project generates source rather than
vendoring it, and what it generates is the product -- see *Generated
output* below, which is where this project departs from the source and
why.

`project.md` section 25 states the three rules in brief and points here
for the detail. The decisions behind several of them are recorded in
`doc/decision/`, which is append-only: where a rule here has a decision
file, that file is the reasoning and this is the summary.

### Generated output follows the same rule, and it is checked

**This is the one place this project departs from the source, and it
needs to be read as a departure.** The source exempts "vendored,
generated and attic sources", on the reasoning that a file some other
tool produced keeps that tool's style. Here the generator is the product:
`situc` emits C, C++, Rust, Python and Lua that people read, review and
ship, and code this project emits in a style this project rejects would
be a strange thing to hand anybody.

So generated output is **not** exempt, and that is checked rather than
assumed. `make lint` cannot see it -- it lands in `build/`, which the
lint skips, and before that in Python string literals, which are excluded
so section 17's golden diagnostics keep their space gutter. So
`style_gate.check_text` takes text rather than a path, and
`test/unit/test_generated_sources_follow_the_conventions.py` runs it over
what every schema generates in all four languages, plus the checks, the
fuzz harness and the dissector (project.md 26.58).

Nothing vendored or attic exists in this tree, so those two thirds of the
source's exemption have nothing to apply to.

### Formatter verdicts

Per the source's *Formatters* section, the verdict and the finding behind
it:

- **`black`, `ruff format`** -- rejected. Both rewrite tabs to spaces
  unconditionally and neither can be configured out of it, so adopting
  either would silently revert decision 0003 on every save. **Do not run
  them, not even ad hoc on a single file.**
- **`pycodestyle`** -- usable only with W191 (`indentation contains
  tabs`) disabled.
- **Written instead:** `tool/style_gate.py`, run by `make style`. A
  checker that gates indentation is worth more here than a formatter that
  reflows everything, which is the source's own third option.

What it checks: ASCII-only content, tab indentation, no space before a
tab, no trailing whitespace, a final newline. Two exemptions are built
in, both because the leading whitespace on those lines is not indentation
-- C block-comment continuations, where a leading space aligns the `*`,
and lines inside multi-line Python string literals, found with `tokenize`
rather than guessed at.

### Naming, in the parts that are this project's own

- Every module has a docstring stating its **single responsibility**. If
  it needs two sentences joined by "and", split the module.
- **Identifier casing in a schema is the author's**, deliberately:
  `snake_case` and `PascalCase` are both first-class, may be mixed, and
  nothing in the compiler reads casing. What the compiler checks is that
  two constructs never generate the same C identifier -- a property of
  flattening a path, not of spelling
  (`doc/decision/0013-identifier-conventions.md`). `example/telemetry/`
  is `snake_case` throughout as the working proof.
- The C++ backend adds one rule of its own: a member may not be called
  what its class is called, which `struct option { u8 option; }` reaches
  without trying. The backend renames the class and aliases the schema's
  name to it rather than refusing the schema
  (`doc/decision/0025-cpp-class-and-member-names.md`).

### Which files the indent rule reaches

`.py`, `.c`, `.h`, `.cpp`, `.hpp`, `.rs`, `.situ`, `.ebnf`, `.lua`, plus
`Makefile` and `bin/situc`. `doc/decision/0003-source-formatting.md` has
the reasoning, including the verification that Python accepts
tabs-then-spaces at every tab width.

### Switch labels sit at the switch's own indentation

A `case` or `default:` label sits at the same tab count as its `switch`,
and the body one deeper. The criterion is the comparison rather than a
census: for every label, the label's leading tab count equals that of the
`switch` enclosing it.

```c
switch (kind) {
case CHECK_MUST_EQ:
	broken = ((int64_t)value != want);
	break;
}
```

The hand-written C is under `runtime/c/` and `walker/c/`; what the
compiler builds and what `situc` emits follow the generator, and the
editor and the tooling are Python. So the recount is

	git ls-files '*.c' '*.h' '*.cpp' '*.hpp' | grep -vE '^(build/|test/generated/)'

and then each label's leading tabs against its own `switch`. Re-measured
2026-10-19: every label in the tree obeys it. The count that used to
stand here is gone deliberately -- it was stated in two places, which
disagreed with each other and with the tree, and what a reader needs is
the rule and the way to recount it rather than a number that rots twice.

Written down because the tree is uniform and nothing was holding it so.
`tool/style_gate.py` models the *other* convention -- its comment says
"`level` is brace-nesting depth plus one for a switch's case/default
body, since C labels open no brace but their bodies sit a level deeper"
-- so it expects labels one level in from the `switch` and bodies two. It
does not complain about either: a line shallower than the model is passed
through unchanged rather than reported, which is what lets the gate
tolerate a label style it does not model.

That tolerance is the reason this needs saying. A contributor who follows
the gate's model rather than the surrounding code writes the other style,
the gate accepts it, and the tree has two. Neither is wrong in C; having
both is.

This records what is there rather than choosing it. If the other style
is wanted, that is a convention change and belongs to a deliberate pass,
and the gate's own model is what such a pass would have to weigh.

### ASCII, and the two things this project adds to it

The rule itself is in the copied part above and is not restated here.
This file carried two paraphrases of it for a while, which is how a copy
stops matching its source without anyone editing the source: what a
project adds belongs under this heading, and what the source says belongs
in the source's words.

`ascii_only = true` in `.style-gate.toml`. Two additions:

- **Markdown is not exempt here.** The source's documentation exception
  takes Markdown out of the *global* rule's scope; it does not stop a
  project holding its own documents to ASCII, and this one does --
  `ascii_exclude_markdown = false`. Earned: the first draft of this file
  carried thirteen em dashes and four section signs, and failed
  `make lint` on arrival. Markdown stays exempt from the *indent* rule,
  which is the source's exception and is untouched.
- **Commit messages too**, which the source says nothing about because no
  gate outside a repository can check one. `tool/hooks/commit-msg` does.

**The rule governs the repository, not the wire**, and in this project
the distinction is load-bearing rather than pedantic: a schema describes
arbitrary octets and the compiler must not assume otherwise. A `.situ`
file naming a UTF-8 encoding, a test fixture holding a byte nobody can
print, a parser's own tables -- those are data. The two never conflict,
because one is about what this repository writes and the other is about
what the software reads.

**One consequence to know, and it is the source's rather than this
project's: docstrings.** A whole-file byte check covers them; the
tokenizer does not, because a docstring *is* a string literal and nothing
distinguishes it from one a program prints. This file used to name
docstrings among what it holds to ASCII, and that is why it no longer
can. Every docstring in this tree is ASCII and none of them needs not to
be, so nothing is broken -- but the gate would not notice if one changed,
and a rule that quietly stops being enforced is worth a sentence rather
than a silence. Whether the source should draw that line differently is
the source's question, not situ's.

### The commit-msg hook

The commit-msg hook is `tool/hooks/commit-msg`, installed with
`make hooks`. It rejects generator attribution, a subject over 75
columns, and body prose over 75 columns. It lives in the tree rather than
only in `.git/hooks` so that it is reviewable and survives a clone; the
copy that runs is installed from it.

The body limit was stated long before anything checked it, and only the
subject was checked -- so a body line at 76 columns went through while a
subject at 76 was refused. No message in this project's history is
affected: its 351 commits were checked against the rule and none has a
body line past 75.

Three things it deliberately does not reject. Three *names* are spared,
so a message may say where the shared tooling comes from: the directory
`.claude`, the file `CLAUDE.md`, and `claude-guidelines`, the repository
the guidelines live in. The ban is on crediting a generator and none of
the three is a spelling of that. Only the names are neutralised, never
the token around them -- a vendor word at the end of a path under the
tree is still refused, which was measured rather than assumed. And it
ignores what git is about to discard: comment lines, and the diff that
`git commit -v` puts below the scissors line. Reading those refused
commits over text that never reaches the message -- the hook's own diff
contains its own pattern list, so it rejected every commit that edited
it.

The third name was added because this file's own reconciliation commit
could not name the repository its source is in, and had to write "the
repository holding it" instead. A rule that makes the log vaguer exactly
where it is trying to be exact is the rule misfiring. This project had
that change first, and carried it locally while the source went without
it; it is in the source now, so the copy here is no longer ahead of it.

And the length check exempts three shapes, each because wrapping it is
the actual mistake rather than a concession: a *trailer*, since git
parses the block a line at a time and a broken `Link:` stops being a
trailer at all; a line holding a *url*, which no longer works once it is
split; and an *indented* line, which is how a message quotes a compiler
error or a stack trace, where reflowing what you are quoting corrupts the
one thing it was included for. It cannot tell prose opening `Note:` from
a trailer, so it forgives that -- the wrong way round would refuse a real
trailer, and that is the expensive error.

### See also

- **`project.md` section 25** -- the same rules in brief.
- **`doc/decision/0003-source-formatting.md`** -- why tabs in Python, and
  why no autoformatter.
- **`doc/decision/0013-identifier-conventions.md`**,
  **`0025-cpp-class-and-member-names.md`** -- schema identifier rules.
