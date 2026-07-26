"""Codec signatures and impl bindings (project.md sections 13.1, 13.2).

The property signature is the interface between both codec tiers and everything
downstream. Two consequences drive most of what is tested here: a tier-1 codec
can lie, and swapping its implementation must change nothing the compiler
concluded.
"""

from __future__ import annotations

import pytest

from situc import ast, capmap
from situc.diagnostics import SituError
from situc.layout import solve
from situc.parser import parse_text
from situc.resolve import resolve

PREAMBLE = "endian big;\n"

CTR = (
	"codec aes_ctr { length_preserving; seekable = linear; "
	"granularity = byte; invertible; deterministic; }"
)


def rendered_map(body: str) -> str:
	schema   = parse_text(PREAMBLE + body)
	resolved = resolve(schema, solve(schema))
	return capmap.render(schema, resolved, "x.situ")


def rejected(body: str) -> str:
	with pytest.raises(SituError) as caught:
		schema = parse_text(PREAMBLE + body)
		resolve(schema, solve(schema))
	return caught.value.diagnostic.render()


# -- the signature ----------------------------------------------------------


def test_the_property_set_is_fixed() -> None:
	"""A codec cannot declare a property the lattice does not read."""
	report = rejected("codec c { fast; }")
	assert "unknown codec property `fast`" in report
	assert "the property set is fixed by section 13.2" in report


def test_defaults_claim_nothing() -> None:
	"""Silence in a declaration the compiler cannot verify has to mean the
	conservative thing."""
	codec = parse_text(PREAMBLE + "codec c { }").codecs()[0]

	assert codec.seekable is ast.Seekable.NONE
	assert codec.granularity is ast.Granularity.STREAM
	assert not codec.systematic
	assert not codec.invertible
	assert not codec.deterministic


@pytest.mark.parametrize(("source", "expansion", "extra"), [
	("length_preserving;", ast.Expansion.PRESERVING, None),
	("expansion = +4;", ast.Expansion.FIXED_ADD, 4),
	("expansion = unbounded;", ast.Expansion.UNBOUNDED, None),
	("expansion = ratio_exact(2, 1);", ast.Expansion.RATIO_EXACT, (2, 1)),
	("expansion = ratio_bounded(3, 2);", ast.Expansion.RATIO_BOUNDED, (3, 2)),
])
def test_every_expansion_form_parses(source: str, expansion: ast.Expansion,
		extra: object) -> None:
	codec = parse_text(PREAMBLE + f"codec c {{ {source} }}").codecs()[0]
	assert codec.expansion is expansion

	if expansion is ast.Expansion.FIXED_ADD:
		assert codec.expansion_add == extra
	elif extra is not None:
		assert codec.ratio == extra


def test_bare_seekable_means_linear() -> None:
	"""As example 5.3 writes it."""
	codec = parse_text(PREAMBLE + "codec c { seekable; }").codecs()[0]
	assert codec.seekable is ast.Seekable.LINEAR


def test_not_seekable_is_explicit() -> None:
	codec = parse_text(PREAMBLE + "codec c { not seekable; }").codecs()[0]
	assert codec.seekable is ast.Seekable.NONE


def test_granularity_carries_a_size() -> None:
	codec = parse_text(PREAMBLE + "codec c { granularity = block(16); }").codecs()[0]
	assert codec.granularity is ast.Granularity.BLOCK
	assert codec.granularity_size == 16


def test_granularity_accepts_any() -> None:
	"""`block(any)` is how section 13.1 writes a CRC's granularity."""
	codec = parse_text(PREAMBLE + "codec c { granularity = block(any); }").codecs()[0]
	assert codec.granularity is ast.Granularity.BLOCK
	assert codec.granularity_size is None


def test_a_property_given_twice_is_rejected() -> None:
	assert "`invertible` is given twice" in rejected(
		"codec c { invertible; invertible; }")


def test_a_ratio_needs_positive_literals() -> None:
	assert "two positive literals" in rejected("codec c { expansion = ratio_exact(0, 1); }")


# -- impl bindings ----------------------------------------------------------


