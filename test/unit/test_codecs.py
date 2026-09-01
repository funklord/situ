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


# -- gen-codec-tests (section 13.1) -----------------------------------------


def codec_tests(body: str, bind: bool = True) -> str:
	"""The harness for `body`, with an implementation bound to every codec.

	Bound by default because a harness with nothing to call is not one: the
	suite tests the functions an `impl` names (13.2a), and a signature with no
	implementation gets a stated refusal instead (26.35).
	"""
	from situc.codegen.c import codectests

	schema = parse_text(PREAMBLE + body)
	if bind:
		bound = "\n".join(f'impl {decl.name} extern "my_{decl.name}";'
		                   for decl in schema.codecs())
		schema = parse_text(PREAMBLE + body + "\n" + bound)

	return codectests.generate(schema, "unit")


def test_a_length_claim_gets_a_sweep() -> None:
	text = codec_tests("codec c { length_preserving; }")
	assert "test_c_length" in text
	assert "assert_int_equal(out_len, in_len);" in text


def test_a_fixed_expansion_is_checked_exactly() -> None:
	text = codec_tests("codec c { expansion = +4; }")
	assert "assert_int_equal(out_len, in_len + 4u);" in text


def test_an_exact_ratio_is_checked_exactly() -> None:
	text = codec_tests("codec c { expansion = ratio_exact(2, 1); }")
	assert "assert_int_equal(out_len, (in_len * 2u + 0u) / 1u);" in text


def test_a_bounded_ratio_is_checked_as_a_bound() -> None:
	text = codec_tests("codec c { expansion = ratio_bounded(255, 254); }")
	assert "assert_true(out_len <= (in_len * 255u + 253u) / 254u);" in text


def test_unbounded_expansion_gets_no_length_test() -> None:
	"""It claims nothing about extent, so there is nothing to falsify. Saying
	so beats emitting a test that always passes."""
	text = codec_tests("codec c { expansion = unbounded; }")
	assert "makes no length claim" in text
	assert "test_c_length" not in text


@pytest.mark.parametrize(("property_", "expected"), [
	("deterministic;", "test_c_deterministic"),
	("invertible;", "test_c_invertible"),
	("seekable = linear;", "test_c_seekable_linear"),
])
def test_each_declared_property_gets_its_test(property_: str, expected: str) -> None:
	assert expected in codec_tests(f"codec c {{ {property_} }}")


def test_an_undeclared_property_gets_no_test() -> None:
	"""The suite attacks what was claimed, not what might have been."""
	text = codec_tests("codec c { length_preserving; }")
	assert "test_c_deterministic" not in text
	assert "test_c_invertible" not in text
	assert "test_c_seekable_linear" not in text


def test_a_systematic_appended_parity_codec_is_checked() -> None:
	text = codec_tests("codec c { expansion = +4; systematic; }")
	assert "test_c_systematic" in text
	assert "assert_memory_equal(output, input, sizeof(input));" in text


def test_a_systematic_codec_with_no_computable_offsets_says_so() -> None:
	"""Rather than emitting a test that checks the wrong bytes."""
	text = codec_tests("codec c { expansion = ratio_exact(7, 4); systematic; }")
	assert "cannot say where the data lands" in text
	assert "test_c_systematic" not in text


def test_block_granularity_checks_independence() -> None:
	text = codec_tests("codec c { length_preserving; granularity = block(16); }")
	assert "test_c_block_independence" in text
	assert "Disturb one byte of the second block" in text


def test_a_signature_claiming_nothing_produces_no_tests() -> None:
	"""And the generated main says why, rather than reporting a pass."""
	text = codec_tests("codec c { expansion = unbounded; }")
	assert "That is not a pass: it means the signatures claim nothing" in text


def test_a_derived_codec_is_attacked_through_its_kernel_pair() -> None:
	"""A tier-2 codec has no `impl extern` to bind the tier-1 ABI to, and its
	implementation is situ's own. Its *properties* cannot lie -- they follow
	from the kernel the code is generated from -- and the implementation still
	can, which is what these attack (26.35).

	The call shape is the kernel's: `(in, count, out) -> count`, counting bits
	where the kernel is bit-oriented."""
	text = codec_tests(
		"codec mm { kernel = table(input_bits = 1, output_bits = 2,"
		" code = manchester_802_3); }\nimpl mm derived;",
		bind=False)

	assert "situ_mm_encode(input, in_len * 8u, coded)" in text
	assert "test_mm_derived_invertible" in text
	assert "test_mm_derived_deterministic" in text
	assert "test_mm_derived_length" in text


def test_a_padded_codec_is_cut_on_a_group_boundary() -> None:
	"""`seekable = linear` is a claim at the codec's own granularity. base64
	emits whole groups of four from three input bytes, so cutting at half of
	128 pads the 64 and the outputs diverge at the last group -- the tier-1
	harness cuts at half without asking, which would fail a correct
	implementation the first time one was bound."""
	text = codec_tests(
		"codec b64 { kernel = table(input_bits = 6, output_bits = 8,"
		" code = base64, pad = 0x3D); }\nimpl b64 derived;",
		bind=False)

	assert "situ_b64_encode(input, 126u, whole)" in text
	assert "situ_b64_encode(input, 63u, partial)" in text


def test_a_kernel_with_no_pair_is_declined() -> None:
	"""A polynomial kernel is a checksum over its input rather than a
	transform with an inverse, so there is no pair to attack -- and the file
	says so where the suite would have been."""
	text = codec_tests(
		"codec c { kernel = polynomial(width = 32, poly = 0x04C11DB7,"
		" init = 0xFFFFFFFF, xorout = 0xFFFFFFFF, reflect); }\n"
		"impl c derived;", bind=False)

	assert "no suite" in text
	assert "static void test_c_" not in text


def test_the_standard_library_declines_every_suite() -> None:
	"""`std/codecs.situ` is contracts and no `impl`, which is what it is for.

	A harness with nothing to call is not one, so each signature gets a stated
	refusal instead -- the same rule every other generated artifact follows:
	one that quietly omits a codec is indistinguishable from one that never
	had it (26.35)."""
	from pathlib import Path

	from situc.codegen.c import codectests

	path   = Path(__file__).resolve().parents[2] / "std" / "codecs.situ"
	schema = parse_text(path.read_text(encoding="ascii"))
	text   = codectests.generate(schema, "codecs")

	assert "static void test_" not in text
	assert text.count("no suite") >= 19
	assert "no `impl` binds an implementation" in text


def test_the_standard_library_generates_a_full_suite() -> None:
	from pathlib import Path

	from situc.codegen.c import codectests

	path   = Path(__file__).resolve().parents[2] / "std" / "codecs.situ"
	source = path.read_text(encoding="ascii")
	schema = parse_text(source)
	bound  = "\n".join(f'impl {decl.name} extern "my_{decl.name}";'
	                    for decl in schema.codecs())
	text   = codectests.generate(parse_text(source + "\n" + bound), "codecs")

	# Every signature that claims something gets attacked.
	assert text.count("static void test_") >= 60
	assert "test_aes_ctr_128_seekable_linear" in text
	assert "test_manchester_invertible" in text
	assert "test_crc32_systematic" in text
