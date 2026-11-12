"""A struct takes its byte order from its caller's marker (26.961).

A pcap file says its byte order once, in the header's magic, and every
record after it is in that order:

    struct record [endian = from(order)] {
        parameter magic order [stream];
        u32 ts_sec; ...; u32 incl_len; u32 orig_len; u8 data[incl_len];
    }
    struct pcap [endian = from(magic)] { endian_marker magic; ...;
        record(order = magic) packets[remaining]; }

A marker governed only its own struct's members, so `record` had no way
to say this: 26.833 named the question, and a whole pcap file -- or a
TIFF's directory entries -- was not something situ could describe. The
argument is the marker as the caller read it, big-endian, and each reader
compares it against the literal the marker declares.

And `from(m)` naming no marker at all was accepted: C called an
`is_little` nobody emits, and the walker refused every message.

The values below are the ones the records were built from, by hand.
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

import pytest

import fourway
from situc.diagnostics import SituError
from situc.parser import parse_text
from test_walker_c import (COMPILER, c_verdict, packed_text, python_verdict,
                           shape_named_text)

SCHEMA = """target buffer;

endian_marker magic : u32 {
	little = 0xD4C3B2A1,
	big    = 0xA1B2C3D4,
}

struct record [endian = from(order)] {
	parameter magic order [stream];
	u32  ts_sec;
	u32  ts_usec;
	u32  incl_len;
	u32  orig_len;
	u8   data[incl_len];
}

struct pcap [endian = from(magic)] {
	endian_marker magic;
	u16     version_major [must_eq = 2];
	u16     version_minor;
	i32     thiszone;
	u32     sigfigs;
	u32     snaplen;
	u32     network;
	record(order = magic)  packets[remaining];
}
"""

#: Two records: (ts_sec, ts_usec, data). Lengths are the data's.
RECORDS = ((1794486017, 1, bytes.fromhex("00112233445566778899aabb0800")),
           (1794486017, 2, bytes.fromhex("deadbeef")))


def capture(order: str) -> bytes:
	"""A pcap file in `order` ("<" or ">"), as libpcap writes one."""
	out = struct.pack(order + "IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 262144, 1)
	for seconds, micros, data in RECORDS:
		out += struct.pack(order + "IIII", seconds, micros, len(data),
		                   len(data)) + data
	return out


FILES = {"little": capture("<"), "big": capture(">")}


def test_all_four_read_each_order(tmp_path: Path) -> None:
	schema = tmp_path / "unit.situ"
	schema.write_text(SCHEMA, encoding="ascii")
	command = fourway.build(tmp_path, schema)
	for order, packet in FILES.items():
		given = {name: fourway.answers(argv, packet, tmp_path)
		         for name, argv in command.items()}
		first = next(iter(given.values()))
		assert all(answer == first for answer in given.values()), \
			(order, given)
		lines = first.splitlines()
		# A record read in the other order has an `incl_len` of 0x0E000000
		# and runs off the end, so two records and a valid file is the
		# order arriving.
		assert "packets count=2" in lines, (order, lines)
		assert "validate 0" in lines, (order, lines)
		assert f"magic little={int(order == 'little')}" in lines, \
			(order, lines)

	# And the values, through one backend, against the bytes they were
	# built from: four readers agreeing is not the same as four being right.
	sys.path.insert(0, str(tmp_path))
	try:
		import situ_runtime  # type: ignore[import-not-found]
		import unit  # type: ignore[import-not-found]
		for order, packet in FILES.items():
			raw  = bytearray(packet)
			view = unit.pcap.at(situ_runtime.Message(raw), 0, len(raw))
			assert view.version_major == 2 and view.snaplen == 262144, order
			held = [view.packets(index) for index in range(view.packets_count)]
			assert [(one.ts_sec, one.ts_usec, bytes(one.data))
			        for one in held] == list(RECORDS), order
	finally:
		sys.path.remove(str(tmp_path))
		sys.modules.pop("unit", None)


@pytest.mark.skipif(COMPILER is None, reason="no C compiler")
def test_both_walkers_read_each_order(tmp_path: Path) -> None:
	blob  = packed_text(SCHEMA)
	shape = shape_named_text(SCHEMA, "pcap")
	for order, packet in FILES.items():
		assert python_verdict(blob, packet, shape) == "0", order
		assert c_verdict(tmp_path, blob, packet, shape) == "0", order
		# The first record's length, made to run past the file in the
		# order the header states.
		broken = bytearray(packet)
		broken[32 if order == "little" else 35] = 0xFF
		assert python_verdict(blob, bytes(broken), shape) \
			== c_verdict(tmp_path, blob, bytes(broken), shape) != "0", order


def test_a_from_that_names_no_marker_is_refused() -> None:
	with pytest.raises(SituError, match=r"`from\(nothing\)` names no"):
		parse_text("target buffer;\n"
		           "endian_marker bo : u16 { little = 0x4949, big = 0x4D4D, }\n"
		           "struct s [endian = from(nothing)] { u16 a; }\n")
	# A marker TYPE is not a marker the struct can read.
	with pytest.raises(SituError, match=r"`from\(bo\)` names no"):
		parse_text("target buffer;\n"
		           "endian_marker bo : u16 { little = 0x4949, big = 0x4D4D, }\n"
		           "struct s [endian = from(bo)] { u16 a; }\n")


def test_a_handed_order_is_stream() -> None:
	with pytest.raises(SituError, match="must be `\\[stream\\]`"):
		parse_text(SCHEMA.replace("order [stream];", "order;"))
