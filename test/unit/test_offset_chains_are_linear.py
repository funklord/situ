"""An offset chain sums each earlier member once, in all four backends.

`raidcfgd` reported one decode of a 12,344-byte snapshot costing **3,000,000
calls and 573 ms**, through generated accessors for a struct with ten
variable-length members (26.555). The shape: `_offset(k)` summed `_extent(i)`
for every earlier member, and each `_extent(i)` re-derived `_offset(i)` the
same way, so the cost went as 3^k.

Measured here before the fix, one access to the last member:

    members      C        Python
    8          0.009 ms    8.3 ms
    12         0.670 ms  625.9 ms
    14         6.204 ms  5374.4 ms      -- 5.4 SECONDS

Doubling k from 12 to 14 multiplied the cost by about nine, which is 3^2 and
the whole of the diagnosis.

**This test is structural rather than timed.** A wall-clock bound would pin
the same property and fail on a loaded machine, and what actually went wrong
is expressible exactly: the chain must reach each earlier member through the
`_extent_from` form, which measures at an offset the caller already holds,
and never through the plain `_extent`, which derives its own.
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

#: Six variable-length members, which is enough that a 3^k chain is visible
#: and few enough that the generated source stays readable when this fails.
MEMBERS = 6

SCHEMA = (
	"target buffer;\nendian big;\n"
	"struct str { u16 len; u8 v[len]; }\n"
	"struct rec {\n"
	+ "".join(f"\tstr m{i};\n" for i in range(1, MEMBERS + 1))
	+ "}\n"
)

class Emitted(Protocol):
	"""What the four backends have in common for this test's purpose.

	Each returns its own `Generated`, so a dict over the four collapses to
	`object` and `.files()` is unreachable through it. The protocol names the
	one method this file needs rather than widening to `Any`.
	"""

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


def _emitted(backend: str) -> str:
	schema   = parse_text(SCHEMA, path="chain.situ")
	resolved = resolve(schema, solve(schema))
	built    = BACKENDS[backend](schema, resolved, "chain")
	return "".join(text for _, text in sorted(built.files().items()))


@pytest.mark.parametrize("backend", sorted(BACKENDS))
def test_each_earlier_member_is_measured_at_a_known_offset(
		backend: str) -> None:
	"""Every member but the first is reached through `_extent_from`.

	The first needs none: its offset is the struct's own base, so there is no
	earlier extent to thread. Every later one is summed by the chains of the
	members after it, and that is the call that must take the running offset.
	"""
	source = _emitted(backend)
	missing = [f"m{i}" for i in range(1, MEMBERS)
	           if f"m{i}_extent_from" not in source]
	assert not missing, (
		f"the {backend} backend sums these without threading the offset "
		f"it already has, so each re-derives its own: {missing}")


@pytest.mark.parametrize("backend", sorted(BACKENDS))
def test_the_chain_does_not_re_derive_what_it_has_already_summed(
		backend: str) -> None:
	"""And the plain `_extent` is absent from the accumulating lines.

	Emitting `_extent_from` is not enough on its own: the fix is only a fix
	where the chain USES it. Python reached 2^k with `_extent_from` present
	and unused, because `_offset_expression` built the sum as an expression
	and an expression cannot hold a running total -- so each term called the
	plain `_extent` and re-derived its own start. Rust had the same shape.

	So this reads the accumulating lines -- the ones that advance a running
	offset -- and requires every extent in them to be the `_from` form.
	"""
	# Each backend's own spelling of "advance the running offset", verified
	# to match real lines rather than assumed: the first version of this test
	# had `cpp` and `rust` wrong, so those two cells matched nothing and
	# passed while both backends were still re-deriving at 2^k. A mark that
	# matches no line is this file's own subject arriving in its instrument.
	marks = {"c":      "situ_advance_u32(offset,",
	         "cpp":    "at = situ_advance_u32(at,",
	         "python": "at = advance(at,",
	         "rust":   "at = situ_rt::advance(at,"}
	source = _emitted(backend)
	lines  = [line.strip() for line in source.splitlines()
	          if marks[backend] in line]
	# The instrument before the finding: a mark matching nothing makes the
	# assertion below vacuous, which is how two of these passed on a tree
	# that still re-derived.
	assert len(lines) >= MEMBERS - 1, (
		f"only {len(lines)} accumulating lines found in the {backend} "
		"backend; the mark has stopped matching")

	guilty = [line for line in lines
	          if "_extent" in line and "_extent_from" not in line]
	assert not guilty, (
		f"the {backend} backend re-derives an offset it has already "
		"accumulated:\n  " + "\n  ".join(guilty[:4]))
