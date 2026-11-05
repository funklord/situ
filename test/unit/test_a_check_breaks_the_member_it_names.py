"""A generated check writes to the member it names, under a name of its own.

Both faults came from `example/pes` (26.884) and neither failed anywhere else,
because no schema before it held a pinned member in a nested struct behind a
variant, or two arms whose structs pin a member of one name.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

HEAD = "target buffer;\nendian big;\nbit_order msb_first;\n"

#: The pin in `late` sits wherever `early` ended. Its offset is 0 in the
#: `stamp` struct and nowhere fixed in the message.
MOVING = HEAD + """
struct nothing { }
struct stamp { u8 mark [must_eq = 1]; }
struct holder {
	u1 a;
	u1 b;
	reserved u6 [must_be_zero];
	variant early switch (a) {
		case 1: u32 early_present;
		case 0: nothing early_absent;
		default: error;
	}
	variant late switch (b) {
		case 1: stamp late_present;
		case 0: nothing late_absent;
		default: error;
	}
}
"""

#: Two arms, each pinning a member called `mark` at one fixed offset.
ALIKE = HEAD + """
struct one { u8 mark [must_eq = 1]; u8 a; }
struct two { u8 mark [must_eq = 2]; u8 b; }
struct holder {
	u8 kind;
	variant body switch (kind) {
		case 1: one first;
		case 2: two second;
		default: error;
	}
}
"""


def _python_checks(tmp_path: Path, text: str) -> tuple[str, str]:
	"""The generated suite, and what running it said."""
	schema = tmp_path / "held.situ"
	schema.write_text(text, encoding="ascii")
	for command in (["gen-checks", str(schema), "--target", "python"],
	                ["build", str(schema), "--target", "python",
	                 "--layer", "frame"]):
		subprocess.run([sys.executable, "-m", "situc", *command,
		                "--out", str(tmp_path)],
		               check=True, capture_output=True, cwd=ROOT)
	(tmp_path / "situ_runtime.py").write_bytes(
		(ROOT / "runtime" / "python" / "situ_runtime.py").read_bytes())
	done = subprocess.run([sys.executable, "-m", "unittest", "held_checks"],
	                      capture_output=True, text=True, cwd=tmp_path)
	assert done.returncode == 0, done.stderr[-1500:]
	return (tmp_path / "held_checks.py").read_text(encoding="ascii"), done.stderr


def test_a_pin_behind_a_variant_is_declined(tmp_path: Path) -> None:
	"""Breaking byte 0 of the message would break `a` and `b`, not `mark`."""
	suite, _ = _python_checks(tmp_path, MOVING)
	assert "holder_late_late_present_mark_must_eq" not in suite
	assert "`holder.late.late_present.mark`, because its pinned value sits " \
	       "at an offset the message chooses" in suite


def test_two_arms_pinning_one_name_keep_both_checks(tmp_path: Path) -> None:
	"""A second `def` of one name replaces the first without a word."""
	suite, _ = _python_checks(tmp_path, ALIKE)
	for arm in ("first", "second"):
		assert suite.count(
			f"def test_holder_body_{arm}_mark_must_eq_is_enforced") == 1
