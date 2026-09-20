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

import base64
from collections import Counter
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable, cast

import pytest

from situc import ast, kernels, traverse
from situc.codegen.c import derived, generate
from situc.codegen.python import derived as py_derived
from situc.codegen.rust import derived as rs_derived
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
	# BITS since 0046, because a five-bit CRC adds five and `width // 8` for
	# one is zero. A 32-bit CRC adds 32, and the map still renders `+4`.
	assert decl.expansion_add == 32
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


def test_complementing_the_feedback_changes_no_derived_property() -> None:
	"""The flag says which bit causes a transition, and nothing else.

	It is one xor on the term the register hands to the data, so it changes
	which bits come out and none of where they can be read from. If it ever
	moved a property, a schema would be choosing a convention and silently
	buying a different capability vector with it -- so all nine are compared
	rather than the two the feedback source is known to set.
	"""
	plain = only("codec mul { kernel = shift_register(taps = 1, width = 1, "
	             "seed = 0, feedback = output); }")
	inverted = only("codec mul { kernel = shift_register(taps = 1, width = 1, "
	                "seed = 0, feedback = output, complement_feedback); }")

	for prop in PROPERTIES:
		assert getattr(plain, prop) == getattr(inverted, prop), prop


def test_an_additive_scrambler_may_not_complement_its_feedback() -> None:
	"""The flag exists for the differential convention, and says so.

	Complementing an additive keystream is well defined -- it inverts every
	output bit -- and no protocol here names one, so accepting it would emit a
	generator nothing in this repository ever runs. The refusal says what
	would have been accepted instead, which is the lesson 26.137 records: a
	refusal that does not name the accepted spelling is read as "nothing
	works".
	"""
	rendered = refusal("codec s { kernel = shift_register(taps = 5, "
	                   "feedback = input, complement_feedback); }")
	assert "complements the feedback of an additive scrambler" in rendered
	assert "`complement_feedback` goes with `feedback = output`" in rendered
	assert "USB's NRZI" in rendered


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


def test_a_named_stuffing_code_may_not_be_given_someone_else_s_overhead() -> None:
	"""The declared pair is the signature; the implementation is fixed.

	`stuffing(worst_case = 7, per = 6, unit = bit, code = hdlc)` derived
	`ratio_bounded(7,6)` and generated a stuffer inserting after five, with
	its own comment saying five and a buffer bound of `len + len / 5`. A
	consumer sizing from the signature would allocate for a code it was not
	linking, which is the disagreement between signature and object file that
	a derived codec exists to make impossible.

	The refusal names the pair the code does have rather than only rejecting
	the one it was given, so a reader learns the boundary instead of
	inferring it.
	"""
	rendered = refusal("codec c { kernel = stuffing(worst_case = 7, per = 6, "
	                   "unit = bit, code = hdlc); }")
	assert "declares an overhead `hdlc` does not have" in rendered
	assert "`hdlc` is 6 for 5, not 7 for 6" in rendered
	assert "worst_case = 6, per = 5" in rendered


def test_an_unnamed_stuffing_code_keeps_the_overhead_it_declares() -> None:
	"""Nothing is generated for it, so nothing can disagree with it.

	The check above must not become a rule that every stuffing kernel states
	one of five known ratios: a code this build has no implementation for is
	a signature and only a signature, and section 13.1 permits that.
	"""
	decl = only("codec c { kernel = stuffing(worst_case = 9, per = 4, "
	            "code = something_else); }")
	assert decl.ratio == (9, 4)


def test_a_kernel_argument_the_family_does_not_read_is_refused() -> None:
	"""A misspelled argument was ignored, and its value replaced by a default.

	Most of them are harmless: `input_bits` has no default, so a table kernel
	missing one already fails. `seed` is the expensive case, because it has a
	default and nothing downstream measures it.

	    shift_register(taps = 0xB400, width = 16, sed = 0xACE1,
	                   feedback = input)

	compiled and generated a scrambler starting at 0xFFFF rather than 0xACE1
	-- a different keystream, one character apart. `std/kernels.situ` says in
	so many words why nothing later can catch it: a mistyped tap shows up as
	a short period, and the seed "is not checkable the same way -- a wrong one
	produces a wrong keystream with no property to catch it". The argument
	name is the only place it is catchable.
	"""
	rendered = refusal("codec s { kernel = shift_register(taps = 0xB400, "
	                   "width = 16, sed = 0xACE1, feedback = input); }")
	assert "passes `sed` to a `shift_register` kernel" in rendered
	assert "did you mean `seed`?" in rendered
	assert "`seed`" in rendered and "`taps`" in rendered


def test_a_table_map_given_outright_generates() -> None:
	"""`symbol = ...`, repeated once per input symbol, instead of `code = x`.

	This form is implemented and documented -- `_symbol_map` reads it as
	`arg.name == "symbol"` -- and until this test nothing exercised it: no
	committed schema gives a map outright, and no test did either. That is
	how closing the argument vocabulary refused it without anything going
	red. The sweep below reads committed schemas, and the schemas do not use
	it; the helper-call scan that built the vocabulary looks for
	`argument("x")` and `flag("x")`, and this is the one place a family
	filters `kernel.args` itself.

	So the gap was a *form* rather than a name, and a vocabulary built by
	scanning for names cannot see one. What covers it is a test that writes
	the form.
	"""
	schema = parse_text("endian big;\n"
		"codec c { kernel = table(input_bits = 1, output_bits = 2,\n"
		"                         symbol = 0b01, symbol = 0b10); }\n"
		"impl c derived;\nstruct t { u8 a; }\n")
	emitted = derived.generate(schema, "u")

	assert "situ_c_encode" in emitted, "a map given outright generated nothing"
	assert "situ_c_encode_table[2] = {\n\t0x1u, 0x2u\n}" in emitted, emitted


def test_the_argument_vocabulary_covers_every_kernel_in_the_tree() -> None:
	"""The whitelist is a claim, so it is held to the schemas rather than to
	the code it was read out of.

	It was built from the `argument()` and `flag()` calls in this module and
	in the C generator together, which is two places and therefore two chances
	to miss one. A name only the generator reads -- `poly`, `seed`, `init` --
	is still one a schema legitimately writes, and leaving those out would
	have refused every CRC in the standard kernels. This asks the schemas
	instead: whatever they write, the vocabulary must already contain.
	"""
	from situc.kernels import KERNEL_ARGUMENTS

	seen = 0
	for path in sorted(ROOT.glob("std/*.situ")) + sorted(
			ROOT.glob("example/*/*.situ")) + sorted(
			ROOT.glob("test/schema/*.situ")):
		schema = parse(path)
		for decl in schema.codecs():
			if decl.kernel is None:
				continue
			known = KERNEL_ARGUMENTS[decl.kernel.family]
			for arg in decl.kernel.args:
				seen += 1
				assert arg.name in known, (
					f"{path.name}: `{decl.name}` writes `{arg.name}` to a "
					f"`{decl.kernel.family.value}` kernel and the vocabulary "
					f"does not have it")

	assert seen >= 100, (
		f"only {seen} kernel arguments found across the committed schemas; "
		f"this guard is reading the wrong files and an empty sweep passes "
		f"as loudly as a real one")


def test_a_polynomial_width_may_be_any_width_a_word_holds() -> None:
	"""Whole bytes only until 0046, and the refusal said "a checksum is
	appended as bytes".

	That is true of a code applied to a REGION and not of the algorithm, and
	the two are different questions: `traverse.region_extent` carries the
	bytes rule now, because a region's size program is byte-valued. USB's
	five-bit CRC, CAN's fifteen and MMC's seven were all refused by a rule
	about appending.
	"""
	decl = only("codec c { kernel = polynomial(width = 12, poly = 3); }")
	assert decl.expansion_add == 12		# bits, and not a whole byte count


def test_a_polynomial_wider_than_a_word_is_refused() -> None:
	"""The ceiling that replaced it, and it is not theoretical: `accumulator`
	picks the next word up from 8, 16, 32 and 64, so a wider one has nothing
	to hold it and would have raised at generation time rather than here.

	It caught a fixture in this very file -- `polynomial(width = 256)` named
	`rs`, standing in for Reed-Solomon, which no register holds."""
	rendered = refusal("codec c { kernel = polynomial(width = 65, poly = 3); }")
	assert "wider than 64 bits" in rendered


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


# `rs` is an actual Reed-Solomon now. It was `polynomial(width = 256)` --
# a 256-bit CRC wearing the name -- which no register holds: `accumulator`
# picks the next word up from 8, 16, 32, 64 and would have raised on it, so
# the fixture was only ever safe because nothing generated from it. 0046's
# width ceiling caught it. The parity is 32 bytes either way, so every
# number these tests assert is unchanged.
PIPELINE = """codec rs { kernel = polynomial(field = 256, n = 255, k = 223); }
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
	(doc/decision/0016-composed-expansion.md).

	Counted in BITS since 0046: 256 of parity, doubled to 512, which is the
	same 64 bytes the map renders and has always rendered.
	"""
	framed = codecs(PIPELINE)["framed"]

	assert framed.expansion is ast.Expansion.RATIO_EXACT
	assert framed.ratio == (2, 1)
	assert framed.expansion_add == 512		# bits: 64 bytes


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

codec crc24_ble {
	kernel = polynomial(width = 24, poly = 0x00065B, init = 0x555555, reflect);
}
impl crc24_ble derived;

codec crc40_gsm {
	kernel = polynomial(width = 40, poly = 0x0004820009,
	                    xorout = 0xFFFFFFFFFF);
}
impl crc40_gsm derived;

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


