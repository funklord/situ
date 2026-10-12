"""Inverting a size expression: `invariant.invertible` (26.630).

A reader is handed a field and derives a length; a writer is handed a
length and has to derive the field. Which expressions allow that is a
question about the schema rather than about any backend -- a second
writer would ask it the same way -- so it lives beside the other walks
over these expressions, and the C writer only spells the arithmetic.

The boundary: affine in exactly one field, with a positive scale. The
cases below are the corpus's own expressions, which is why there are
eight that invert and four that do not rather than a tidier split.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import pytest				# noqa: E402

from situc import invariant		# noqa: E402
from situc.parser import parse_text	# noqa: E402


def size_expression(source: str):	# type: ignore[no-untyped-def]
	"""The array size in `u8 x[<source>]`, as the parser built it.

	Through the parser rather than by hand-building nodes, because what
	the inverter has to handle is what the front end produces -- a
	dotted path is `Access(NameRef(...))` and not a name with a dot in
	it, which is the shape `paths_in` exists to say.
	"""
	schema = parse_text("target buffer;\nendian big;\n\n"
	                    "struct s {\n\tu16 n;\n\tu8 x[" + source + "];\n}\n")
	for member in schema.structs()[0].members:
		array = getattr(member, "array", None)
		if array is not None and array.size is not None:
			return array.size
	raise AssertionError(f"no array size parsed from {source!r}")


@pytest.mark.parametrize(("source", "path", "scale", "offset"), [
	# UDP: the length counts the eight header bytes as well.
	("length - 8",                  "length",       1, -8),
	# IPv4: the header length in four-byte words, minus the five that
	# are the fixed header -- so a scale AND an offset.
	("(ihl - 5) * 4",               "ihl",          4, -20),
	# netlink, where the length includes its own header too.
	("nla_len - 4",                 "nla_len",      1, -4),
	# IPv6 extension headers: eight-byte units, not counting the first.
	("((hdr_ext_len + 1) * 8) - 2", "hdr_ext_len",  8, 6),
	("(units + 1) * 4 - 2",         "units",        4, 2),
	("n + 1",                       "n",            1, 1),
	("length - 1",                  "length",       1, -1),
	("size_3 - 6",                  "size_3",       1, -6),
])
def test_an_affine_expression_names_the_field_and_how(
		source: str, path: str, scale: int, offset: int) -> None:
	"""The form, which is what the writer emits from."""
	got = invariant.invertible(size_expression(source))
	assert got is not None, invariant.inversion_refusal(
		size_expression(source))
	assert (got.path, got.scale, got.offset) == (path, scale, offset)


@pytest.mark.parametrize(("source", "length", "field"), [
	("length - 8",                  0,   8),
	("length - 8",                  65,  73),
	("(ihl - 5) * 4",               0,   5),
	("(ihl - 5) * 4",               4,   6),
	("(ihl - 5) * 4",               40,  15),
	("((hdr_ext_len + 1) * 8) - 2", 6,   0),
	("((hdr_ext_len + 1) * 8) - 2", 14,  1),
	("n + 1",                       1,   0),
])
def test_the_inverse_round_trips_against_the_expression(
		source: str, length: int, field: int) -> None:
	"""The arithmetic both ways, which is the only check that catches a
	sign error in the offset.

	A form with the wrong sign still looks like a form, and every test
	above would pass with `offset` negated -- so each case here puts the
	solved field value back through the expression and asserts the length
	comes out. `(ihl - 5) * 4` at ihl = 5 is zero bytes of options,
	which is also the canonical header's value.
	"""
	got = invariant.invertible(size_expression(source))
	assert got is not None
	assert (length - got.offset) % got.scale == 0
	assert (length - got.offset) // got.scale == field

	# And forwards, from the field to the length, by evaluating the
	# schema's own expression with the field substituted.
	forward = eval(			# noqa: S307 - a test's own arithmetic
		source.replace(got.path, str(field)).replace("/", "//"))
	assert forward == length, f"{source} at {got.path}={field}"


@pytest.mark.parametrize(("source", "wanted"), [
	# Floor division: two field values give one length, so there is
	# nothing to invert back to. edges' `arithmetic` is the case.
	("(units / 2) + 1",  "divides by a value"),
	# Two fields, and one length cannot say what both hold.
	("span_base.narrow + n", "one length cannot say what two fields hold"),
	# ID3's synchsafe integer: four fields and a bitwise or.
	("(((size_0 << 21) | (size_1 << 14)) | (size_2 << 7)) | size_3",
	 "it uses `|`"),
	# cpio's and netlink's padding, whose whole point is that the result
	# depends on where the run lands rather than on how long it is.
	("align_up(nla_len, 4) - nla_len", "calls `align_up`"),
	# A constant: no field records the length at all.
	("8", "names no field"),
	# A shift by a field, which is not a constant multiply.
	("n << m", "shifts by a field"),
	# And two fields multiplied, which is not affine in either.
	("n * m", "multiplies two fields together"),
])
def test_what_cannot_be_inverted_says_which_shape_stopped_it(
		source: str, wanted: str) -> None:
	"""The refusal names the operator or the shape, not the node type.

	A writer's refusal is its only interface for what it cannot do, and
	these are what a schema author reads. The fallback -- "is not
	arithmetic over one field and constants" -- is a sentence too, so a
	case this does not anticipate still says something true.
	"""
	expr = size_expression(source)
	assert invariant.invertible(expr) is None
	why = invariant.inversion_refusal(expr)
	assert wanted in why, why
	assert why and not why.endswith(" "), why


def test_an_expression_over_a_wide_run_counts_elements() -> None:
	"""`u16 a[n]` makes `n` a count of ELEMENTS, not of bytes.

	So an expression over such a run inverts in elements too, and the
	writer's arithmetic has to read the caller's `_count` rather than a
	`_len` that does not exist. It read `_len` first, which the generated
	header could not compile -- and before that the element branch
	matched ahead of the inverted one and wrote the raw count, three
	elements where `n + 1` means the field says two.

	Both are the same fault as `situ_at_len` earlier today: a name or a
	branch chosen by the shape a member usually has. The compiler caught
	the first and an unused `situ_solved` caught the second, which is why
	the generated file is compiled with `-Werror` in the suite that
	builds it rather than only inspected.
	"""
	got = invariant.invertible(size_expression("n + 1"))
	assert got is not None and (got.path, got.scale, got.offset) \
		== ("n", 1, 1)

	# The module says nothing about elements or bytes -- it solves the
	# arithmetic, and which unit the length is in belongs to the member.
	# Asserted because a reader of this file would otherwise have to
	# guess where that decision lives.
	assert not hasattr(got, "element_bits")
