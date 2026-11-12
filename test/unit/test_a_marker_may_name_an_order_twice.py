"""A byte-order marker may give an order more than one value (26.975).

pcap writes two pairs of magics: `a1 b2 c3 d4` for microsecond timestamps
and `a1 b2 3c 4d` for nanosecond ones, each in either byte order. A marker
took one value per order, so a nanosecond file could not be described --
26.964 refused it as naming no order at all. Naming an order again adds
a value:

    endian_marker magic : u32 {
        little = 0xD4C3B2A1, big = 0xA1B2C3D4,
        little = 0x4D3CB2A1, big = 0xA1B23C4D,
    }

Every reader's test is membership: the four backends' order test and
`validate`, both walkers, the dissector. The first value of an order is
the one a writer emits. Files are built by hand.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import fourway
from situc.parser import parse_text
from situc.unparse import unparse
from test_a_struct_takes_its_byte_order import FILES, SCHEMA as MICRO
from test_walker_c import (COMPILER, c_verdict, packed_text, python_verdict,
                           shape_named_text)

SCHEMA = MICRO.replace("""	big    = 0xA1B2C3D4,
}""", """	big    = 0xA1B2C3D4,
	little = 0x4D3CB2A1,
	big    = 0xA1B23C4D,
}""")

#: Each file with its magic replaced by the nanosecond one of its order.
NANO = {"little": bytes.fromhex("4d3cb2a1") + FILES["little"][4:],
        "big":    bytes.fromhex("a1b23c4d") + FILES["big"][4:]}


def test_all_four_read_a_nanosecond_file(tmp_path: Path) -> None:
	schema = tmp_path / "unit.situ"
	schema.write_text(SCHEMA, encoding="ascii")
	command = fourway.build(tmp_path, schema)
	for order, packet in {**NANO, **FILES}.items():
		for label, message in ((order, packet),):
			given = {name: fourway.answers(argv, message, tmp_path)
			         for name, argv in command.items()}
			first = next(iter(given.values()))
			assert all(answer == first for answer in given.values()), given
			lines = first.splitlines()
			assert "validate 0" in lines, (label, lines)
			assert "packets count=2" in lines, (label, lines)
			assert f"magic little={int(order == 'little')}" in lines, \
				(label, lines)
	# And a third value is still none of them.
	other = bytes.fromhex("a1b2c3d5") + FILES["big"][4:]
	given = {name: fourway.answers(argv, other, tmp_path)
	         for name, argv in command.items()}
	assert all("validate 2" in answer.splitlines()
	           for answer in given.values()), given


@pytest.mark.skipif(COMPILER is None, reason="no C compiler")
def test_both_walkers_read_a_nanosecond_file(tmp_path: Path) -> None:
	blob  = packed_text(SCHEMA)
	shape = shape_named_text(SCHEMA, "pcap")
	for order, packet in NANO.items():
		assert python_verdict(blob, packet, shape) == "0", order
		assert c_verdict(tmp_path, blob, packet, shape) == "0", order


def test_the_dissector_reads_a_nanosecond_file(tmp_path: Path) -> None:
	from test_dissector import LUA, read_back
	from situc.cli import analyse
	from situc.dissector import generate
	from situc.parser import parse

	if LUA is None:
		pytest.skip("no Lua interpreter")
	schema = tmp_path / "unit.situ"
	schema.write_text(SCHEMA, encoding="ascii")
	source, resolved, _ = analyse(schema)
	script = tmp_path / "unit.lua"
	script.write_text(generate(parse(source), resolved, "unit"),
	                  encoding="ascii")
	for order, packet in NANO.items():
		_, rows = read_back(script, "pcap", packet)
		shown = [(name, value) for name, _, _, value in rows]
		assert ("pcap.version_major", "2") in shown, (order, rows)
		assert [value for name, value in shown
		        if name == "record.incl_len"] == ["14", "4"], (order, rows)


def test_the_second_values_survive_a_round_trip() -> None:
	again = unparse(parse_text(SCHEMA))
	assert "little = 0x4D3CB2A1" in again and "big = 0xA1B23C4D" in again, again


def test_a_value_named_for_both_orders_is_refused() -> None:
	from situc.diagnostics import SituError

	with pytest.raises(SituError, match="for both orders"):
		parse_text("target buffer;\nendian_marker m : u16 { little = 0x4949,"
		           " big = 0x4D4D, big = 0x4949, }\n")
