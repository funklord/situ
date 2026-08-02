"""Tier-2 codecs: signatures derived from kernels, and code generated from them.

The difference between the tiers, stated exactly: a tier-1 signature is
declared and trusted, and a tier-2 signature is computed from a description the
compiler could also generate the implementation from. The properties in the
capability map and the code in the object file come from one source, so they
cannot disagree -- which is what makes a derived codec worth more than a
carefully written declaration.

Section 26.12 is explicit that no propagation rule changes in this phase, and
`test_no_propagation_rule_reads_a_kernel` holds it to that: the lattice reads
nine properties and never asks where they came from.

The generated implementations are checked against published constants -- the CRC
catalogue's check values, IEEE 802.3's Manchester encoding -- rather than
against situ's own output, which is the only way to tell an implementation of
CRC-32 from an implementation of whatever this happens to do.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from situc import ast, kernels
from situc.codegen.c import derived, generate
from situc.diagnostics import SituError
from situc.dump import dump
from situc.layout import solve
from situc.parser import parse_text
from situc.resolve import resolve
from situc.unparse import unparse

ROOT    = Path(__file__).resolve().parents[2]
RUNTIME = ROOT / "runtime" / "c"
HOST_CC = shutil.which("gcc") or shutil.which("cc")

WARNINGS = ["-std=c11", "-O1", "-Wall", "-Wextra", "-Werror",
	"-Wconversion", "-Wsign-conversion"]


def codecs(body: str) -> dict[str, ast.CodecDecl]:
	return {decl.name: decl for decl in parse_text(body).codecs()}


def only(body: str) -> ast.CodecDecl:
	found = codecs(body)
	assert len(found) == 1
	return next(iter(found.values()))


def refusal(body: str) -> str:
	with pytest.raises(SituError) as caught:
		parse_text(body)
	return caught.value.diagnostic.render()


# -- derived signatures (13.4) ----------------------------------------------


def test_a_polynomial_kernel_derives_a_crc_signature() -> None:
	"""Appended parity over data left verbatim: systematic, fixed expansion."""
	decl = only("codec crc32 { kernel = polynomial(width = 32, poly = 0x04C11DB7); }")

	assert decl.expansion is ast.Expansion.FIXED_ADD
	assert decl.expansion_add == 4
	assert decl.systematic
	assert decl.seekable is ast.Seekable.LINEAR
	assert not decl.invertible		# a digest cannot be undone
	assert decl.deterministic


def test_a_table_kernel_derives_an_exact_ratio() -> None:
	"""Every symbol the same width, so output position is linear in input."""
	decl = only("codec manchester { kernel = table(input_bits = 1, output_bits = 2); }")

	assert decl.expansion is ast.Expansion.RATIO_EXACT
	assert decl.ratio == (2, 1)
	assert decl.granularity is ast.Granularity.BIT
	assert decl.seekable is ast.Seekable.LINEAR
	assert not decl.systematic		# the input does not appear verbatim


def test_a_shift_register_derives_its_properties_from_the_feedback_source() -> None:
	"""The whole derivation, and the one worth having.

	Feedback from the input is an additive scrambler: startable anywhere, and a
	corrupt bit spoils only itself. Feedback from the output is neither, and a
	signature that did not distinguish them would promise seekability that is
	not there.
	"""
	additive = only("codec add { kernel = shift_register(taps = 5, "
	                "feedback = input); }")
	assert additive.seekable is ast.Seekable.LINEAR
	assert not additive.error_propagating

	multiplicative = only("codec mul { kernel = shift_register(taps = 5, "
	                      "feedback = output); }")
	assert multiplicative.seekable is ast.Seekable.NONE
	assert multiplicative.error_propagating


def test_a_shift_register_must_say_where_its_feedback_comes_from() -> None:
	rendered = refusal("codec s { kernel = shift_register(taps = 5); }")
	assert "does not say where its feedback comes from" in rendered
	assert "seekable and self-synchronising" in rendered


def test_a_linear_block_is_systematic_only_in_standard_form() -> None:
	plain = only("codec h { kernel = linear_block(n = 7, k = 4); }")
	assert not plain.systematic
	assert plain.ratio == (7, 4)

	standard = only("codec h { kernel = linear_block(n = 7, k = 4, "
	                "standard_form); }")
	assert standard.systematic


def test_a_block_code_may_not_shrink() -> None:
	assert "a block code cannot shrink" in refusal(
		"codec h { kernel = linear_block(n = 4, k = 7); }")


def test_a_permutation_is_seekable_but_not_in_order() -> None:
	decl = only("codec inter { kernel = permutation(span = 16); }")
	assert decl.seekable is ast.Seekable.PERMUTED
	assert decl.expansion is ast.Expansion.PRESERVING


def test_stuffing_loses_interior_addressing() -> None:
	decl = only("codec cobs { kernel = stuffing(worst_case = 255, per = 254); }")
	assert decl.expansion is ast.Expansion.RATIO_BOUNDED
	assert decl.ratio == (255, 254)
	assert decl.seekable is ast.Seekable.NONE


def test_a_polynomial_width_must_be_a_whole_number_of_bytes() -> None:
	rendered = refusal("codec c { kernel = polynomial(width = 12, poly = 3); }")
	assert "not a whole number of bytes" in rendered


def test_an_unknown_kernel_family_lists_the_ones_there_are() -> None:
	rendered = refusal("codec c { kernel = wishful(x = 1); }")
	assert "unknown kernel family `wishful`" in rendered
	assert "`polynomial`" in rendered and "`stuffing`" in rendered


def test_a_kernel_argument_that_is_missing_says_which() -> None:
	rendered = refusal("codec c { kernel = table(input_bits = 4); }")
	assert "needs `output_bits`" in rendered


# -- declarations must agree with the kernel --------------------------------


def test_a_declaration_that_contradicts_its_kernel_is_refused() -> None:
	"""One of the two is wrong, and preferring either would hide which."""
	rendered = refusal("""codec c {
		kernel = table(input_bits = 1, output_bits = 2);
		systematic;
	}
	""")
	assert "declares `systematic` but its kernel implies `not systematic`" in rendered
	assert "one of the two being wrong" in rendered


def test_a_declaration_that_agrees_with_its_kernel_is_accepted() -> None:
	"""Saying it twice is redundant, not wrong."""
	decl = only("""codec c {
		kernel = table(input_bits = 1, output_bits = 2);
		seekable = linear;
		invertible;
	}
	""")
	assert decl.ratio == (2, 1)


# -- pipelines (13.4) -------------------------------------------------------


PIPELINE = """codec rs { kernel = polynomial(width = 256); }
codec inter { kernel = permutation(span = 16); }
codec manchester { kernel = table(input_bits = 1, output_bits = 2); }
codec framed = rs |> inter |> manchester;
"""


def test_a_pipeline_takes_the_weakest_seekability() -> None:
	"""Pointwise and conservative: a pipeline claiming more than its weakest
	stage would be a signature that lies, and the lattice believes signatures."""
	assert codecs(PIPELINE)["framed"].seekable is ast.Seekable.PERMUTED


def test_a_pipeline_is_systematic_only_if_every_stage_is() -> None:
	found = codecs(PIPELINE)
	assert found["rs"].systematic
	assert not found["framed"].systematic


def test_a_pipeline_propagates_errors_if_any_stage_does() -> None:
	found = codecs("""codec a { kernel = shift_register(taps = 3, feedback = output); }
	codec b { kernel = permutation(span = 4); }
	codec both = a |> b;
	""")
	assert found["both"].error_propagating


def test_appended_parity_is_scaled_by_what_follows_it() -> None:
	"""The spec's own example, and the case that needed the vocabulary widened.

	`rs |> inter |> manchester` appends 32 bytes of parity and then doubles
	all of it, so the composed expansion is 2:1 *and* 64 bytes. Section 13.2
	offers those as alternatives; a pipeline needs both at once
	(docs/decisions/0016-composed-expansion.md).
	"""
	framed = codecs(PIPELINE)["framed"]

	assert framed.expansion is ast.Expansion.RATIO_EXACT
	assert framed.ratio == (2, 1)
	assert framed.expansion_add == 64


def test_a_bounded_ratio_anywhere_makes_the_pipeline_bounded() -> None:
	found = codecs("""codec cobs { kernel = stuffing(worst_case = 255, per = 254); }
	codec manchester { kernel = table(input_bits = 1, output_bits = 2); }
	codec both = manchester |> cobs;
	""")
	assert found["both"].expansion is ast.Expansion.RATIO_BOUNDED


def test_a_pipeline_of_one_stage_is_refused() -> None:
	assert "pipeline of one stage" in refusal(
		"codec a { length_preserving; }\ncodec b = a;")


def test_a_pipeline_may_not_name_another_pipeline() -> None:
	rendered = refusal("""codec a { length_preserving; }
	codec b { length_preserving; }
	codec inner = a |> b;
	codec outer = inner |> a;
	""")
	assert "names the pipeline `inner` as a stage" in rendered


def test_a_pipeline_naming_an_unknown_stage_lists_the_known_ones() -> None:
	rendered = refusal("codec a { length_preserving; }\ncodec b = a |> nope;")
	assert "unknown stage `nope`" in rendered


def test_kernels_and_pipelines_round_trip() -> None:
	"""A derived signature unparses as the properties it derived.

	The kernel is the source and the properties are its consequence, so a
	round-trip through source has to preserve the consequence. It does, because
	unparse writes the filled-in signature rather than the kernel.
	"""
	first = parse_text("endian big;\n" + PIPELINE)
	again = parse_text(unparse(first))

	composed = {decl.name: decl for decl in again.codecs()}["framed"]
	assert composed.ratio == (2, 1)
	assert composed.seekable is ast.Seekable.PERMUTED


# -- the lattice is untouched (26.12) ---------------------------------------


def test_no_propagation_rule_reads_a_kernel() -> None:
	"""Section 26.12: no propagation rule changes in this phase.

	It holds because the property signature is the only interface (13.1). If a
	row ever reached past it to the kernel, tier 2 would have stopped being
	purely additive and this test is where that shows up.
	"""
	import inspect

	from situc import propagate

	source = inspect.getsource(propagate)
	assert "kernel" not in source
	assert "KernelFamily" not in source


def test_a_derived_codec_reaches_the_lattice_as_any_other_would() -> None:
	"""Swapping a declaration for a kernel changes no capability."""
	declared = """codec c {
		expansion = ratio_exact(2, 1);
		granularity = bit(1);
		seekable = linear;
		invertible;
		deterministic;
	}
	"""
	kernelled = "codec c { kernel = table(input_bits = 1, output_bits = 2); }\n"
	body = """struct s { coded body(c) { u16 inner; } }
	require size(s) == 4;
	"""

	def vectors(preamble: str) -> dict[str, str]:
		schema   = parse_text("endian big;\n" + preamble + body)
		resolved = resolve(schema, solve(schema))
		return {entry.placement.path: str(entry.vector.items())
		        for struct in resolved.structs.values()
		        for entry in struct.entries}

	assert vectors(declared) == vectors(kernelled)


# -- generated implementations (26.12) --------------------------------------


DERIVED = """codec crc32 {
	kernel = polynomial(width = 32, poly = 0x04C11DB7, init = 0xFFFFFFFF,
	                    xorout = 0xFFFFFFFF, reflect);
}
impl crc32 derived;

