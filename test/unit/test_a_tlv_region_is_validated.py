"""`validate` walks a `tlv` region's items to its end (26.737).

Nothing validated a `tlv` region in any of the six readers. Each walks the
items to count them and stops at the first it cannot read, so a cut item
and a `default: error` arm both read as the region ending there.
`example/protobuf` passed three malformed messages as zero items:

- a varint value cut short;
- a length-delimited value claiming five bytes and holding two;
- wire type 3, which the schema declares `default: error`.

The verdict is asserted, and not only agreement -- the six agreed on OK.
BOUNDS for an item the frame cuts short, CONSTRAINT for one no rule admits,
which is how each backend's item reader already reported them.

Two regions, because the fix is the shape's rather than protobuf's: a
CoAP option header sizes its value from a nibble of its own tag.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import fourway
from every_schema import ROOT
from test_walker_c import (COMPILER, c_verdict, image_for, packed_text,
                           python_verdict, shape_named, shape_named_text)

PROTOBUF = ROOT / "example" / "protobuf" / "protobuf.situ"

COAP = """target buffer;
endian little;
struct options {
	u8 head;
	tlv items (
		tag_type = u8,
		tag_decode = { delta = tag >> 4, length = tag & 0x0F },
		value_size = switch (delta) { default: length },
	);
}
"""

#: label: (verdict, bytes). 0 valid, 1 BOUNDS, 2 CONSTRAINT.
PROTOBUF_CASES = {
	"field 1 = 150":                      (0, bytes([0x08, 0x96, 0x01])),
	"no fields at all":                   (0, b""),
	"a varint value cut":                 (1, bytes([0x08, 0x96])),
	"a length claiming five, holding two": (1, bytes([0x12, 5, 0x61, 0x62])),
	"wire type 3, `default: error`":      (2, bytes([0x0B, 0x00])),
}

COAP_CASES = {
	"one whole item":                     (0, bytes([0, 0x13, 1, 2, 3])),
	"the only item's value cut":          (1, bytes([0, 0x13, 1])),
	"the second item's value cut":        (1, bytes([0, 0x11, 9, 0x13, 1])),
}


def _verdict(answer: str, struct: str) -> str:
	"""The `validate` line under `-- struct` in a driver's answer."""
	lines = answer.splitlines()
	start = lines.index(f"-- {struct}")
	return next(line for line in lines[start + 1:]
	            if line.startswith("validate "))


@pytest.mark.parametrize("schema, struct, cases", [
	(PROTOBUF, "proto_message", PROTOBUF_CASES),
	(None, "options", COAP_CASES),
], ids=["protobuf", "coap"])
def test_a_malformed_tlv_region_is_refused_by_all_four(
		tmp_path: Path, schema: Path | None, struct: str,
		cases: dict[str, tuple[int, bytes]]) -> None:
	if schema is None:
		schema = tmp_path / "unit.situ"
		schema.write_text(COAP, encoding="ascii")
	try:
		command = fourway.build(tmp_path, schema)
	except fourway.BuildFailed as failed:
		pytest.skip(f"a backend could not be built here: {failed}")

	for label, (verdict, packet) in cases.items():
		given = {name: _verdict(fourway.answers(argv, packet, tmp_path),
		                        struct)
		         for name, argv in command.items()}
		assert set(given.values()) == {f"validate {verdict}"}, (
			f"{label}: {given}")


@pytest.mark.skipif(COMPILER is None, reason="no C compiler")
def test_a_malformed_tlv_region_is_refused_by_both_walkers(
		tmp_path: Path) -> None:
	regions = [
		(image_for(PROTOBUF), shape_named(PROTOBUF, "proto_message"),
		 PROTOBUF_CASES),
		(packed_text(COAP), shape_named_text(COAP, "options"), COAP_CASES),
	]
	for blob, shape, cases in regions:
		for label, (verdict, packet) in cases.items():
			assert python_verdict(blob, packet, shape) == str(verdict), label
			assert c_verdict(tmp_path, blob, packet, shape) == str(verdict), \
				label


