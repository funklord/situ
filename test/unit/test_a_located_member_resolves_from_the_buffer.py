"""`at expr` is measured from the buffer, not from the view (26.583).

A located member -- bmp's `pixels at file.pixel_offset`, section 9.8 --
carries an offset the data states. What that offset is measured FROM has a
right answer and a wrong one, and until a document could be opened
part-way into a file (26.581) the two were the same byte for every reader
in this repository: `bitmap_file` was always acquired at zero, so
buffer-relative and view-relative agreed by construction.

The generated C is unambiguous:

	out->base = msg->base + at;

`msg`, not `view` -- its own comment says *the view reads the offset
field, the message says where zero is*. So the walker subtracts the
view's base before answering, because `offset_bits` answers view-relative
and every caller adds it back. 26.228 fixed that for a NESTED struct,
where the walker had read two bytes past the generated code. What it
could not then test is a TOP-LEVEL struct at a non-zero base, because
nothing could open one.

Measured now, both readers asked the same question about a 70-byte BMP
embedded at offset 8 of a 78-byte container:

	C says      pixels start at absolute 54, 16 bytes
	walker says view-relative 46, absolute 54, 16 bytes

**AND THAT AGREEMENT IS A LIMITATION, NOT A REASSURANCE.** Absolute 54 is
inside the eight bytes of container preceding the BMP -- `bfOffBits` is
defined from the start of the file, and a window is not a file. So
`--offset` composes with everything except a located member: an embedded
image's pixels resolve against the container. Both readers are right
about the format and the answer is useless, which is worth stating where
somebody would otherwise discover it by reading garbage.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "test" / "unit"))

import pytest					# noqa: E402
from every_schema import load_schema		# noqa: E402
from situc import pack as packer			# noqa: E402
from situc.codegen.c import generate as generate_c	# noqa: E402
from situc.layout import solve			# noqa: E402
from situc.parser import parse			# noqa: E402
from situc.diagnostics import Source		# noqa: E402
from situc.resolve import resolve			# noqa: E402
from walker.image import load			# noqa: E402
from walker.walk import acquire, offset_bits, size_bits	# noqa: E402

RUNTIME  = ROOT / "runtime" / "c"
HOST_CC  = shutil.which("gcc") or shutil.which("cc")
WARNINGS = ("-std=c11", "-O1", "-Wall", "-Wextra", "-Werror")

#: A 2x2 BMP ImageMagick wrote -- `convert -size 2x2 xc:red BMP3:` -- kept
#: as bytes so this test needs nothing installed. Its `pixel_offset` is 54
#: and its `image_size` 16, which are the two numbers under test; the pixel
#: values are not.
BMP = bytes.fromhex(
	"424d460000000000000036000000280000000200000002000000010018000000"
	"000010000000000000000000000000000000000000000000ff0000ff00000000"
	"ff0000ff0000")

#: Where the BMP sits inside its container. Eight bytes, so the wrong
#: reading differs from the right one by eight and cannot be mistaken for
#: an off-by-one in something else.
AT = 8
EMBEDDED = b"JUNKJUNK" + BMP

PIXEL_OFFSET = 54
IMAGE_SIZE   = 16


def _parts() -> tuple[object, object]:
	parsed   = load_schema(ROOT / "example" / "bmp" / "bmp.situ")
	resolved = resolve(parsed, solve(parsed))
	return parsed, resolved


def _walker_says(at: int) -> tuple[int, int]:
	"""Where the walker puts the pixels, absolutely, and how many."""
	parsed, resolved = _parts()
	blob, _ = packer.pack(parsed, resolved, metadata=True)	# type: ignore[arg-type]
	image   = load(blob)

	which = next(i for i in range(len(image.structs))
	             if image.struct_name(i) == "bitmap_file")
	view  = acquire(image, EMBEDDED, which, at = at)
	pixels = next(i for i in image.members(image.structs[which])
	              if image.name_of(i).endswith(".pixels"))
	return (view.at + offset_bits(view, pixels) // 8,
	        size_bits(view, pixels) // 8)


def test_the_walker_resolves_a_located_member_from_the_buffer() -> None:
	"""Absolute 54, for a view based at 8 -- not 62.

	The assertion is the relationship rather than the number: the same
	located member must land on the same absolute byte whatever base the
	view was acquired at, because the offset is the buffer's. A reader that
	resolved from the view would move it by exactly the base.
	"""
	assert _walker_says(AT) == (PIXEL_OFFSET, IMAGE_SIZE)

	# The relationship, which is what distinguishes the two readings: a
	# fixture whose BMP is at zero puts the pixels in the same place.
	parsed, resolved = _parts()
	blob, _ = packer.pack(parsed, resolved, metadata=True)	# type: ignore[arg-type]
	image   = load(blob)
	which   = next(i for i in range(len(image.structs))
	               if image.struct_name(i) == "bitmap_file")
	alone   = acquire(image, BMP, which)
	pixels  = next(i for i in image.members(image.structs[which])
	               if image.name_of(i).endswith(".pixels"))

	assert alone.at + offset_bits(alone, pixels) // 8 == PIXEL_OFFSET, (
		"the same located member moved when the base did, so it is being "
		"resolved from the view rather than from the buffer")


@pytest.mark.skipif(HOST_CC is None, reason="no C compiler on PATH")
def test_the_generated_c_agrees_at_a_non_zero_base(tmp_path: Path) -> None:
	"""The axis the four-backend differential has never compared.

	That differential acquires every one of its buffers at offset zero, for
	all forty-five schemas, because until 26.581 the walker could not be
	asked anything else. This is one construct on that axis -- the one where
	the base question has a wrong answer -- held to the compiled code rather
	than to a second reading of the same intent.
	"""
	parsed, resolved = _parts()
	built = generate_c(parsed, resolved, "bmp")		# type: ignore[arg-type]
	for name, text in built.files().items():
		(tmp_path / name).write_text(text, encoding="ascii")

	(tmp_path / "embedded.bin").write_bytes(EMBEDDED)
	(tmp_path / "probe.c").write_text(f"""
