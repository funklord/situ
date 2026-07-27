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
	kernel = table(input_bits = 1, output_bits = 2, code = manchester);
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