def test_a_declining_family_names_what_would_have_been_accepted() -> None:
	"""26.137's rule, held to every family that can decline rather than one.

	A refusal that does not say what *would* have worked gets read as
	"nothing works": decision 0017 recorded that `stuffing` "returns no
	implementation for any input at all" when three codes generated then and
	six do now, because the comment said only "described but not yet
	generated". The fix named the codes -- and named them for `stuffing`
	alone, leaving `table` and `linear_block` saying the old sentence to
	anyone who met them. That the test above checks the message exists and
	not what it says is how the second half stayed uncovered.

	The catalogues come from the generator rather than being listed here, so
	a ninth table code has to appear in the message the day it is added.
	"""
	from situc.codegen.c.derived import (DERIVED_LINEAR, DERIVED_STUFFING,
	                                     NAMED_CODES)

	families = {
		"table": ("kernel = table(input_bits = 1, output_bits = 2, "
		          "code = wishful);", sorted(NAMED_CODES)),
		"stuffing": ("kernel = stuffing(worst_case = 2, per = 1, "
		             "code = wishful);", list(DERIVED_STUFFING)),
		"linear_block": ("kernel = linear_block(n = 15, k = 11, "
		                 "code = wishful);", list(DERIVED_LINEAR)),
	}

	for family, (kernel, catalogue) in families.items():
		emitted = derived.generate(parse_text(
			f"endian big;\ncodec c {{ {kernel} }}\n"
			f"impl c derived;\nstruct s {{ u8 a; }}\n"), "unit")

		assert "No implementation for `c`" in emitted, family
		assert "`wishful` is not one of them" in emitted, family
		assert catalogue, f"{family}: an empty catalogue names nothing"
		for code in catalogue:
			assert f"`{code}`" in emitted, (
				f"{family}: the message does not name `{code}`, which it "
				f"generates")

	# `permutation` declines for a different reason -- a bare `span` is an
	# extent without a mapping -- so it names the form rather than a code.
	emitted = derived.generate(parse_text(
		"endian big;\ncodec c { kernel = permutation(span = 8); }\n"
		"impl c derived;\nstruct s { u8 a; }\n"), "unit")
	assert "No implementation for `c`" in emitted
	assert "`rows` and `columns`" in emitted


@pytest.mark.skipif(HOST_CC is None, reason="no host compiler")
def test_the_generated_crcs_match_the_published_check_values(tmp_path: Path) -> None:
	"""The acceptance criterion: vectors from an independent reference.

	0xCBF43926, 0x29B1, 0xC25A56 and 0xD4164FC646 are the CRC catalogue's check
	values for
	the string "123456789". Testing against them is what makes this an
	implementation of CRC-32 rather than an implementation of whatever this
	file happens to do.

	CRC-24/BLE is here because it is two firsts at once. It is the first width
	that is not a C word -- twenty-four bits held in a `uint32_t` and masked
	after every shift -- and the first *reflected* CRC whose initial value is
	not its own bit-reverse. A reflected register starts reversed, and this
	emitted it as written; CRC-32 and CRC-16/MODBUS both start all-ones, so
	every check value in this file agreed with the bug. It came out as
	0xD39857.

	CRC-40/GSM is the same point on the other side of thirty-two: forty bits
	in a `uint64_t`, which is where a mask that was written for narrowing
	would go wrong the other way."""
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
		"uint32_t situ_crc24_ble(const uint8_t *data, uint32_t len);\n"
		"uint64_t situ_crc40_gsm(const uint8_t *data, uint32_t len);\n"
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
		"\tif (situ_crc24_ble(check, 9u) != 0xC25A56u) { return 7; }\n"
		"\tif (situ_crc40_gsm(check, 9u) != 0xD4164FC646u) { return 8; }\n"
		"\n"
		"\t/* IEEE 802.3: a zero is 01 and a one is 10. */\n"
		"\tif (situ_manchester_encode(plain, 8u, coded) != 16u) { return 3; }\n"
		"\tif (coded[0] != 0x9Au || coded[1] != 0x55u) { return 4; }\n"
		"\tif (situ_manchester_decode(coded, 16u, back) != 8u) { return 5; }\n"
		"\tif (back[0] != 0xB0u) { return 6; }\n"
		"\treturn 0;\n"
		"}\n", encoding="ascii")

	compiled = subprocess.run(
		[HOST_CC or "cc", *WARNINGS, f"-I{RUNTIME}", f"-I{tmp_path}",
		 str(tmp_path / "probe.c"), str(tmp_path / "unit_derived.c"),
		 str(tmp_path / "unit.c"),
		 str(ROOT / "build" / "host" / "runtime" / "libsitu.a"),
		 "-o", str(tmp_path / "run")],
		capture_output=True, text=True)
		# `check=True` raised a CalledProcessError whose message is the
		# command and not the compiler's reason, so a CI failure said
		# "exit status 1" and nothing else -- which is what three runs of
		# diagnosing this from a distance cost. The compiler's own words
		# are the whole value of a compile gate (26.87).
	assert compiled.returncode == 0, compiled.stderr

	result = subprocess.run([str(tmp_path / "run")], capture_output=True)
	assert result.returncode == 0, f"check {result.returncode} failed"


# -- the shift register at widths that are not C words (26.12) --------------


SCRAMBLERS = """codec scr24 {
	kernel = shift_register(taps = 0x80000D, width = 24, seed = 0x555555,
	                        feedback = input);
}
impl scr24 derived;

codec scr24_sync {
	kernel = shift_register(taps = 0x80000D, width = 24, seed = 0x555555,
	                        feedback = output);
}
impl scr24_sync derived;

codec scr64 {
	kernel = shift_register(taps = 0x800000000000000D, width = 64,
	                        seed = 0xACE1ACE1ACE1ACE1, feedback = input);
}
impl scr64 derived;

codec scr64_sync {
	kernel = shift_register(taps = 0x800000000000000D, width = 64,
	                        seed = 0xACE1ACE1ACE1ACE1, feedback = output);
}
impl scr64_sync derived;

struct s { u8 a; }
"""


def test_a_shift_register_generates_at_any_width_it_accepts() -> None:
	"""The emitter's widths must be the language's, not a subset of them.

	`kernels.py` validates a shift register's feedback source and never its
	width, so every width the parser takes has a derived signature. The
	emitter took 8, 16 and 32 and returned nothing for the rest, which is
	the worst shape a gap can have: the codec is accepted, its properties
	are correct, and what arrives is a comment suggesting an `extern` --
	for a code the compiler could have written. Widths 1, 4, 12, 24, 48 and
	64 were all silently ungenerated this way.

	Twenty-four and sixty-four are the two ends worth pinning. Twenty-four
	is `_accumulator`'s case, the same one CRC-24/BLE established: held in
	the next word up and masked back after the shift. Sixty-four is the
	other edge, where the accumulator *is* the width and a mask written for
	narrowing would either be a no-op or -- shifted one bit wider -- be
	undefined.
	"""
	emitted = derived.generate(parse_text("endian big;\n" + SCRAMBLERS), "unit")

	assert "No implementation for" not in emitted
	for name in ("scr24", "scr24_sync", "scr64", "scr64_sync"):
		assert f"uint32_t situ_{name}_encode(" in emitted
		assert f"uint32_t situ_{name}_decode(" in emitted

	# Only the multiplicative path shifts left, so only it can carry a bit
	# past the register's end. Twenty-four bits are masked back into their
	# `uint32_t`; sixty-four fill the word, so the type does the masking and
	# the line is left exactly as the three working widths already emitted
	# it.
	assert ("state = (uint32_t)(((uint32_t)(state << 1) | output)"
		" & (uint32_t)0xFFFFFFu);") in emitted
	assert "state = (uint64_t)((uint64_t)(state << 1) | output);" in emitted


@pytest.mark.skipif(HOST_CC is None, reason="no host compiler")
def test_the_generated_scramblers_invert_at_every_width(tmp_path: Path) -> None:
	"""Emitting is not the check -- the code has to scramble and come back.

	Additive is held to the stronger property, because the derived signature
	claims it: the register runs on its own state and the keystream is XORed
	in, so encoding twice returns the plaintext. Multiplicative is fed from
	the scrambled output and is not its own inverse, so it gets
	decode(encode(x)).

	Both are also checked against being a no-op. That is what a register
	stuck on its seed looks like, and what a mask one bit too wide would
	produce -- and either would pass a round-trip test on its own.
	"""
	schema   = parse_text("endian big;\n" + SCRAMBLERS)
	resolved = resolve(schema, solve(schema))
	built    = generate(schema, resolved, "unit")

	(tmp_path / "unit.h").write_text(built.header, encoding="ascii")
	(tmp_path / "unit_derived.c").write_text(
		derived.generate(schema, "unit"), encoding="ascii")
	(tmp_path / "probe.c").write_text(
		"#include <string.h>\n"
		'#include "unit.h"\n'
		"\n"
		"int main(void)\n"
		"{\n"
		"\tuint8_t plain[32];\n"
		"\tuint8_t coded[32];\n"
		"\tuint8_t back[32];\n"
		"\tuint32_t i;\n"
		"\n"
		"\tfor (i = 0; i < 32u; i++) { plain[i] = (uint8_t)(i * 7u + 3u); }\n"
		"\n"
		"\t/* Additive: its own inverse, so encoding twice is identity. */\n"
		"\tif (situ_scr24_encode(plain, 32u, coded) != 32u) { return 1; }\n"
		"\tif (memcmp(plain, coded, 32u) == 0) { return 2; }\n"
		"\tif (situ_scr24_encode(coded, 32u, back) != 32u) { return 3; }\n"
		"\tif (memcmp(plain, back, 32u) != 0) { return 4; }\n"
		"\n"
		"\tif (situ_scr64_encode(plain, 32u, coded) != 32u) { return 5; }\n"
		"\tif (memcmp(plain, coded, 32u) == 0) { return 6; }\n"
		"\tif (situ_scr64_encode(coded, 32u, back) != 32u) { return 7; }\n"
		"\tif (memcmp(plain, back, 32u) != 0) { return 8; }\n"
		"\n"
		"\t/* Multiplicative: fed from the scrambled side, so decoding is\n"
		"\t * its own function rather than the same one again. */\n"
		"\tif (situ_scr24_sync_encode(plain, 32u, coded) != 32u) { return 9; }\n"
		"\tif (memcmp(plain, coded, 32u) == 0) { return 10; }\n"
		"\tif (situ_scr24_sync_decode(coded, 32u, back) != 32u) { return 11; }\n"
		"\tif (memcmp(plain, back, 32u) != 0) { return 12; }\n"
		"\n"
		"\tif (situ_scr64_sync_encode(plain, 32u, coded) != 32u) { return 13; }\n"
		"\tif (memcmp(plain, coded, 32u) == 0) { return 14; }\n"
		"\tif (situ_scr64_sync_decode(coded, 32u, back) != 32u) { return 15; }\n"
		"\tif (memcmp(plain, back, 32u) != 0) { return 16; }\n"
		"\n"
		"\treturn 0;\n"
		"}\n", encoding="ascii")

	compiled = subprocess.run(
		[HOST_CC or "cc", *WARNINGS, f"-I{RUNTIME}", f"-I{tmp_path}",
		 str(tmp_path / "probe.c"), str(tmp_path / "unit_derived.c"),
		 "-o", str(tmp_path / "run")],
		capture_output=True, text=True)
	assert compiled.returncode == 0, compiled.stderr

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
codec manch    { kernel = table(input_bits = 1, output_bits = 2,
                                code = manchester_802_3); }
