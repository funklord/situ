"""`u8 content[remaining] max 470` -- a cap on a run sized by the frame
(decision 0059).

`fuzznet` reported the gap from a record body whose real bound lives in
the enclosing record, so a body-only schema has no field to size it from
and could only say `[remaining]`, which reads Unbounded.

They asked for `[max = 470]`, and situc refuses that correctly: `[min]`,
`[max]` and `[must_eq]` are claims about a VALUE that `validate`
compares, an array has no single value, and 14.5's rule is what keeps one
attribute from meaning two things. The refusal message already named the
spelling the language uses -- a size cap is syntax after the run form, as
in `until D max N` -- so that is what this is.

A declared cap REFUSES a longer frame rather than truncating the read.
The copyright holder chose that: such a cap restates a bound the
enclosing format guarantees, so a message exceeding it is malformed
rather than something to read part of.
"""

from __future__ import annotations

import pytest

from situc import pack as packer
from situc.codegen.c import generate as generate_c
from situc.codegen.cpp import generate as generate_cpp
from situc.codegen.python import generate as generate_py
from situc.codegen.rust import generate as generate_rs
from situc.diagnostics import SituError
from situc.layout import solve
from situc.parser import parse_text
from situc.resolve import resolve
from situc.unparse import unparse

from walker.image import load
from walker.report import _validate
from walker.walk import acquire

PREAMBLE = "target buffer;\nendian big;\n"
CAPPED   = "struct b { u8 kind; u8 content[remaining] max 470; }\n"


def _resolved(body: str):  # type: ignore[no-untyped-def]
	schema = parse_text(PREAMBLE + body)
	return schema, resolve(schema, solve(schema))


def test_the_cap_bounds_the_extent() -> None:
	"""Without it the run is Unbounded, which is all `[remaining]` can say."""
	layout = solve(parse_text(PREAMBLE + CAPPED))
	held   = {one.path: one for one in layout.structs["b"].placements}

	assert held["b.content"].size_max_bits == 470 * 8
	assert held["b.content"].remaining_cap == 470
	assert layout.structs["b"].size_max_bits == 471 * 8

	# The control: the same schema without the cap says nothing.
	bare = solve(parse_text(PREAMBLE + "struct b { u8 kind; u8 c[remaining]; }\n"))
	assert bare.structs["b"].size_max_bits is None


def test_a_cap_where_something_already_bounds_the_run_is_refused() -> None:
	"""14.5: an attribute -- or here a clause -- has to sit where something
	reads it. A counted run is bounded by its count and a delimited one by
	`until D max N`, so a third spelling would state what nothing enforces.
	"""
	with pytest.raises(SituError) as raised:
		solve(parse_text(PREAMBLE + "struct b { u8 n; u8 x[4] max 9; }\n"))

	assert "already bounded" in str(raised.value)


def test_the_attribute_form_is_still_refused() -> None:
	"""The refusal fuzznet met, unchanged: it names the spelling instead."""
	with pytest.raises(SituError) as raised:
		solve(parse_text(
			PREAMBLE + "struct b { u8 k; u8 c[remaining] [max = 470]; }\n"))

	assert "means nothing here" in str(raised.value)


def test_the_cap_survives_a_round_trip() -> None:
	"""`cap` is stored independently rather than derived from anything the
	unparser already prints, so a round trip drops it unless the unparser
	renders it -- which is 26.144's checklist and what `Sealed.until` paid
	for.
	"""
	first = parse_text(PREAMBLE + CAPPED)
	text  = unparse(first)

	assert "max 470" in text
	again = parse_text(text)
	held  = solve(again).structs["b"].placements
	assert [one for one in held if one.path == "b.content"][0].remaining_cap == 470


def test_every_backend_refuses_a_longer_frame() -> None:
	"""Four readers and one number. The guard is the mirror of the size
	floor each already emits, and the bound is the struct's own maximum --
	1 for `kind` plus 470 for the run.

	Asserted on all four rather than one, because a rule that reaches
	three backends is this tree's most expensive recurring defect.
	"""
	schema, resolved = _resolved(CAPPED)
	sources = {
		"c":      generate_c(schema, resolved, "unit").source,
		"cpp":    generate_cpp(schema, resolved, "unit").header,
		"rust":   generate_rs(schema, resolved, "unit").module,
		"python": generate_py(schema, resolved, "unit").module,
	}
	wanted = {
		"c":      "view.limit > SITU_B_SIZE_MAX",
		"cpp":    "raw_.limit > 471u",
		"rust":   "self.bytes.len() > 471",
		"python": "self._len > 471",
	}

	for lang, text in sources.items():
		assert wanted[lang] in text, f"{lang} does not refuse a longer frame"


def test_the_walker_refuses_a_longer_frame() -> None:
	"""And agrees with the four about WHICH frames.

	471 is the last frame the schema allows; 472 is the first it does not.
	Both sides of the boundary, because a guard tested only on the bad
	side passes just as well when it refuses everything.
	"""
	schema, resolved = _resolved(CAPPED)
	blob, _ = packer.pack(schema, resolved, metadata=True)
	image   = load(blob)

	assert _validate(image, acquire(image, bytes(471), 0), 0) == 0
	assert _validate(image, acquire(image, bytes(472), 0), 0) != 0
	# And a frame the floor rejects is still the floor's answer.
	assert _validate(image, acquire(image, bytes(100), 0), 0) == 0
