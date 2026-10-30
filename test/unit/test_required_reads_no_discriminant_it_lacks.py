"""C's `required` reads a variant's discriminant only where it has arrived.

A getter at a static offset trusts the view to have been acquired with the
struct's minimum size, and `required`'s view is whatever has arrived. So
for `peek u8 kind; variant held switch (kind) {...}` C read `data[0]` at
`have = 0` -- a byte the caller never handed over -- and chose the arm off
it: its answer for an empty buffer varied with whatever followed the
buffer, and the corpus differential caught it as C answering 2 where C++,
Rust and Python, whose reads check, answered 3 (26.753).

The probe below puts an 'a' in the slack after an empty prefix, which is
the byte that picks the two-byte arm: a C that reads it answers 2.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import fourway						# noqa: E402
from situc.codegen.c import generate as generate_c	# noqa: E402
from situc.layout import solve				# noqa: E402
from situc.parser import parse_text			# noqa: E402
from situc.resolve import resolve			# noqa: E402

HOST_CC = shutil.which("cc") or shutil.which("gcc")

SCHEMA = """target buffer;
endian big;
struct kinded_a { u8  tag; u8   body; }
struct kinded_b { u8  tag; u16  body; }
struct kinded {
	peek u8  kind;
	variant held switch (kind) {
		case 'a': kinded_a  as_a;
		default:  kinded_b  as_b;
	}
}
"""

PROBE = """#include "unit.h"
#include <stdio.h>
#include <stdlib.h>
int main(void)
{
	uint8_t *p = malloc(1);
	uint32_t need = 0u;
	p[0] = 'a';		/* slack: not part of the zero bytes handed over */
	(void)situ_kinded_required(p, 0u, &need);
	printf("%u\\n", need);
	free(p);
	return 0;
}
"""


@pytest.mark.skipif(HOST_CC is None, reason="no host C compiler")
def test_c_does_not_read_the_slack(tmp_path: Path) -> None:
	schema   = parse_text(SCHEMA)
	resolved = resolve(schema, solve(schema))
	built    = generate_c(schema, resolved, "unit")
	(tmp_path / "unit.h").write_text(built.header, encoding="ascii")
	(tmp_path / "unit.c").write_text(built.source, encoding="ascii")
	(tmp_path / "main.c").write_text(PROBE, encoding="ascii")
	made = subprocess.run(
		[HOST_CC or "cc", "-std=c11", "-g", f"-I{ROOT / 'runtime' / 'c'}",
		 f"-I{tmp_path}", str(tmp_path / "main.c"), str(tmp_path / "unit.c"),
		 str(ROOT / "runtime" / "c" / "situ.c"), "-o", str(tmp_path / "bin")],
		capture_output=True, text=True)
	assert made.returncode == 0, made.stderr[-2000:]
	ran = subprocess.run([str(tmp_path / "bin")], capture_output=True,
	                     text=True)
	assert ran.stdout.strip() == "3", ran.stdout


def test_all_four_answer_alike_for_an_empty_buffer(tmp_path: Path) -> None:
	schema = tmp_path / "unit.situ"
	schema.write_text(SCHEMA, encoding="ascii")
	command = fourway.build(tmp_path, schema)
	for packet in (b"", b"a", b"a\x01", b"b\x01\x02"):
		lines = {name: next(line for line in
		                    fourway.answers(argv, packet, tmp_path).split("-- ")
		                    if line.startswith("kinded\n")).splitlines()[-1]
		         for name, argv in command.items()}
		first = next(iter(lines.values()))
		assert all(line == first for line in lines.values()), lines
