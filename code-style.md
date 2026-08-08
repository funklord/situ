<!-- Copied from ~/.claude/guidelines/code-style.md -- the source. Keep in
     sync; fix drift the moment you notice it. -->

# code-style.md

Code style for this project. Applies to the compiler (`situc/`), the runtime
(`runtime/`), the tooling (`tools/`), the standard library (`std/`), the tests
and the build files.

**Exempt paths: none.** This project generates source rather than vendoring
it, and what it generates is the product -- see *Generated output* below,
which is where this copy departs from the source and why.

`project.md` section 25 states the three rules in brief and points here for
the detail. The decisions behind several of them are recorded in
`docs/decisions/`, which is append-only: where a rule here has a decision
file, that file is the reasoning and this is the summary.

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
not a violation, it is the API's name. But names *you* introduce stay
`snake_case`. Do not let the surrounding convention pull your own names
across.

- Prefer the plain descriptive name over the redundant one. Name the thing,
  not its category: `plan`, not `plan_struct` or `plan_result`.
- **No abbreviations that are not already vocabulary.** `observed`, not
  `obs`; `interface`, not `iff`. This matters most wherever an internal
  name escapes into something you cannot rename later -- a wire format, a
  config key, a CLI output, an on-disk path.
- **One word per concept, everywhere.** The same word in the type name, the
  file path, the subcommand and the documentation. A synonym introduced for
  variety reads as a second concept.

### Prefixes, and visibility

Prefixes exist to keep this project's symbols from colliding with a
library's. So they follow **visibility**, and the choice is a matter of
judgement rather than a mechanical rule:

- **Anything with more than small visibility carries the project prefix** --
  the public API, and anything a linker or importer outside its own module
  can reach.
- **Module-private symbols are left unprefixed**, precisely so that the
  absence of a prefix reads as "this does not leave the module."

The middle case decides itself on link safety, not on taste. A symbol that
is internal by intent but still reaches the linker -- cross-file within a
library, not `static`, not part of the API -- is *not* private for this
purpose. Prefix it. A deliberate parallel copy of a function in two
libraries needs a **distinct** name, not the same name in both on the
assumption that nothing will ever link both sides; that assumption fails
later, at a call site that changed nothing, and names files you did not
touch.

Where a language enforces its own scheme, accept it rather than fight it,
and say in the project's copy that the toolchain is doing it:

- **Rust** -- `non_snake_case` and `non_camel_case_types` are on by default,
  so types are `PascalCase` and constants `SCREAMING_SNAKE_CASE`. That is
  the toolchain's, not a choice. Package systems that demand kebab-case
  (Cargo crate names, Debian package names) likewise read back with their
  own spelling; do not invent a third by naming the directory differently
  from the package.
- **Python** -- a leading underscore (`_name`) is the language's private
  marker and stands in for "unprefixed" above.

## 2. Indentation and alignment

Indent structural nesting with **tab** characters, one tab per level. When
lining up tokens *within* a line -- continuation parameters under an open
paren, a block comment's `*` column, an aligned trailing comment -- use
**spaces**, after the indent tabs.

The point of the split: alignment is expressed relative to the shared
leading tabs, so it survives at any tab width. No tab width is prescribed
anywhere; the viewer decides.

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
spaces come after; the reverse, or an alternation, is what breaks at a
different tab width -- and in Python it is a syntax error.

### Settled exceptions

Divergence needs a technical reason. These reasons are already accepted and
need no discussion:

- **Makefile recipe lines** -- `make` requires a literal tab. Compliant by
  construction.
- **YAML** -- the spec forbids tabs for indentation outright. Use spaces.
- **Markdown** -- list continuation and code fences are space-indented by
  specification. Exempt.
- **Debian packaging files** -- exempt, and the two halves are exempt for
  different reasons. `debian/changelog` has a fixed layout that a tab is
  not part of: `dpkg-parsechangelog` calls a tab-indented change line
  "unrecognized" and loses the trailer outright if a tab precedes `--`. A
  deb822 continuation in `control` or `copyright` is the opposite case --
  `deb822(5)` allows a leading SPACE *or* TAB and dpkg round-trips either,
  but that leading whitespace is field syntax rather than indentation, so
  the rule has nothing to say about it and everything past it is
  alignment. Both measured against dpkg rather than read off the manual.
