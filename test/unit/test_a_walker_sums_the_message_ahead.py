"""Both walkers and situ-edit compute a sum over the message ahead (26.1062).

26.1050 let a checksum cover every byte of the message ahead of it -- PCI
VPD's RV, which makes the image from its first byte through itself sum to
zero -- and left the walkers out: the image carried coverage as a run of
members, this is none, and the packer wrote no row. So two of the six
readers read RV as a plain byte, situ-edit left it stale after an edit
ahead of it, and no corpus schema held one, so no sweep asked.

Pinned here against sums worked by hand, as `-(bytes ahead) & 0xFF`:

	summed_ahead   90 02 01 02 | 6b           0x90 + 2 + 1 + 2 = 149
	ahead_held     12 34 | 90 02 01 02 | 25   the two bytes ahead count

and the case the backends got wrong while this was written: a declared
length that overruns the frame puts the tag past its end, and every reader
refuses rather than summing a span read off clamped lengths.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "test" / "unit"))

import pytest							# noqa: E402

from test_walker_c import (COMPILER, _build_driver, _run_driver,	# noqa: E402
                           image_for, shape_named)
from test_a_walker_checks_a_checksum import SUMS, python_sums	# noqa: E402
from walker import report					# noqa: E402
from walker.image import load					# noqa: E402
from walker.walk import _sub_view, acquire			# noqa: E402

pytestmark = pytest.mark.skipif(COMPILER is None, reason="needs a C compiler")

EDGES = ROOT / "test" / "schema" / "edges.situ"

AHEAD  = bytes([0x90, 2, 1, 2])
GOOD   = AHEAD + bytes([(-sum(AHEAD)) & 0xFF])
#: `length = 9` with one byte of data: the tag would sit past the end.
SHORT  = bytes([0x90, 9, 1])


def test_both_walkers_sum_the_message_ahead(tmp_path: Path) -> None:
	blob  = image_for(EDGES)
	shape = shape_named(EDGES, "summed_ahead")
	drive = _build_driver(tmp_path, blob, SUMS)

	c_said = _run_driver(drive, tmp_path, GOOD, shape, by_line=True)
	assert c_said == ["-", "-", "-", "sum=107"], c_said
	assert GOOD[-1] == 107
	assert python_sums(blob, GOOD, shape) == c_said


def test_a_tag_past_the_frame_is_refused_by_both(tmp_path: Path) -> None:
	blob  = image_for(EDGES)
	shape = shape_named(EDGES, "summed_ahead")
	drive = _build_driver(tmp_path, blob, SUMS)

	c_said = _run_driver(drive, tmp_path, SHORT, shape, by_line=True)
	assert c_said[-1] == "refused", c_said
	assert python_sums(blob, SHORT, shape) == c_said


def test_a_nested_tag_sums_from_the_message_s_first_byte() -> None:
	"""Not from the nested struct's: the two bytes ahead of it count, as
	they do for the backends' view owner and the dissector's root tvb."""
	image = load(image_for(EDGES))
	shape = shape_named(EDGES, "ahead_held")
	message = bytes([0x12, 0x34]) + AHEAD + bytes([0])
	view  = acquire(image, message, shape)
	inner = _sub_view(view, image.members(image.structs[shape])[1], 0)
	assert inner is not None
	tag = image.members(image.structs[inner.struct])[-1]
	want = (-(0x12 + 0x34 + sum(AHEAD))) & 0xFF
	assert report._sum(image, inner, tag, "inner.sum") \
		== [f"inner.sum.sum {want}"]


def test_situ_edit_recomputes_it_after_a_write_ahead() -> None:
	from editor.document import open_document

	from situc import pack as packer
	from situc.diagnostics import Source
	from situc.layout import solve
	from situc.parser import parse
	from situc.resolve import resolve

	parsed  = parse(Source(str(EDGES), EDGES.read_text(encoding="ascii")))
	blob, _ = packer.pack(parsed, resolve(parsed, solve(parsed)),
	                      metadata=True)
	doc  = open_document(blob, bytearray(GOOD), "summed_ahead")
	notes = doc.set("kind", 0x91)
	assert doc.buffer[-1] == (-(0x91 + 2 + 1 + 2)) & 0xFF, notes
	assert any("recomputed" in note for note in notes), notes


def test_the_four_backends_say_what_the_walkers_say(tmp_path: Path) -> None:
	"""All four computed 102 over `SHORT` while this was written: the span
	was read off lengths the frame clamps, so the tag sat at the frame's
	end and the sum ran over bytes the message does not say are there. The
	four agreed with each other, which is why it took the walkers to see
	it -- so they are held to the walkers' lines here, not only to each
	other."""
	import fourway

	command = fourway.build(tmp_path, EDGES)
	want = {GOOD: ["sum.covered 0+4", "sum.sum 107"],
	        SHORT: ["sum.covered refused", "sum.sum refused"]}
	for message, lines in want.items():
		for backend, run in command.items():
			said = [line for line in
			        fourway.answers(run, message, tmp_path).splitlines()
			        if line.startswith(("sum.covered", "sum.sum"))]
			assert said == lines, (backend, message.hex(), said)
