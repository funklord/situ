"""`--layer edit` says when it emits no builder for a target (26.854).

`project.md` section 1 promises a ladder -- "`situc build --layer` runs from
`view` ... up to `drive`" -- and 0032's table calls rung 2 *build or resize
a message whose extent is not fixed*. The write half of that rung was C
only, which `cli.py` had said in a comment since 26.610 and said to nobody
else: asking `--layer edit` of C++, Rust or Python emitted **exactly what
`--layer view` emits**, byte-identical, with no notice.

That is the one failure this file's siblings exist to prevent, arriving one
axis over. `cli.py` refuses a STRUCT by name -- "no builder for `x`", "no
owned form for `x` at any rung" -- on the stated principle that "a rung is
absent rather than defaulted where the schema states something it cannot
hold to". The target's absence is the same sentence about a different
subject, and it was the one left silent.

**Python has a builder since 26.855 and this file moved with it**, which
is what a gate over a claim does: `WITHOUT` is two targets now, Python is
asserted on the other side beside C, and the identity test -- `edit` IS
`view` for a target with no builder -- covers the two that remain. The day
C++ or Rust gains one, this file goes red again and names which.
"""

from __future__ import annotations

import filecmp
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from situc.cli import main				# noqa: E402

SCHEMA = ROOT / "example" / "dns" / "dns.situ"

#: The targets whose rung-2 write half does not exist. The two WITH one are
#: deliberately not parametrised in: their presence is asserted separately,
#: because a notice printed for every target would satisfy a test over this
#: list alone.
WITHOUT = ("cpp", "rust")

#: And the two that have it, which is what keeps the list above honest.
WITH = ("c", "python")


@pytest.mark.parametrize("target", WITHOUT)
def test_the_notice_names_the_rung_and_the_target(
		target: str, tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
	assert main(["build", str(SCHEMA), "--target", target,
	             "--layer", "edit", "--out", str(tmp_path)]) == 0

	said = capsys.readouterr().err
	assert "--layer edit emits no builder" in said, said
	assert f"--target {target}" in said, said
	# The rung and where to read about it, because a notice that says only
	# "not supported" leaves the reader to guess whether they asked wrongly.
	assert "rung 2" in said, said
	assert "0032" in said, said


#: `example/modbus` rather than `dns` for the half below, and the reason is
#: a measurement: Python's builder serves 48 corpus structs and none of
#: `dns`'s is among them -- its header is bit-packed, which 26.855's class
#: excludes because the packed run is written zeroed and then set in place,
#: a second write to a byte the cursor has passed. C builds `dns` because
#: C's builder has the packed case. So asking this of `dns` would assert
#: Python has no builder anywhere, which is a different and false claim.
SERVED = ROOT / "example" / "modbus" / "modbus.situ"


@pytest.mark.parametrize("target", WITH)
def test_a_target_with_a_builder_gets_one_and_no_notice(
		target: str, tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
	"""The other half, without which the notice could be unconditional.

	A notice printed for every target would pass the test above and say
	something false about the two that have a builder, which is 154's
	shape: two states rendered alike.
	"""
	assert main(["build", str(SERVED), "--target", target,
	             "--layer", "edit", "--out", str(tmp_path)]) == 0

	said = capsys.readouterr().err
	assert "emits no builder for" not in said, said
	suffix = "h" if target == "c" else "py"
	assert (tmp_path / f"modbus_build.{suffix}").exists(), sorted(
		one.name for one in tmp_path.iterdir())


@pytest.mark.parametrize("target", WITHOUT)
def test_the_notice_is_true_about_the_output(target: str,
		tmp_path: Path) -> None:
	"""The claim the notice makes, asserted on the files rather than taken
	on trust: `--layer edit` IS `--layer view`'s output for these three.

	The day one of them gains a builder, this fails -- which is the point.
	A notice is a claim about generated code, and a claim nothing checks is
	the kind this repository has had to correct three times in a week.
	"""
	view = tmp_path / "view"
	edit = tmp_path / "edit"
	for out, layer in ((view, "view"), (edit, "edit")):
		out.mkdir()
		assert main(["build", str(SCHEMA), "--target", target,
		             "--layer", layer, "--out", str(out)]) == 0

	names = sorted(one.name for one in view.iterdir())
	assert names == sorted(one.name for one in edit.iterdir())
	same, differ, odd = filecmp.cmpfiles(view, edit, names, shallow=False)
	assert differ == [] and odd == [], (differ, odd)
	assert sorted(same) == names
