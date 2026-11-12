"""A byte-order marker that states neither order is refused (26.964).

Every reader asked one question of a marker -- is it the `little` literal?
-- and read big-endian otherwise. So a header naming no order at all read
as a big-endian one: a TIFF starting `XX`, or a pcap file carrying the
nanosecond magic `a1 b2 3c 4d`, which validated with `ts_usec` holding
nanoseconds. Section 8.3 says exactly one encoding is valid given the
marker, and a third value is no marker.

`validate` refuses it now, as a constraint, in all four backends and both
walkers -- the walkers through the membership row an enum that rejects an
unknown value already writes.
"""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

import fourway
from test_a_struct_takes_its_byte_order import FILES, SCHEMA as PCAP
from test_walker_c import (COMPILER, c_verdict, packed_text, python_verdict,
                           shape_named_text)

ROOT = Path(__file__).resolve().parents[2]


def with_magic(order: str, magic: str) -> bytes:
	return bytes.fromhex(magic) + FILES[order][4:]


#: The nanosecond magic in each order, and a header of zeros.
REFUSED = {"nanosecond, big":    with_magic("big", "a1b23c4d"),
           "nanosecond, little": with_magic("little", "4d3cb2a1"),
           "no magic":           with_magic("little", "00000000")}


def test_all_four_refuse_a_third_value(tmp_path: Path) -> None:
	schema = tmp_path / "unit.situ"
	schema.write_text(PCAP, encoding="ascii")
	command = fourway.build(tmp_path, schema)
	for label, packet in {**REFUSED, **FILES}.items():
		given = {name: fourway.answers(argv, packet, tmp_path)
		         for name, argv in command.items()}
		first = next(iter(given.values()))
		assert all(answer == first for answer in given.values()), \
			(label, given)
		want = "validate 0" if label in FILES else "validate 2"
		assert want in first.splitlines(), (label, first)


@pytest.mark.skipif(COMPILER is None, reason="no C compiler")
def test_both_walkers_refuse_a_third_value(tmp_path: Path) -> None:
	blob  = packed_text(PCAP)
	shape = shape_named_text(PCAP, "pcap")
	for label, packet in REFUSED.items():
		assert python_verdict(blob, packet, shape) == "2", label
		assert c_verdict(tmp_path, blob, packet, shape) == "2", label
	tiff  = (ROOT / "example/tiff/tiff.situ").read_text(encoding="ascii")
	blob  = packed_text(tiff)
	shape = shape_named_text(tiff, "tiff_header")
	for marker, verdict in ((b"II", "0"), (b"MM", "0"), (b"XX", "2")):
		order  = "<" if marker == b"II" else ">"
		packet = marker + struct.pack(order + "HI", 42, 8)
		assert python_verdict(blob, packet, shape) == verdict, marker
		assert c_verdict(tmp_path, blob, packet, shape) == verdict, marker
