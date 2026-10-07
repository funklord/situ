"""A pad running past the end is short bytes, not a short frame (26.578).

Every backend's pad check is

	const uint32_t at = situ_byte_run_pad_offset(view);
	const uint32_t n  = situ_align_up_u32(at, 4u, view.limit) - at;
	for (i = 0; i < n; i++)
		if (situ_base(view)[at + i] != 0u) return SITU_ERR_CONSTRAINT;

-- the align-up is CLAMPED TO THE FRAME and the loop runs over however
many bytes that is. So a pad whose alignment would reach past the end is
as many zero bytes as are present.

The walker's offset chain already clamped it and `content_bits` did not,
so the two halves of one reader disagreed: `validate` called
`padded.byte_run` BOUNDS where C answers CONSTRAINT over a nonzero byte
it could see and the walker could not. **Not a disagreement about whether
a message is malformed but about which refusal it earns** -- and the
walker's own `_validate` says the first failure is the answer, so the
order decides the code a caller reads.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "test" / "unit"))

from every_schema import load_schema			# noqa: E402
from situc import pack as packer			# noqa: E402
from situc.layout import solve				# noqa: E402
from situc.resolve import resolve			# noqa: E402
from walker.image import Image, load			# noqa: E402
from walker import report				# noqa: E402
from walker.walk import View, content_bits, offset_bits	# noqa: E402

ERR_CONSTRAINT = 2

PADDED = ROOT / "test" / "schema" / "padded.situ"

#: A planted draw whose pad carries a nonzero byte and whose alignment
#: reaches past the end.
BUFFER = bytes.fromhex(
	"3420383637343330803780383031383480323030017f2035203980ff012001ff"
	"803730333633353337203132377fff383235397f7f39")


def _image() -> Image:
	parsed   = load_schema(PADDED)
	resolved = resolve(parsed, solve(parsed))
	blob, _  = packer.pack(parsed, resolved, metadata=True)
	return load(blob)


def _view(image: Image) -> tuple[int, View]:
	index = next(i for i, _ in enumerate(image.structs)
	             if image.struct_names[i] == "byte_run")
	return index, View(image, bytearray(BUFFER), index, 0, len(BUFFER))


def test_a_nonzero_pad_byte_is_a_constraint_not_a_short_frame() -> None:
	"""CONSTRAINT, which is what every backend answers."""
	image = _image()
	index, view = _view(image)
	found: list[tuple[int, int]] = []

	verdict = report._validate(image, view, index, found)
	assert verdict == ERR_CONSTRAINT, (
		f"read {verdict} where every backend answers {ERR_CONSTRAINT}: an "
		f"unclamped pad reports a short frame over a byte it can see")


def test_the_pad_span_agrees_with_the_offset_chain() -> None:
	"""The two halves of one reader, which is what went wrong.

	`offset_bits` clamps a pad to the frame and `content_bits` did not, so
	a member's own span exceeded what the chain had spent on it. Asserted
	as a relationship rather than a number: a pad can never claim more
	bytes than remain after where it starts, whatever the alignment is.
	"""
	image = _image()
	_, view = _view(image)

	pads = [index for index in range(len(image.placements))
	        if image.placements[index].pad_to
	        and image.placement_names[index].startswith("byte_run.")]
	assert pads, "padded.situ has no pad in `byte_run`"

	for index in pads:
		at   = offset_bits(view, index)
		span = content_bits(view, index)
		room = (view.limit - view.at) * 8
		assert at + span <= room, (
			f"{image.placement_names[index]} claims {span} bits at {at} "
			f"with {room} in the frame")
