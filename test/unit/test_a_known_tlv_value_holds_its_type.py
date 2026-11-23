"""A `known` tlv value is a whole number of its `type`, each holding the
type's own checks (26.1167).

openmlx4 (inbox 20261123T1138Z) measured an 8-byte struct verifying in a
3-byte value and a date whose BCD year its struct refuses verifying inside
an image-info region. A struct `type` is the element filling the item's
length, so 16 bytes of a date are two. A scalar `type` is read two ways
by the corpus -- CoAP's uint options are `type = u16` over 0 to 2 bytes,
openmlx4's are runs of u32 -- so it is held to nothing. The verdicts are
worked by hand.

The struct a value holds is declared after the region on purpose: C and
C++ have to name its validator before its definition.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import fourway

SCHEMA = """target buffer;
endian big;
struct image_info {
	tlv tags (
		tag_type = u8, length_type = u16,
		known = {
			1 : { name = version,    type = u8 },
			2 : { name = build_time, type = build_time },
			3 : { name = area,       type = u32 },
		},
		unknown = skip
	);
}
struct build_time { u32 time_of_day; bcd4 year; bcd2 month; bcd2 day; }
"""

DATE = "00000000 20170928"
BINARY_YEAR = "00000000 07e1091c"

#: (label, packet, valid)
CASES = [
	("a date", "02 0008" + DATE, True),
	("a binary year", "02 0008" + BINARY_YEAR, False),
	("a date in three bytes", "02 0003 000000", False),
	("two dates", "02 0010" + DATE + DATE, True),
	("a date and a binary year", "02 0010" + DATE + BINARY_YEAR, False),
	# A scalar's `type` is held to nothing: CoAP's uint options are
	# `type = u16` over 0 to 2 bytes, where openmlx4's are runs of u32
	# (gate g35 caught the first reading refused).
	("a u32 in two bytes", "03 0002 0000", True),
	("a u32 in ten bytes", "03 000a" + "00" * 10, True),
	("three u32", "03 000c" + "00" * 12, True),
	("sixteen bytes of u8", "01 0010" + "41" * 16, True),
	("an unknown tag, skipped", "04 0003 aabbcc", True),
	("a good item, then a bad one", "01 0001 41 02 0003 000000", False),
]


@pytest.mark.skipif(not fourway.COMPLETE, reason="needs all four toolchains")
def test_the_four_hold_a_value_to_its_type(tmp_path: Path) -> None:
	source = tmp_path / "unit.situ"
	source.write_text(SCHEMA, encoding="ascii")
	command = fourway.build(tmp_path, source)
	assert len(command) == 4
	for label, packet, valid in CASES:
		data  = bytes.fromhex(packet.replace(" ", ""))
		given = {name: fourway.answers(argv, data, tmp_path)
		         for name, argv in command.items()}
		assert len(set(given.values())) == 1, (label, given)
		region = given["c"].split("-- image_info")[1].split("\n-- ")[0]
		assert ("validate 0" in region) == valid, (label, region)


@pytest.mark.skipif(not fourway.COMPLETE, reason="needs a C compiler")
def test_the_walkers_decline_rather_than_pass(tmp_path: Path) -> None:
	"""The image carries no element type, so both walkers say they cannot
	answer -- never `0` for the binary year the four refuse -- as they do
	for the region's other policies (26.738)."""
	from test_walker_c import (c_verdict, packed_text, python_verdict,
	                           shape_named_text)
	blob  = packed_text(SCHEMA)
	shape = shape_named_text(SCHEMA, "image_info")
	for label, packet, _ in CASES:
		data = bytes.fromhex(packet.replace(" ", ""))
		said = python_verdict(blob, data, shape)
		assert said == "cannot-say", (label, said)
		assert c_verdict(tmp_path, blob, data, shape) == said, label


FORMS = """target buffer;
endian big;
struct options {
	tlv tags (
		tag_type = u8, length_type = u16,
		known = {
			3 : { name = devices, type = u32, form = run },
			4 : { name = port,    type = u16, form = number },
			5 : { name = loose,   type = u32 },
		},
		unknown = skip
	);
}
"""

#: (label, packet, valid): a scalar `type` is held only where `form` says
#: which reading the schema means -- openmlx4's run of u32, or CoAP's
#: integer of at most the width -- and not at all where it says neither.
FORM_CASES = [
	("a run of three u32", "03 000c" + "00" * 12, True),
	("a run cut to two bytes", "03 0002 0000", False),
	("a run of none", "03 0000", True),
	("a number in one byte", "04 0001 05", True),
	("a number in two bytes", "04 0002 0105", True),
	("a number in three bytes", "04 0003 000105", False),
	("a number in none", "04 0000", True),
	("no form, two bytes of u32", "05 0002 0000", True),
]


@pytest.mark.skipif(not fourway.COMPLETE, reason="needs all four toolchains")
def test_a_form_says_which_reading(tmp_path: Path) -> None:
	source = tmp_path / "unit.situ"
	source.write_text(FORMS, encoding="ascii")
	command = fourway.build(tmp_path, source)
	assert len(command) == 4
	for label, packet, valid in FORM_CASES:
		data  = bytes.fromhex(packet.replace(" ", ""))
		given = {name: fourway.answers(argv, data, tmp_path)
		         for name, argv in command.items()}
		assert len(set(given.values())) == 1, (label, given)
		assert ("validate 0" in given["c"]) == valid, (label, given["c"])


def test_a_form_is_in_the_signature_and_refused_off_a_scalar() -> None:
	from situc import wire
	from situc.diagnostics import SituError
	from situc.layout import solve
	from situc.parser import parse_text
	from situc.resolve import resolve
	schema = parse_text(FORMS, path="f.situ")
	text = wire.render(schema, resolve(schema, solve(schema)), "f.situ")
	assert "devices type=u32 form=run" in text
	assert "port type=u16 form=number" in text
	with pytest.raises(SituError, match="for a scalar `type`"):
		parse_text(SCHEMA.replace("type = build_time }",
		                          "type = build_time, form = run }"),
		           path="f.situ")
	with pytest.raises(SituError, match="`run` or `number`"):
		parse_text(FORMS.replace("form = run", "form = list"), path="f.situ")


@pytest.mark.parametrize(("before", "after", "kind"), [
	("type = u32 }", "type = u32, form = run }", "backward"),
	("type = u32, form = run }", "type = u32 }", "forward"),
	("type = u32, form = run }", "type = u32, form = number }", "breaking"),
])
def test_wire_check_calls_a_form_a_narrowing(before: str, after: str,
                                             kind: str) -> None:
	"""openmlx4 adopting `form = run` was reported BREAKING, "read as
	something else", though no accepted value reads differently: adding a
	form only refuses values accepted before, which is the `backward`
	heading's sentence, and dropping one is its mirror. `run` for `number`
	stays breaking, each accepting what the other refuses."""
	from situc import wire
	from situc.layout import solve
	from situc.parser import parse_text
	from situc.resolve import resolve
	text = FORMS.replace("type = u32, form = run }", "TAG }")

	def signature(spelled: str) -> str:
		schema = parse_text(text.replace("TAG }", spelled), path="f.situ")
		return wire.render(schema, resolve(schema, solve(schema)), "f.situ")

	verdict = wire.compare(signature(before), signature(after))
	kinds = {finding.kind for finding in verdict.findings
	         if finding.kind != "format"}
	assert kinds == {kind}, verdict.findings