- **Go** -- `gofmt` emits tabs natively. Compliant already.
- **Vendored, generated and attic sources** -- exempt, per the header.

Python deserves a note, because PEP 8 prefers spaces and the tension looks
worse than it is: the language's only hard rule is that indentation must not
be *ambiguous*, and tabs-then-spaces is unambiguous at every tab width.
Continuation lines inside brackets are not indentation-significant at all.
Never a space *before* a tab in leading whitespace -- that is the case that
raises `TabError`.

Anything else that seems to need spaces: raise it, get it settled, and add
it here.

## 3. Filenames

Lowercase and `snake_case` for everything the project names itself --
sources, headers, documentation. So `main_window.cpp`, not
`MainWindow.cpp`.

Settled exceptions:

- **Names a tool will not accept lowercased** -- `Makefile`,
  `CMakeLists.txt`, `AndroidManifest.xml`, `Dockerfile`, `Cargo.toml`.
- **Root files with an established convention** -- `README.md`, `LICENSE`,
  `CHANGELOG.md`, `AUTHORS`.
- **Package-system spellings** -- kebab-case where Cargo or Debian require
  it.

## ASCII in source

Source and comments are ASCII. Write `--` where prose would use an em dash,
and "section" for a section sign.

This governs **the text the repository writes about itself**, not the data
the software handles. Three exceptions, and they are the rule's shape
rather than holes in it:

- **Documentation.** Markdown may use typographic punctuation.
- **User-facing text in UI software.** A tick a program prints is output,
  not prose -- `GREEN('gpg ')` is correct as it stands.
- **Anything that genuinely requires Unicode**: a fixture for a UTF-8
  parser, a terminal emulator's character tables, a font tool.

Where a project needs the rule enforced, `ascii_only` in `.style-gate.toml`
turns it on. In Python it enforces exactly the shape above -- ASCII outside
string literals, Unicode allowed inside them -- because the gate reads the
file with `tokenize`. Other languages get a whole-file byte check, having
no tokenizer here, and so does a Python file that will not tokenise: a file
nobody can parse is not a file that has been cleared.

It was the whole file for everyone until a project that prints two status
ticks had to switch the check off to keep them, which switched it off for
its comments as well, and an em dash arrived in one. **An exception wider
than its reason is how a rule stops being enforced.**

## Formatters

A formatter is allowed **only if it can be configured to honour the three
rules completely**. Configuration gaps are disqualifying, not something to
work around: a formatter that gets indentation right and alignment wrong
will rewrite the tree on somebody's next save.

So the decision is per tool, per project, and it is a real evaluation:

- If it can be made to comply, use it, and commit the config with a comment
  saying which setting is load-bearing and what happens without it.
- If it cannot, do not run it -- **not even ad hoc on a single file**. The
  failure mode is a silent conversion of files that were already correct,
  discovered later as a reverted commit rather than an error.
- If no existing tool fits and the rule is worth mechanising, write our
  own. A checker that only gates indentation is worth more than a formatter
  that reflows everything.

**Record the decision and the finding that produced it** in the project's
copy of this file -- which tool, what specifically failed, what would change
the answer. A verdict without its evidence gets re-litigated, and a tool
that improves later never gets reconsidered because nobody remembers what
was actually wrong with it.

Naming and filename rules are review items, not automated ones.

## Precedence

Three layers, and they are not equals:

1. **The global guidelines** (`~/.claude/CLAUDE.md` and the files it
   imports) -- the source, and they win.
2. **The project's `project.md`** -- project-specific design and conventions.
3. **The project's `code-style.md`** -- this file, copied.

A project copy that disagrees with the source is **drift, not an
override**: fix it. A project that genuinely needs to diverge needs a
technical reason, and that is a decision to raise with the user -- not one
to make while working on something else.

**When a conflict between layers actually comes up, stop and ask.** Do not
silently pick a winner, even the global one.

This precedence rule lives here and in the global guidelines only. It does
not belong in a `project.md`.

## Keeping the copies in sync

Each private project keeps a copy of this file at its repo root -- except
the one this file lives in. `claude-guidelines` holds the source at
`guidelines/code-style.md`, and a copy beside it would be the same document
twice in one repository with nothing to keep the two honest; its root
`code-style.md` says so and points here. Every other private project carries
a copy, opening with a header that names the source:

```markdown
<!-- Copied from ~/.claude/guidelines/code-style.md -- the source. Keep in
     sync; fix drift the moment you notice it. -->
```

