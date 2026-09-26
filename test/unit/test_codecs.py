"""Codec signatures and impl bindings (project.md sections 13.1, 13.2).

The property signature is the interface between both codec tiers and everything
downstream. Two consequences drive most of what is tested here: a tier-1 codec
can lie, and swapping its implementation must change nothing the compiler
concluded.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from every_schema import ROOT
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
	# `expansion_add` is BITS since 0046 and the surface stays bytes, so
	# `+4` is 32. `+N bits` is the spelling for a growth no byte count
	# says -- a five-bit CRC adds five -- and nothing else writes one.
	("expansion = +4;", ast.Expansion.FIXED_ADD, 32),
	("expansion = +5 bits;", ast.Expansion.FIXED_ADD, 5),
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


# Every expansion form the parser takes: as the refusal below spells it, and
# as a schema writes it. The refusal is asserted against this table rather
# than against a frozen string, and the table is asserted against the parser
# and against the lattice -- so a form that parses and is not named in the
# message fails here rather than being discovered by whoever wrote a schema
# from the message (26.215).
EXPANSION_FORMS = {
	"`+N`":                  "+4",
	"`+N bits`":             "+5 bits",
	"`unbounded`":           "unbounded",
	"`ratio_exact(a, b)`":   "ratio_exact(2, 1)",
	"`ratio_padded(a, b)`":  "ratio_padded(2, 1)",
	"`ratio_bounded(a, b)`": "ratio_bounded(3, 2)",
}


@pytest.mark.parametrize("source", sorted(EXPANSION_FORMS.values()))
def test_every_form_in_the_table_is_one_the_parser_takes(source: str) -> None:
	"""The table has to be the parser's list and not the message's, or the
	check below compares the message against itself."""
	parse_text(PREAMBLE + f"codec c {{ expansion = {source}; }}")


def test_every_expansion_the_lattice_carries_is_a_form_or_a_property() -> None:
	"""And the other direction, so that a seventh expansion arrives as a
	failure addressed to whoever added it rather than being absorbed."""
	assert "`+N`" in EXPANSION_FORMS and "`+N bits`" in EXPANSION_FORMS

	for member in ast.Expansion:
		if member is ast.Expansion.PRESERVING:
			continue	# a property of its own: `length_preserving;`
		if member is ast.Expansion.FIXED_ADD:
			continue	# spelled `+N`, not by its value
		assert (f"`{member.value}`" in EXPANSION_FORMS
		        or f"`{member.value}(a, b)`" in EXPANSION_FORMS), \
			f"`{member.value}` parses and the table does not carry it"


def test_an_unknown_expansion_form_names_every_form_that_parses() -> None:
	"""What a diagnostic no test produces loses is not the refusal but the
	wording (26.215), and this one had lost it: `+N bits` arrived with 0046
	and the label went on listing the five forms it knew, so a schema author
	who asked the compiler what to write was told the wrong set.

	Against the LABEL rather than against the whole rendering: the note below
	it glosses `+N bits` too, so a check reading the report passes with the
	label stale -- which is the state this test was written for."""
	report = rejected("codec c { expansion = wibble; }")
	label  = next(line for line in report.splitlines() if "^" in line)

	assert "unknown expansion form `wibble`" in report
	for spelling in EXPANSION_FORMS:
		assert spelling in label, f"the label does not name {spelling}"


def test_an_expansion_addend_needs_a_literal_byte_count() -> None:
	"""0048's addend is a literal because a consumer sizes a buffer from it
	before reading anything: `len * 255 / 254 + 1`. A path would make the
	size depend on the data it is sizing."""
	report = rejected("codec c { expansion = ratio_bounded(255, 254) + hdr.n; }")

	assert "an expansion addend needs a literal byte count" in report
	assert "not a non-negative integer literal" in report
	assert "len * 255 / 254 + 1" in report


def test_an_expansion_addend_may_not_be_negative() -> None:
	"""A code that expands appends no bytes at worst, and `+ -1` is what a
	typo for a shrinking code looks like."""
	assert "an expansion addend needs a literal byte count" in rejected(
		"codec c { expansion = ratio_bounded(255, 254) + -1; }")


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


def test_an_authenticated_claim_says_what_checks_it_instead() -> None:
	"""The four AEADs guarding every sealed region got neither.

	Every other declared property here gets a test or a note saying why the
	compiler cannot write one, and the emitting loop says what a builder that
	returns nothing reads as: "silence would read as coverage". This one
	returned nothing while claiming the strongest thing a codec can claim.
	"""
	text = codec_tests("codec c { length_preserving; authenticated; }")

	assert "`authenticated` is declared and nothing here" in text
	assert "section 14.2" in text


def test_an_authenticated_codec_that_carries_its_tag_is_told_to_attack_it() -> None:
	"""Where the expansion accounts for `tag_bytes` the tag is in the output,
	so `decode` of a modified one must refuse -- a cheap falsifier, and one
	that is not written because no codec here is that shape. The note says so
	rather than reading like the case above."""
	text = codec_tests("codec c { expansion = +16; tag_bytes = 16;"
	                   " authenticated; }")

	assert "must refuse" in text
	assert "nothing here" not in text


def test_an_error_propagating_claim_gets_a_note_too() -> None:
	"""Falsifiable at this ABI and measured that way for derived codecs
	(26.150). No tier-1 codec here declares it, so the note is the whole of
	what a generated suite can honestly say."""
	text = codec_tests("codec c { length_preserving; error_propagating; }")

	assert "`error_propagating` is declared and nothing" in text


def test_a_codec_claiming_neither_gets_neither_note() -> None:
	"""Silence is right for a property the signature does not claim -- that is
	what every other builder does -- so the notes must not fire on a codec
	that says nothing about either."""
	text = codec_tests("codec c { length_preserving; }")

	assert "`authenticated` is declared" not in text
	assert "`error_propagating` is declared" not in text


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


def test_a_ones_complement_kernel_is_declined_like_any_other_digest() -> None:
	"""RFC 1071's sum reduces its input to a value exactly as a CRC does, so
	it has no inverse of the same shape and no pair to attack.

	It was answered as a pair until 2026-09-08, because `pair_of` EXCLUDED
	the families that produce none and this family arrived after the list
	was written (26.245). `gen-codec-tests` then emitted a suite calling
	`situ_internet_checksum_encode`, which `gen-derived` does not define --
	and `make test-c` did not link."""
	text = codec_tests(
		"codec c { kernel = ones_complement(width = 16, complement); }\n"
		"impl c derived;", bind=False)

	assert "no suite" in text
	assert "static void test_c_" not in text
	assert "situ_c_encode" not in text


def test_every_kernel_family_is_decided_as_a_transform_or_a_digest() -> None:
	"""The partition, rather than the two cells a schema happens to reach.

	This is the assertion that would have caught the link failure the entry
	above records, and it is the reason the predicate lists what it INCLUDES:
	a family added to `KernelFamily` and to no set fails here, by name,
	before anything generates a call to a function nobody emits."""
	from situc.codegen.c.derived import DIGEST_FAMILIES, TRANSFORM_FAMILIES

	assert TRANSFORM_FAMILIES | DIGEST_FAMILIES == set(ast.KernelFamily)
	assert not TRANSFORM_FAMILIES & DIGEST_FAMILIES


def test_a_refusal_names_the_kernel_rather_than_the_tier() -> None:
	""""its implementation is derived" was never the reason a derived codec
	was declined: `manchester_802_3` is derived and gets a full suite.

	What decides it is the kernel, and a refusal naming the wrong reason
	sends its reader to look at the wrong thing -- which is worse than one
	that says nothing, because it arrives with the authority of a diagnosis."""
	text = codec_tests(
		"codec c { kernel = polynomial(width = 32, poly = 0x04C11DB7,"
		" init = 0xFFFFFFFF, xorout = 0xFFFFFFFF, reflect); }\n"
		"impl c derived;", bind=False)

	assert "its implementation is derived" not in text
	assert "`polynomial` kernel is a digest over its input" in text


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


# ---------------------------------------------------------------------------
# Which decoder a header declares, when the impl is extern
# ---------------------------------------------------------------------------

#: A stuffing codec over a `coded` region: length-changing, so nothing inside
#: is addressable without decoding, and `decodes_here` is true -- which is
#: what makes the derived prototype reachable at all.
#:
#: The combination this needs is not in the corpus, which is why the defect
#: lived. Every committed `extern` binding is an AEAD or a transform whose
#: decode shape is not a settled kernel, so `decodes_here` is False for all
#: six and none reaches the branch below. Measured: `doubling`,
#: `sealing_aead`, `masking`, `aes_gcm_128`, `aes_128_gcm`, `aes_gcm_256`.
_STUFFED = """target buffer;
endian big;

