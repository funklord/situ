"""`required` walks a counted run element by element, in all four backends.

`raidcfgd` reported it: over `item items[count]`, `required` asked the
accessors for the run's span, and that walk stops where an element would run
past the buffer and returns what it reached. A buffer holding two of five
declared elements therefore came back COMPLETE, with `need` equal to `have`
-- so their decoder walks to the declared count itself (26.557).

The gate was `is_run`, which names the two spellings that END where the bytes
decide: a `while` run and a delimited one. A count ends where the count says,
which reads as a different question and is the same one. Each element still
has to be whole, and one walk cannot tell the end of the run from the end of
the buffer whichever decides where it stops.

**The behaviour is pinned in C** by
`test_codegen_c.test_a_counted_run_refuses_a_buffer_short_of_its_count`,
which compiles and runs the generated accessors against ten bytes declaring
five elements. This file pins the same property in all four, structurally,
because the defect was per-backend: each has its own framing walk and its own
gate, and a single witness over one of them would pass while three were
wrong. That is this tree's most expensive recurring shape.
"""

from __future__ import annotations

from typing import Protocol

import pytest

from situc.codegen.c import generate as generate_c
from situc.codegen.cpp import generate as generate_cpp
from situc.codegen.python import generate as generate_python
from situc.codegen.rust import generate as generate_rust
from situc import ast
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

#: A counted run whose element has no single size: the walk is the only way
#: to know how far it reaches, and whether each element is whole.
VARIABLE = ("target buffer;\nendian big;\n"
            "struct item { u16 len; u8 v[len]; }\n"
            "struct rec { u16 count; item items[count]; }\n")

#: The same shape over a FIXED-size element, where `count * 2` is the exact
#: answer and no walk is wanted. This is what makes the mark below a
#: measurement rather than a presence check: a mark that matched everything
#: would pass here too, and a mark that matched nothing would fail above.
FIXED = ("target buffer;\nendian big;\n"
         "struct pair { u8 a; u8 b; }\n"
         "struct rec { u16 count; pair items[count]; }\n")

#: What every backend's framing walk writes above itself. One mark for the
#: four because the comment is shared text, which is also why it is safe to
#: key on: a backend dropping its walk drops the comment with it.
MARK = "framed one"


def _emitted(backend: str, schema_text: str) -> str:
	schema   = parse_text(schema_text, path="counted.situ")
	resolved = resolve(schema, solve(schema))
	built    = BACKENDS[backend](schema, resolved, "counted")
	return "".join(text for _, text in sorted(built.files().items()))


@pytest.mark.parametrize("backend", sorted(BACKENDS))
def test_a_counted_run_of_variable_elements_is_walked(backend: str) -> None:
	"""The walk is emitted, so each element is asked whether it is whole."""
	assert MARK in _emitted(backend, VARIABLE), (
		f"the {backend} backend frames a counted run of variable-size "
		"elements through the accessors' span walk, which stops at the end "
		"of the buffer as readily as at the end of the run")


@pytest.mark.parametrize("backend", sorted(BACKENDS))
def test_a_counted_run_of_fixed_elements_is_not(backend: str) -> None:
	"""And a fixed-size element keeps its exact arithmetic.

	`count * 2` is the whole answer there, and it was already right: measured
	before the fix, such a run reported a `need` beyond what had arrived and
	refused correctly. Walking it would be slower and no more honest -- and
	this assertion is what stops the test above passing for a backend that
	emits a walk for everything.
	"""
	assert MARK not in _emitted(backend, FIXED), (
		f"the {backend} backend walks a counted run whose element has a "
		"fixed size, where the count times the size is exact")