Below the copied rules, a project adds only what is genuinely its own: its
exempt paths, its formatter verdicts, its language-specific notes, its
tooling commands.

**This source is deliberately plain ASCII** -- no em dashes, no section
signs, no arrows -- so that a copy can be byte-verbatim in every project,
including one whose own rules restrict the characters its files may
contain. Keep it that way when editing: a typographic character introduced
here becomes a transliteration problem in every repository that carries a
copy.

Where a copy must still be adapted, **"do not diverge" means semantically
identical, not byte-identical**: a project transliterating to satisfy its
own character-set rule, or renumbering a heading to fit its own structure,
is that project's rule working correctly, **not drift, and not something to
reconcile back**. What must match is every rule and every exception, in
substance.

**If you notice a copy diverging from the source, reconcile it as soon as
you notice** -- do not leave it for later and do not work around it. If the
divergence looks deliberate rather than stale, that is the conflict case
above: ask.

Noticing requires looking. **Re-read this source before writing or
reconciling any project's copy**, rather than working from what was loaded
at the start of the session -- it may have changed since, and a copy
reconciled against a stale source is drift being written rather than
fixed.

The project's `project.md` may state the three rules in brief and point
here for the detail. It does not restate the precedence rule.
---

# Additions specific to situ

Everything above is the source, copied. Everything below is this project's
own: its exempt paths, its formatter verdicts, its tooling commands.

## Generated output follows the same rule, and it is checked

**This is the one place this copy departs from the source, and it needs to be
read as a departure.** The source exempts "vendored, generated and attic
sources", on the reasoning that a file some other tool produced keeps that
tool's style. Here the generator is the product: `situc` emits C, C++, Rust,
Python and Lua that people read, review and ship, and code this project emits
in a style this project rejects would be a strange thing to hand anybody.

So generated output is **not** exempt, and that is checked rather than
assumed. `make lint` cannot see it -- it lands in `build/`, which the lint
skips, and before that in Python string literals, which are excluded so
section 17's golden diagnostics keep their space gutter. So
`style_gate.check_text` takes text rather than a path, and
`tests/unit/test_generated_sources_follow_the_conventions.py` runs it over
what every schema generates in all four languages, plus the checks, the fuzz
harness and the dissector (project.md 26.58).

Nothing vendored or attic exists in this tree, so those two thirds of the
source's exemption have nothing to apply to.

## Formatter verdicts

Per the source's *Formatters* section, the verdict and the finding behind it:

- **`black`, `ruff format`** -- rejected. Both rewrite tabs to spaces
  unconditionally and neither can be configured out of it, so adopting either
  would silently revert decision 0003 on every save. **Do not run them, not
  even ad hoc on a single file.**
- **`pycodestyle`** -- usable only with W191 (`indentation contains tabs`)
  disabled.
- **Written instead:** `tools/style_gate.py`, run by `make style`. A
  checker that gates indentation is worth more here than a formatter that
  reflows everything, which is the source's own third option.

What it checks: ASCII-only content, tab indentation, no space before a tab,
no trailing whitespace, a final newline. Two exemptions are built in, both
because the leading whitespace on those lines is not indentation -- C
block-comment continuations, where a leading space aligns the `*`, and lines
inside multi-line Python string literals, found with `tokenize` rather than
guessed at.

## Naming, in the parts that are this project's own

- Every module has a docstring stating its **single responsibility**. If it
  needs two sentences joined by "and", split the module.
- **Identifier casing in a schema is the author's**, deliberately:
  `snake_case` and `PascalCase` are both first-class, may be mixed, and
  nothing in the compiler reads casing. What the compiler checks is that two
  constructs never generate the same C identifier -- a property of flattening
  a path, not of spelling
  (`docs/decisions/0013-identifier-conventions.md`). `examples/telemetry/` is
  `snake_case` throughout as the working proof.
- The C++ backend adds one rule of its own: a member may not be called what
  its class is called, which `struct option { u8 option; }` reaches without
  trying. The backend renames the class and aliases the schema's name to it
  rather than refusing the schema
  (`docs/decisions/0025-cpp-class-and-member-names.md`).

## Which files the indent rule reaches

