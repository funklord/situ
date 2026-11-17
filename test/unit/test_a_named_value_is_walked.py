"""Both walkers read a value a struct's fields assemble (26.1059).

26.1056 gave every backend a getter for `let` and left the image without
one, so the two independent readers of a layout -- and everything built on
them, situ-edit and the listing -- could not say what `.mfa`'s 48-bit
offset is. The image carries each `let` as a program now, opening with
`WRAP`: the backends' arithmetic, modulo 2^64 with `/` and `%` by zero 0,
where a length's division by zero is still refused.

Pinned here: the values of `edges`' `assembled`, worked by hand, in both
walkers, and the two walkers against each other over random messages. The
four backends are held to the Python walker's line by `test_walker`, which
compares every listing line with C's driver, and to each other by the
differential's `LET` probe.
"""
from __future__ import annotations

import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "test" / "unit"))

import pytest							# noqa: E402

from fourway import draw					# noqa: E402
from test_walker_c import (COMPILER, _build_driver, _run_driver,	# noqa: E402
                           image_for, shape_named)
from walker import report					# noqa: E402
from walker.image import _string_at, load			# noqa: E402
from walker.walk import Refused, Unplaceable, acquire		# noqa: E402

pytestmark = pytest.mark.skipif(COMPILER is None, reason="needs a C compiler")

EDGES = ROOT / "test" / "schema" / "edges.situ"

#: Every `let` of the struct, in declaration order, at the first member.
LETS = """if (i != 0u) {
			continue;
		}
		uint32_t lets = 0u;
		if (situ_walk_let_count(&image, shape, &lets) != SITU_WALK_OK) {
			return 1;
		}
		for (uint32_t k = 0u; k < lets; k++) {
			int64_t value = 0;
			int     sign  = 0;
			if (situ_walk_let(&image, msg, len, shape, k, &value, &sign)
					!= SITU_WALK_OK) {
				printf("refused\\n");
			} else if (sign) {
				printf("%lld\\n", (long long)value);
			} else {
				printf("%llu\\n", (unsigned long long)(uint64_t)value);
			}
		}"""

#: high, low, a, b, zero, big, then the tail `m` sizes.
MESSAGE = bytes([0x12, 0x34, 0x89, 0xAB, 0xCD, 0xEF, 0xFD, 200, 0, 70,
                 1, 2, 3])

#: offset, diff, quotient, rest, shifted, down, wrapped, flipped, negated, m
#: -- each by hand: 0x1234 << 32 | 0x89ABCDEF; -3 - 200; a zero divisor;
#: 0x89ABCDEF % 200; a shift of 70; 0x1234 >> 6; -600 mod 256;
#: ~0x89ABCDEF ^ 0xFFFF000000000000 over 64 bits; -(-3); 200 & 3.
WANT = [0x123489ABCDEF, -203, 0, 167, 0, 72, 168, 0x0000FFFF76543210, 3, 0]


def python_lets(blob: bytes, message: bytes, shape: int) -> list[str]:
	"""The same lines from the Python walker's listing, in row order."""
	image = load(blob)
	view  = acquire(image, message, shape)
	said  = dict(line.split(" ", 1)
	             for line in report._lets(image, view, shape))
	return [said.get(_string_at(image.strings, held.name), "refused")
	        for held in image.lets if held.owner == shape]


def test_both_walkers_read_the_worked_values(tmp_path: Path) -> None:
	blob  = image_for(EDGES)
	shape = shape_named(EDGES, "assembled")
	drive = _build_driver(tmp_path, blob, LETS)

	c_said = _run_driver(drive, tmp_path, MESSAGE, shape, by_line=True)
	assert c_said == [str(one) for one in WANT]
	assert python_lets(blob, MESSAGE, shape) == c_said


def test_a_short_message_refuses_in_both(tmp_path: Path) -> None:
	"""A field the value reads that is not there: no number either way."""
	blob  = image_for(EDGES)
	shape = shape_named(EDGES, "assembled")
	drive = _build_driver(tmp_path, blob, LETS)
	short = MESSAGE[:4]

	c_said = _run_driver(drive, tmp_path, short, shape, by_line=True)
	assert "refused" in c_said
	assert python_lets(blob, short, shape) == c_said


def test_the_walkers_agree_over_random_messages(tmp_path: Path) -> None:
	blob  = image_for(EDGES)
	shape = shape_named(EDGES, "assembled")
	drive = _build_driver(tmp_path, blob, LETS)
	rng   = random.Random(20261117)
	numbers = 0
	for _ in range(40):
		message = draw(rng)[:rng.randrange(4, 24)]
		try:
			py_said = python_lets(blob, message, shape)
		except (Refused, Unplaceable):
			continue
		c_said = _run_driver(drive, tmp_path, message, shape, by_line=True)
		assert c_said == py_said, message.hex()
		numbers += sum(one != "refused" for one in c_said)
	assert numbers >= 100, numbers