codec crc16_ccitt {
	kernel = polynomial(width = 16, poly = 0x1021, init = 0xFFFF);
}
impl crc16_ccitt derived;

codec manchester {
	kernel = table(input_bits = 1, output_bits = 2, code = manchester_802_3);
}
impl manchester derived;

struct s { u8 a; }
"""


def test_a_derived_binding_generates_an_implementation() -> None:
	emitted = derived.generate(parse_text("endian big;\n" + DERIVED), "unit")

	assert "uint32_t situ_crc32(const uint8_t *data, uint32_t len)" in emitted
	assert "situ_crc32_table[256]" in emitted
	assert "computed from the polynomial, not copied" in emitted


def test_an_ungenerated_family_says_so_rather_than_emitting_nothing() -> None:
	emitted = derived.generate(parse_text(
		"endian big;\n"
		"codec c { kernel = stuffing(worst_case = 255, per = 254); }\n"
		"impl c derived;\nstruct s { u8 a; }\n"), "unit")

	assert "No implementation for `c`" in emitted
	assert "properties are derived and correct" in emitted


@pytest.mark.skipif(HOST_CC is None, reason="no host compiler")
def test_the_generated_crcs_match_the_published_check_values(tmp_path: Path) -> None:
	"""The acceptance criterion: vectors from an independent reference.

	0xCBF43926 and 0x29B1 are the CRC catalogue's check values for the string
	"123456789". Testing against them is what makes this an implementation of
	CRC-32 rather than an implementation of whatever this file happens to do.
	"""
	schema   = parse_text("endian big;\n" + DERIVED)
	resolved = resolve(schema, solve(schema))
	built    = generate(schema, resolved, "unit")

	(tmp_path / "unit.h").write_text(built.header, encoding="ascii")
	(tmp_path / "unit.c").write_text(built.source, encoding="ascii")
	(tmp_path / "unit_derived.c").write_text(
		derived.generate(schema, "unit"), encoding="ascii")
	(tmp_path / "probe.c").write_text(
		'#include "unit.h"\n'
		"uint32_t situ_crc32(const uint8_t *data, uint32_t len);\n"
		"uint16_t situ_crc16_ccitt(const uint8_t *data, uint32_t len);\n"
		"uint32_t situ_manchester_encode(const uint8_t *in, uint32_t bits,\n"
		"                                uint8_t *out);\n"
		"uint32_t situ_manchester_decode(const uint8_t *in, uint32_t bits,\n"
		"                                uint8_t *out);\n"
		"int main(void)\n"
		"{\n"
		'\tconst uint8_t check[9] = "123456789";\n'
		"\tconst uint8_t plain[1] = { 0xB0u };\n"
		"\tuint8_t coded[2] = { 0u, 0u };\n"
		"\tuint8_t back[1] = { 0u };\n"
		"\n"
		"\tif (situ_crc32(check, 9u) != 0xCBF43926u) { return 1; }\n"
		"\tif (situ_crc16_ccitt(check, 9u) != 0x29B1u) { return 2; }\n"
		"\n"
		"\t/* IEEE 802.3: a zero is 01 and a one is 10. */\n"
		"\tif (situ_manchester_encode(plain, 8u, coded) != 16u) { return 3; }\n"
		"\tif (coded[0] != 0x9Au || coded[1] != 0x55u) { return 4; }\n"
		"\tif (situ_manchester_decode(coded, 16u, back) != 8u) { return 5; }\n"
		"\tif (back[0] != 0xB0u) { return 6; }\n"
		"\treturn 0;\n"
		"}\n", encoding="ascii")

	subprocess.run(
		[HOST_CC or "cc", *WARNINGS, f"-I{RUNTIME}", f"-I{tmp_path}",
		 str(tmp_path / "probe.c"), str(tmp_path / "unit_derived.c"),
		 str(tmp_path / "unit.c"),
		 str(ROOT / "build" / "host" / "runtime" / "libsitu.a"),
		 "-o", str(tmp_path / "run")],
		check=True, capture_output=True, text=True)

	result = subprocess.run([str(tmp_path / "run")], capture_output=True)
	assert result.returncode == 0, f"check {result.returncode} failed"


# -- the acceptance criterion (26.12) ---------------------------------------


PROPERTIES = ("expansion", "expansion_add", "ratio", "seekable", "granularity",
	"granularity_size", "systematic", "invertible", "deterministic",
	"error_propagating")


def _library(path: Path) -> dict[str, ast.CodecDecl]:
	from situc.diagnostics import Source
	from situc.parser import parse

	schema = parse(Source(str(path), path.read_text(encoding="ascii")))
	return {decl.name: decl for decl in schema.codecs()}


def test_derived_properties_match_the_hand_written_library() -> None:
	"""Section 26.12's acceptance, per family.

	`std/codecs.situ` is the tier-1 library: every signature hand-written from
	somebody reading the standard. `std/kernels.situ` describes the same codes
	as kernels and derives the signatures. Where a code appears in both, every
	one of the nine properties must agree.

	This is the test that says the derivation is right rather than merely
	self-consistent, and it earned its keep: it caught three families deriving
	the wrong granularity and one claiming an error propagation that a block
	code does not have.
	"""
	hand = _library(ROOT / "std" / "codecs.situ")
	auto = _library(ROOT / "std" / "kernels.situ")
	shared = sorted(set(hand) & set(auto))

	assert len(shared) >= 7, f"only {shared} overlap; the check is not exercised"

	for name in shared:
		for prop in PROPERTIES:
			assert getattr(auto[name], prop) == getattr(hand[name], prop), (
				f"{name}.{prop}: kernel derives {getattr(auto[name], prop)}, "
				f"the hand-written signature says {getattr(hand[name], prop)}")


def test_the_kernel_library_covers_every_family() -> None:
	"""A family with no entry is a derivation nobody has ever run."""
	auto = _library(ROOT / "std" / "kernels.situ")
	families = {decl.kernel.family for decl in auto.values()
	            if decl.kernel is not None}

	assert families == set(ast.KernelFamily)


# -- the remaining four families (26.12) ------------------------------------


REMAINING = """codec inter    { kernel = permutation(rows = 4, columns = 8); }
codec hamming  { kernel = linear_block(n = 7, k = 4, standard_form,
                                       code = hamming_7_4); }
