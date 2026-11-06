"""A committed vector, cut or flipped, through both walkers (26.903).

Random bytes stop at the first refusal, and a real vector never reaches a
refusal at all. What lies between them is a message nearly right: a header
that holds and a body cut short. Mutating the vectors found five disagreements
in 16650 messages that every other sweep had passed, one of them the C walker
accepting a truncated JSON array.
"""

from __future__ import annotations

import random
from pathlib import Path

import pytest

from situc.codegen.c.vectors import parse_vectors
from situc.diagnostics import Source
from test_walker_c import (COMPILER, VERDICT, _build_driver, c_verdict,
                           image_for, python_verdict, shape_named)

ROOT    = Path(__file__).resolve().parents[2]
VECTORS = sorted(ROOT.glob("example/*/*.vectors"))
TRIES   = 25

#: Named in `test_a_walk_reads_nothing_past_its_view`: what the C walker
#: declines rather than guesses.
DECLINED = {"netlink", "tiff"}

#: Each was a disagreement. The backends are what said which walker was wrong.
FOUND = [
	# The C walker framed an arm by the rest of the frame, so the array's
	# floor passed on bytes past the arm: `["` was accepted.
	("json", "value", "5b22", "1"),
	("json", "value", "2c", "1"),
	# The Python walker clamped the arm to the frame and read on, so a member
	# of an arm the frame cuts answered CONSTRAINT.
	("mqtt", "packet", "1010000c4d5154540402003c0004", "1"),
	("modbus", "request", "10000100007f000a0102", "1"),
	# A located run past 2^29 bytes overflowed the C walker's count of bits.
	("bmp", "bitmap_file",
	 "424d4e00000000000000360000002800000003000000020000000100180000000000"
	 "180000ff000000000000000000000000000000ff0000ff0000ff0000000000ff0000"
	 "ff0000ff000000", "0"),
]


def _nearly(data: bytes, rng: random.Random) -> bytes:
	"""One small wrong step, as a cut frame or a bad field is."""
	held = bytearray(data)
	kind = rng.randrange(5)
	if kind == 0 and held:
		held[rng.randrange(len(held))] ^= 1 << rng.randrange(8)
	elif kind == 1 and held:
		held[rng.randrange(len(held))] = rng.choice(
			(0, 1, 2, 3, 0x7f, 0x80, 0xff))
	elif kind == 2 and held:
		del held[rng.randrange(len(held)):]
	elif kind == 3:
		held += rng.randbytes(rng.randrange(1, 5))
	elif kind == 4 and len(held) > 1:
		start = rng.randrange(len(held))
		del held[start:start + rng.randrange(1, 4)]
	return bytes(held)


@pytest.mark.skipif(COMPILER is None, reason="needs a C compiler")
@pytest.mark.parametrize("example, struct, message, verdict", FOUND,
                         ids=[found[0] + "-" + found[2][:8] for found in FOUND])
def test_what_the_sweep_found(example: str, struct: str, message: str,
		verdict: str, tmp_path: Path) -> None:
	path  = ROOT / "example" / example / f"{example}.situ"
	image = image_for(path)
	shape = shape_named(path, struct)
	data  = bytes.fromhex(message)
	assert python_verdict(image, data, shape) == verdict
	assert c_verdict(tmp_path, image, data, shape) == verdict


@pytest.mark.skipif(COMPILER is None, reason="needs a C compiler")
@pytest.mark.parametrize("vectors", VECTORS, ids=lambda path: path.stem)
def test_a_near_miss_gets_one_answer(vectors: Path, tmp_path: Path) -> None:
	path  = next(vectors.parent.glob("*.situ"))
	cases = parse_vectors(Source(str(vectors),
	                             vectors.read_text(encoding="utf-8")))
	image = image_for(path)
	(tmp_path / "img").write_bytes(image)
	drive = _build_driver(tmp_path, image, VERDICT)
	rng   = random.Random(vectors.stem)
	asked = 0
	for case in cases:
		shape = shape_named(path, case.struct)
		for _ in range(TRIES):
			data = _nearly(bytes(case.data), rng)
			if rng.randrange(2):
				data = _nearly(data, rng)
			said = c_verdict(tmp_path, image, data, shape, drive)
			if vectors.stem in DECLINED and said == "cannot-say":
				continue
			assert said == python_verdict(image, data, shape), data.hex()
			asked += 1
	assert asked or vectors.stem in DECLINED