codec puffer {
\tkernel = stuffing(worst_case = 2, per = 1, unit = byte, code = slip);
}
impl puffer IMPL

struct frame {
\tcoded payload(puffer) until "\\xC0" {
\t\tu8  body[remaining];
\t}
}
"""


def _built(tmp_path: Path, impl: str, target: str) -> str:
	schema = tmp_path / "stuffed.situ"
	schema.write_text(_STUFFED.replace("IMPL", impl), encoding="ascii")
	out = tmp_path / target
	done = subprocess.run(
		[sys.executable, "-m", "situc.cli", "build", str(schema),
		 "--target", target, "--out", str(out)],
		cwd=ROOT, capture_output=True, text=True)
	assert done.returncode == 0, done.stdout + done.stderr
	suffix = {"c": "h", "cpp": "hpp", "rust": "rs"}[target]
	text: str = (out / f"stuffed.{suffix}").read_text(encoding="ascii")
	return text


def _declared(source: str, target: str) -> set[str]:
	"""Symbols the header DECLARES, which is not the same as mentions.

	A header names a codec in a comment and calls it in a body, so a search
	of the whole file answers "does this appear" where the question is
	"does a consumer get a prototype". Anchoring on each language's
	declaration form is what separates them -- the first measurement of
	this did not, and reported C declaring a symbol that was a comment
	(26.513).
	"""
	forms = {
		"c":    r"^extern .*?\b(situ_puffer|app_puffer)_(\w+)",
		"cpp":  r"^(?:extern )?(?:uint32_t|situ_err_t) .*?\b"
		        r"(situ_puffer|app_puffer)_(\w+)",
		"rust": r"^\tfn (situ_puffer|app_puffer)_(\w+)",
	}
	found = set()
	for line in source.splitlines():
		match = re.search(forms[target], line)
		if match:
			found.add(f"{match.group(1)}_{match.group(2)}")
	return found


@pytest.mark.parametrize("target", ["c", "cpp", "rust"])
def test_an_extern_impl_declares_only_the_symbol_it_binds(
		target: str, tmp_path: Path) -> None:
	"""A header must not promise a function no `impl` provides.

	With `impl puffer extern "app_puffer"` the accessor decodes through
	`app_puffer_decode` under the tier-1 ABI -- `_decode_accessor` returns
	`_extern_decode` before it ever asks the kernel. C++ and Rust declared
	`situ_puffer_decode` anyway: a symbol no binding provides and nothing
	calls, inert in both languages and still a header describing a
	function that will not exist. C never did (26.138, 26.513).

	The same argument had already been made and applied one list over:
	`checksums` in the C++ backend drops a codec nothing writes, in
	26.368's words -- "harmless to the linker while nothing calls it, and
	a promise the header cannot keep".
	"""
	extern  = _declared(_built(tmp_path, 'extern "app_puffer";', target),
	                    target)
	assert "app_puffer_decode" in extern, (
		f"{target}: the bound symbol is not declared, so this is testing "
		f"nothing about which of two it chose")
	assert "situ_puffer_decode" not in extern, (
		f"{target}: declares a derived decoder no `impl` provides")


@pytest.mark.parametrize("target", ["c", "cpp", "rust"])
def test_a_derived_impl_still_declares_the_derived_decoder(
		target: str, tmp_path: Path) -> None:
	"""The control, and the half a one-sided fix would break.

	Dropping the declaration for an extern binding is right; dropping it
	for a derived one would remove the prototype the accessor calls. Both
	directions are asserted, because the change that produced this test
	could satisfy the other test by declaring nothing at all.
	"""
	derived = _declared(_built(tmp_path, "derived;", target), target)
	assert "situ_puffer_decode" in derived, (
		f"{target}: a derived impl must still declare its decoder")
	assert "app_puffer_decode" not in derived


@pytest.mark.parametrize("impl", ['extern "app_puffer";', "derived;"])
def test_the_stuffed_header_compiles_either_way(impl: str,
		tmp_path: Path) -> None:
	"""A declaration removed must not take its caller with it.

	The corpus cannot answer this: every committed `extern` binding is an
	AEAD or a transform whose decode is not a settled kernel, so none of
	the six reaches the branch, and 84 corpus builds came back byte-for-
	byte identical across the change. A comparison that finds nothing is
	worth nothing until it is shown able to speak, and that one was --
	sabotaging the emitter's banner moved `slip` and `edges` at once. So
	the zero is real and the gate will not protect this; only a schema
	built here will.

	Syntax-only, which is the right depth: the question is whether the
	header still declares what it calls, not whether anybody supplies the
	implementation. A tier-1 symbol is the consumer's to provide and
	`-fsyntax-only` does not ask for it.
	"""
	host = shutil.which("g++") or shutil.which("clang++")
	if host is None:
		pytest.skip("no C++ compiler")

	source = _built(tmp_path, impl, "cpp")
	(tmp_path / "stuffed.hpp").write_text(source, encoding="ascii")
	(tmp_path / "main.cpp").write_text(
		'#include "stuffed.hpp"\nint main() { return 0; }\n', encoding="ascii")

	runtime = ROOT / "runtime"
	built = subprocess.run(
		[host, "-std=c++17", "-pedantic-errors", "-Wall", "-Wextra",
		 "-Werror", "-fsyntax-only",
		 f"-I{runtime / 'c'}", f"-I{runtime / 'cpp'}", f"-I{tmp_path}",
		 str(tmp_path / "main.cpp")],
		capture_output=True, text=True)

	assert built.returncode == 0, built.stderr