#include <stdio.h>
#include "bmp.h"

int main(void)
{{
	unsigned char raw[{len(EMBEDDED)}];
	FILE *f = fopen("embedded.bin", "rb");
	situ_msg_t  msg;
	situ_view_t view, pixels;

	if (!f || fread(raw, 1, sizeof raw, f) != sizeof raw) return 2;
	fclose(f);

	situ_msg_init(&msg, raw, sizeof raw);
	if (situ_bitmap_file_view(&msg, {AT}, &view) != SITU_OK) return 3;
	if (situ_bitmap_file_pixels_view(&msg, view, &pixels) != SITU_OK) return 4;

	printf("%ld %u\\n", (long)(pixels.base - msg.base), pixels.limit);
	return 0;
}}
""", encoding="ascii")

	assert HOST_CC is not None
	done = subprocess.run(
		[HOST_CC, *WARNINGS, f"-I{RUNTIME}", f"-I{tmp_path}",
		 str(tmp_path / "probe.c"), str(tmp_path / "bmp.c"),
		 str(RUNTIME / "situ.c"), "-o", str(tmp_path / "probe")],
		capture_output=True, text=True)
	assert done.returncode == 0, done.stderr

	ran = subprocess.run([str(tmp_path / "probe")], capture_output=True,
	                     text=True, cwd=tmp_path)
	assert ran.returncode == 0, f"the probe exited {ran.returncode}"

	theirs = tuple(int(part) for part in ran.stdout.split())
	assert theirs == _walker_says(AT), (
		f"C puts the pixels at {theirs} and the walker at "
		f"{_walker_says(AT)}, for a view based at {AT}")
	assert theirs == (PIXEL_OFFSET, IMAGE_SIZE)


def test_the_tool_says_so_when_a_window_cannot_reach_the_pixels() -> None:
	"""The actionable half: both readers are right and the answer is wrong.

	A BMP embedded at byte 8 reports its pixels at absolute 54, which is ten
	bytes short of its own -- the run shown is `0000...ff0000ff0000` where
	the image's pixels are `0000ff0000ff...`. Nothing refuses it, because
	the schema and the window are both legitimate and only the reader knows
	whether the offset is a container's or a file's. So the tool says it.
	"""
	from editor.document import open_document
	from editor.text import render

	parsed, resolved = _parts()
	blob, _ = packer.pack(parsed, resolved, metadata=True)	# type: ignore[arg-type]

	windowed = open_document(blob, EMBEDDED, "bitmap_file", AT)
	pixels   = next(field for field in windowed.fields()
	                if field.name == "pixels")

	assert "measured from the start of the buffer" in pixels.note, pixels.note
	assert str(AT) in pixels.note, pixels.note
	assert any("start of the buffer" in line for line in render(windowed)), (
		"the note never reaches the reader, which is the whole point of it")

	# And NOT at zero, where the two bases are the same byte and there is
	# nothing to warn about. A note on every BMP would be noise, and a gate
	# carrying noise is one somebody switches off.
	whole = open_document(blob, BMP, "bitmap_file")
	assert all("start of the buffer" not in field.note
	           for field in whole.fields()), "warned with nothing to warn of"


def test_the_pixels_a_window_shows_are_the_container_s() -> None:
	"""Named, so that the note above is not the only record of the cost.

	The ten bytes are a header's, not an image's, and both readers agree
	they are what `bfOffBits` names. Pinned because this is the behaviour a
	future `--offset` improvement would have to change deliberately rather
	than discover.
	"""
	from editor.document import open_document

	parsed, resolved = _parts()
	blob, _ = packer.pack(parsed, resolved, metadata=True)	# type: ignore[arg-type]

	windowed = open_document(blob, EMBEDDED, "bitmap_file", AT)
	whole    = open_document(blob, BMP, "bitmap_file")

	shown = next(f.value for f in windowed.fields() if f.name == "pixels")
	real  = next(f.value for f in whole.fields() if f.name == "pixels")

	assert isinstance(shown, bytes) and isinstance(real, bytes)
	assert shown == EMBEDDED[PIXEL_OFFSET:PIXEL_OFFSET + IMAGE_SIZE]
	assert real  == BMP[PIXEL_OFFSET:PIXEL_OFFSET + IMAGE_SIZE]
	assert shown != real, (
		"the window and the whole file show the same pixels, so this "
		"fixture no longer demonstrates the cost it was built for")
