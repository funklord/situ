"""A builder counts each counted run of structs on its own.

fuzznet (inbox 20261121T0130Z) built a routing manifest -- a count and a
run of entries, then a count and a run of hops -- and the C builder walked
both runs into one counter, so `hop_count` was written as entries plus
hops: one entry and no hops wrote a hop count of 1, and the builder's own
validate refused what it had just built. Python and Rust declared a
counter per run and were right.

And a count past what its field holds was written cut: 256 elements under
a `u8` count went down as 0, where Python and Rust refused it.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT     = Path(__file__).resolve().parents[2]
COMPILER = shutil.which("cc") or shutil.which("gcc")

SCHEMA = """target buffer;
endian big;
struct entry { u16 id; u8 len; u8 name[len]; }
struct hop { u32 addr; }
struct routing {
	u8 entry_count;
	entry entries[entry_count];
	u8 hop_count [max = 8];
	hop hops[hop_count];
	u8 sig[4];
}
"""

DRIVE = r"""#include <string.h>
#include "two_build.h"

int main(void)
{
	static const uint8_t one[] = { 0x00, 0x07, 0x02, 'a', 'b' };
	static const uint8_t hop[] = { 0x0a, 0x00, 0x00, 0x01 };
	static const uint8_t sig[4] = { 1, 2, 3, 4 };
	static const uint8_t want[] = {
		0x01, 0x00, 0x07, 0x02, 'a', 'b',	/* one entry */
		0x00,					/* no hops */
		1, 2, 3, 4,
	};
	static uint8_t many[256u * sizeof one];
	uint8_t  buf[2048];
	uint32_t wrote = 0;
	unsigned i;

	if (situ_routing_build(buf, sizeof buf, one, sizeof one, NULL, 0u,
	                       sig, &wrote) != SITU_OK
	    || wrote != sizeof want || memcmp(buf, want, wrote) != 0) {
		return 1;
	}
	/* Two of each: the second count is its own run's. */
	{
		uint8_t two[2u * sizeof one];
		uint8_t hops[2u * sizeof hop];
		memcpy(two, one, sizeof one);
		memcpy(two + sizeof one, one, sizeof one);
		memcpy(hops, hop, sizeof hop);
		memcpy(hops + sizeof hop, hop, sizeof hop);
		if (situ_routing_build(buf, sizeof buf, two, sizeof two, hops,
		                       sizeof hops, sig, &wrote) != SITU_OK
		    || buf[0] != 2u || buf[1u + sizeof two] != 2u) {
			return 2;
		}
	}
	for (i = 0; i < 256u; i++) {
		memcpy(many + i * sizeof one, one, sizeof one);
	}
	if (situ_routing_build(buf, sizeof buf, many, sizeof many, NULL, 0u,
	                       sig, &wrote) != SITU_ERR_CONSTRAINT) {
		return 3;
	}
	return 0;
}
"""


@pytest.mark.skipif(COMPILER is None, reason = "needs a C compiler")
def test_c_counts_each_run_apart(tmp_path: Path) -> None:
	schema = tmp_path / "two.situ"
	schema.write_text(SCHEMA, encoding = "ascii")
	out = tmp_path / "c"
	done = subprocess.run(
		[sys.executable, "-m", "situc", "build", str(schema), "--target",
		 "c", "--layer", "edit", "--out", str(out)],
		cwd = ROOT, capture_output = True, text = True)
	assert done.returncode == 0, done.stderr
	(tmp_path / "drive.c").write_text(DRIVE, encoding = "ascii")
	runtime = ROOT / "runtime" / "c"
	compiled = subprocess.run(
		[COMPILER or "cc", "-std=c11", "-pedantic-errors", "-Wall", "-Wextra",
		 "-Werror", "-Wconversion", "-Wsign-conversion", f"-I{runtime}",
		 f"-I{out}", str(tmp_path / "drive.c"), str(out / "two.c"),
		 str(runtime / "situ.c"), "-o", str(tmp_path / "drive")],
		capture_output = True, text = True)
	assert compiled.returncode == 0, compiled.stderr
	ran = subprocess.run([str(tmp_path / "drive")], timeout = 30)
	assert ran.returncode == 0


FIXED = """target buffer;
endian big;
struct inner { u8 a; }
struct names { u8 n; inner y[n]; }
"""


@pytest.mark.skipif(COMPILER is None, reason = "needs a C compiler")
def test_a_run_of_fixed_structs_declares_its_counter(tmp_path: Path) -> None:
	"""`u8 n; inner y[n];` -- a count found by walking a run of a FIXED
	struct -- counted into `situ_count` without declaring it: the
	declaration looked for the run's own part to name its element, and a
	fixed element's names none. Found probing derived names (2026-11-21)."""
	schema = tmp_path / "fixed.situ"
	schema.write_text(FIXED, encoding = "ascii")
	out = tmp_path / "c"
	done = subprocess.run(
		[sys.executable, "-m", "situc", "build", str(schema), "--target",
		 "c", "--layer", "edit", "--out", str(out)],
		cwd = ROOT, capture_output = True, text = True)
	assert done.returncode == 0, done.stderr
	(tmp_path / "drive.c").write_text(r"""#include "fixed_build.h"
int main(void)
{
	static const uint8_t y[] = { 7, 8, 9 };
	uint8_t  buf[8];
	uint32_t wrote = 0;
	return situ_names_build(buf, sizeof buf, y, sizeof y, &wrote) != SITU_OK
	       || wrote != 4u || buf[0] != 3u;
}
""", encoding = "ascii")
	runtime = ROOT / "runtime" / "c"
	compiled = subprocess.run(
		[COMPILER or "cc", "-std=c11", "-pedantic-errors", "-Wall", "-Wextra",
		 "-Werror", "-Wconversion", "-Wsign-conversion", f"-I{runtime}",
		 f"-I{out}", str(tmp_path / "drive.c"), str(out / "fixed.c"),
		 str(runtime / "situ.c"), "-o", str(tmp_path / "drive")],
		capture_output = True, text = True)
	assert compiled.returncode == 0, compiled.stderr
	assert subprocess.run([str(tmp_path / "drive")], timeout = 30).returncode == 0
