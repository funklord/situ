"""A builder writes a run of structs that take an argument (26.1017).

pcap's `record(order = magic) packets[remaining]` and TIFF's
`ifd_entry(order = order) entries[count]` were refused as "a run of
`record`, not of bytes": the element frames itself, but its `_required`
takes the byte order as well, and the writer had no value to pass. It has
one wherever the caller already handed it over -- the file's endian marker,
or the struct's own argument -- and passes that to each element's
`_required` and `_validate`, as the reader does. C only.

Judged by the real consumers: tshark reads the capture, Pillow decodes the
image.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from test_an_arm_sized_by_its_argument_is_written import COMPILER, ROOT, _built

TSHARK = shutil.which("tshark")

DRIVER = r"""#include <stdio.h>
#include <string.h>
#include "pcap_build.h"
#include "tiff_build.h"

static int save(const char *path, const uint8_t *data, uint32_t len)
{
	FILE *f = fopen(path, "wb");

	if (f == NULL || fwrite(data, 1u, len, f) != len) {
		return 1;
	}
	return fclose(f) != 0;
}

int main(int argc, char **argv)
{
	/* One record, little-endian as the magic says: 1700000000 s, 5 us,
	 * 14 bytes captured of 14, then a broadcast Ethernet header with the
	 * local experimental EtherType 0x88B5. */
	static const uint8_t record[30] = {
		0x00, 0xF1, 0x53, 0x65, 5, 0, 0, 0, 14, 0, 0, 0, 14, 0, 0, 0,
		0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0x02, 0, 0, 0, 0, 0x01,
		0x88, 0xB5 };
	uint8_t  swapped[30];
	/* Eight entries, little-endian: a 1x1 8-bit greyscale strip of one
	 * byte at offset 110 -- 8 header bytes, 2 + 8 * 12 + 4 of directory. */
	static const uint8_t entries[96] = {
		0x00, 0x01, 3, 0, 1, 0, 0, 0, 1, 0, 0, 0,	/* ImageWidth 1 */
		0x01, 0x01, 3, 0, 1, 0, 0, 0, 1, 0, 0, 0,	/* ImageLength 1 */
		0x02, 0x01, 3, 0, 1, 0, 0, 0, 8, 0, 0, 0,	/* BitsPerSample 8 */
		0x03, 0x01, 3, 0, 1, 0, 0, 0, 1, 0, 0, 0,	/* Compression none */
		0x06, 0x01, 3, 0, 1, 0, 0, 0, 1, 0, 0, 0,	/* BlackIsZero */
		0x11, 0x01, 4, 0, 1, 0, 0, 0, 110, 0, 0, 0,	/* StripOffsets */
		0x16, 0x01, 3, 0, 1, 0, 0, 0, 1, 0, 0, 0,	/* RowsPerStrip 1 */
		0x17, 0x01, 4, 0, 1, 0, 0, 0, 1, 0, 0, 0 };	/* StripByteCounts */
	uint8_t  out[256];
	uint32_t wrote = 0, at = 0;

	if (argc != 3) {
		return 9;
	}
	if (situ_pcap_build(out, sizeof out, 0xD4C3B2A1u, 4u, 0, 0u, 65535u, 1u,
	                    record, 30u, &wrote) != SITU_OK || wrote != 54u
	    || out[0] != 0xD4 || out[24] != 0x00 || out[25] != 0xF1) {
		return 1;
	}
	if (save(argv[1], out, wrote) != 0) {
		return 2;
	}
	/* The same record big-endian, in a little-endian file: its incl_len
	 * reads as 14 << 24 and it is not one record. */
	memcpy(swapped, record, 30u);
	swapped[8] = 0; swapped[11] = 14;
	swapped[12] = 0; swapped[15] = 14;
	if (situ_pcap_build(out, sizeof out, 0xD4C3B2A1u, 4u, 0, 0u, 65535u, 1u,
	                    swapped, 30u, &wrote) == SITU_OK) {
		return 3;
	}
	/* And big-endian in a big-endian file is right again. */
	swapped[0] = 0x65; swapped[1] = 0x53; swapped[2] = 0xF1; swapped[3] = 0;
	swapped[4] = 0; swapped[7] = 5;
	if (situ_pcap_build(out, sizeof out, 0xA1B2C3D4u, 4u, 0, 0u, 65535u, 1u,
	                    swapped, 30u, &wrote) != SITU_OK || wrote != 54u) {
		return 4;
	}

	if (situ_tiff_header_build(out, sizeof out, 0x4949u, 8u, &wrote)
	    != SITU_OK || wrote != 8u) {
		return 5;
	}
	at = wrote;
	if (situ_ifd_build(out + at, (uint32_t)sizeof out - at, 0x4949u,
	                   entries, 96u, 0u, &wrote) != SITU_OK || wrote != 102u
	    || out[at] != 8 || out[at + 1] != 0) {
		return 6;
	}
	at += wrote;
	out[at++] = 0x80;
	if (save(argv[2], out, at) != 0) {
		return 7;
	}
	/* The order is the argument's: under `MM` the count is big-endian.
	 * (The entries themselves are twelve fixed bytes, which read in the
	 * other order are other numbers rather than an error.) */
	if (situ_ifd_build(out, sizeof out, 0x4D4Du, entries, 96u, 0u, &wrote)
	    != SITU_OK || out[0] != 0 || out[1] != 8) {
		return 8;
	}
	return 0;
}
"""


@pytest.mark.skipif(COMPILER is None, reason = "needs a C compiler")
def test_a_capture_and_an_image(tmp_path: Path) -> None:
	pcap = _built(tmp_path, "pcap")
	tiff = _built(tmp_path, "tiff")
	(tmp_path / "drive.c").write_text(DRIVER, encoding = "ascii")
	runtime = ROOT / "runtime" / "c"
	built = subprocess.run(
		[COMPILER or "cc", "-std=c11", "-pedantic-errors", "-Wall", "-Wextra",
		 "-Werror", "-Wconversion", "-Wsign-conversion", f"-I{runtime}",
		 f"-I{pcap}", f"-I{tiff}", str(tmp_path / "drive.c"),
		 str(pcap / "pcap.c"), str(tiff / "tiff.c"), str(runtime / "situ.c"),
		 "-o", str(tmp_path / "drive")],
		capture_output = True, text = True)
	assert built.returncode == 0, built.stderr
	ran = subprocess.run([str(tmp_path / "drive"), str(tmp_path / "out.pcap"),
	                      str(tmp_path / "out.tif")], capture_output = True)
	assert ran.returncode == 0, f"check {ran.returncode} failed"

	if TSHARK is not None:
		read = subprocess.run(
			[TSHARK, "-r", str(tmp_path / "out.pcap"), "-T", "fields",
			 "-e", "frame.len", "-e", "eth.type", "-e",
			 "frame.time_epoch"],
			capture_output = True, text = True, timeout = 60)
		assert read.returncode == 0, read.stderr
		assert read.stdout.split() == ["14", "0x88b5",
		                               "1700000000.000005000"], read.stdout

	image = pytest.importorskip("PIL.Image")
	with image.open(tmp_path / "out.tif") as opened:
		assert opened.size == (1, 1)
		assert opened.mode == "L"
		assert opened.getpixel((0, 0)) == 0x80
