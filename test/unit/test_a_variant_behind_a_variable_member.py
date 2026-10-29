"""C's `validate` names where a variant starts, behind a variable member.

A variant whose arms are all one size is a fixed-size member, and behind a
variable-length one its offset is dynamic, so `validate` checks the frame
holds it. C wrote that check through the member's offset function -- and C
emits offset functions for arm MEMBERS, not for the variant, so the call
named `situ_s_body_offset`, which nothing defines, and the source did not
compile. C++, Rust and Python inline the sum (26.749).

A variant starts where its arms do, so C now takes its first arm member's
offset, as it already took an `authenticated` region's from its first
member. Found by the composed sweep in every `nested-fixed-u8-*-in-arm`
cell; no corpus schema puts a uniform-width variant behind a variable
member.
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
struct hdr { u16 lead; u8 n; }
enum arm_kind : u8 {
	first   = 0x11,
	second  = 0x22,
	default = error,
}
struct s {
	hdr  head;
	u8   gap[head.n];
	arm_kind  which;
	variant body switch (which) {
		case arm_kind.first:  u8  run[2];
		case arm_kind.second: u8  other[2];
	}
}
"""

#: `which` is in the frame; the arm it selects is two bytes or one.
CASES = {
	"the arm whole":        ("0", bytes.fromhex("000001aa11bbcc")),
	"the arm one byte short": ("1", bytes.fromhex("000001aa11bb")),
}


@pytest.mark.skipif(HOST_CC is None, reason="no host C compiler")
def test_the_c_source_compiles(tmp_path: Path) -> None:
	schema   = parse_text(SCHEMA)
	resolved = resolve(schema, solve(schema))
	built    = generate_c(schema, resolved, "unit")
	(tmp_path / "unit.h").write_text(built.header, encoding="ascii")
	(tmp_path / "unit.c").write_text(built.source, encoding="ascii")
	made = subprocess.run(
		[HOST_CC or "cc", "-std=c11", "-Wall", "-Wextra", "-Werror",
		 f"-I{ROOT / 'runtime' / 'c'}", f"-I{tmp_path}", "-c",
		 str(tmp_path / "unit.c"), "-o", str(tmp_path / "unit.o")],
		capture_output=True, text=True)
	assert made.returncode == 0, made.stderr[-2000:]


def test_all_four_check_the_arm_fits(tmp_path: Path) -> None:
	schema = tmp_path / "unit.situ"
	schema.write_text(SCHEMA, encoding="ascii")
	try:
		command = fourway.build(tmp_path, schema)
	except fourway.BuildFailed as failed:
		pytest.fail(f"a backend did not build: {failed}")
	for label, (verdict, packet) in CASES.items():
		for name, argv in command.items():
			lines = fourway.answers(argv, packet, tmp_path).splitlines()
			# The last `validate` line is `s`'s; `head` reports first.
			got = [line for line in lines if line.startswith("validate")]
			assert got and got[-1] == f"validate {verdict}", (
				f"{label}: {name} answered {lines!r}")
