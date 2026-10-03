"""A walked element accessor documents its cost; an indexed one does not.

`raidcfgd` asked for it (decision 0060): *say in the generated header which
access pattern costs what. The map already says `access=Sequential`; it does
not say "exponential in k".*

The exponential half was 26.555 and is fixed. What remains is that
`access=Sequential` states a cost CLASS -- element N cannot be reached
directly -- and reads like a constant factor. Measured in C at `-O2` on a run
of four-byte elements, calling `_at(i)` for every `i` against a cursor the
consumer holds:

    n       _at(i) for every i      a cursor      ratio
    16            0.0022 ms        0.0001 ms        18
    1024          6.5589 ms        0.0056 ms      1177

6.56 ms to read a four-kilobyte message. The map cannot say that, because the
quadratic belongs to the loop the consumer writes rather than to the format --
so the number goes in the header beside the accessor that has the property,
which is where a reader of `_at` is standing.

**This is a measurement rather than a presence check**, which is the whole
reason the indexed case is here: a fixed-size element is reached by
arithmetic, the sentence would be noise, and a test that only looked for the
note would pass just as loudly if every accessor carried it.
"""

from __future__ import annotations

from typing import Protocol

import pytest

from situc import ast
from situc.codegen.c import generate as generate_c
from situc.codegen.cpp import generate as generate_cpp
from situc.codegen.python import generate as generate_python
from situc.codegen.rust import generate as generate_rust
from situc.layout import solve
from situc.parser import parse_text
from situc.resolve import ResolvedSchema, resolve


class Emitted(Protocol):
	def files(self) -> dict[str, str]: ...


class Backend(Protocol):
	def __call__(self, schema: ast.Schema, resolved: ResolvedSchema,
	             basename: str) -> Emitted: ...


BACKENDS: dict[str, Backend] = {
	"c":      generate_c,
	"cpp":    generate_cpp,
	"python": generate_python,
	"rust":   generate_rust,
}

#: A counted run whose elements have no single size: `_at(index)` walks, so
#: reaching element N costs N element measurements.
WALKED = ("target buffer;\nendian big;\n"
          "struct item { u16 len; u8 v[len]; }\n"
          "struct bag { u16 count; item items[count]; }\n")

#: The same run over a fixed-size element, which is reached by arithmetic.
#: This is what makes the assertion below a measurement.
INDEXED = ("target buffer;\nendian big;\n"
           "struct pair { u8 a; u8 b; }\n"
           "struct bag { u16 count; pair items[count]; }\n")

#: The consequence, spelled the same way in all four languages so one mark
#: reads them all. The per-call cost is `O(index)` and is also present; this
#: is the sentence a reader needs and would not have derived.
MARK = "O(n^2)"


def _emitted(backend: str, text: str) -> str:
	schema   = parse_text(text, path="cost.situ")
	resolved = resolve(schema, solve(schema))
	built    = BACKENDS[backend](schema, resolved, "cost")
	return "".join(one for _, one in sorted(built.files().items()))


@pytest.mark.parametrize("backend", sorted(BACKENDS))
def test_a_walked_element_accessor_says_what_a_loop_costs(
		backend: str) -> None:
	"""And names the cursor to write instead, which is the actionable half."""
	source = _emitted(backend, WALKED)
	assert MARK in source, (
		f"the {backend} backend walks an element accessor without saying a "
		"loop over it is quadratic")
	assert "O(index)" in source, (
		f"the {backend} backend does not say what one call costs")
	assert "cursor" in source, (
		f"the {backend} backend states the cost and not the remedy, which "
		"leaves a reader knowing they have a problem and not what to do")


@pytest.mark.parametrize("backend", sorted(BACKENDS))
def test_an_indexed_element_accessor_says_nothing_of_the_kind(
		backend: str) -> None:
	"""A fixed-size element is arithmetic, and the note would be noise.

	Without this the test above would pass for a backend that put the
	sentence on every accessor, which is the same vacuous pass as a gate
	over an empty file list.
	"""
	source = _emitted(backend, INDEXED)
	assert MARK not in source, (
		f"the {backend} backend warns about a quadratic loop over elements "
		"it reaches by arithmetic")