codec crc      { kernel = polynomial(width = 16, poly = 0x1021,
                                     init = 0xFFFF); }
codec sum16    { kernel = ones_complement(width = 16, complement); }

impl inter derived;
impl manch derived;
impl crc derived;
impl hamming derived;
impl additive derived;
impl selfsync derived;
impl cobs derived;
impl hdlc derived;
impl sum16 derived;

struct s { u8 a; }
"""


def test_every_family_generates_an_implementation() -> None:
	"""Every kernel family, read from the language rather than listed here.

	The name claimed all six and the list held four: `table` and `polynomial`
	were absent, so the two families every schema in the tree actually uses
	were the two this never asked about. That is the shape `evidence.md` calls
	a name that quantifies over a hand-written enumeration -- the assertion
	was fine, the population was short, and no assertion can fail about a
	family it does not name.

	So the families come from `ast.KernelFamily` and the codecs from the
	schema, and the two are compared. A seventh family fails here until
	something in `REMAINING` declares one.
	"""
	schema  = parse_text("endian big;\n" + REMAINING)
	emitted = derived.generate(schema, "unit")

	covered: dict[ast.KernelFamily, list[str]] = {}
	for decl in schema.codecs():
		if decl.kernel is None:
			continue
		covered.setdefault(decl.kernel.family, []).append(decl.name)

	missing = set(ast.KernelFamily) - set(covered)
	assert not missing, (
		f"{sorted(family.value for family in missing)}: no codec in "
		f"`REMAINING` declares this family, so this test cannot say whether "
		f"it generates. Add one rather than narrowing what the name claims")

	for family, names in sorted(covered.items(), key=lambda pair: pair[0].value):
		for name in names:
			assert f"No implementation for `{name}`" not in emitted, (
				f"{family.value}: `{name}` derived a signature and generated "
				f"no implementation")
			assert f"situ_{name}" in emitted, (
				f"{family.value}: nothing named `situ_{name}` was emitted")


def test_every_stuffing_code_generated_is_offered_by_the_standard_kernels(
		) -> None:
	"""`DERIVED_STUFFING` and `std/kernels.situ` say the same thing, or this
	fails in whichever direction they have come apart.

	The list above it enumerates names by hand, which is a claim about
	coverage that ages every time a code is added -- and one did age: the
	message a schema gets for a code this build cannot generate used to say
	only that a stuffing kernel was "not yet generated", so decision 0017
	recorded that stuffing generated nothing at all while three codes were
	generating. Reading both sides is what keeps the two honest.

	A code the compiler can generate and the standard kernels do not offer is
	work already done that nobody can reach. One offered and not generated
	would put a "No implementation" comment in the standard library itself.
	"""
	source = (ROOT / "std" / "kernels.situ").read_text(encoding="ascii")
	schema = parse_text(source)

	offered = {}
	for decl in schema.codecs():
		if decl.kernel is None:
			continue
		if decl.kernel.family is not ast.KernelFamily.STUFFING:
			continue
		named = decl.kernel.argument("code")
		assert isinstance(named, ast.NameRef), (
			f"{decl.name} states a stuffing kernel with no `code`, so nothing "
			f"selects which stuffing it is")
		offered[named.name] = decl.name

	assert offered, (
		"no stuffing codec found in std/kernels.situ -- this is reading the "
		"wrong thing, and an empty set passes as loudly as a real one")

	generated = set(derived.DERIVED_STUFFING)
	assert set(offered) == generated, (
		f"the standard kernels offer {sorted(offered)} and this build "
		f"generates {sorted(generated)}; "
		f"unreachable: {sorted(generated - set(offered))}, "
		f"unimplemented: {sorted(set(offered) - generated)}")

	emitted = derived.generate(schema, "kernels")
	for code, codec in sorted(offered.items()):
		assert f"situ_{codec}_encode(" in emitted, (
			f"`{code}` is in DERIVED_STUFFING and `{codec}` declares it, but "
			f"no implementation was generated")


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


# -- what the generated function counts (26.35) -----------------------------

HEAD     = "endian big;\nbit_order msb_first;\n"
PADDED   = ("codec b64 { kernel = table(input_bits = 6, output_bits = 8,"
	" code = base64, pad = 0x3D); }\nimpl b64 derived;\n")
UNPADDED = ("codec mm { kernel = table(input_bits = 1, output_bits = 2,"
	" code = manchester_802_3); }\nimpl mm derived;\n")


def test_a_padded_table_counts_bytes_and_says_so() -> None:
	"""The prototype and the definition differ in a parameter *name*, which C
	does not check: the types are the same. So `situ_base64_encode` was
	declared taking `bits` and defined walking `len` bytes, and a header a
	caller reads promised the wrong unit."""
	schema = parse_text(HEAD + PADDED)
	header = "\n".join(derived.declarations(schema, "situ"))
	body   = derived.generate(schema, "unit")

	assert "situ_b64_encode(const uint8_t *in, uint32_t len," in header
	assert "situ_b64_encode(const uint8_t *in, uint32_t len," in body


def test_an_unpadded_table_still_counts_bits() -> None:
	"""A symbol map with no padding is bit-oriented by construction, which is
	what a Manchester line code is. Only the padded loop walks whole bytes."""
	schema = parse_text(HEAD + UNPADDED)
	header = "\n".join(derived.declarations(schema, "situ"))

	assert "situ_mm_encode(const uint8_t *in, uint32_t bits," in header


def test_a_padded_region_passes_its_decoder_bytes() -> None:
	"""Not a wrong answer -- a buffer overrun. The region accessor scaled its
	length by eight for every table kernel, so a `coded body(base64)` handed
	the decoder eight times the region's bytes and it wrote eight times the
	output, past whatever the caller supplied. No schema in the tree used one,
	so nothing ran it."""
	source = (HEAD + PADDED
	          + "struct s { u8 n; coded body(b64) { u8 content[n]; } }")
	schema   = parse_text(source)
	resolved = resolve(schema, solve(schema))
	header   = generate(schema, resolved, "unit").header

	assert "situ_b64_decode(situ_s_body_ptr(view),\n\t\tencoded, out)" in header
	assert "encoded * 8u" not in header


@pytest.mark.skipif(shutil.which("cc") is None, reason="no C compiler")
def test_the_internet_checksum_matches_rfc_1071(tmp_path: Path) -> None:
	"""Three vectors from outside situ, because a checksum agreeing with
	itself is worth nothing.

	RFC 1071 section 3 sums a byte sequence by hand and gives the result;
	the IPv4 header is a real one with a published checksum; and the odd
	length is where the fiddly half of the algorithm shows -- a trailing
	byte is the HIGH half of a final word, and padding it low is right for
	every even-length test anybody writes and wrong for every odd one.

	A fourth vector is 128 KiB, and it is here because the first three did
	not guard the fold: replacing the `while` with an `if` left all three
	green. Below 65537 words a single fold is correct for every input, so
	nothing shorter can distinguish them. Trying to break the code is what
	found that -- and found, on the way, that the `uint32_t` accumulator
	this was first written with overflows one word past the same point.

	Situ could describe every CRC in practical use and had nothing to say
	about the sum on almost every packet on the internet, because it is not
	a polynomial and there was no family for it (26.245).
	"""
	schema = parse_text("endian big;\n"
	                    "codec internet_checksum {\n"
	                    "\tkernel = ones_complement(width = 16, complement);\n"
	                    "}\nimpl internet_checksum derived;\n"
	                    "struct s { u16 a; }\n")
	(tmp_path / "unit.c").write_text(derived.generate(schema, "unit"),
	                                 encoding="ascii")
	(tmp_path / "probe.c").write_text("""
#include <stdint.h>
uint16_t situ_internet_checksum(const uint8_t *data, uint32_t len);

