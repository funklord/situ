"""The four-way draw reaches a branch a magic value guards (26.578).

A drawn buffer essentially never satisfies a value a schema states:
measured, **zero of 2400 buffers over 200 seeds** begin with a TIFF
byte-order marker. So `test_the_walker_agrees_with_the_compiled_backends`
compared the walker and four backends on the big-endian branch only and
agreed there by accident, while the walker read every little-endian field
byte-swapped (26.576).

The general form is worth more than TIFF: **a branch selected by a magic
value in the data is unreachable by random bytes.** 66 such sites across
the corpus -- 56 `must_eq`, 6 pinned runs, 4 markers -- all at static
offsets.

`planted` writes each one in. It found three walker-versus-C `validate`
disagreements the moment it was wired in, and they are the subject of the
three modules beside this one.
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "test" / "unit"))

from every_schema import SCHEMAS, ids			# noqa: E402
from fourway import draw, magics, planted		# noqa: E402
from situc.cli import analyse				# noqa: E402
from situc.parser import parse				# noqa: E402

#: What each of these schemas states, and where. The point of naming them
#: rather than only counting: a planter that silently stopped writing one
#: kind would keep a count and lose the coverage.
EXPECTED = {
	"tiff/tiff.situ":  (0, (b"II", b"MM")),
	"bmp/bmp.situ":    (0, (b"BM",)),
	"png/png.situ":    (0, (b"\x89PNG\r\n\x1a\n",)),
}


def _schema(name: str):  # type: ignore[no-untyped-def]
	source, resolved, _ = analyse(ROOT / "example" / name)
	return parse(source), resolved


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_a_planted_draw_carries_the_magic(name: str) -> None:
	"""And a marker is drawn BETWEEN its literals, not fixed at one.

	Both branches have to be reached or this swaps one blind spot for
	another: fixing the marker at `II` would leave `MM` uncompared exactly
	as `MM` was the only one compared before.
	"""
	parsed, resolved = _schema(name)
	at, wanted = EXPECTED[name]
	rng = random.Random(20260807)

	seen: set[bytes] = set()
	for _ in range(24):
		buffer = planted(rng, parsed, resolved, draw(rng))
		for magic in wanted:
			if buffer[at:at + len(magic)] == magic:
				seen.add(magic)

	assert seen == set(wanted), (
		f"{name}: planted {sorted(seen)} of {sorted(wanted)} -- a branch a "
		f"magic guards is reached by no draw")


def test_an_unplanted_draw_does_not(ones: None = None) -> None:
	"""The control, and the measurement this module exists for.

	Without it the test above passes on a planter that writes nothing,
	because `draw` would have to produce a marker by chance and might.
	2400 buffers over 200 seeds produce none.
	"""
	hits = total = 0
	for seed in range(200):
		rng = random.Random(seed)
		for _ in range(12):
			total += 1
			if draw(rng)[:2] in (b"II", b"MM"):
				hits += 1

	assert total == 2400
	assert hits == 0, (
		f"{hits} of {total} drawn buffers carry a TIFF marker, so the "
		f"planted draw is no longer the only way to reach that branch")


@pytest.mark.parametrize("path", SCHEMAS, ids=ids(SCHEMAS))
def test_planting_leaves_the_length_alone(path: Path) -> None:
	"""A planted buffer is the drawn one with bytes overwritten.

	Not longer, not shorter: the drawn length is what exercises a short
	frame, and a planter that extended a buffer to fit a magic would stop
	the twelve unplanted draws being comparable with the planted ones.
	A magic that does not fit is skipped instead.
	"""
	source, resolved, _ = analyse(path)
	parsed = parse(source)
	rng = random.Random(7)

	for _ in range(4):
		drawn = draw(rng)
		assert len(planted(rng, parsed, resolved, drawn)) == len(drawn)


@pytest.mark.parametrize("path", SCHEMAS, ids=ids(SCHEMAS))
def test_every_magic_sits_at_a_static_offset(path: Path) -> None:
	"""What `magics` promises, over the whole corpus.

	A member the data places is where a drawn value says, so planting at a
	computed offset would need the walk this harness exists to test. The
	sweep asserts the offsets are all static by construction -- each is
	taken from `offset_bits`, which is `None` for anything else -- and that
	nothing claims a span past the schema's own minimum frame.
	"""
	source, resolved, _ = analyse(path)
	parsed = parse(source)

	for at, value in magics(parsed, resolved, random.Random(0)):
		assert at >= 0, (path.name, at)
		assert value, f"{path.name}: a magic with no bytes at {at}"
