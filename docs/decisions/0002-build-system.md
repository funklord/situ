# 0002: GNU Make only; CMake deferred

Status: accepted
Date: 2026-07-26
Phase: 0

Supersedes the requirement in project.md section 24 that CMake and GNU Make be
maintained in parallel.

## Context

Section 24 called for both build systems, maintained side by side, as
independently usable entry points. Two build systems describing one project
have to be kept in sync by hand forever, and the second one is only worth that
cost when an external consumer needs it.

At phase 0 there is no external consumer. There are three build units: the C
runtime, the generated-code tests, and (later) whatever a downstream project
does with generated sources.

## Decision

GNU Make only, for now. No `CMakeLists.txt` is written.

The structural requirements of section 24 are kept in full, because they are
about isolation rather than about which tool implements it:

- Sub-projects are self-contained. `runtime/c` and `tests/generated` each build
  standalone with their own defaults; the parent overrides through the
  environment. There is no shared `.mk` include anywhere.
- Cross-compilation works out of the box via `CROSS_COMPILE`.
- `-Wall -Wextra -Werror -Wconversion -Wsign-conversion` everywhere.

CMake may be added later if a downstream consumer needs it. Nothing in this
decision makes that harder: the sub-projects are already isolated, which is the
part that would have been painful to retrofit.

## The built-in variable trap

Section 24 warns that `LD ?= ld` never fires because GNU Make defines `LD=ld`
as a built-in default. The warning is correct but incomplete: `CC` and `AR`
have built-in defaults too. The first cross build here silently used the host
`cc` for exactly that reason.

Every toolchain variable is therefore guarded with `$(origin ...)`:

```make
ifeq ($(origin CC),default)
CC := $(CROSS_COMPILE)gcc
endif
```

The top-level Makefile compiles nothing, so it propagates rather than uses the
toolchain, and it exports `CC`/`AR`/`LD` only when `origin` shows the user
actually chose them. Exporting them unconditionally would push the host tools
into the environment of every sub-project and defeat their `CROSS_COMPILE`
handling.

## Alternatives considered

**Both, as specified.** Rejected: double the surface with no consumer for the
second one. Reconsider when someone outside this repository needs CMake.

**CMake only.** Rejected: it does not fit how the sub-projects are meant to be
consumed, and the project owner's preference is Make.

**Meson, Ninja directly.** Rejected: another dependency, same objection as 0001.