def test_a_signature_may_have_no_implementation() -> None:
	"""Section 13.1: schemas are designed and analysed long before any codec is
	written, which is the normal case for a protocol under design."""
	rendered_map(CTR + "struct S { coded b(aes_ctr) { u32 x; } }")


def test_an_unbound_signature_is_marked_in_the_map() -> None:
	text = rendered_map(CTR + "struct S { coded b(aes_ctr) { u32 x; } }")
	assert "codec aes_ctr unbound" in text


def test_an_extern_binding_is_marked_trusted() -> None:
	"""Its properties rest on an assertion rather than a proof."""
	text = rendered_map(CTR + 'impl aes_ctr extern "my_aes";'
	                    "struct S { coded b(aes_ctr) { u32 x; } }")
	assert "codec aes_ctr trusted" in text
	assert "run `situc gen-codec-tests` to falsify a lying one" in text


def test_a_derived_binding_is_marked_derived() -> None:
	text = rendered_map(CTR + "impl aes_ctr derived;"
	                    "struct S { coded b(aes_ctr) { u32 x; } }")
	assert "codec aes_ctr derived" in text


def test_swapping_the_implementation_changes_nothing_else() -> None:
	"""Section 13.1's load-bearing claim: every capability conclusion derives
	from the signature, so substituting a hand-tuned routine or a hardware unit
	changes nothing about the layout, the map or the accessors.

	Only the binding line itself may differ.
	"""
	schema = CTR + "struct S { u16 h; coded b(aes_ctr) { u32 x; } u16 t; }"

	derived = rendered_map(schema + "impl aes_ctr derived;")
	first   = rendered_map(schema + 'impl aes_ctr extern "my_fast_aes";')
	second  = rendered_map(schema + 'impl aes_ctr extern "hw_aes_unit";')

	def without_bindings(text: str) -> list[str]:
		return [line for line in text.splitlines() if not line.startswith("codec ")]

	assert without_bindings(derived) == without_bindings(first)
	assert without_bindings(first) == without_bindings(second)

	# The two extern bindings differ only in the symbol, which the map does not
	# record: both read `trusted`.
	assert first == second


def test_an_impl_for_an_unknown_codec_is_rejected() -> None:
	report = rejected("impl nope derived;")
	assert "names unknown codec `nope`" in report
	assert "an implementation binds to a contract" in report


def test_a_codec_bound_twice_is_rejected() -> None:
	report = rejected(CTR + 'impl aes_ctr derived; impl aes_ctr extern "other";')
	assert "declared more than once" in report
	assert "swapping it means replacing the binding" in report


def test_extern_needs_a_symbol() -> None:
	assert "needs a quoted symbol name" in rejected(CTR + "impl aes_ctr extern;")


def test_a_region_naming_an_unknown_codec_is_rejected() -> None:
	report = rejected("struct S { coded b(nope) { u32 x; } }")
	assert "unknown codec `nope`" in report
	assert "a codec's properties are what the lattice reads" in report


def test_codec_names_share_the_type_namespace() -> None:
	assert "declared more than once" in rejected(
		"struct aes_ctr { u8 a; }" + CTR)


# -- the decidability rule (section 13.3) -----------------------------------


def test_a_size_may_not_reference_transform_output() -> None:
	report = rejected(CTR + "struct S { coded b(aes_ctr) { u8 n; } u8 v[n]; }")
	assert "cannot be referenced here" in report
	assert "may not reference transform output" in report
	assert "undecidable" in report


def test_a_dotted_reference_into_a_region_is_rejected_too() -> None:
	report = rejected(CTR + "struct S { coded b(aes_ctr) { u8 n; } u8 v[b.n]; }")
	assert "`b.n` is inside a `aes_ctr` region" in report


def test_a_discriminant_may_not_reference_transform_output() -> None:
	report = rejected(
		CTR + "enum K : u8 { a = 1, } struct A { u8 z; }"
		"struct S { coded b(aes_ctr) { K k; } "
		"variant v switch (k) { case K.a: A p; default: error; } }")
	assert "is inside a `aes_ctr` region" in report


def test_a_size_from_outside_the_region_is_fine() -> None:
	rendered_map(CTR + "struct S { u8 n; coded b(aes_ctr) { u16 x; } u8 v[n]; }")
