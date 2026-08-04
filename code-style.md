<!-- The three rules and their detail are copied from
     ~/.claude/guidelines/code-style.md -- the source. Keep in sync; fix
     drift the moment you notice it. -->

# code-style.md

Code style for this project. Applies to the compiler (`situc/`), the
runtime (`runtime/`), the tooling (`tools/`), the standard library
(`std/`), the tests and the build files -- **and to the code the compiler
emits**, in every backend language.

`project.md` section 25 states the rules in brief and points here for the detail;
where the two disagree, `project.md` wins. **Above both sits the global
source**, `~/.claude/guidelines/code-style.md`, which applies to every
private project. Where either disagrees with it, that is **drift to fix,
not a local override**. A genuine divergence needs a technical reason and
is raised rather than decided in passing -- and when a conflict between the
three actually comes up, stop and ask instead of picking a winner.

The decisions behind several of these rules are recorded in
`docs/decisions/`, which is append-only. Where a rule here has a decision
file, that file is the reasoning and this is the summary.

## The three rules

1. **`snake_case`, not `camelCase`,** for identifiers this project defines.
2. **Tabs for indentation, spaces for alignment.**
3. **Lowercase filenames,** unless a tool demands otherwise.

## 1. Naming

`snake_case` over `camelCase` in every language, for everything this
project names itself.

- **No abbreviations that are not already vocabulary**, and **one word per
  concept, everywhere** -- the same word in the type name, the file path,
  the CLI subcommand and the documentation. This matters more here than in
  most projects: names in the schema language and in diagnostics are a
  public interface long before anything freezes.
- Every module has a docstring stating its **single responsibility**. If it
  needs two sentences joined by "and", split the module.

**Identifier casing in a schema is the author's**, and that is deliberate:
`snake_case` and `PascalCase` are both first-class, may be mixed, and
nothing in the compiler reads casing. What the compiler checks is that two
constructs never generate the same C identifier -- a property of flattening
a path, not of spelling (`docs/decisions/0013-identifier-conventions.md`).
`examples/telemetry/` is `snake_case` throughout as the working proof. The
C++ backend adds one rule of its own: a member may not be called what its
class is called, which `struct option { u8 option; }` reaches without
trying, and the backend renames the class and aliases the schema's name to
it rather than refusing the schema
(`docs/decisions/0025-cpp-class-and-member-names.md`).

## 2. Indentation and alignment

Tabs carry structural indent level; spaces carry alignment within a level.
Continuation lines use one tab per level of indent, then spaces to the
alignment column. If two lines are short enough to merge rather than align,
merge them.

No prescriptive tab width anywhere, in the codebase or in generated output.
Elastic tabstops are the model: the viewer decides.

**Never a space before a tab in leading whitespace.**

This applies to `.py`, `.c`, `.h`, `.situ`, `.ebnf` and Makefiles --
`docs/decisions/0003-source-formatting.md` has the reasoning, including the
verification that Python accepts it. Python's only hard rule is that
indentation must not be *ambiguous* across tab widths; tabs-then-spaces is
unambiguous at every width, and continuation lines inside brackets are not
indentation-significant at all.

**Markdown is exempt** -- its list continuation and code fences are
space-indented by specification.

### Generated output follows the same rule, and it is checked

`make lint` cannot see emitted code: it lands in `build/`, which is
skipped, and before that in string literals, which are excluded so section 17's
golden texts keep their gutter. So `lint_conventions.check_text` takes text
rather than a path, and
`tests/unit/test_generated_sources_follow_the_conventions.py` runs it over
what every schema generates in all four languages, plus the checks, the
fuzz harness and the dissector (section 26.58).

### No autoformatter

`black` and `ruff format` rewrite tabs to spaces unconditionally and cannot
be configured out of it, so adopting either would silently revert
decision 0003 on every save. `pycodestyle` would need W191 disabled.

**Do not run any of them, not even ad hoc on a single file.**

Enforcement is `tools/lint_conventions.py` under `make lint`, which checks
ASCII-only content, tab indentation, no space-before-tab, no trailing
whitespace, and a final newline. Two exemptions are built in, both because
the leading whitespace on those lines is not indentation: C block-comment
continuations, where a leading space aligns the `*`; and lines inside
multi-line Python string literals, found with `tokenize` rather than
guessed at.

## 3. Filenames

Lowercase, unless there is a reason otherwise. Python modules are
`snake_case.py`, matching the import path. Schema files are `*.situ`.

The exception is a name a tool will not accept lowercased:
`CMakeLists.txt`, `Makefile`, `README.md`, `LICENSE`.

## 4. ASCII only in source

Source, comments, docstrings, test fixtures and commit messages are
**ASCII**. Write `--` where prose would use an em dash. Markdown documents
are the exception and may use typographic punctuation.

This is a rule about the text this project writes, not about the data it
handles -- a schema describes arbitrary octets and the compiler must not
assume otherwise. The two do not conflict: one governs the repository, the
other governs the wire.

## 5. Line length

Soft 100 columns. Do not sacrifice clarity to it -- a 104-column line that
reads as one thought beats a wrapped one that does not.

## See also

- **`~/.claude/guidelines/code-style.md`** -- the source this file copies.
- **`project.md` section 25** -- the same rules in brief.
- **`docs/decisions/0003-source-formatting.md`** -- why tabs in Python, and
  why no autoformatter.
- **`docs/decisions/0013-identifier-conventions.md`**,
  **`0025-cpp-class-and-member-names.md`** -- schema identifier rules.
