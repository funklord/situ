"""`polynomial(..., augmented)`: the other way `init` enters a CRC (26.890).

openmlx4 found a Mellanox CRC agreeing with situ's on 5000 of 5000 messages at
`init = 0` and on none at `0xFFFF`, with nothing in the schema to say which
convention was meant. Both are correct CRCs: the catalogue's folds each bit in
at the top of the register, the augmented model shifts it in at the bottom and
flushes `width` zero bits at the end. The reference here is that second
algorithm written out bit by bit, which nothing in situ runs.
"""

from __future__ import annotations

import importlib
import random
import subprocess
import sys
from pathlib import Path

import pytest

import fourway
from situc.codegen.kernel_math import crc_init
from situc.parser import parse_text

ROOT = Path(__file__).resolve().parents[2]


def by_augmentation(message: bytes, width: int, poly: int, init: int,
                    reflect: bool = False) -> int:
	"""Shift every message bit in at the bottom, then `width` zeros."""
	mask = (1 << width) - 1
	bits = [byte >> (at if reflect else 7 - at) & 1
	        for byte in message for at in range(8)] + [0] * width
	held = init
	for bit in bits:
		carry = held >> (width - 1) & 1
		held  = (held << 1 | bit) & mask
		if carry:
			held ^= poly
	if reflect:
		held = int(f"{held:0{width}b}"[::-1], 2)
	return held


def schema(width: int, poly: int, init: int, extra: str = "") -> str:
	return ("target buffer;\nendian big;\n"
	        f"codec check {{ kernel = polynomial(width = {width}, poly = {poly},"
	        f" init = {init}, augmented{extra}); }}\n"
	        "impl check derived;\n"
	        f"struct s {{ authenticated body {{ u8 a; u8 b; }}"
	        f" checksum u8 c[{width // 8}] covers(body) is check; }}\n")


def start_of(text: str) -> int:
	return crc_init(parse_text(text, path="s.situ").codecs()[0])


def test_zero_is_where_the_two_agree() -> None:
	"""Which is how a wrong model goes unseen."""
	assert start_of(schema(16, 0x100B, 0)) == 0


def test_the_start_is_the_register_clocked_through_zeros() -> None:
	assert start_of(schema(16, 0x100B, 0xFFFF)) == 0xF6AA


def test_a_catalogue_init_is_left_alone() -> None:
	text = schema(16, 0x100B, 0xFFFF).replace(", augmented", "")
	assert start_of(text) == 0xFFFF


@pytest.mark.parametrize("width, poly, reflect", [
	(16, 0x100B, False), (16, 0x1021, False), (32, 0x04C11DB7, False),
	(8, 0x07, False), (16, 0x1021, True), (32, 0x04C11DB7, True),
])
def test_the_digest_is_the_augmented_algorithm_s(
		tmp_path: Path, width: int, poly: int, reflect: bool) -> None:
	"""Generated code, against the bit-by-bit reference, on random messages."""
	source = tmp_path / "aug.situ"
	source.write_text(schema(width, poly, (1 << width) - 3,
	                         ", reflect" if reflect else ""), encoding="ascii")
	subprocess.run([sys.executable, "-m", "situc", "build", str(source),
	                "--target", "python", "--out", str(tmp_path)],
	               check=True, capture_output=True, cwd=ROOT)
	sys.path[:0] = [str(tmp_path), str(ROOT / "runtime" / "python")]
	try:
		sys.modules.pop("aug", None)
		built = importlib.import_module("aug")
	finally:
		del sys.path[:2]
		sys.modules.pop("aug", None)
	held = built
	picked = random.Random(width + poly)
	for _ in range(200):
		message = picked.randbytes(picked.randrange(0, 40))
		assert held.check(message) == by_augmentation(
			message, width, poly, (1 << width) - 3, reflect), message.hex()


@pytest.mark.skipif(not fourway.COMPLETE, reason="needs all four toolchains")
def test_the_four_compute_one_digest(tmp_path: Path) -> None:
	"""A message carrying the right digest validates in all four."""
	source = tmp_path / "unit.situ"
	source.write_text(schema(16, 0x100B, 0xFFFF), encoding="ascii")
	command = fourway.build(tmp_path, source)
	assert len(command) == 4
	right = by_augmentation(b"\x12\x34", 16, 0x100B, 0xFFFF)
	for digest in (right, right ^ 1):
		packet = b"\x12\x34" + digest.to_bytes(2, "big")
		given = {name: fourway.answers(argv, packet, tmp_path)
		         for name, argv in command.items()}
		assert len(set(given.values())) == 1, given
	good = fourway.answers(command["c"], b"\x12\x34"
	                       + right.to_bytes(2, "big"), tmp_path)
	assert "validate 0" in good
