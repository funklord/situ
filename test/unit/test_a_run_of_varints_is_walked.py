"""`uvar v[n]` is a run, in all four backends (26.683).

bbq-predictor reported that a varint run compiles, emits accessors, and
`required` answers 3 for a message holding 5. The construct has been in
`project.md` since it first described the capability axes -- "`access` of
an array of varints is `Sequential`, never `Random`" -- and no reader
implemented it.

**Six readers were wrong the same way, so they agreed**, which is what the
corpus differential cannot see: C dispatched on `placement.varint`, the
other three asked `traverse.classify` where `data_sized` claimed the member
first, and both walkers measured one element. `edges.varint_run` closes the
corpus half and this file closes the static half.

**Why a static test as well as the fixture.** Two of this pass's own
mistakes were invisible to a differential and visible here:

- the run's `_at` returned the RAW value, dropping the zigzag transform,
  so a `svar` run would read -1 as 1 -- and the corpus has no zigzag
  varint, so no differential could have asked;
- `required` asked `_span`, which SHRINKS on a truncated element, so two
  bytes of a six-byte message answered SITU_OK. A differential over whole
  messages never asks a framing function about a prefix.

So the properties below are relationships between what is emitted and what
calls it, not a value anybody read off one run.
"""

from __future__ import annotations

import pytest

from situc.codegen.c import generate as generate_c
from situc.codegen.cpp import generate as generate_cpp
from situc.codegen.python import generate as generate_py
from situc.codegen.rust import generate as generate_rs
from situc.layout import solve
from situc.parser import parse_text
from situc.resolve import resolve

#: A run, and a member after it so that a wrong span has somewhere to show.
RUN = """target buffer;
endian little;
varint_type uvar { encoding = leb128; max_bits = 64; max_bytes = 10; }
struct r {
	u8    n;
	uvar  v[n];
	u16   tail;
}
"""

#: One varint rather than a run, which must keep the two accessors it has.
#: The predicate separating them is `traverse.is_varint_run`, and a
#: predicate that answered True for everything would pass every assertion
#: below without this case.
ONE = """target buffer;
endian little;
varint_type uvar { encoding = leb128; max_bits = 64; max_bytes = 10; }
struct r {
	uvar  v;
	u16   tail;
}
"""


def generated(text: str, backend: str) -> str:
	schema   = parse_text(text)
	resolved = resolve(schema, solve(schema))
	if backend == "c":
		return generate_c(schema, resolved, "u").header
	if backend == "cpp":
		return generate_cpp(schema, resolved, "u").header
	if backend == "rust":
		return generate_rs(schema, resolved, "u").module
	return generate_py(schema, resolved, "u").module


#: What each backend spells the four accessors, for one member named `v`.
#: Written out per backend rather than derived, because a derivation shared
#: with the emitter would test agreement with itself.
SPELLINGS = {
	"c":      ("situ_r_v_count", "situ_r_v_span", "situ_r_v_span_min",
	           "situ_r_v_at"),
	"cpp":    ("v_count", "v_span", "v_span_min", "v_at"),
	"rust":   ("v_count", "v_span", "v_span_min", "v_at"),
	"python": ("v_count", "v_span", "v_span_min", "v_at"),
}

BACKENDS = tuple(SPELLINGS)


@pytest.mark.parametrize("backend", BACKENDS)
def test_a_run_gets_all_four_accessors(backend: str) -> None:
	"""A run answers four questions; one varint answers two."""
	emitted = generated(RUN, backend)
	for name in SPELLINGS[backend]:
		assert name in emitted, f"{backend} emits no {name}"


@pytest.mark.parametrize("backend", BACKENDS)
def test_one_varint_is_not_a_run(backend: str) -> None:
	"""The positive control for the predicate above.

	Without this, `is_varint_run` returning True unconditionally would
	satisfy every other test in this file -- and would emit a count for a
	member that has none, from a `sized_by` that is not there.
	"""
	emitted = generated(ONE, backend)
	for name in SPELLINGS[backend]:
		if name.endswith("_count") or "span" in name or name.endswith("_at"):
			assert name not in emitted, \
				f"{backend} emits {name} for a single varint"


@pytest.mark.parametrize("backend", BACKENDS)
def test_required_asks_the_minimum_span(backend: str) -> None:
	"""`required` asks `_span_min`, and nothing else does.

	This is the assertion that would have failed on the first draft. The
	span SHRINKS when an element is truncated -- correctly, it is the bytes
	that are there -- so `required` reading it answered SITU_OK for two
	bytes of a six-byte message. The minimum counts one byte per element
	not yet seen, which is a true lower bound and cannot report a complete
	message short.
	"""
	emitted = generated(RUN, backend)
	least   = SPELLINGS[backend][2]
	# The definition, plus exactly one call -- `required`'s. A second
	# caller would mean the offset chain had taken the lower bound, which
	# would place every member after the run too early.
	assert emitted.count(least) == 2, \
		f"{backend} names {least} {emitted.count(least)} times, want 2"


@pytest.mark.parametrize("backend", BACKENDS)
def test_the_three_internal_accessors_are_called_and_at_is_not(
		backend: str) -> None:
	"""Which of the four the generated code calls, and which it only emits.

	The trap this pass fell into twice was a definition and a call keyed on
	different conditions -- `situ_pieces_two_fits` emitted and never
	called, `filters_fits` called and never defined. One predicate decides
	all four now, and these counts are its consequence.

	`_at` is the one that must be mentioned EXACTLY once, and that is a
	property rather than an accident: it is the caller's accessor, and
	anything internal reaching for it would be walking the run once per
	element where `_span` walks it once. A rising count here is an offset
	chain that has gone quadratic.
	"""
	emitted = generated(RUN, backend)
	count, span, least, at = SPELLINGS[backend]

	assert emitted.count(count) >= 2, "nothing calls the count"
	assert emitted.count(span) >= 2, "nothing places a member through it"
	assert emitted.count(at) == 1, \
		f"{backend} mentions {at} {emitted.count(at)} times; only the " \
		f"caller walks a run element by element"
	# `_span_min`'s single caller is asserted above, in
	# `test_required_asks_the_minimum_span`.
	assert emitted.count(least) == 2


def test_a_zigzag_run_decodes_through_the_transform() -> None:
	"""A `svar` run's `_at` applies zigzag, as the single one's getter does.

	The first draft returned the raw accumulator, which would have read -1
	as 1 and 1 as -2 with nothing in the frame looking wrong. No
	differential could have caught it: the corpus declares one varint type
	and it has no transform.
	"""
	text = RUN.replace("max_bytes = 10; }",
	                   "max_bytes = 10; transform = zigzag; }")
	assert "zigzag_decode" in generated(text, "c")
	assert "zigzag_decode" in generated(text, "cpp")
	assert "zigzag_decode" in generated(text, "rust")
	assert "zigzag_decode" in generated(text, "python")


def test_a_run_places_the_member_after_it_through_the_span() -> None:
	"""The fault itself, as a property of C's offset chain.

	`tail` is placed at a sum of the lengths before it. Reading the SINGLE
	element's `_len` there is what put it inside the run -- so the
	assertion is that the span, not the length, is what `tail`'s offset is
	derived from.
	"""
	emitted = generated(RUN, "c")
	start   = emitted.index("situ_r_tail_offset")
	body    = emitted[start:start + 400]
	assert "situ_r_v_span(" in body, "tail is not placed through the span"
	assert "situ_r_v_len(" not in body, "tail is placed through a _len"
