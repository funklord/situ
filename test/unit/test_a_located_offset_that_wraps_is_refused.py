"""A located run whose offset and length sum past 2^32.

`Probe.LOCATED` compares what four backends say about a byte run placed at
an offset the message chooses, and the random-bytes differential beside it
cannot reach the one failure that matters here. The arithmetic says why:
the wrapping form of the bounds check, `at + n > size`, is wrong only when
the sum lands back inside the buffer -- `at + n` in `[2^32, 2^32 + size]`,
which for a 60-byte buffer is **61 values out of 2^32**, about 1.4e-8 per
draw. Forty-eight random buffers per schema will never see it.

So the probe guards agreement and this guards the answer. Measured both
ways before it was written: with the correct clamp all four say
`pixels ok=0`, and with C's clamp replaced by the wrapping form the C
driver does not disagree -- it **dies**, having accepted the offset and
read at `base + 0x80000001`. `fourway.answers` counts a driver that dies
as part of its answer, which is why that is caught rather than silent.

The assertion pins the VALUE and not only the agreement, because four
backends that all adopted the wrapping form would agree with each other
about a buffer they should all refuse. That is `evidence.md`'s *break the
symmetry, not a side of it*.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fourway import COMPLETE, answers, build

#: `bitmap_file.pixels` is the located run with the widest offset field in
#: the corpus -- a `u32` -- which is what makes the sum reach 2^32 at all.
#: `edges.located_tail.payload` is driven by a `u8` and cannot.
BMP = Path(__file__).resolve().parents[2] / "example" / "bmp" / "bmp.situ"

#: `pixel_offset + image_size` is exactly 2^32, so the wrapping form reads
#: the sum as 0 -- inside any buffer -- while the non-wrapping form sees a
#: length larger than the message and refuses.
PIXEL_OFFSET = 0x80000001
IMAGE_SIZE   = 0x7FFFFFFF


def _crafted() -> bytes:
	packet = bytearray(60)
	packet[0:2]   = b"BM"
	packet[10:14] = PIXEL_OFFSET.to_bytes(4, "little")
	packet[34:38] = IMAGE_SIZE.to_bytes(4, "little")
	return bytes(packet)


@pytest.mark.skipif(not COMPLETE, reason="needs all four toolchains")
def test_a_located_offset_that_wraps_is_refused_by_all_four(
		tmp_path: Path) -> None:
	assert (PIXEL_OFFSET + IMAGE_SIZE) % 2**32 == 0, \
		"the crafted offset no longer wraps, so this test proves nothing"

	command = build(tmp_path, BMP)
	assert command, "no driver could be built for bmp"

	given = {name: answers(argv, _crafted(), tmp_path)
	         for name, argv in command.items()}

	assert len(set(given.values())) == 1, (
		"the four disagree about a located offset that wraps:\n"
		+ "\n".join(f"-- {name}\n{text}" for name, text in given.items()))

	for name, text in given.items():
		lines = [line for line in text.splitlines()
		         if line.startswith("pixels ")]
		assert lines, f"{name} printed no `pixels` line; the probe is not reached"
		assert lines[0] == "pixels ok=0 len=0 first=-1", (
			f"{name} accepted a located offset that wraps: {lines[0]}")
