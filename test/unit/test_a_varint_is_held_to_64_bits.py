"""A varint's value is held to 64 bits by every reader (26.942).

A ten-byte leb128 number puts its last group at bit 63, where one bit of it
fits. C's `uint64_t`, C++ on the same runtime and Rust's `u64` keep that bit
and lose the rest; Python's integer kept all seven, so the Python backend
and the walker read `80 80 80 80 80 80 80 80 80 7f`, zigzag-decoded, as
585684124340278263808 -- past 2^64, a number no 64-bit reader has -- where
the other three read 4611686018427387904. A be128 number of ten groups is
seventy bits and wraps the same way. The dissector, reading by the walker's
rules in Lua 5.4 integers, already wrapped, and is what showed it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import fourway
from situc import pack as packer
from situc.cli import analyse
from situc.dissector import generate
from situc.parser import parse
from test_dissector import LUA, read_back
from walker import report
from walker.image import load

SCHEMA = """target buffer;
endian big;
varint_type leb { encoding = leb128; max_bits = 64; }
varint_type zz  { encoding = leb128; max_bits = 64; transform = zigzag; }
varint_type be  { encoding = be128; max_bits = 64; max_bytes = 10; }
struct u { leb v; u8 last; }
struct z { zz v; u8 last; }
struct b { be v; u8 last; }
"""

#: Each message, and what each struct reads `v` as: worked by hand.
#: `7f` at bit 63 keeps one bit, 2^63, whose zigzag is 2^62; ten `ff`
#: groups of be128 are seventy one-bits, of which the low 64 stay.
MESSAGES = {
	"8080808080808080807f00": {"u": 1 << 63, "z": 1 << 62, "b": 127},
	"ffffffffffffffffff7f00": {"u": (1 << 64) - 1, "z": -(1 << 63),
	                           "b": (1 << 64) - 1},
}


def _value(listing: str, struct: str) -> int | None:
	"""`v`'s value in one struct's section of a listing."""
	inside = False
	for line in listing.splitlines():
		if line.startswith("-- "):
			inside = line[3:] == struct
			continue
		parts = line.split()
		if inside and parts and parts[0] == "v":
			return int(next(part[6:] for part in parts
			                if part.startswith("value=")))
	return None


def _schema(tmp_path: Path) -> Path:
	schema = tmp_path / "unit.situ"
	schema.write_text(SCHEMA, encoding="ascii")
	return schema


@pytest.mark.skipif(not fourway.COMPLETE, reason="needs gcc, g++ and rustc")
def test_the_four_backends_hold_it(tmp_path: Path) -> None:
	command = fourway.build(tmp_path, _schema(tmp_path))
	for message, wanted in MESSAGES.items():
		for backend, argv in command.items():
			said = fourway.answers(argv, bytes.fromhex(message), tmp_path)
			for struct, value in wanted.items():
				# A signed value prints as its two's complement in some
				# drivers and with a sign in others: compare modulo 2^64.
				got = _value(said, struct)
				assert got is not None and \
					got % (1 << 64) == value % (1 << 64), \
					(backend, message, struct, got)


def test_the_walker_holds_it(tmp_path: Path) -> None:
	source, resolved, _ = analyse(_schema(tmp_path))
	image = load(packer.pack(parse(source), resolved, metadata=True)[0])
	for message, wanted in MESSAGES.items():
		said = report.listing(image, bytes.fromhex(message))
		for struct, value in wanted.items():
			assert _value(said, struct) == value, (message, struct)


@pytest.mark.skipif(LUA is None, reason="no Lua interpreter")
def test_the_dissector_holds_it(tmp_path: Path) -> None:
	source, resolved, _ = analyse(_schema(tmp_path))
	lua = tmp_path / "unit.lua"
	lua.write_text(generate(parse(source), resolved, "unit"), encoding="ascii")
	for message, wanted in MESSAGES.items():
		for struct, value in wanted.items():
			_, rows = read_back(lua, struct, bytes.fromhex(message))
			assert (f"{struct}.v", 0, 10, str(value)) in rows, \
				(message, struct)