int main(void)
{
	static const uint8_t rfc[8]  = { 0x00, 0x01, 0xf2, 0x03,
	                                 0xf4, 0xf5, 0xf6, 0xf7 };
	static const uint8_t head[20] = {
		0x45, 0x00, 0x00, 0x73, 0x00, 0x00, 0x40, 0x00,
		0x40, 0x11, 0x00, 0x00, 0xc0, 0xa8, 0x00, 0x01,
		0xc0, 0xa8, 0x00, 0xc7 };
	static const uint8_t odd[3]  = { 'a', 'b', 'c' };

	if (situ_internet_checksum(rfc,  sizeof rfc)  != 0x220du) return 1;
	if (situ_internet_checksum(head, sizeof head) != 0xb861u) return 2;
	if (situ_internet_checksum(odd,  sizeof odd)  != 0x3b9du) return 3;

	/* Two things at once, and they need different sizes, so this is the
	   larger.

	   65537 words of 0xffff sum to 0xffffffff, whose first fold carries
	   again -- below that a single `if` is correct for every input, so
	   nothing shorter distinguishes it from the `while`.

	   65538 is one word further and is where a uint32_t accumulator wraps.
	   At exactly 65537 it holds 0xffffffff and does not, so a vector sized
	   for the fold alone passes with the narrow accumulator restored. Both
	   sabotages were run; this is the size that fails under either. */
	{
		static uint8_t big[131076];
		uint32_t k;

		for (k = 0; k < sizeof big; k++) {
			big[k] = 0xffu;
		}
		if (situ_internet_checksum(big, sizeof big) != 0x0000u) return 4;
	}
	return 0;
}
""", encoding="ascii")

	# The generated file includes the schema's header for its integer types;
	# the probe declares the one function it calls, so nothing else is needed.
	source = (tmp_path / "unit.c").read_text(encoding="ascii")
	(tmp_path / "unit.c").write_text(
		source.replace('#include "unit.h"', "#include <stdint.h>"),
		encoding="ascii")

	binary = tmp_path / "probe"
	built  = subprocess.run(
		["cc", "-std=c11", "-Wall", "-Wextra", "-Werror", "-Wconversion",
		 "-Wsign-conversion", str(tmp_path / "probe.c"),
		 str(tmp_path / "unit.c"), "-o", str(binary)],
		capture_output=True, text=True)
	assert built.returncode == 0, built.stderr
	assert subprocess.run([str(binary)]).returncode == 0


# -- a `parameter` cannot reach any of the three emitters (0050) ------------

#: Two codec families, so the comparison is over an output with something in
#: it: a one's-complement kernel and a polynomial one are what all three
#: emitters write, and Python and Rust write nothing else.
BOUND = ("codec ic { kernel = ones_complement(width = 16); }\n"
	"impl ic derived;\n"
	"codec crc32 { kernel = polynomial(width = 32, poly = 0x04C11DB7,\n"
	"                                  init = 0xFFFFFFFF, xorout = 0xFFFFFFFF,\n"
	"                                  reflect); }\nimpl crc32 derived;\n")

#: The same schema with an argument and without one. Every way a parameter
#: can be READ is in the first: it sizes an array, it is an `at` offset, and
#: a `require` names it. A fixture that only DECLARED one would pass for a
#: schema whose parameter no expression touches, which is not the case that
#: worried anybody.
WITH_ARGUMENT = HEAD + BOUND + """struct S {
	parameter u8 block [stream];
	u8 body[block];
	u8 tail at block;
}
require block > 0;
"""

NO_ARGUMENT = HEAD + BOUND + """struct S {
	u8 body[4];
	u8 tail at 4;
}
"""

#: What a real difference looks like, for the control: one more derived impl.
ONE_MORE = HEAD + BOUND + ("codec crc16 { kernel = polynomial(width = 16,"
	" poly = 0x8005, reflect); }\nimpl crc16 derived;\n"
	"struct S { u8 a; }\n")

#: The three emitters this covers. C++ is not among them: it calls the C
#: implementation rather than writing one (see `WRITES_THE_KERNEL` in
#: `test_backends_refuse_the_same_members.py`).
DERIVED_EMITTERS = {"c": derived, "python": py_derived, "rust": rs_derived}


@pytest.mark.parametrize("language", sorted(DERIVED_EMITTERS))
def test_a_parameter_does_not_change_what_a_derived_emitter_writes(
		language: str) -> None:
	"""All three refused a schema carrying one until 2026-09-17, and the
	refusal was answering a question about the four `situc build` backends
	rather than about this file.

	A relationship rather than a value: the argument may not move a byte of
	what these emit, whatever they emit. The control below is what says the
	comparison can report a difference at all -- two identical strings are
	as loud from a comparison that cannot fail as from one that can.
	"""
	emitter = DERIVED_EMITTERS[language]

	assert emitter.generate(parse_text(WITH_ARGUMENT), "unit") \
		== emitter.generate(parse_text(NO_ARGUMENT), "unit"), (
		f"{language}'s derived emitter wrote something different for a "
		f"schema carrying a `parameter`. It reads `impls()` and `codecs()` "
		f"and nothing else, so either that stopped being true or an "
		f"argument now reaches the output -- in which case it needs "
		f"passing, not ignoring (0050)")

	assert emitter.generate(parse_text(ONE_MORE), "unit") \
		!= emitter.generate(parse_text(NO_ARGUMENT), "unit"), (
		f"CONTROL: {language}'s output did not change for an extra derived "
		f"impl, so the comparison above compared nothing")


class _PoisonedStruct(ast.StructDecl):
	"""A struct declaration that raises the moment a field of it is read.

	`isinstance` reads the type rather than the instance, so `codecs()` and
	`impls()` still filter these out and `structs()` still returns them --
	which is what lets the control fire while the emitter runs.
	"""

	def __getattribute__(self, name: str) -> object:
		# The dunders go through: `__class__` is what `isinstance` falls
		# back to for a subclass, so poisoning it would stop the schema
		# being readable at all and the test would pass for that reason.
		# Every data field -- `name`, `members`, `attrs`, `span` -- trips.
		if name.startswith("__") and name.endswith("__"):
			return object.__getattribute__(self, name)
		raise AssertionError(f"a derived emitter read StructDecl.{name}")


def _poisoned(schema: ast.Schema) -> ast.Schema:
	return ast.Schema(span=schema.span, decls=[
		object.__new__(_PoisonedStruct)
		if isinstance(decl, ast.StructDecl) else decl
		for decl in schema.decls])


@pytest.mark.parametrize("language", sorted(DERIVED_EMITTERS))
def test_no_derived_emitter_reads_a_struct_declaration_at_all(
		language: str) -> None:
	"""The structural half, and the reason the test above is not enough.

	Equal output for one fixture says a parameter did not reach it; it
	cannot say none could. A `parameter` is a member of a struct, so an
	emitter that never reads a struct declaration has no path to one
	whatever the schema says -- and this fails the day somebody gives one
	of these emitters a struct to read, which is the change that would make
	an argument reachable.
	"""
	poisoned = _poisoned(parse_text(WITH_ARGUMENT))

	# The control, first: something that DOES read structs must trip the
	# poison, or the emitters below are being cleared by a tripwire that
	# cannot fire.
	with pytest.raises(AssertionError) as tripped:
		traverse.parameters(poisoned)
	assert "read StructDecl." in str(tripped.value)

	emitter = DERIVED_EMITTERS[language]
	assert emitter.generate(poisoned, "unit") \
		== emitter.generate(parse_text(WITH_ARGUMENT), "unit")

	# C's module has a second entry point, and it never carried the
	# refusal: `emit.py` calls it while writing the header, so a `situc
	# build` of a parameterised schema has been running it since the
	# backends learned to take an argument. It reads the same two lists,
	# and this says so rather than leaving it inferred.
	if language == "c":
		assert emitter.declarations(poisoned, "situ") \
			== emitter.declarations(parse_text(WITH_ARGUMENT), "situ")


# ---------------------------------------------------------------------------
# The table family in Rust and Python (26.451)
# ---------------------------------------------------------------------------

#: The five unpadded table codes, and what each one's standard says the
#: first two symbols are. Written as bit strings rather than as integers
#: because that is how the standards print them, and because a table read
#: back as a number hides a bit-order error that a string shows.
#:
#: Checked against the standards and NOT against C's output: C is the only
#: backend that generated these until now, so comparing to it would be
#: asking the implementation whether it agrees with itself.
TABLE_CODES = {
	# RFC 4648 base16: a nibble to an uppercase ASCII hex digit.
	"base16":            (4, 8, bytes([0x01]), "0011000000110001"),
	"base16_lower":      (4, 8, bytes([0x01]), "0011000000110001"),
	# ANSI X3.263 / FDDI: 0 is 11110, 1 is 01001. Chosen to bound the run
	# length, so they are not an arithmetic function of the input.
	"code_4b5b":         (4, 5, bytes([0x01]), "1111001001"),
	# IEEE 802.3: a one is a falling edge, encoded 10.
	"manchester_802_3":  (1, 2, bytes([0b0100_0000]), "0110"),
	# G.E. Thomas's, which is the bit-inverse and a different code.
	"manchester_thomas": (1, 2, bytes([0b0100_0000]), "1001"),
}

#: How many input bits each case above feeds in.
TABLE_INPUT_BITS = {"base16": 8, "base16_lower": 8, "code_4b5b": 8,
                    "manchester_802_3": 2, "manchester_thomas": 2}


def _table_schema(name: str, inputs: int, outputs: int) -> str:
	code = "fddi_4b5b" if name == "code_4b5b" else name
	return (f"codec {name} {{ kernel = table(input_bits = {inputs},"
	        f" output_bits = {outputs}, code = {code}); }}\n"
	        f"impl {name} derived;\n")


@pytest.mark.parametrize("name", sorted(TABLE_CODES))
@pytest.mark.parametrize("language", sorted(DERIVED_EMITTERS))
def test_every_backend_writes_a_body_for_an_unpadded_table_code(
		language: str, name: str) -> None:
	"""Rust and Python declined all five until 26.451, and said so in a
	comment rather than failing -- which is the right way to decline and
	is indistinguishable from a body when nobody looks.

	This asserts the absence of the decline note as well as the presence
	of the function, because the note is what the gap looked like.
	"""
	inputs, outputs, _data, _want = TABLE_CODES[name]
	text = _table_schema(name, inputs, outputs)
	out  = DERIVED_EMITTERS[language].generate(parse_text(text), "unit")

	assert f"No implementation for `{name}`" not in out, (
		f"{language} still declines {name}")
	assert f"{name}_encode" in out and f"{name}_decode" in out


@pytest.mark.parametrize("name", sorted(TABLE_CODES))
def test_the_generated_python_encodes_what_the_standard_says(
		name: str, tmp_path: Path) -> None:
	"""Run it, do not read it.

	Generated Python fails at the call rather than at the build, so a
	module that imports cleanly has demonstrated nothing. Each case feeds
	the first two symbols and compares the output BITS against the
	standard's own spelling, then round-trips.
	"""
	inputs, outputs, data, want = TABLE_CODES[name]
	text   = _table_schema(name, inputs, outputs)
	module = tmp_path / "unit.py"
	module.write_text(py_derived.generate(parse_text(text), "unit"),
	                  encoding="ascii")

	namespace: dict[str, object] = {}
	exec(compile(module.read_text(encoding="ascii"), str(module), "exec"),
	     namespace)

	encode = namespace[f"{name}_encode"]
	decode = namespace[f"{name}_decode"]
	assert callable(encode) and callable(decode)

	bits = TABLE_INPUT_BITS[name]
	out  = bytearray(16)
	n    = encode(data, bits, out)

	got = "".join(f"{byte:08b}" for byte in out)[:n]
	assert got == want, f"{name}: encoded {got}, the standard says {want}"

	back = bytearray(16)
	recovered = decode(bytes(out), n, back)
	assert recovered == bits, f"{name}: round trip returned {recovered} bits"
	assert bytes(back)[:len(data)] == data


@pytest.mark.parametrize("name", sorted(TABLE_CODES))
def test_the_generated_python_refuses_a_symbol_the_code_omits(
		name: str, tmp_path: Path) -> None:
	"""The control for the test above, and the half a round trip cannot
	show: a decoder that accepted anything would round-trip perfectly.

	`0xFF` repeated is not a codeword in any of the five -- base16's
	alphabet is ASCII, 4b5b's is five bits with a bounded run length, and
	a Manchester symbol is never 11.
	"""
	inputs, outputs, _data, _want = TABLE_CODES[name]
	text   = _table_schema(name, inputs, outputs)
	module = tmp_path / "unit.py"
	module.write_text(py_derived.generate(parse_text(text), "unit"),
	                  encoding="ascii")

	namespace: dict[str, object] = {}
	exec(compile(module.read_text(encoding="ascii"), str(module), "exec"),
	     namespace)

	decode = namespace[f"{name}_decode"]
	assert decode(b"\xFF" * 4, outputs * 4, bytearray(16)) == 0, (  # type: ignore[operator]
		f"{name} decoded a symbol its table does not define")



def _generated(source: str, tmp_path: Path, *names: str
		) -> list[Callable[..., Any]]:
	"""Execute a generated Python module and hand back named functions.

	`exec` fills a `dict[str, object]`, so what comes out needs saying:
	`Any` rather than `int`, because these do not share a shape: a byte
	codec is `(bytes, int, bytearray) -> int` and Hamming is
	`(int) -> int` beside `(int) -> tuple[int, bool]`. Narrowing to the
	common case made mypy call a correct tuple comparison
	non-overlapping. The call in each test is what checks the shape.
	"""
	module = tmp_path / "unit.py"
	module.write_text(source, encoding="ascii")

	namespace: dict[str, object] = {}
	exec(compile(module.read_text(encoding="ascii"), str(module), "exec"),
	     namespace)

	found = []
	for name in names:
		got = namespace[name]
		assert callable(got), f"{name} is not callable"
		found.append(cast("Callable[..., Any]", got))
	return found


# ---------------------------------------------------------------------------
# The padded table codes (26.452)
# ---------------------------------------------------------------------------

#: `base32` and `base64` emit whole groups and fill a partial one, so the
#: last group's symbol count depends on how much input was left. That is
#: the whole difference from `base16`, where four bits divide a byte and
#: the question never arises.
#:
#: The oracle is Python's own `base64` module -- independent of situ, and
#: what C's arithmetic was written against. C's note says what the sweep
#: has to cover: an encoder wrong only for inputs of length 3n+1 looks
#: right in casual testing, so every residue is swept rather than a
#: convenient case picked.
PADDED_CODES: dict[str, tuple[int, Callable[[bytes], bytes]]] = {
	"base64": (6, base64.b64encode),
	"base32": (5, base64.b32encode),
}


def _padded_schema(name: str, inputs: int) -> str:
	return (f"codec {name} {{ kernel = table(input_bits = {inputs},"
	        f" output_bits = 8, code = {name}, pad = 0x3D); }}\n"
	        f"impl {name} derived;\n")


@pytest.mark.parametrize("name", sorted(PADDED_CODES))
@pytest.mark.parametrize("language", sorted(DERIVED_EMITTERS))
def test_every_backend_writes_a_body_for_a_padded_base_code(
		language: str, name: str) -> None:
	inputs, _ref = PADDED_CODES[name]
	out = DERIVED_EMITTERS[language].generate(
		parse_text(_padded_schema(name, inputs)), "unit")

	assert f"No implementation for `{name}`" not in out, (
		f"{language} still declines {name}")
	assert f"{name}_encode" in out and f"{name}_decode" in out


@pytest.mark.parametrize("name", sorted(PADDED_CODES))
def test_the_generated_python_agrees_with_the_base64_module(
		name: str, tmp_path: Path) -> None:
	"""Every input length from 0 to 39, against the standard library.

	Not against C, which was the only backend generating these: a diff
	against it asks the implementation whether it agrees with itself. The
	sweep is every length rather than a sample because the failure this
	guards is length-dependent by construction -- the padding rule is the
	only thing that varies between one residue and the next.
	"""
	inputs, reference = PADDED_CODES[name]
	encode, decode = _generated(
		py_derived.generate(parse_text(_padded_schema(name, inputs)), "unit"),
		tmp_path, f"{name}_encode", f"{name}_decode")

	for length in range(40):
		data = bytes((i * 37 + 11) & 0xFF for i in range(length))
		out  = bytearray(length * 2 + 32)
		got  = bytes(out[:encode(data, length, out)])

		assert got == reference(data), (
			f"{name} at length {length}: {got!r} against the standard "
			f"library's {reference(data)!r}")

		back = bytearray(length + 32)
		kept = decode(got, len(got), back)
		assert bytes(back[:kept]) == data, (
			f"{name} at length {length} did not round trip")


@pytest.mark.parametrize("name", sorted(PADDED_CODES))
def test_the_padded_decoder_refuses_a_byte_outside_the_alphabet(
		name: str, tmp_path: Path) -> None:
	"""The control, and the half the sweep above cannot show: a decoder
	that accepted anything would agree with the oracle on every input the
	oracle produced.

	`*` is in neither alphabet -- base64's is A-Za-z0-9+/ and base32's is
	A-Z2-7 -- and a group of them is a whole number of groups, so length
	is not what refuses it.
	"""
	inputs, _reference = PADDED_CODES[name]
	decode, = _generated(
		py_derived.generate(parse_text(_padded_schema(name, inputs)), "unit"),
		tmp_path, f"{name}_decode")
	assert decode(b"*" * 8, 8, bytearray(32)) == 0, (
		f"{name} decoded a byte its alphabet does not contain")


#: Rust is compiled and run rather than read, for the reason C is: a
#: generated body that parses has demonstrated nothing about what it
#: computes. Without this the padded path was verified once, by hand, and
#: never again -- and the Python sweep above cannot speak for it, which a
#: sabotage of Rust's padding shows by leaving that sweep green.
RUSTC = shutil.which("rustc")


@pytest.mark.skipif(RUSTC is None, reason="no rustc")
@pytest.mark.parametrize("name", sorted(PADDED_CODES))
def test_the_generated_rust_agrees_with_the_base64_module(
		name: str, tmp_path: Path) -> None:
	"""The same sweep as Python's, in the other backend that inlines these.

	One process for all forty lengths: rustc is slow enough that a
	compile per length would be the test's whole cost, and the driver
	printing one line per length keeps the comparison in Python where the
	oracle is.
	"""
	inputs, reference = PADDED_CODES[name]
	(tmp_path / "unit.rs").write_text(
		rs_derived.generate(parse_text(_padded_schema(name, inputs)), "unit"),
		encoding="ascii")
	(tmp_path / "main.rs").write_text(
		"#[path = \"unit.rs\"] mod unit;\n"
		"use unit::*;\n"
		"\n"
		"fn main() {\n"
		"\tfor n in 0..40usize {\n"
		"\t\tlet data: Vec<u8> = (0..n)\n"
		"\t\t\t.map(|i| ((i * 37 + 11) & 0xFF) as u8).collect();\n"
		"\t\tlet mut out = vec![0u8; n * 2 + 32];\n"
		f"\t\tlet k = {name}_encode(&data, n, &mut out);\n"
		"\t\tprintln!(\"{}\", String::from_utf8_lossy(&out[..k]));\n"
		"\t}\n"
		"}\n", encoding="ascii")

	built = subprocess.run(
		[RUSTC or "rustc", "--edition", "2021", "-A", "warnings",
		 "-o", str(tmp_path / "run"), str(tmp_path / "main.rs")],
		capture_output=True, text=True)
	assert built.returncode == 0, built.stderr

	ran = subprocess.run([str(tmp_path / "run")], capture_output=True,
	                     text=True)
	assert ran.returncode == 0, ran.stderr

	lines = ran.stdout.splitlines()
	assert len(lines) == 40, f"driver printed {len(lines)} lines"

	for length, got in enumerate(lines):
		data = bytes((i * 37 + 11) & 0xFF for i in range(length))
		assert got == reference(data).decode("ascii"), (
			f"{name} at length {length}: Rust wrote {got!r}, the standard "
			f"library says {reference(data).decode('ascii')!r}")


# ---------------------------------------------------------------------------
# The shift-register family in Rust and Python (26.453)
# ---------------------------------------------------------------------------

#: One additive and one multiplicative, which is the whole axis: the same
#: taps and seed, differing only in where the feedback comes from, and
#: having opposite properties because of it.
LFSR = """codec additive { kernel = shift_register(width = 15, taps = 0x6000,
	seed = 0x7FFF, feedback = input); }
