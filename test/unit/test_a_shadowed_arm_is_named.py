"""A bound's bare name that is both a sibling and an arm of the member's own
enum reads the sibling (26.1094), and a refusal over the sibling says so
(26.1150).

fuzznet wrote `fzn_reconcile_type type [must_eq = base]` beside `u64 base`,
meaning the arm; every backend refused the bound for comparing a 64-bit
unsigned value, and the arm's qualified spelling was found by trial. The
refusal now names the shadowing and the spelling that means the arm.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

SCHEMA = """target buffer;
endian big;

enum kind : u8 {
	other = 1,
	base  = 8,
}

struct r {
	u8   version [must_eq = 6];
	kind type    [must_eq = BOUND];
	u64  base    [min = 1];
}
"""


@pytest.mark.parametrize("target", ["c", "cpp", "rust", "python"])
def test_the_refusal_names_the_arm_it_shadows(tmp_path: Path,
		target: str) -> None:
	(tmp_path / "r.situ").write_text(SCHEMA.replace("BOUND", "base"),
	                                 encoding="ascii")
	built = subprocess.run(
		[sys.executable, "-m", "situc.cli", "build", "--target", target,
		 "--out", str(tmp_path / "out"), str(tmp_path / "r.situ")],
		capture_output=True, text=True, timeout=300, cwd=ROOT)
	assert built.returncode != 0
	assert "shadows the arm of `kind`" in built.stderr, built.stderr
	assert "write `kind.base` for the arm" in built.stderr, built.stderr

	(tmp_path / "r.situ").write_text(SCHEMA.replace("BOUND", "kind.base"),
	                                 encoding="ascii")
	built = subprocess.run(
		[sys.executable, "-m", "situc.cli", "build", "--target", target,
		 "--out", str(tmp_path / "out"), str(tmp_path / "r.situ")],
		capture_output=True, text=True, timeout=300, cwd=ROOT)
	assert built.returncode == 0, built.stderr
