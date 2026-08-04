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

Each private project keeps a copy of this file at its repo root, opening
with a header that names the source:

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
here becomes a transliteration problem in seven repositories.

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

## ASCII only

Source, comments, docstrings, test fixtures and commit messages are ASCII.
Write `--` where prose would use an em dash.

This is a rule about the text this project writes, not about the data it
handles: a schema describes arbitrary octets and the compiler must not assume
otherwise. The two do not conflict -- one governs the repository, the other
governs the wire.

**Markdown is not exempt from this**, although it is exempt from the indent
rule. The gate checks every `.md` it can see for ASCII, which is
how the first draft of this file -- carrying thirteen em dashes and four
section signs -- failed `make lint` on arrival.

## Line length

Soft 100 columns. Do not sacrifice clarity to it: a 104-column line that
reads as one thought beats a wrapped one that does not.

## See also

- **`~/.claude/guidelines/code-style.md`** -- the source this file copies.
- **`project.md` section 25** -- the same rules in brief.
- **`docs/decisions/0003-source-formatting.md`** -- why tabs in Python, and
  why no autoformatter.
- **`docs/decisions/0013-identifier-conventions.md`**,
  **`0025-cpp-class-and-member-names.md`** -- schema identifier rules.
