"""A walker does not read a `native` tlv tag or length as big (26.863).

The image is one file for every host, so the packer cannot resolve `native`
for it. It wrote the flags as "big unless little", and both walkers read a
`native` tag big on a little-endian machine while the four generated readers
read it little: the same bytes were valid to one and cut short to the other.

Each walker now does what it does for a `native` member: Python answers as
the machine it runs on and C declines, since a capture does not record which
machine wrote it.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

import fourway
from test_walker_c import (COMPILER, c_verdict, packed_text, python_verdict,
                           shape_named_text)

TAG = """target buffer;
endian native;
struct opts [allow_host_dependent] {
	u8 head;
	tlv items (
		tag_type = u16,
		tag_decode = { kind = tag >> 8, length = tag & 0x0F },
		value_size = switch (kind) { default: length },
	);
}
"""

LENGTH = """target buffer;
endian native;
struct opts [allow_host_dependent] {
	u8 head;
	tlv items (
		tag_type = u8,
		tag_decode = { kind = tag },
		value_size = switch (kind) { default: prefixed(u16) },
	);
}
"""


def _word(value: int) -> bytes:
	return value.to_bytes(2, sys.byteorder)


#: A two-byte value, and its number read the other way round is 512 or more.
CASES = {
	"tag":    (TAG, bytes([0]) + _word(0x0702) + b"ab"),
	"length": (LENGTH, bytes([0, 9]) + _word(2) + b"ab"),
}


@pytest.mark.parametrize("schema, message", CASES.values(), ids=CASES)
def test_the_four_read_it_as_the_host_does(tmp_path: Path, schema: str,
		message: bytes) -> None:
	path = tmp_path / "unit.situ"
	path.write_text(schema, encoding="ascii")
	try:
		command = fourway.build(tmp_path, path)
	except fourway.BuildFailed as failed:
		pytest.skip(f"a backend could not be built here: {failed}")
	for name, argv in command.items():
		said = fourway.answers(argv, message, tmp_path)
		assert "validate 0" in said.splitlines(), (name, said)


@pytest.mark.skipif(COMPILER is None, reason="no C compiler")
@pytest.mark.parametrize("schema, message", CASES.values(), ids=CASES)
def test_neither_walker_reads_it_as_big(tmp_path: Path, schema: str,
		message: bytes) -> None:
	blob  = packed_text(schema)
	shape = shape_named_text(schema, "opts")
	assert python_verdict(blob, message, shape) == "0"
	assert c_verdict(tmp_path, blob, message, shape) == "cannot-say"
