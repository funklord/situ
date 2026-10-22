"""A generated length is never `limit - at` (26.675).

bbq-predictor's C++ parser read five bytes past a nine-byte buffer inside
`validate()`, which is the function whose job is to decide whether the bytes
are acceptable. The mechanism was one subtraction:

    situ_view_t{ situ_base(raw_) + at, raw_.limit - at, ... }

With `at` past the limit -- which it reached because a clamped advance was
followed by a bare `at += 2` -- the subtraction wrapped to about four
billion, and every `situ_in_bounds` below it passed against that limit. The
hostile input was `01 c8 01 02 03 04 05 06 07`: nine bytes, one element,
`n = 200`.

**Why nothing caught it, which is what this file is for.** Three gaps, and
the first is the one that matters:

1. No corpus schema has the shape -- a fixed-width member between a dynamic
   member and a nested struct whose extent is read from the bytes. Across 43
   schemas the emitted pattern appeared once, in an offset accessor rather
   than an extent chain. Every dynamic check here is corpus-driven: the
   four-way differential, `make fuzz` (which does build C++ under libFuzzer
   and ASan), the vectors, the walkers. None could reach the path.
2. Nothing compared the backends' USE of the saturating helpers. The
   differential compares answers, which is a weaker question: it cannot see
   one backend bypassing a guard while the answers agree. C and Rust did in
   fact disagree here, 4 against 0, so the differential would have reported
   it -- given gap 1 closed.
3. `make fuzz` is not part of `make check`.

This file closes gap 2, and it closes it statically: no schema need have the
shape and nothing need be executed. `test/schema/edges.situ` closes gap 1,
which is what that file is for.

**What this test does not cover, stated because the first draft got it
wrong.** It is a property of the emitted TEXT. An earlier draft asserted "no
bare `at +=`" and C passed it with zero hits -- by spelling the same
operation `at = at + size`, three lines below a guard proving it safe. A
count inherits its detector. So the property here is the narrow one that is
genuinely spelling-independent in its remedy: the subtraction itself is
gone, replaced by a saturating helper, in every backend. The behavioural
half lives in the differential and in the sanitized probe in
`test_codegen_cpp.py`.
"""

from __future__ import annotations

import re

import pytest

from every_schema import SCHEMAS, ids, load_schema
from situc.codegen.c import generate as generate_c
from situc.codegen.cpp import generate as generate_cpp
from situc.codegen.python import generate as generate_py
from situc.codegen.rust import generate as generate_rs
from situc.layout import solve
from situc.resolve import resolve

#: `limit - x` and Rust's `self.bytes.len() - x`, which is the same
#: arithmetic in the backend whose slice panics on the result.
SUBTRACTION = re.compile(r"\blimit\s*-\s*(?!=)|\.len\(\)\s*-\s*(?!=)")

#: Rust's count, pinned rather than zero. Its sites are a different hazard:
#: a wrapped `usize` makes the slice panic, which 26.27 already records as a
#: denial of service rather than a mitigation -- bad, but not the read past
#: the end that C++ performed. Sweeping them wants `situ_rt::remaining`,
#: which does not exist yet, so the floor stops the count growing while that
#: is its own piece of work.
#:
#: Re-measure with this test rather than by hand: it prints the number.
RUST_PINNED = 131


def generated(path, backend: str) -> str:  # type: ignore[no-untyped-def]
	schema   = load_schema(path)
	resolved = resolve(schema, solve(schema))
	if backend == "c":
		return generate_c(schema, resolved, "u").header
	if backend == "cpp":
		return generate_cpp(schema, resolved, "u").header
	if backend == "rust":
		return generate_rs(schema, resolved, "u").module
	return generate_py(schema, resolved, "u").module


@pytest.mark.parametrize("path", SCHEMAS, ids=ids(SCHEMAS))
@pytest.mark.parametrize("backend", ("c", "cpp", "python"))
def test_no_generated_length_is_a_bare_subtraction(path, backend) -> None:  # type: ignore[no-untyped-def]
	"""The three backends that are clean, held there.

	C++ had 17 of these when the bug was reported and C had 28 -- 80 more
	passed a wrapped extent to `situ_view_sub`, which refuses it, so those
	were safe by the callee and are converted anyway: a guard that is three
	lines up is a guard somebody moves.
	"""
	found = SUBTRACTION.findall(generated(path, backend))
	assert not found, (
		f"{path.name}: {len(found)} generated length(s) computed by "
		f"subtracting from a limit. A hostile length field puts the offset "
		f"past the limit and the subtraction wraps, which makes every "
		f"bounds check below it pass. Use the saturating helper "
		f"(`situ_remaining_u32`) -- 26.675")


def test_the_rust_backend_does_not_grow_more_of_them() -> None:
	"""A floor, not a clean bill. See `RUST_PINNED`."""
	total = sum(len(SUBTRACTION.findall(generated(path, "rust")))
	            for path in SCHEMAS)
	assert total <= RUST_PINNED, (
		f"the Rust backend now computes {total} lengths by subtraction, up "
		f"from {RUST_PINNED}. Each one panics rather than reading out of "
		f"bounds, so this is a denial of service rather than a disclosure "
		f"-- but it is the same arithmetic and the floor is not for growing")
	assert total == RUST_PINNED, (
		f"the Rust count is {total}, below the pinned {RUST_PINNED}. Good "
		f"news needs the floor lowered to keep the test honest: set "
		f"RUST_PINNED to {total}")
