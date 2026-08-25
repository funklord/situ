# 0003: Tabs for indent in every language, including Python; no autoformatter

Status: accepted
Date: 2026-07-26
Phase: 0

## Context

project.md section 25 requires tabs for structural indent and spaces for
alignment, with no prescribed tab width, in all source and in generated output.
PEP 8 prefers spaces for Python, so the two are in tension for `situc` itself.

Python the language has no objection to tabs. The one hard rule is that Python 3
rejects *ambiguous* indentation: it compares each line's indent under more than
one tab width and raises `TabError` if the ordering disagrees. Tabs first, then
spaces for alignment, is unambiguous under every tab width, and continuation
lines inside brackets are not indentation-significant at all. This was verified
on the interpreter before adopting the rule.

## Decision

Tabs for structural indent in `.py`, `.c`, `.h`, `.situ`, `.ebnf` and Makefiles.
Spaces only after the tabs, for alignment within a level. Never a space before a
tab in leading whitespace.

Markdown is exempt: its list continuation and code fences are space-indented by
specification.

No autoformatter runs on this repository.

## Consequences

`black` and `ruff format` cannot be used at all. Both rewrite tabs to four
spaces unconditionally and neither has an option to preserve them, so adopting
either would silently revert this decision on every save. `pycodestyle` would
also have to have W191 disabled.

Enforcement is therefore `tool/style_gate.py`, run by `make style`, with
`make lint` kept as an alias. It checks ASCII-only content, tab indentation,
no space-before-tab, no trailing whitespace, and a final newline.

The enforcing tool was `tool/lint_conventions.py` when this was decided. It
was replaced in 26.68 by the shared style gate, copied verbatim from one
source into every project rather than maintained here. That is a change of
mechanism, not of decision: the rule, the two exemptions below and the ban on
autoformatters are unaffected, and the gate was checked against all of them
before the swap.

Two exemptions are built into the gate, both because the leading whitespace on
those lines is not indentation:

- C block-comment continuations, where a leading space aligns the `*`.
- Lines inside multi-line Python string literals. The golden diagnostic texts of
  section 17 have a space gutter that is content, and reformatting it would
  change what the tests assert. The linter uses `tokenize` to find them rather
  than guessing.

## Alternatives considered

**Spaces in Python, tabs elsewhere.** Would allow `black`. Rejected: the
compiler emits C that must follow the tab rule, so the codegen templates are
tab-indented regardless. Having the generator and the generated code disagree on
indentation is a needless seam, and the project owner's preference is tabs.

**Tabs with no enforcement.** Rejected: a convention nothing checks is a
convention that decays, and this one is invisible in most diffs.
