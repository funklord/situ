"""C names what a parent hands a child (0068).

    struct entry { parameter u16 block [stream]; u8 tag; u8 data[block]; }
    struct vol   { u16 block; entry(block = block) ent; entry(block = 2) two; }

A C view carries no arguments, so a caller holding `ent`'s view passes
`block` to every accessor on it, and had to re-derive what `vol` hands
`ent` from the schema by hand. `situ_vol_ent_arg_block(view)` is that
expression.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import fourway
from situc.codegen.c import generate as generate_c
from situc.diagnostics import Source
from situc.layout import solve
from situc.parser import parse
from situc.resolve import resolve

SCHEMA = """target buffer;
endian big;
struct entry { parameter u16 block [stream]; u8 tag; u8 data[block]; }
struct vol { u16 block; entry(block = block) ent; entry(block = 2) two; }
"""

DRIVER = """#include <stdio.h>
#include "unit.h"
int main(void)
{
	uint8_t bytes[] = {0x01, 0x02, 0xaa};
	situ_view_t view = {bytes, sizeof bytes, 0u, NULL};
	printf("%u %u\\n", (unsigned)situ_vol_ent_arg_block(view),
	       (unsigned)situ_vol_two_arg_block(view));
	return 0;
}
"""


@pytest.mark.skipif(fourway.HOST_CC is None, reason="no C compiler")
def test_the_value_is_the_parent_s(tmp_path: Path) -> None:
	schema = parse(Source("unit.situ", SCHEMA))
	for name, text in generate_c(schema, resolve(schema, solve(schema)),
	                             "unit").files().items():
		(tmp_path / name).write_text(text, encoding="ascii")
	(tmp_path / "main.c").write_text(DRIVER, encoding="ascii")
	assert fourway.HOST_CC is not None
	built = subprocess.run(
		[fourway.HOST_CC, "-std=c11", "-Wall", "-Wextra", "-Werror",
		 f"-I{fourway.RUNTIME / 'c'}", f"-I{tmp_path}",
		 str(tmp_path / "main.c"), str(tmp_path / "unit.c"),
		 str(fourway.RUNTIME / "c" / "situ.c"), "-o", str(tmp_path / "probe")],
		capture_output=True, text=True)
	assert built.returncode == 0, built.stderr
	ran = subprocess.run([str(tmp_path / "probe")], capture_output=True,
	                     text=True)
	assert ran.stdout.split() == ["258", "2"]