impl additive derived;

codec multiplicative { kernel = shift_register(width = 15, taps = 0x6000,
	seed = 0x7FFF, feedback = output); }
impl multiplicative derived;
"""

#: The two NRZI conventions, which differ by one word and produce
#: complementary streams. A receiver built on the wrong one returns the
#: complement of what was sent, and nothing at run time can see that.
NRZI = """codec on_one { kernel = shift_register(width = 1, taps = 0x1,
	seed = 0x0, feedback = output); }
impl on_one derived;

codec on_zero { kernel = shift_register(width = 1, taps = 0x1, seed = 0x0,
	feedback = output, complement_feedback); }
impl on_zero derived;
"""


@pytest.mark.parametrize("language", sorted(DERIVED_EMITTERS))
def test_every_backend_writes_a_body_for_a_shift_register(
		language: str) -> None:
	out = DERIVED_EMITTERS[language].generate(parse_text(LFSR), "unit")

	assert "No implementation for" not in out, f"{language} declines an LFSR"
	for name in ("additive", "multiplicative"):
		assert f"{name}_encode" in out and f"{name}_decode" in out


def test_the_generated_python_scrambles_and_unscrambles(tmp_path: Path) -> None:
	"""Round trip in both conventions.

	Necessary and not sufficient -- a scrambler that did nothing would
	round-trip perfectly -- so the output is also required to differ from
	the input, and the cross-backend comparison below is what says the
	bytes are the RIGHT ones.
	"""
	data = bytes(range(32))
	for name in ("additive", "multiplicative"):
		encode, decode = _generated(py_derived.generate(parse_text(LFSR),
		                                                "unit"),
		                            tmp_path, f"{name}_encode",
		                            f"{name}_decode")
		out = bytearray(32)
		assert encode(data, 32, out) == 32
		assert bytes(out) != data, f"{name} left the data unscrambled"

		back = bytearray(32)
		assert decode(bytes(out), 32, back) == 32
		assert bytes(back) == data, f"{name} did not round trip"


def test_the_two_nrzi_conventions_are_complements(tmp_path: Path) -> None:
	"""`complement_feedback` is one word in the schema and the whole
	difference between the two NRZI conventions.

	A relationship rather than two values: either stream alone looks
	plausible, and what says the flag is read is that the two differ in
	every bit. Pinning one of them would pass against a build that
	ignored the flag entirely.
	"""
	data = bytes(range(16))
	source = py_derived.generate(parse_text(NRZI), "unit")
	one, = _generated(source, tmp_path, "on_one_encode")
	zero, = _generated(source, tmp_path, "on_zero_encode")

	a, b = bytearray(16), bytearray(16)
	one(data, 16, a)
	zero(data, 16, b)

	assert bytes(a) != bytes(b), "the complement flag changed nothing"


@pytest.mark.skipif(RUSTC is None, reason="no rustc")
def test_rust_and_python_scramble_identically(tmp_path: Path) -> None:
	"""Three spellings of one algorithm, held to each other.

	C is the reference: it had this family before either of the others,
	and Rust and Python are re-spellings of it. So what this catches is a
	transcription error between backends, which is the error re-spelling
	can make -- not a shared design error, which it cannot see and which
	C's own tests are for. Said rather than implied.
	"""
	data = bytes(range(32))

	(tmp_path / "unit.rs").write_text(
		rs_derived.generate(parse_text(LFSR), "unit"), encoding="ascii")
	(tmp_path / "main.rs").write_text(
		'#[path = "unit.rs"] mod unit;\n'
		"use unit::*;\n"
		"\n"
		"fn main() {\n"
		"\tlet data: Vec<u8> = (0..32u8).collect();\n"
		"\tlet mut a = vec![0u8; 32];\n"
		"\tlet mut b = vec![0u8; 32];\n"
		"\tadditive_encode(&data, 32, &mut a);\n"
		"\tmultiplicative_encode(&data, 32, &mut b);\n"
		"\tfor x in &a { print!(\"{:02x}\", x); }\n"
		"\tprintln!();\n"
		"\tfor x in &b { print!(\"{:02x}\", x); }\n"
		"\tprintln!();\n"
		"}\n", encoding="ascii")

	built = subprocess.run(
		[RUSTC or "rustc", "--edition", "2021", "-A", "warnings",
		 "-o", str(tmp_path / "run"), str(tmp_path / "main.rs")],
		capture_output=True, text=True)
	assert built.returncode == 0, built.stderr

	ran = subprocess.run([str(tmp_path / "run")], capture_output=True,
	                     text=True)
	assert ran.returncode == 0, ran.stderr
	rust = ran.stdout.split()

	source = py_derived.generate(parse_text(LFSR), "unit")
	for index, name in enumerate(("additive", "multiplicative")):
		encode, = _generated(source, tmp_path, f"{name}_encode")
		out = bytearray(32)
		encode(data, 32, out)
		assert bytes(out).hex() == rust[index], (
			f"{name}: Python wrote {bytes(out).hex()} and Rust "
			f"wrote {rust[index]}")


# ---------------------------------------------------------------------------
# COBS and the escape-stuffed codes in Rust and Python (26.454)
# ---------------------------------------------------------------------------

STUFFING = """codec cobs { kernel = stuffing(worst_case = 255, per = 254,
	code = cobs); }
