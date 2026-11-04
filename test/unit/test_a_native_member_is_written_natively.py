"""C's builder and owned form write `native` as the view reads it (26.857).

    endian native;
    struct native [allow_host_dependent] { u24 wide; u64 big; u16 small; u8 tail; }

Each picked its byte order with "little, or else big", so a `native` member
was big-endian on a little-endian host, while the view beside it reads
`situ_get_ne`: a message built or encoded in C read back byte-swapped
through C's own accessors, and `validate` had nothing to say about it.
Found by building the same struct in four languages and comparing bytes
(26.858); a round trip through one backend's own owned pair is
self-consistent and cannot see it.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "test" / "unit"))

import fourway						# noqa: E402

RUNTIME = ROOT / "runtime"

SCHEMA = """target buffer;
endian native;
struct small_one [allow_host_dependent] { u16 a; u32 b; u64 c; }
"""

DRIVER = """#include <stdio.h>
#include "unit_build.h"
#include "unit_owned.h"
static int same(const uint8_t *bytes, uint32_t n)
{
	situ_view_t view = {(uint8_t *)bytes, n, 0u, NULL};
	return situ_small_one_a_get(view) == 0x1234u
	    && situ_small_one_b_get(view) == 0x12345678u
	    && situ_small_one_c_get(view) == 0x1122334455667788ull;
}
int main(void)
{
	uint8_t built[14], encoded[14];
	uint32_t wrote = 0;
	situ_small_one_t in = {0x1234u, 0x12345678u, 0x1122334455667788ull};
	situ_small_one_t out;

	if (situ_small_one_build(built, sizeof built, in.a, in.b, in.c, &wrote)
	    != SITU_OK) {
		return 2;
	}
	if (situ_small_one_encode(&in, encoded, sizeof encoded) != SITU_OK) {
		return 3;
	}
	/* Decode bytes the VIEW's order put down, not bytes this form wrote. */
	if (situ_small_one_decode(built, wrote, &out) != SITU_OK) {
		return 4;
	}
	printf("%d %d %d\\n", same(built, wrote), same(encoded, sizeof encoded),
	       out.b == in.b);
	return 0;
}
"""


@pytest.mark.skipif(fourway.HOST_CC is None, reason="no C compiler")
def test_built_and_encoded_bytes_read_back_through_the_view(
		tmp_path: Path) -> None:
	schema = tmp_path / "unit.situ"
	schema.write_text(SCHEMA, encoding="ascii")
	done = subprocess.run(
		[sys.executable, "-m", "situc", "build", str(schema), "--target", "c",
		 "--layer", "edit", "--owned", "--out", str(tmp_path)],
		capture_output=True, text=True, cwd=ROOT)
	assert done.returncode == 0, done.stderr[-800:]
	(tmp_path / "main.c").write_text(DRIVER, encoding="ascii")
	assert fourway.HOST_CC is not None
	built = subprocess.run(
		[fourway.HOST_CC, "-std=c11", f"-I{RUNTIME / 'c'}", "-I.", "main.c",
		 "unit.c", "unit_owned.c", str(RUNTIME / "c" / "situ.c"), "-o", "probe"],
		capture_output=True, text=True, cwd=tmp_path)
	assert built.returncode == 0, built.stderr[-1500:]
	ran = subprocess.run([str(tmp_path / "probe")], capture_output=True,
	                     text=True)
	assert ran.returncode == 0, ran.returncode
	assert ran.stdout.split() == ["1", "1", "1"]
