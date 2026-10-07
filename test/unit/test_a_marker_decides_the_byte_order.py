"""The walker resolves a byte-order marker, as the backends do (26.576).

`endian_marker` resolves byte order from a value in the data -- TIFF's
first two bytes say which order the rest of the file uses, which is
section 8.3's argument for the construct rather than for `endian native`.
The generated C branches on it:

	situ_tiff_header_magic_get(view) =
		is_little(view) ? situ_get_le16(base + 2) : situ_get_be16(base + 2)

`walker/walk.py` did not, and read everything big-endian. On a real
little-endian TIFF, through `situ-edit`:

	magic         10752        should be 42   (0x2A00)
	ifd_offset    335544320    should be 20   (0x14000000)

`tiff.situ` declares no file-level `endian` at all, so the order comes
only from `[endian = from(byte_order)]`. Every field of every real TIFF
was byte-swapped in `situ-edit`, `situ-edit-tui` and the Qt window, all
of which are built on this walker -- and `magic [must_eq = 42]` did not
catch it.

What the four-way comparison could not see is its own entry: a drawn
buffer essentially never begins with `II` or `MM`, so the walker and the
backends had only ever been compared on the big-endian branch.
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "test" / "unit"))

from every_schema import load_schema			# noqa: E402
from situc import pack as packer			# noqa: E402
from situc.layout import solve				# noqa: E402
from situc.resolve import resolve			# noqa: E402
from walker.image import Image, load			# noqa: E402
from walker.walk import View, read_scalar		# noqa: E402

TIFF = ROOT / "example" / "tiff" / "tiff.situ"

#: The same header in both orders. One value in two encodings is the whole
#: point: a reader that ignores the marker gets exactly one of them right,
#: so a test on one order alone passes for the wrong reason.
HEADERS = {
	"little": struct.pack("<2sHI", b"II", 42, 20) + bytes(40),
	"big":    struct.pack(">2sHI", b"MM", 42, 20) + bytes(40),
}


def _image() -> Image:
	parsed   = load_schema(TIFF)
	resolved = resolve(parsed, solve(parsed))
	blob, _  = packer.pack(parsed, resolved, metadata=True)
	return load(blob)


def _members(image: Image, name: str) -> tuple[int, dict[str, int]]:
	"""Each member's index by local name, for the struct called `name`."""
	index = next(i for i, _ in enumerate(image.structs)
	             if image.struct_names[i] == name)
	found: dict[str, int] = {}
	for member in image.members(image.structs[index]):
		local = image.placement_names[member].rsplit(".", 1)[-1]
		found[local] = member
	return index, found


@pytest.mark.parametrize("order", sorted(HEADERS))
def test_the_marker_decides_how_a_governed_member_reads(order: str) -> None:
	"""42 and 20 in both encodings, which is the discriminating pair.

	Before this the little-endian row read 10752 and 335544320 -- the same
	bytes read from the wrong end -- and the big-endian row was right, so
	either row alone is a test that passes whatever the walker does.
	"""
	image = _image()
	index, members = _members(image, "tiff_header")
	buffer = bytearray(HEADERS[order])
	view = View(image, buffer, index, 0, len(buffer))

	assert read_scalar(view, members["magic"]) == 42, order
	assert read_scalar(view, members["ifd_offset"]) == 20, order


def test_a_governed_member_refuses_rather_than_guessing() -> None:
	"""And the direction the failure falls, which is a design decision.

	A member whose order comes from a marker, with no marker in scope,
	refuses. The alternative -- falling back to big -- is silently wrong
	data, which is the whole of this entry: the walker read a TIFF for as
	long as it did precisely because a wrong order looks like a number.

	Asserted through the predicate rather than by building a schema that
	cannot exist: every marker-governed placement in this repository has
	its marker among its own struct's members, measured, so the refusal is
	unreachable from any corpus schema and this pins the intent.
	"""
	from walker.walk import _marker_order

	image = _image()
	index, _ = _members(image, "tiff_header")
	buffer = bytearray(HEADERS["little"])

	# A view whose struct has the marker: resolves.
	assert _marker_order(View(image, buffer, index, 0, len(buffer))) \
		== "little"

	# And one truncated before the marker: refuses rather than answering.
	from walker.walk import Refused
	with pytest.raises(Refused):
		_marker_order(View(image, buffer, index, 0, 1))