impl cobs derived;

codec slip { kernel = stuffing(worst_case = 2, per = 1, unit = byte,
	code = slip); }
impl slip derived;

codec ppp { kernel = stuffing(worst_case = 2, per = 1, unit = byte,
	code = ppp_async); }
impl ppp derived;
"""

#: Cheshire and Baker's own table, which is the oracle situ did not
#: write. The last two are the ones that separate a correct encoder from
#: a plausible one: a run with no zero at all, and a run that ends in
#: zeros.
COBS_VECTORS = (
	(bytes([0x00]),                bytes([0x01, 0x01, 0x00])),
	(bytes([0x00, 0x00]),          bytes([0x01, 0x01, 0x01, 0x00])),
	(bytes([0x11, 0x22, 0x00, 0x33]),
	 bytes([0x03, 0x11, 0x22, 0x02, 0x33, 0x00])),
	(bytes([0x11, 0x22, 0x33, 0x44]),
	 bytes([0x05, 0x11, 0x22, 0x33, 0x44, 0x00])),
	(bytes([0x11, 0x00, 0x00, 0x00]),
	 bytes([0x02, 0x11, 0x01, 0x01, 0x01, 0x00])),
)

#: RFC 1055 and RFC 1662: the delimiter and the escape are the two bytes
#: that must be escaped, and each becomes the escape plus a substitute.
ESCAPE_VECTORS = {
	"slip": (bytes([0xC0, 0x01, 0xDB]),
	         bytes([0xDB, 0xDC, 0x01, 0xDB, 0xDD, 0xC0])),
	"ppp":  (bytes([0x7E, 0x01, 0x7D]),
	         bytes([0x7D, 0x5E, 0x01, 0x7D, 0x5D, 0x7E])),
}


@pytest.mark.parametrize("language", sorted(DERIVED_EMITTERS))
def test_every_backend_writes_cobs_and_the_escape_codes(
		language: str) -> None:
	out = DERIVED_EMITTERS[language].generate(parse_text(STUFFING), "unit")

	assert "No implementation for" not in out, f"{language} declines one"
	for name in ("cobs", "slip", "ppp"):
		assert f"{name}_encode" in out and f"{name}_decode" in out


def test_the_generated_python_matches_the_cobs_paper(tmp_path: Path) -> None:
	"""Against Cheshire and Baker's table, not against C.

	The round trip is asserted too and is not enough on its own: an
	encoder and decoder that agreed on a wrong scheme would round-trip
	perfectly, which is what the published bytes rule out.
	"""
	encode, decode = _generated(
		py_derived.generate(parse_text(STUFFING), "unit"),
		tmp_path, "cobs_encode", "cobs_decode")

	for plain, want in COBS_VECTORS:
		out = bytearray(64)
		got = bytes(out[:encode(plain, len(plain), out)])
		assert got == want, (
			f"cobs {plain.hex()}: wrote {got.hex()}, the paper says "
			f"{want.hex()}")

		back = bytearray(64)
		kept = decode(got, len(got), back)
		assert bytes(back[:kept]) == plain, f"cobs {plain.hex()} round trip"


@pytest.mark.parametrize("name", sorted(ESCAPE_VECTORS))
def test_the_generated_python_escapes_what_the_rfc_says(
		name: str, tmp_path: Path) -> None:
	"""RFC 1055 for SLIP and RFC 1662 for PPP.

	The round trip here caught a real defect and is why it is asserted
	separately from the bytes: the encoder was right and the decoder
	returned zeros, because the generated `undo` block was indented one
	level too deep and Python read it as the body of the truncation
	guard above it. It parsed, it ran, and it decoded nothing.
	"""
	plain, want = ESCAPE_VECTORS[name]
	encode, decode = _generated(
		py_derived.generate(parse_text(STUFFING), "unit"),
		tmp_path, f"{name}_encode", f"{name}_decode")

	out = bytearray(64)
	got = bytes(out[:encode(plain, len(plain), out)])
	assert got == want, (
		f"{name} {plain.hex()}: wrote {got.hex()}, the RFC says {want.hex()}")

	back = bytearray(64)
	kept = decode(got, len(got), back)
	assert bytes(back[:kept]) == plain, f"{name} did not round trip"


def test_slip_refuses_an_escape_it_does_not_define(tmp_path: Path) -> None:
	"""SLIP's substitutions are a table rather than a transformation, so
	an escape outside it is refused rather than guessed.

	PPP is deliberately NOT held to this: RFC 1662 defines its escape as
	exclusive-or with 0x20, so every escaped byte is reversible and there
	is no such thing as one it does not define -- which is the difference
	the two decoders are written around.
	"""
	decode, = _generated(py_derived.generate(parse_text(STUFFING), "unit"),
	                     tmp_path, "slip_decode")

	# 0xDB is the escape; 0x01 is not one of its two substitutes.
	assert decode(bytes([0xDB, 0x01, 0xC0]), 3, bytearray(16)) == 0


@pytest.mark.skipif(RUSTC is None, reason="no rustc")
def test_rust_stuffs_identically_to_the_published_vectors(
		tmp_path: Path) -> None:
	"""The same vectors through the other backend, compiled and run."""
	(tmp_path / "unit.rs").write_text(
		rs_derived.generate(parse_text(STUFFING), "unit"), encoding="ascii")

	cases = [("cobs", plain) for plain, _want in COBS_VECTORS]
	cases += [(name, ESCAPE_VECTORS[name][0]) for name in sorted(ESCAPE_VECTORS)]
	calls = "\n".join(
		f"\tround(\"{name}\", &{list(plain)!r}, {name}_encode, {name}_decode);"
		.replace("[", "[").replace("'", "")
		for name, plain in cases)

	(tmp_path / "main.rs").write_text(
		'#[path = "unit.rs"] mod unit;\n'
		"use unit::*;\n"
		"\n"
		"fn hex(b: &[u8]) -> String {\n"
		"\tb.iter().map(|x| format!(\"{:02x}\", x)).collect()\n"
		"}\n"
		"\n"
		"fn round(name: &str, plain: &[u8],\n"
		"\t\tenc: fn(&[u8], usize, &mut [u8]) -> usize,\n"
		"\t\tdec: fn(&[u8], usize, &mut [u8]) -> usize) {\n"
		"\tlet mut out = vec![0u8; 64];\n"
		"\tlet n = enc(plain, plain.len(), &mut out);\n"
		"\tlet mut back = vec![0u8; 64];\n"
		"\tlet k = dec(&out[..n], n, &mut back);\n"
		"\tprintln!(\"{} {} {}\", name, hex(&out[..n]), hex(&back[..k]));\n"
		"}\n"
		"\n"
		"fn main() {\n" + calls + "\n}\n", encoding="ascii")

	built = subprocess.run(
		[RUSTC or "rustc", "--edition", "2021", "-A", "warnings",
		 "-o", str(tmp_path / "run"), str(tmp_path / "main.rs")],
		capture_output=True, text=True)
	assert built.returncode == 0, built.stderr

	ran = subprocess.run([str(tmp_path / "run")], capture_output=True,
	                     text=True)
	assert ran.returncode == 0, ran.stderr

	lines = ran.stdout.splitlines()
	assert len(lines) == len(cases), f"driver printed {len(lines)} lines"

	wanted = [want for _plain, want in COBS_VECTORS]
	wanted += [ESCAPE_VECTORS[name][1] for name in sorted(ESCAPE_VECTORS)]

	for line, (name, plain), want in zip(lines, cases, wanted):
		parts = line.split()
		assert parts[1] == want.hex(), (
			f"{name} {plain.hex()}: Rust wrote {parts[1]}, the published "
			f"vector is {want.hex()}")
		assert parts[2] == plain.hex(), f"{name} {plain.hex()} round trip"


#: The group boundary, which Cheshire and Baker's table does not reach
#: and which a sabotage found missing: every published vector is four
#: bytes or fewer, so the `code == 0xFF` flush was never executed and
#: breaking it left the suite green.
#:
#: 254 non-zero bytes is the case that matters. A full group at the very
#: END must NOT open another -- the output is one code byte, the 254
#: bytes and the delimiter, 256 in all. Opening a group there would spend
#: a second overhead byte, which is the one thing COBS promises not to
#: do, and it is a one-token change away in both backends.
COBS_GROUPS = ((253, 255, 0xFE), (254, 256, 0xFF),
               (255, 258, 0xFF), (256, 259, 0xFF))


@pytest.mark.parametrize("count,expected,first", COBS_GROUPS)
def test_cobs_spends_one_overhead_byte_at_a_full_group(
		count: int, expected: int, first: int, tmp_path: Path) -> None:
	encode, decode = _generated(
		py_derived.generate(parse_text(STUFFING), "unit"),
		tmp_path, "cobs_encode", "cobs_decode")

	plain = bytes([0xAA]) * count
	out   = bytearray(1024)
	got   = bytes(out[:encode(plain, count, out)])

	assert len(got) == expected, (
		f"{count} non-zero bytes encoded to {len(got)}, not {expected}")
	assert got[0] == first
	assert got[-1] == 0x00, "no delimiter"

	back = bytearray(1024)
	kept = decode(got, len(got), back)
	assert bytes(back[:kept]) == plain


@pytest.mark.skipif(RUSTC is None, reason="no rustc")
@pytest.mark.parametrize("count,expected,first", COBS_GROUPS)
def test_rust_cobs_spends_one_overhead_byte_at_a_full_group(
		count: int, expected: int, first: int, tmp_path: Path) -> None:
	"""The group boundary in the other backend, and it needs its own test.

	The published-vector sweep above is four bytes at its longest, so it
	never reaches a full group -- measured: sabotaging Rust's flush alone
	left all 119 tests in this file green, which is the same
	one-backend-only gap the shift-register tranche hit and is why this
	is written out rather than folded into the sweep.
	"""
	(tmp_path / "unit.rs").write_text(
		rs_derived.generate(parse_text(STUFFING), "unit"), encoding="ascii")
	(tmp_path / "main.rs").write_text(
		'#[path = "unit.rs"] mod unit;\n'
		"use unit::*;\n"
		"\n"
		"fn main() {\n"
		f"\tlet plain = vec![0xAAu8; {count}];\n"
		"\tlet mut out = vec![0u8; 1024];\n"
		f"\tlet n = cobs_encode(&plain, {count}, &mut out);\n"
		"\tlet mut back = vec![0u8; 1024];\n"
		"\tlet k = cobs_decode(&out[..n], n, &mut back);\n"
		"\tprintln!(\"{} {} {} {}\", n, out[0], out[n - 1],\n"
		"\t\t(back[..k] == plain[..]) as u8);\n"
		"}\n", encoding="ascii")

	built = subprocess.run(
		[RUSTC or "rustc", "--edition", "2021", "-A", "warnings",
		 "-o", str(tmp_path / "run"), str(tmp_path / "main.rs")],
		capture_output=True, text=True)
	assert built.returncode == 0, built.stderr

	ran = subprocess.run([str(tmp_path / "run")], capture_output=True,
	                     text=True)
	assert ran.returncode == 0, ran.stderr

	length, head, tail, round_tripped = ran.stdout.split()
	assert int(length) == expected, (
		f"{count} non-zero bytes encoded to {length}, not {expected}")
	assert int(head) == first
	assert int(tail) == 0, "no delimiter"
	assert round_tripped == "1", "did not round trip"


# ---------------------------------------------------------------------------
# Hamming(7,4), the block interleaver and SMTP dot-stuffing (26.455)
# ---------------------------------------------------------------------------

THREE = """codec hamming_7_4 { kernel = linear_block(n = 7, k = 4,
	standard_form, code = hamming_7_4); }