`.py`, `.c`, `.h`, `.cpp`, `.hpp`, `.rs`, `.situ`, `.ebnf`, `.lua`, plus
`Makefile` and `bin/situc`. `docs/decisions/0003-source-formatting.md` has
the reasoning, including the verification that Python accepts tabs-then-spaces
at every tab width.

## ASCII, and the two things this project adds to it

The rule itself is above, in the copied portion, and is not restated here.
This file carried two paraphrases of it for a while, which is how a copy
stops matching its source without anyone editing the source: what a project
adds belongs under this heading, and what the source says belongs in the
source's words.

`ascii_only = true` in `.style-gate.toml`. Two additions:

- **Markdown is not exempt here.** The source's documentation exception
  takes Markdown out of the *global* rule's scope; it does not stop a
  project holding its own documents to ASCII, and this one does --
  `ascii_exclude_markdown = false`. Earned: the first draft of this file
  carried thirteen em dashes and four section signs, and failed `make lint`
  on arrival. Markdown stays exempt from the *indent* rule, which is the
  source's exception and is untouched.
- **Commit messages too**, which the source says nothing about because no
  gate outside a repository can check one. `tools/hooks/commit-msg` does.

**The rule governs the repository, not the wire**, and in this project the
distinction is load-bearing rather than pedantic: a schema describes
arbitrary octets and the compiler must not assume otherwise. A `.situ` file
naming a UTF-8 encoding, a test fixture holding a byte nobody can print, a
parser's own tables -- those are data. The two never conflict, because one
is about what this repository writes and the other is about what the
software reads.

**One consequence to know, and it is the source's rather than this
project's: docstrings.** A whole-file byte check covers them; the
tokenizer does not, because a docstring *is* a string literal and nothing
distinguishes it from one a program prints. This file used to name
docstrings among what it holds to ASCII, and that is why it no longer can.
Every docstring in this tree is ASCII and none of them needs not to be, so
nothing is broken -- but the gate would not notice if one changed, and a
rule that quietly stops being enforced is worth a sentence rather than a
silence. Whether the source should draw that line differently is the
source's question, not situ's.

## The commit-msg hook

The commit-msg hook is `tools/hooks/commit-msg`, installed with `make hooks`.
It rejects generator attribution, a subject over 75 columns, and body prose
over 75 columns. It lives in the tree rather than only in `.git/hooks` so
that it is reviewable and survives a clone; the copy that runs is installed
from it.

The body limit was stated long before anything checked it, and only the
subject was checked -- so a body line at 76 columns went through while a
subject at 76 was refused. No message in this project's history is affected:
its 351 commits were checked against the rule and none has a body line past
75.

Three things it deliberately does not reject. Three *names* are spared, so a
message may say where the shared tooling comes from: the directory
`.claude`, the file `CLAUDE.md`, and `claude-guidelines`, the repository
the guidelines live in. The ban is on crediting a generator and none of the
three is a spelling of that. Only the names are neutralised, never the
token around them -- a vendor word at the end of a path under the tree is
still refused, which was measured rather than assumed. And it ignores what
git is about to discard: comment lines, and the diff that `git commit -v`
puts below the scissors line. Reading those refused commits over text that
never reaches the message -- the hook's own diff contains its own pattern
list, so it rejected every commit that edited it.

The third name was added because this file's own reconciliation commit
could not name the repository its source is in, and had to write "the
repository holding it" instead. A rule that makes the log vaguer exactly
where it is trying to be exact is the rule misfiring. This project had that
change first, and carried it locally while the source went without it; it is
in the source now, so the copy here is no longer ahead of it.

And the length check exempts three shapes, each because wrapping it is the
actual mistake rather than a concession: a *trailer*, since git parses the
block a line at a time and a broken `Link:` stops being a trailer at all; a
line holding a *url*, which no longer works once it is split; and an
*indented* line, which is how a message quotes a compiler error or a stack
trace, where reflowing what you are quoting corrupts the one thing it was
included for. It cannot tell prose opening `Note:` from a trailer, so it
forgives that -- the wrong way round would refuse a real trailer, and that
is the expensive error.

## See also

- **`~/.claude/guidelines/code-style.md`** -- the source this file copies.
- **`project.md` section 25** -- the same rules in brief.
- **`docs/decisions/0003-source-formatting.md`** -- why tabs in Python, and
  why no autoformatter.
- **`docs/decisions/0013-identifier-conventions.md`**,
  **`0025-cpp-class-and-member-names.md`** -- schema identifier rules.