# -- `unknown = error` (26.738) ---------------------------------------------
#
# The default, and nothing enforced it: an item tagged outside `known`
# validated clean in all four. Three regions, because the policy has three
# readings worth holding apart -- refused under the default, admitted under
# `skip`, and matched on decision 0023's part rather than the raw tag where
# the schema names one.

KNOWN = """target buffer;
endian little;
struct strict {
	tlv items (
		tag_type    = u8,
		length_type = u8,
		known       = { 1 : { name = mtu, type = u8 } },
	);
}
struct lenient {
	tlv items (
		tag_type    = u8,
		length_type = u8,
		known       = { 1 : { name = mtu, type = u8 } },
		unknown     = skip,
	);
}
struct numbered {
	u8 head;
	tlv items (
		tag_type     = u8,
		tag_decode   = { delta = tag >> 4, length = tag & 0x0F },
		tag_running  = { number = previous + delta },
		tag_identity = number,
		value_size   = switch (delta) { default: length },
		known        = { 3 : { name = port, type = u8 } },
	);
}
struct numbered_lenient {
	u8 head;
	tlv items (
		tag_type     = u8,
		tag_decode   = { delta = tag >> 4, length = tag & 0x0F },
		tag_running  = { number = previous + delta },
		tag_identity = number,
		value_size   = switch (delta) { default: length },
		known        = { 3 : { name = port, type = u8 } },
		unknown      = skip,
	);
}
"""

#: struct, label, verdict, bytes.
KNOWN_CASES = [
	("strict", "a known tag", 0, bytes([1, 1, 9])),
	("strict", "an unknown tag", 2, bytes([2, 1, 9])),
	("strict", "a known tag, then an unknown one", 2,
	 bytes([1, 1, 9, 2, 1, 9])),
	("lenient", "an unknown tag under `skip`", 0, bytes([2, 1, 9])),
	# Option number 1 then 3 -- deltas 1 and 2. The raw tag 0x21 is not 3,
	# so an item matched on the raw tag would have been refused.
	("numbered", "numbers 1 and 3, only 3 known", 2,
	 bytes([0, 0x11, 9, 0x21, 9])),
	("numbered", "number 3 alone", 0, bytes([0, 0x31, 9])),
	("numbered_lenient", "numbers 1 and 3 under `skip`", 0,
	 bytes([0, 0x11, 9, 0x21, 9])),
]


def test_an_unknown_tag_is_refused_by_all_four(tmp_path: Path) -> None:
	schema = tmp_path / "unit.situ"
	schema.write_text(KNOWN, encoding="ascii")
	try:
		command = fourway.build(tmp_path, schema)
	except fourway.BuildFailed as failed:
		pytest.skip(f"a backend could not be built here: {failed}")

	for struct, label, verdict, packet in KNOWN_CASES:
		given = {name: _verdict(fourway.answers(argv, packet, tmp_path),
		                        struct)
		         for name, argv in command.items()}
		assert set(given.values()) == {f"validate {verdict}"}, (
			f"{struct}, {label}: {given}")


@pytest.mark.skipif(COMPILER is None, reason="no C compiler")
def test_the_walkers_decline_a_region_that_refuses_unknown_tags(
		tmp_path: Path) -> None:
	"""The image carries neither `known`'s tags nor the part they match, so
	a walker cannot enforce the policy; it must not call such a region valid
	either. `cannot-say` on the regions that refuse, and the verdict where
	the region admits anything.

	`strict` and `lenient` are the simple `length_type` form, which no
	walker reads at all -- the image has no selector for it -- so both are
	`cannot-say` whatever the policy, and `numbered_lenient` is the region
	whose verdict the walkers do give."""
	blob = packed_text(KNOWN)
	for struct, label, verdict, packet in KNOWN_CASES:
		shape    = shape_named_text(KNOWN, struct)
		expected = (str(verdict) if struct == "numbered_lenient"
		            else "cannot-say")
		assert python_verdict(blob, packet, shape) == expected, label
		assert c_verdict(tmp_path, blob, packet, shape) == expected, label
