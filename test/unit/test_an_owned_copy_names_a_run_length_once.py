"""The C edit layer's owned copy names a run's length once.

`u8 card_len; u8 card[card_len]` is the commonest way a schema writes a
length, and the owned copy declared `card_len` twice -- the field, and
the run's length beside its pointer -- so `_edit.h` did not compile for
any such struct. Found in fuzznet's contact request (2026-11-21). Where
`<run>_len` is taken, the run's length is `<run>_span`, and where that
is taken too, `<run>_run_len`.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT     = Path(__file__).resolve().parents[2]
RUNTIME  = ROOT / "runtime" / "c"
COMPILER = shutil.which("cc") or shutil.which("gcc")

SCHEMA = """target buffer;
endian big;

struct outer {
	u8 card_len;
	u8 card[card_len];
	u8 note_len;
	u8 tag[note_len];
	u8 sig_len;
	u8 sig_span;
	u8 sig[sig_len];
}
"""

PROGRAM = r"""#include <stdio.h>
#include "s_edit.h"
int main(void)
{
	uint8_t b[] = { 3, 'a', 'b', 'c', 1, 'z', 2, 0, 'q', 'r' };
	uint8_t backing[16];
	situ_msg_t m;
	situ_outer_edit_t out;
	situ_msg_init(&m, b, sizeof b);
	if (situ_outer_edit_decode(&m, sizeof b, backing, sizeof backing, &out)
	    != SITU_OK) {
		return 1;
	}
	/* `card_len` is the field; the run's own length is `card_span`. A run
	 * whose `_len` is free keeps it. */
	printf("%u %u %c %u %c %u\n", (unsigned)out.card_len, out.card_span,
	       out.card[2], out.tag_len, out.tag[0], out.sig_run_len);
	return 0;
}
"""


@pytest.mark.skipif(COMPILER is None, reason="needs a C compiler")
def test_a_length_field_named_for_its_run_compiles(tmp_path: Path) -> None:
	(tmp_path / "s.situ").write_text(SCHEMA, encoding="ascii")
	done = subprocess.run(
		[sys.executable, "-m", "situc", "build", str(tmp_path / "s.situ"),
		 "--target", "c", "--layer", "edit", "--out", str(tmp_path)],
		cwd=ROOT, capture_output=True, text=True)
	assert done.returncode == 0, done.stderr
	(tmp_path / "m.c").write_text(PROGRAM, encoding="ascii")
	compiled = subprocess.run(
		[COMPILER or "cc", "-std=c11", "-Wall", "-Wextra", "-Werror",
		 "-Wconversion", "-Wsign-conversion", f"-I{RUNTIME}",
		 f"-I{tmp_path}", str(tmp_path / "m.c"), str(tmp_path / "s.c"),
		 str(RUNTIME / "situ.c"), "-o", str(tmp_path / "m")],
		capture_output=True, text=True)
	assert compiled.returncode == 0, compiled.stderr
	ran = subprocess.run([str(tmp_path / "m")], capture_output=True,
	                     text=True, timeout=30)
	assert ran.stdout.split() == ["3", "3", "c", "1", "z", "2"], ran.stdout
