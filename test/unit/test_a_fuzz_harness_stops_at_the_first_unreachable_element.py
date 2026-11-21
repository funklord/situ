"""A fuzz harness walking a counted run stops where the run's bytes end.

The C harness read every element of a run up to its COUNT -- what the
bytes claim, up to 2^32 on random input -- and `_at` walks from the run's
start on every call. So a CBOR map whose head claimed four billion pairs
and held one was walked four billion times, each walk through that pair's
nested items: `make test-c`'s smoke step, feeding 2048 random bytes, hung
for 30 minutes on one draw. `_at` fails only where the walk cannot reach
its index, so every later index fails too, and the harness now stops at
the first failure, losing no element it could have read.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT     = Path(__file__).resolve().parents[2]
COMPILER = shutil.which("cc") or shutil.which("gcc")


@pytest.mark.skipif(COMPILER is None, reason="no C compiler")
def test_a_map_claiming_four_billion_pairs_is_read_quickly(tmp_path: Path) -> None:
	schema = ROOT / "example" / "cbor" / "cbor.situ"
	for command in ("build", "gen-fuzz"):
		done = subprocess.run(
			[sys.executable, "-m", "situc", command, str(schema), "--out",
			 str(tmp_path)], cwd=ROOT, capture_output=True, text=True)
		assert done.returncode == 0, done.stderr
	harness = (tmp_path / "cbor_fuzz.c").read_text(encoding="ascii")
	cases = re.findall(r"\tcase (\d+)u:\n\t\t(fuzz_\w+)\(", harness)
	selector = next(int(at) for at, call in cases if call == "fuzz_map")

	runtime = ROOT / "runtime" / "c"
	built = subprocess.run(
		[COMPILER or "cc", "-std=c11", "-O1", "-DSITU_FUZZ_STANDALONE",
		 f"-I{runtime}", f"-I{tmp_path}", str(tmp_path / "cbor_fuzz.c"),
		 str(tmp_path / "cbor.c"), str(runtime / "situ.c"), "-o",
		 str(tmp_path / "fuzz")], capture_output=True, text=True)
	assert built.returncode == 0, built.stderr

	# Major 5 with a four-byte count of 2^32 - 1, then one pair: a key
	# under thirty tags, so each walk to the second pair is long, and 0.
	claimed = bytes([0xBA, 0xFF, 0xFF, 0xFF, 0xFF])
	pair    = bytes([0xC0] * 30) + bytes([0x00, 0x00])
	ran = subprocess.run([str(tmp_path / "fuzz")],
	                     input=bytes([selector]) + claimed + pair,
	                     capture_output=True, timeout=20)
	assert ran.returncode == 0, ran.stderr
