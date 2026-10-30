"""A setter for a field that drives a location stays in the frame (26.767).

    struct located_tail { u8 n; u8 v[n]; u8 spot; u8 payload[2] at spot; u8 tail; }

`spot` is placed after `v[n]`, so its offset is a sum the message chose,
and it drives where `payload` is, so its setter is the one that bumps the
message generation. C's stored at that offset with no test that the frame
reaches it: with `n = 255` in a three-byte message the offset clamps to
the frame's end and the store lands one byte past it (ASan,
heap-buffer-overflow). The plain and covered setters have tested this since
26.27, and C++, Rust and Python's setter for the same field does nothing
where it does not fit. Found by calling every C setter in the corpus over
exactly-sized buffers under ASan.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from situc.codegen.c import generate as generate_c	# noqa: E402
from situc.layout import solve				# noqa: E402
from situc.parser import parse_text			# noqa: E402
from situc.resolve import resolve			# noqa: E402

HOST_CC  = shutil.which("cc") or shutil.which("gcc")
SANITIZE = ["-g", "-fsanitize=address,undefined", "-fno-sanitize-recover=all"]

SCHEMA = """target buffer;
endian big;
struct located_tail {
	u8 n;
	u8 v[n];
	u8 spot;
	u8 payload[2] at spot;
	u8 tail;
}
"""

PROBE = """#include "unit.h"
#include <stdio.h>
#include <stdlib.h>
int main(void)
{
	uint8_t *p = calloc(3, 1);
	situ_msg_t m;
	situ_view_t view;
	p[0] = 255;		/* `v` claims more than the frame holds */
	situ_msg_init(&m, p, 3);
	if (situ_located_tail_view(&m, 0, 3, &view) != SITU_OK) {
		return 2;
	}
	situ_located_tail_spot_set(&m, view, 9);
	printf("%u %u %u\\n", p[0], p[1], p[2]);
	free(p);
	return 0;
}
"""


@pytest.mark.skipif(HOST_CC is None, reason="no host C compiler")
def test_c_does_not_store_past_the_frame(tmp_path: Path) -> None:
	schema = parse_text(SCHEMA)
	resolved = resolve(schema, solve(schema))
	built = generate_c(schema, resolved, "unit")
	(tmp_path / "unit.h").write_text(built.header, encoding="ascii")
	(tmp_path / "unit.c").write_text(built.source, encoding="ascii")
	(tmp_path / "main.c").write_text(PROBE, encoding="ascii")
	made = subprocess.run(
		[HOST_CC or "cc", "-std=c11", *SANITIZE,
		 f"-I{ROOT / 'runtime' / 'c'}", f"-I{tmp_path}",
		 str(tmp_path / "main.c"), str(tmp_path / "unit.c"),
		 str(ROOT / "runtime" / "c" / "situ.c"), "-o", str(tmp_path / "bin")],
		capture_output=True, text=True)
	assert made.returncode == 0, made.stderr[-2000:]
	ran = subprocess.run([str(tmp_path / "bin")], capture_output=True,
	                     text=True)
	assert ran.returncode == 0, ran.stderr[-2000:]
	# Nothing written: the frame does not reach `spot`.
	assert ran.stdout.strip() == "255 0 0", ran.stdout
