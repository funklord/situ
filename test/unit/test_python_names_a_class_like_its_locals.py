"""Two Python crashes hull's agent protocol met under `situc verify` (26.992).

hull bisected both to 1cae66fc, which made `validate` measure the arm a
variant selects -- reaching two latent faults in the generated Python:

- a struct named like a local of the method that builds it: `start`, met
  by `start = 1` in the property measuring its arm, and called as the int;
- a walk that measured a variant's arm through its property, whose start
  reads the discriminant the walk exists to find: a RecursionError, where
  a second variant follows a first and a read follows both.

Both shapes are hull's, cut down. Every vector here is read by all four
backends as well as checked by `verify`, which reads through Python.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

import fourway

ROOT = Path(__file__).resolve().parents[2]

SCHEMA = """target buffer;
endian big;
struct start { u8 n; u8 v[n]; }
struct data { u8 k; }
struct entry { u8 len; u8 text[len]; }
struct absent { }
struct m {
	u8 kind;
	variant body switch (kind) {
		case 1: start start;
		case 2: data data;
		default: absent none;
	}
}
struct e {
	entry command;
	peek u8 after;
	variant first switch (after) {
		case 3: entry has_first;
		default: absent no_first;
	}
	peek u8 later;
	variant second switch (later) {
		case 3: entry has_second;
		default: absent no_second;
	}
	u8 n;
	u8 rest[n];
	u8 close [must_eq = 0];
}
"""

VECTORS = """m one 01 02 07 08
m two 02 05
e both 01 41 03 61 62 63 03 64 65 66 01 7a 00
e none 01 41 00 00
e second 01 41 03 64 65 66 02 79 7a 00
"""


def test_verify_reads_every_vector(tmp_path: Path) -> None:
	(tmp_path / "h.situ").write_text(SCHEMA, encoding = "ascii")
	(tmp_path / "h.vectors").write_text(VECTORS, encoding = "ascii")
	ran = subprocess.run([sys.executable, "-m", "situc", "verify",
	                      str(tmp_path / "h.situ"), str(tmp_path / "h.vectors")],
	                     capture_output = True, text = True, cwd = ROOT)
	assert ran.returncode == 0, ran.stderr
	assert "5 vectors conform" in ran.stdout + ran.stderr


@pytest.mark.skipif(not fourway.COMPLETE, reason = "needs gcc, g++ and rustc")
def test_all_four_agree_on_them(tmp_path: Path) -> None:
	source = tmp_path / "h.situ"
	source.write_text(SCHEMA, encoding = "ascii")
	command = fourway.build(tmp_path, source)
	assert len(command) == 4
	for line in VECTORS.splitlines():
		struct, label, *hexes = line.split()
		data = bytes.fromhex("".join(hexes))
		said = {name: fourway.answers(argv, data, tmp_path)
		        for name, argv in command.items()}
		assert len(set(said.values())) == 1, (label, said)
		section = said["c"][said["c"].index(f"-- {struct}\n"):]
		assert "validate 0" in section.split("\n-- ")[0], (label, section)


def test_both_walkers_read_them(tmp_path: Path) -> None:
	from test_walker_c import (COMPILER, _inline_image, c_verdict,
	                           python_verdict, shape_named_text)
	image = _inline_image(SCHEMA)
	for line in VECTORS.splitlines():
		struct, label, *hexes = line.split()
		data  = bytes.fromhex("".join(hexes))
		shape = shape_named_text(SCHEMA, struct)
		assert python_verdict(image, data, shape) == "0", label
		if COMPILER is not None:
			assert c_verdict(tmp_path, image, data, shape) == "0", label
