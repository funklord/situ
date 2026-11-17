"""Both walkers compute a checksum from the image, and agree (26.1058).

The Python walker could compute a derived checksum since 26.579 and only
`situ-edit` asked it to; the C walker read none of the three tables that
say how. So the two independent readers of a layout never checked the one
member whose value is a function of the others, and the four backends'
`_compute` was compared with nothing.

Three things are pinned here, each by a witness that is not situ's:

	PNG    the IEND chunk's CRC, `AE 42 60 82` in every PNG ever written
	IPv4   RFC 791's worked header, checksum `B8 61`
	ICMP   an echo request of zeros, whose RFC 1071 sum is `F7 FF`

and then every corpus schema with a computable tag, both walkers member
for member over random and planted messages -- with the count of computed
sums asserted, because two walkers that both decline everything agree.
"""
from __future__ import annotations

import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "test" / "unit"))

import pytest							# noqa: E402

from every_schema import SCHEMAS, ids				# noqa: E402
from fourway import draw, planted				# noqa: E402
from situc.diagnostics import Source				# noqa: E402
from situc.layout import solve					# noqa: E402
from situc.parser import parse					# noqa: E402
from situc.resolve import resolve				# noqa: E402
from test_walker_c import (COMPILER, _build_driver, _run_driver,	# noqa: E402
                           image_for, shape_named)
from walker import report					# noqa: E402
from walker.image import load					# noqa: E402
from walker.walk import Refused, Unplaceable, Unsupplied, acquire		# noqa: E402

pytestmark = pytest.mark.skipif(COMPILER is None, reason="needs a C compiler")

#: One line per member: `-` for a member that is not a tag, else what
#: `situ_walk_sum` answered.
SUMS = """situ_walk_placement held;
		if (situ_walk_placement_at(&image, first + i, &held) != SITU_WALK_OK) {
			return 1;
		}
		if ((held.flags & SITU_WALK_IS_TAG) == 0u) {
			printf("-\\n");
			continue;
		}
		uint32_t sum = 0u;
		const situ_walk_err got = situ_walk_sum(&image, msg, len, shape,
		                                        first + i, &sum);
		if (got == SITU_WALK_OK) {
			printf("sum=%u\\n", sum);
		} else if (got == SITU_WALK_UNSUPPORTED) {
			printf("cannot\\n");
		} else {
			printf("refused\\n");
		}"""


def python_sums(blob: bytes, message: bytes, shape: int) -> list[str]:
	"""The same lines from `report._sum`, which the listing prints."""
	image = load(blob)
	view  = acquire(image, message, shape)
	found = []
	for index in image.members(image.structs[shape]):
		if not image.placements[index].is_tag:
			found.append("-")
			continue
		try:
			said = report._sum(image, view, index, "t")
		except (Refused, Unplaceable):
			said = ["t.sum refused"]
		if not said:
			found.append("cannot")
		elif said[0] == "t.sum refused":
			found.append("refused")
		else:
			found.append(f"sum={said[0].split()[-1]}")
	return found


KNOWN = (
	("example/png/png.situ", "chunk",
	 "0000000049454E44AE426082", 0xAE426082),
	("example/ipv4/ipv4.situ", "ipv4_header",
	 "45000073000040004011b861c0a80001c0a800c7", 0xB861),
	("example/icmp/icmp.situ", "icmp_message",
	 "0800f7ff00000000", 0xF7FF),
)


@pytest.mark.parametrize("schema,struct,packet,expected", KNOWN,
                         ids=[one[1] for one in KNOWN])
def test_both_walkers_compute_the_published_checksum(
		schema: str, struct: str, packet: str, expected: int,
		tmp_path: Path) -> None:
	path  = ROOT / schema
	blob  = image_for(path)
	shape = shape_named(path, struct)
	message = bytes.fromhex(packet)

	drive = _build_driver(tmp_path, blob, SUMS)
	c_said  = _run_driver(drive, tmp_path, message, shape, by_line=True)
	py_said = python_sums(blob, message, shape)
	assert f"sum={expected}" in c_said, c_said
	assert c_said == py_said


def _computable(path: Path) -> bool:
	text = path.read_text(encoding="ascii")
	if "checksum" not in text or "covers" not in text:
		return False
	image = load(image_for(path))
	return bool(image.tags) and bool(image.kernels)


SUMMED = [path for path in SCHEMAS if _computable(path)]

#: Per schema, how many sums the sweep compared as values.
COMPUTED: dict[str, int] = {}


@pytest.mark.parametrize("schema", SUMMED, ids=ids(SUMMED))
def test_the_walkers_agree_about_every_sum(schema: Path,
		tmp_path: Path) -> None:
	source   = Source(str(schema), schema.read_text(encoding="ascii"))
	parsed   = parse(source)
	resolved = resolve(parsed, solve(parsed))
	blob     = image_for(schema)
	image    = load(blob)
	shapes   = sorted({shape for shape, held in enumerate(image.structs)
	                   if any(index in image.tags
	                          for index in image.members(held))})
	assert shapes, f"{schema.name}: a tag the image holds in no struct"

	drive = _build_driver(tmp_path, blob, SUMS)
	rng   = random.Random(20261117)
	sums  = 0
	for shape in shapes:
		for _ in range(12):
			message = planted(rng, parsed, resolved, draw(rng)) \
				if rng.random() < 0.5 else draw(rng)
			try:
				py_said = python_sums(blob, message, shape)
			except (Refused, Unplaceable, Unsupplied):
				continue
			c_said = _run_driver(drive, tmp_path, message, shape,
			                     by_line=True)
			assert c_said == py_said, (
				f"{schema.name} struct {shape} over {message.hex()}:\n"
				f"  C:      {c_said}\n  Python: {py_said}")
			sums += sum(one.startswith("sum=") for one in c_said)
	COMPUTED[schema.name] = sums


def test_the_sweep_computed_something() -> None:
	"""Two walkers that both decline everything agree, so the sweep has to
	have compared values. Measured when written: 84 sums over flac, icmp,
	ipv4, usb and edges. PNG's random lengths never fit and TCP's and UDP's
	sums run over a pseudo-header, so those three compare refusals -- and
	the published values above are what holds PNG's CRC."""
	if len(COMPUTED) < len(SUMMED):
		pytest.skip("the sweep did not run in full in this session")
	reached = sorted(name for name, sums in COMPUTED.items() if sums)
	assert len(reached) >= 5, reached