impl hamming_7_4 derived;

codec interleave_16 { kernel = permutation(rows = 4, columns = 4); }
impl interleave_16 derived;

codec wide { kernel = permutation(rows = 2, columns = 8); }
impl wide derived;

codec smtp { kernel = stuffing(worst_case = 4, per = 3, unit = stream,
	code = smtp_dot); }
impl smtp derived;
"""

#: Hamming(7,4) in situ's own packing -- byte = 0 p2 p1 p0 d3 d2 d1 d0.
#: These do NOT match either published table's literal bytes, because the
#: packing differs; it is the same code with the parity bits elsewhere.
#: What makes them checkable without trusting the packing is the two
#: properties below, which hold of Hamming(7,4) whoever implements it.
HAMMING = (0x00, 0x71, 0x62, 0x13, 0x54, 0x25, 0x36, 0x47,
           0x38, 0x49, 0x5A, 0x2B, 0x6C, 0x1D, 0x0E, 0x7F)


def test_hamming_is_systematic_and_has_the_right_weights(
		tmp_path: Path) -> None:
	"""Two properties of the code itself, not of this spelling of it.

	Systematic: the low nibble of every codeword is the input. And the
	weight distribution of Hamming(7,4) is one word of weight 0, seven
	of 3, seven of 4 and one of 7 -- a table with a transcription slip
	in it almost certainly breaks one of these, where comparing against
	a table somebody typed would only compare two typings.
	"""
	encode, = _generated(py_derived.generate(parse_text(THREE), "unit"),
	                     tmp_path, "hamming_7_4_encode")
	table = [encode(nibble) for nibble in range(16)]

	assert tuple(table) == HAMMING
	assert all(word & 0x0F == nibble for nibble, word in enumerate(table)), \
		"not systematic: the low nibble is not the input"

	weights = Counter(bin(word).count("1") for word in table)
	assert dict(sorted(weights.items())) == {0: 1, 3: 7, 4: 7, 7: 1}


def test_hamming_corrects_every_single_bit_error(tmp_path: Path) -> None:
	"""All 112 of them, and the 16 clean words.

	Exhaustive because it can be: 16 nibbles times 7 bit positions is
	the whole space, so there is no sampling question to get wrong.
	"""
	source = py_derived.generate(parse_text(THREE), "unit")
	encode, = _generated(source, tmp_path, "hamming_7_4_encode")
	decode  = _generated(source, tmp_path, "hamming_7_4_decode")[0]

	for nibble in range(16):
		word = encode(nibble)
		assert decode(word) == (nibble, False), "a clean word reported an error"

		for bit in range(7):
			assert decode(word ^ (1 << bit)) == (nibble, True), (
				f"nibble {nibble}, bit {bit} not corrected")


def test_hamming_miscorrects_a_double_error_and_says_nothing(
		tmp_path: Path) -> None:
	"""Pinning what this does NOT catch, so nobody reads the flag as
	stronger than it is.

	d_min is 3, so the code corrects one bit and cannot detect two. The
	syndrome's "no error" entry is unreachable from the error branch, so
	there is no path that reports `detected but not correctable`: a
	double error returns a WRONG nibble and still reports True. That is
	the code behaving as it must, and a port must not invent a third
	outcome.
	"""
	source = py_derived.generate(parse_text(THREE), "unit")
	encode, = _generated(source, tmp_path, "hamming_7_4_encode")
	decode  = _generated(source, tmp_path, "hamming_7_4_decode")[0]

	word = encode(0x5)
	got, corrected = decode(word ^ 0b0000011)

	assert corrected is True, "a double error reported no error"
	assert got != 0x5, "a double error was corrected, which d_min 3 forbids"


#: The interleaver at 4x4 is its own inverse, so `encode` and `decode`
#: cannot be told apart by a round trip or by any square fixture. A
#: non-square one is the only case where the plausible wrong answer --
#: the two index expressions swapped -- separates from the right one,
#: and no schema in the tree uses one.
WIDE_ENCODE = [0, 8, 1, 9, 2, 10, 3, 11, 4, 12, 5, 13, 6, 14, 7, 15]
WIDE_DECODE = [0, 2, 4, 6, 8, 10, 12, 14, 1, 3, 5, 7, 9, 11, 13, 15]


def test_the_interleaver_transposes_and_refuses_a_partial_block(
		tmp_path: Path) -> None:
	source = py_derived.generate(parse_text(THREE), "unit")
	encode, decode = _generated(source, tmp_path, "interleave_16_encode",
	                            "interleave_16_decode")
	data = bytes(range(16))

	out = bytearray(16)
	assert encode(data, 16, out) == 16
	assert list(out) == [0, 4, 8, 12, 1, 5, 9, 13, 2, 6, 10, 14, 3, 7, 11, 15]

	back = bytearray(16)
	assert decode(bytes(out), 16, back) == 16
	assert bytes(back) == data

	# A partial block has no defined permutation.
	assert encode(bytes(15), 15, bytearray(32)) == 0
	assert decode(bytes(17), 17, bytearray(32)) == 0


def test_a_non_square_interleaver_separates_encode_from_decode(
		tmp_path: Path) -> None:
	"""The direction, which every square fixture is blind to.

	At 2x8 the two differ, so a port that swapped the index expressions
	in one of the two functions fails here and passes everything else.
	"""
	source = py_derived.generate(parse_text(THREE), "unit")
	encode, decode = _generated(source, tmp_path, "wide_encode",
	                            "wide_decode")
	data = bytes(range(16))

	out = bytearray(16)
	encode(data, 16, out)
	assert list(out) == WIDE_ENCODE

	other = bytearray(16)
	decode(data, 16, other)
	assert list(other) == WIDE_DECODE
	assert WIDE_ENCODE != WIDE_DECODE, "the fixture cannot separate them"


#: Derived from RFC 5321 section 4.5.2's two rules, NOT quoted from it:
#: the RFC states the rule in prose and its Appendix D transcripts carry
#: no body with a leading period, so there is no published byte-level
#: vector for this codec. Said plainly rather than cited as one.
SMTP_PAIRS = ((b".\r\n", b"..\r\n"), (b"..\r\n", b"...\r\n"),
              (b".x\r\n", b"..x\r\n"), (b"a.b\r\n", b"a.b\r\n"),
              (b"a\r\n.b\r\n", b"a\r\n..b\r\n"), (b"", b""),
              (b"hello\r\n", b"hello\r\n"))


@pytest.mark.parametrize("plain,wire", SMTP_PAIRS)
def test_smtp_doubles_a_period_at_the_start_of_a_line(
		plain: bytes, wire: bytes, tmp_path: Path) -> None:
	source = py_derived.generate(parse_text(THREE), "unit")
	encode, decode = _generated(source, tmp_path, "smtp_encode",
	                            "smtp_decode")

	out = bytearray(32)
	got = bytes(out[:encode(plain, len(plain), out)])
	assert got == wire

	back = bytearray(32)
	kept = decode(got, len(got), back)
	assert bytes(back[:kept]) == plain


@pytest.mark.skipif(RUSTC is None, reason="no rustc")
def test_rust_agrees_on_hamming_and_the_non_square_interleaver(
		tmp_path: Path) -> None:
	"""Rust's own executing test, because a backend without one is a
	backend nothing checks -- measured twice in this file's history.

	The two cases here are the ones where a wrong answer is plausible: a
	mis-transcribed Hamming table, and an interleaver whose direction is
	swapped in one function.
	"""
	(tmp_path / "unit.rs").write_text(
		rs_derived.generate(parse_text(THREE), "unit"), encoding="ascii")
	(tmp_path / "main.rs").write_text(
		'#[path = "unit.rs"] mod unit;\n'
		"use unit::*;\n"
		"\n"
		"fn main() {\n"
		"\tlet t: Vec<u8> = (0..16u8).map(hamming_7_4_encode).collect();\n"
		"\tprintln!(\"{}\", t.iter()\n"
		"\t\t.map(|x| format!(\"{:02x}\", x)).collect::<String>());\n"
		"\n"
		"\tlet mut single = 0;\n"
		"\tfor n in 0..16u8 {\n"
		"\t\tfor b in 0..7 {\n"
		"\t\t\tif hamming_7_4_decode(t[n as usize] ^ (1 << b))\n"
		"\t\t\t\t\t== (n, true) { single += 1; }\n"
		"\t\t}\n"
		"\t}\n"
		"\tprintln!(\"{}\", single);\n"
		"\n"
		"\tlet src: Vec<u8> = (0..16u8).collect();\n"
		"\tlet mut e = vec![0u8; 16];\n"
		"\tlet mut d = vec![0u8; 16];\n"
		"\twide_encode(&src, 16, &mut e);\n"
		"\twide_decode(&src, 16, &mut d);\n"
		"\tprintln!(\"{:?}\", e);\n"
		"\tprintln!(\"{:?}\", d);\n"
		"}\n", encoding="ascii")

	built = subprocess.run(
		[RUSTC or "rustc", "--edition", "2021", "-A", "warnings",
		 "-o", str(tmp_path / "run"), str(tmp_path / "main.rs")],
		capture_output=True, text=True)
	assert built.returncode == 0, built.stderr

	ran = subprocess.run([str(tmp_path / "run")], capture_output=True,
	                     text=True)
	assert ran.returncode == 0, ran.stderr
	table, single, encoded, decoded = ran.stdout.splitlines()

	assert table == "".join(f"{word:02x}" for word in HAMMING)
	assert single == "112", f"Rust corrected {single} of 112 single-bit errors"
	assert encoded == repr(WIDE_ENCODE).replace("'", "")
	assert decoded == repr(WIDE_DECODE).replace("'", "")


# ---------------------------------------------------------------------------
# HDLC and USB bit stuffing (26.456)
# ---------------------------------------------------------------------------

BITSTUFF = """codec hdlc { kernel = stuffing(worst_case = 6, per = 5,
	unit = bit, code = hdlc); }
