"""A `tlv` region in the simple form is one the walkers can measure.

`tag_type = u8, length_type = u8` puts one length before every value, so
there is nothing to select a rule on. The packer asked for a selector and
refused the region without one, "the tlv selector `None` is not a part the
tag decodes to", so no walker could measure the form openmlx4 writes all
of its regions in; any struct holding one was declined whole. Found when
26.1167's typed region joined `edges.situ` and the image census named it.

The form is `value_size = prefixed(u8)` for every tag, and is packed as
that one rule behind a selector of 0.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import fourway

SCHEMA = """target buffer;
endian big;
struct plain {
	u8 a;
	tlv items (tag_type = u8, length_type = u8,
	           known = { 1 : { name = one } },
	           unknown = skip, duplicate_tags = allowed);
}
"""

#: (packet, valid), worked by hand: the region runs to the frame's end and
#: each item is a tag, a length and that many bytes.
CASES = [
	("00", True),
	("00 01 01 ff", True),
	("00 01 01 ff 05 00", True),
	("00 01 02 ff", False),
	("00 01", False),
	("00 05 00 01 00", True),
]


@pytest.mark.skipif(not fourway.COMPLETE, reason="needs all four toolchains")
def test_the_walkers_answer_as_the_four_do(tmp_path: Path) -> None:
	from test_walker_c import (c_verdict, packed_text, python_verdict,
	                           shape_named_text)
	source = tmp_path / "unit.situ"
	source.write_text(SCHEMA, encoding="ascii")
	command = fourway.build(tmp_path, source)
	blob    = packed_text(SCHEMA)
	shape   = shape_named_text(SCHEMA, "plain")
	for packet, valid in CASES:
		data = bytes.fromhex(packet.replace(" ", ""))
		for name, argv in command.items():
			said = fourway.answers(argv, data, tmp_path)
			held = said.split("-- plain\n")[1].split("\n-- ")[0]
			assert ("validate 0" in held) == valid, (packet, name, held)
		walked = python_verdict(blob, data, shape)
		assert walked != "cannot-say", packet
		assert (walked == "0") == valid, (packet, walked)
		assert c_verdict(tmp_path, blob, data, shape) == walked, packet
