"""`gen-checks` places a fixed run of wider values without a pointer (26.987).

`u32 fanout[255]` in a struct with a variable member was asked for its start
through `_fanout_ptr`, which no backend emits for a run wider than a byte:
such a run has an indexed getter and no pointer. Only a variable struct is
placed member by member, so `radiotap`'s `u16 he_data[6]`, in a fixed one,
never asked. git's pack index was the first to.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CC   = shutil.which("cc") or shutil.which("gcc")

SCHEMA = """target buffer;
endian big;
struct s {
	u32 n;
	u32 fan[3];
	u8  v[n];
	u8  rest[remaining];
}
"""


@pytest.mark.skipif(CC is None, reason = "no C compiler")
def test_the_generated_checks_build_and_pass(tmp_path: Path) -> None:
	source = tmp_path / "w.situ"
	source.write_text(SCHEMA, encoding = "ascii")
	for args in (["build", str(source), "--layer", "frame"],
	             ["gen-checks", str(source)]):
		subprocess.run([sys.executable, "-m", "situc", *args, "--out",
		                str(tmp_path)], check = True, capture_output = True,
		               cwd = ROOT)
	assert CC is not None
	built = subprocess.run(
		[CC, "-std=c11", "-Wall", "-Wextra", "-Werror",
		 f"-I{ROOT / 'runtime' / 'c'}", f"-I{tmp_path}",
		 str(tmp_path / "w_checks.c"), str(tmp_path / "w.c"),
		 str(ROOT / "runtime" / "c" / "situ.c"), "-o", str(tmp_path / "run")],
		capture_output = True, text = True)
	assert built.returncode == 0, built.stderr
	ran = subprocess.run([str(tmp_path / "run")], capture_output = True,
	                     text = True, timeout = 60)
	assert ran.returncode == 0, ran.stdout