impl hdlc derived;

codec usb { kernel = stuffing(worst_case = 7, per = 6, unit = bit,
	code = usb); }
impl usb derived;
"""

#: Published worked examples, with their sources. Neither standard
#: carries a bit-string vector: RFC 1662 section 5.2 states the HDLC rule
#: in prose and gives no example, and the USB 2.0 figures are waveform
#: drawings with no bit numerals. So these come from externally authored
#: worked examples rather than from the specifications themselves, which
#: is a weaker citation and is said rather than glossed.
#:
#: Both standards TRANSMIT octets least-significant-bit first while this
#: codec walks the buffer MSB-first. That does not affect a vector
#: written as a bit string -- the algorithm consumes a stream -- but a
#: vector taken from a real capture would need each byte reversed.
STUFF_VECTORS = (
	("hdlc", "011110", "011110", "Dordal, Intro to Computer Networks 6.1.5.1"),
	("hdlc", "0111110", "01111100", "Dordal 6.1.5.1"),
	("hdlc", "01111110", "011111010", "Dordal 6.1.5.1"),
	("hdlc", "011011111111111111110010",
	 "011011111011111011111010010", "Tanenbaum, Computer Networks"),
	("usb", "011111111001111", "0111111011001111",
	 "Cypress AN57294 figure 10"),
)

#: Derived from the rules, NOT published -- labelled so nobody cites
#: them as a standard's. The five-ones case is the discriminator between
#: the two codes: HDLC stuffs there and USB does not.
STUFF_DERIVED = (("hdlc", "11111", "111110"),
                 ("hdlc", "1" * 10, "111110111110"),
                 ("usb", "111111", "1111110"),
                 ("usb", "11111", "11111"))


def _bits(text: str) -> bytes:
	"""A bit string as an MSB-first buffer."""
	out = bytearray(len(text) // 8 + 2)
	for index, char in enumerate(text):
		if char == "1":
			out[index // 8] |= 0x80 >> (index % 8)
	return bytes(out)


def _unbits(buffer: bytes, count: int) -> str:
	return "".join("1" if buffer[i // 8] >> (7 - i % 8) & 1 else "0"
	               for i in range(count))


@pytest.mark.parametrize("language", sorted(DERIVED_EMITTERS))
def test_every_backend_writes_both_bit_stuffing_codes(language: str) -> None:
	out = DERIVED_EMITTERS[language].generate(parse_text(BITSTUFF), "unit")

	assert "No implementation for" not in out
	for name in ("hdlc", "usb"):
		assert f"{name}_encode" in out and f"{name}_decode" in out


@pytest.mark.parametrize("name,plain,wire,source", STUFF_VECTORS,
                         ids=[f"{v[0]}-{v[1][:12]}" for v in STUFF_VECTORS])
def test_the_generated_python_stuffs_the_published_way(
		name: str, plain: str, wire: str, source: str,
		tmp_path: Path) -> None:
	source_text = py_derived.generate(parse_text(BITSTUFF), "unit")
	encode, decode = _generated(source_text, tmp_path, f"{name}_encode",
	                            f"{name}_decode")

	out = bytearray(32)
	written = encode(_bits(plain), len(plain), out)
	assert _unbits(bytes(out), written) == wire, f"against {source}"

	back = bytearray(32)
	kept = decode(_bits(wire), len(wire), back)
	assert _unbits(bytes(back), kept) == plain, "did not round trip"


@pytest.mark.parametrize("name,plain,wire", STUFF_DERIVED)
def test_the_run_at_the_very_end_is_still_stuffed(
		name: str, plain: str, wire: str, tmp_path: Path) -> None:
	"""RFC 1662 says "including the last 5 bits of the FCS" and USB 2.0
	section 7.1.9 says a zero goes in "even if it is the last bit before
	the end-of-packet signal", so a run completing on the final bit
	still stuffs. The counter resets afterwards, so ten ones stuff twice.
	"""
	encode, = _generated(py_derived.generate(parse_text(BITSTUFF), "unit"),
	                     tmp_path, f"{name}_encode")

	out = bytearray(32)
	written = encode(_bits(plain), len(plain), out)
	assert _unbits(bytes(out), written) == wire


@pytest.mark.parametrize("name,stream", (("hdlc", "0111111"),
                                         ("usb", "1111111")))
def test_a_run_the_encoder_cannot_produce_is_refused(
		name: str, stream: str, tmp_path: Path) -> None:
	"""Six ones for HDLC, seven for USB.

	situ has no framing layer, so it refuses where a real stack would
	call six ones a flag and seven an abort -- framing events rather
	than stream errors. C's behaviour and C's comment; kept deliberately
	rather than improved, because the reference is what a port matches.
	"""
	decode, = _generated(py_derived.generate(parse_text(BITSTUFF), "unit"),
	                     tmp_path, f"{name}_decode")

	assert decode(_bits(stream), len(stream), bytearray(32)) == 0


def test_truncation_is_not_an_error(tmp_path: Path) -> None:
	"""An input ending exactly where the stuffed bit belongs simply ends.

	Pinned because it is the plausible place for a port to add a refusal
	that C does not have -- and because `0` already means something else
	here, so inventing one would be indistinguishable from empty input.
	"""
	decode, = _generated(py_derived.generate(parse_text(BITSTUFF), "unit"),
	                     tmp_path, "hdlc_decode")

	out = bytearray(32)
	assert decode(_bits("11111"), 5, out) == 5
	assert _unbits(bytes(out), 5) == "11111"


@pytest.mark.skipif(RUSTC is None, reason="no rustc")
def test_rust_stuffs_the_published_way(tmp_path: Path) -> None:
	"""Rust's own executing test, for the reason this file keeps
	relearning: a backend without one is a backend nothing checks."""
	(tmp_path / "unit.rs").write_text(
		rs_derived.generate(parse_text(BITSTUFF), "unit"), encoding="ascii")

	calls = "\n".join(
		f'\tshow("{name}", "{plain}", {name}_encode);'
		for name, plain, _wire, _source in STUFF_VECTORS)

	(tmp_path / "main.rs").write_text(
		'#[path = "unit.rs"] mod unit;\n'
		"use unit::*;\n"
		"\n"
		"fn pack(s: &str) -> Vec<u8> {\n"
		"\tlet mut b = vec![0u8; s.len() / 8 + 2];\n"
		"\tfor (i, c) in s.chars().enumerate() {\n"
		"\t\tif c == '1' { b[i / 8] |= 0x80 >> (i % 8); }\n"
		"\t}\n"
		"\tb\n"
		"}\n"
		"\n"
		"fn show(name: &str, plain: &str,\n"
		"\t\tenc: fn(&[u8], usize, &mut [u8]) -> usize) {\n"
		"\tlet mut out = vec![0u8; 32];\n"
		"\tlet n = enc(&pack(plain), plain.len(), &mut out);\n"
		"\tlet bits: String = (0..n)\n"
		"\t\t.map(|i| if (out[i / 8] >> (7 - i % 8)) & 1 == 1\n"
		"\t\t\t{ '1' } else { '0' }).collect();\n"
		"\tprintln!(\"{} {}\", name, bits);\n"
		"}\n"
		"\n"
		"fn main() {\n" + calls + "\n}\n", encoding="ascii")

	built = subprocess.run(
		[RUSTC or "rustc", "--edition", "2021", "-A", "warnings",
		 "-o", str(tmp_path / "run"), str(tmp_path / "main.rs")],
		capture_output=True, text=True)
	assert built.returncode == 0, built.stderr

	ran = subprocess.run([str(tmp_path / "run")], capture_output=True,
	                     text=True)
	assert ran.returncode == 0, ran.stderr

	lines = ran.stdout.splitlines()
	assert len(lines) == len(STUFF_VECTORS)

	for line, (name, plain, wire, source) in zip(lines, STUFF_VECTORS):
		assert line == f"{name} {wire}", (
			f"Rust {name} {plain}: {line}, against {source}")