codec additive { kernel = shift_register(taps = 0xB400, width = 16,
                                         seed = 0xACE1, feedback = input); }
codec selfsync { kernel = shift_register(taps = 0x8810, width = 16,
                                         seed = 0xFFFF, feedback = output); }
codec cobs     { kernel = stuffing(worst_case = 255, per = 254, code = cobs); }
codec hdlc     { kernel = stuffing(worst_case = 6, per = 5, unit = bit,
                                   code = hdlc); }

impl inter derived;
impl hamming derived;
impl additive derived;
impl selfsync derived;
impl cobs derived;
impl hdlc derived;

struct s { u8 a; }
"""


def test_every_family_generates_an_implementation() -> None:
	emitted = derived.generate(parse_text("endian big;\n" + REMAINING), "unit")

	for name in ("situ_inter_encode", "situ_hamming_encode",
	             "situ_additive_encode", "situ_selfsync_encode",
	             "situ_cobs_encode", "situ_hdlc_encode"):
		assert f"{name}(" in emitted, f"{name} was not generated"


def test_the_two_scramblers_differ_in_the_generated_code() -> None:
	"""The same family and the same shape of kernel, and opposite code.

	An additive scrambler is its own inverse, so its decoder calls its encoder.
	A multiplicative one is not, so it has a decoder of its own that shifts in
	what it received rather than what it produced.
	"""
	emitted = derived.generate(parse_text("endian big;\n" + REMAINING), "unit")

	assert "return situ_additive_encode(in, len, out);" in emitted
	assert "Its own inverse" in emitted
	assert "Not its own inverse" in emitted
	assert "makes a receiver self-synchronising" in emitted


def test_an_interleaver_without_a_shape_derives_but_does_not_generate() -> None:
	"""`span` says how far a permutation reaches, not which permutation it is.

	Enough for the properties, not enough for the code -- and saying so beats
	generating an identity that silently interleaves nothing.
	"""
	emitted = derived.generate(parse_text(
		"endian big;\n"
		"codec inter { kernel = permutation(span = 16); }\n"
		"impl inter derived;\nstruct s { u8 a; }\n"), "unit")

	assert "No implementation for `inter`" in emitted


def test_an_interleaver_of_one_row_is_refused() -> None:
	assert "an interleaver of one row is the identity" in refusal(
		"codec i { kernel = permutation(rows = 1, columns = 8); }")


def test_the_hamming_syndrome_table_is_computed_not_transcribed() -> None:
	"""Every syndrome accuses a different bit, which is what makes the code
	correcting rather than merely detecting. Checked here because a transcribed
	table is exactly the kind of thing that is wrong in one entry."""
	emitted = derived.generate(parse_text("endian big;\n" + REMAINING), "unit")
	body    = emitted.partition("situ_hamming_syndrome[8] = {")[2]
	line    = body.partition("};")[0]

	accused = [int(part.strip().rstrip("u")) for part in line.split(",")
	           if part.strip().rstrip("u").isdigit()]

	# Seven bit positions plus the no-error entry, each named once.
	assert sorted(accused) == sorted(list(range(7)) + [7])


def test_a_two_family_pipeline_composes_conservatively() -> None:
	"""26.12's third acceptance criterion, per family.

	Interleaving then stuffing: the interleaver is permuted and the stuffing is
	not seekable at all, so the pipeline is not seekable. The expansion becomes
	the stuffing's bounded ratio, because a bounded stage anywhere makes the
	product bounded.
	"""
	found = codecs("""codec inter { kernel = permutation(rows = 4, columns = 4); }
	codec cobs { kernel = stuffing(worst_case = 255, per = 254, code = cobs); }
	codec framed = inter |> cobs;
	""")["framed"]

	assert found.seekable is ast.Seekable.NONE
	assert found.expansion is ast.Expansion.RATIO_BOUNDED
	assert found.ratio == (255, 254)
	assert not found.systematic
	assert found.invertible


def test_a_hamming_and_interleaver_pipeline_keeps_the_ratio() -> None:
	"""The pairing this exists for: a block code spreads by an exact ratio and
	an interleaver moves the bytes without adding any, so the codeword
	expansion survives and only the seekability weakens."""
	found = codecs("""codec hamming { kernel = linear_block(n = 7, k = 4,
		standard_form, code = hamming_7_4); }
	codec inter { kernel = permutation(rows = 4, columns = 4); }
	codec coded = hamming |> inter;
	""")["coded"]

	assert found.expansion is ast.Expansion.RATIO_EXACT
	assert found.ratio == (7, 4)
	assert found.seekable is ast.Seekable.PERMUTED
	assert not found.systematic	# the interleaver moves the data bits


def test_a_code_name_that_names_two_codes_is_refused() -> None:
	"""`manchester` is two codes. IEEE 802.3's and G.E. Thomas's are called by
	the same name and are bit-inverses of each other, so a decoder built on the
	wrong one returns the complement of what was sent -- plausible bytes, no
	error, and nothing at run time that could notice.

	Invariant 9: situ never takes a silent default where the wrong choice is
	undetectable at run time. The compiler had one anyway, in a comment beside
	the table: "Manchester is IEEE 802.3's". A schema saying `manchester` got
	that and no way to ask for the other.

	Found by reading `rflab`, a radio project that makes the same choice a
	compile-time option -- which is the evidence that a practitioner has to
	make it."""
	rendered = refusal("codec m { kernel = table(input_bits = 1,"
	                   " output_bits = 2, code = manchester); }")

	assert "names 2 codes" in rendered
	assert "manchester_802_3" in rendered
	assert "manchester_thomas" in rendered


def test_the_two_manchesters_are_inverses() -> None:
	"""One table is the other's complement, which is the whole of the
	difference and the reason the name has to say which."""
	from situc.codegen.c.derived import NAMED_CODES

	thomas = NAMED_CODES["manchester_thomas"]
	ethernet = NAMED_CODES["manchester_802_3"]

	assert [~code & 0b11 for code in ethernet] == thomas


def test_the_kernel_library_binds_every_family_it_can_generate() -> None:
	"""A family that generates and is not bound anywhere is a generator nobody
	has ever run over a real description."""
	auto  = _library(ROOT / "std" / "kernels.situ")
	bound = {decl.name for decl in auto.values()}

	emitted = derived.generate(
		parse((ROOT / "std" / "kernels.situ")), "kernels")

	# Both Manchesters: they are two codes, and the library names them apart
	# because a receiver built on one reads a sender built on the other as the
	# complement of what was sent.
	for name in ("crc32", "manchester_802_3", "manchester_thomas",
	             "cobs", "hamming_7_4",
	             "interleave_16", "scrambler_additive",
	             "scrambler_multiplicative", "hdlc_bit_stuffing"):
		assert name in bound
		assert f"situ_{name}" in emitted, f"{name} is bound but generates nothing"


def parse(path: Path):		# type: ignore[no-untyped-def]
	from situc.diagnostics import Source
	from situc.parser import parse as parse_source

	return parse_source(Source(str(path), path.read_text(encoding="ascii")))


# -- ratio_padded (section 13.2) ---------------------------------------------


def test_a_padded_table_derives_ratio_padded() -> None:
	"""base64 is a table code whose group is three bytes, and an exact ratio
	cannot say that: it would predict two characters for one byte, where the
	answer is four."""
	derived = only("""codec c {
		kernel = table(input_bits = 6, output_bits = 8, code = base64, pad = 0x3D);
	}
	""")

	assert derived.expansion is ast.Expansion.RATIO_PADDED
	assert derived.ratio == (8, 6)
	assert derived.granularity is ast.Granularity.BLOCK
	assert derived.granularity_size == 3		# lcm(8, 6) bits = 3 bytes


def test_the_group_follows_from_the_ratio() -> None:
	"""It is not declared: a group is the smallest run of input that is both a
	whole number of bytes and a whole number of symbols."""
	base32 = only("""codec c {
		kernel = table(input_bits = 5, output_bits = 8, code = base32, pad = 0x3D);
	}
	""")

	assert base32.granularity_size == 5		# lcm(8, 5) bits = 5 bytes


def test_an_unpadded_table_is_still_exact() -> None:
	"""base16 needs no padding at any length, so nothing about it changes."""
	derived = only("""codec c {
		kernel = table(input_bits = 4, output_bits = 8, code = base16);
	}
	""")

	assert derived.expansion is ast.Expansion.RATIO_EXACT
	assert derived.granularity is ast.Granularity.SYMBOL
