"""`--layer edit` says when it emits no builder for a target (26.854).

`project.md` section 1 promises a ladder -- "`situc build --layer` runs from
`view` ... up to `drive`" -- and 0032's table calls rung 2 *build or resize
a message whose extent is not fixed*. The write half of that rung is C
only, which `cli.py` has said in a comment since 26.610 and said to nobody
else: asking `--layer edit` of C++, Rust or Python emitted **exactly what
`--layer view` emits**, byte-identical, with no notice.

That is the one failure this file's siblings exist to prevent, arriving one
axis over. `cli.py` refuses a STRUCT by name -- "no builder for `x`", "no
owned form for `x` at any rung" -- on the stated principle that "a rung is
absent rather than defaulted where the schema states something it cannot
hold to". The target's absence is the same sentence about a different
subject, and it was the one left silent.

**Measured rather than read**: at `--layer edit`, `example/dns` produced
output identical to `--layer view` in all three, file names included, while
C gained `dns_build.h`.
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

#: The three whose rung-2 write half does not exist. `c` is deliberately not
#: parametrised in: its presence is asserted separately, because a notice
#: printed for every target would satisfy a test over this list alone.
WITHOUT = ("cpp", "rust", "python")


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


def test_c_gets_the_builder_and_no_notice(
		tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
	"""The other half, without which the notice could be unconditional.

	A notice printed for every target would pass the test above and say
	something false about C, which is 154's shape: two states rendered
	alike.
	"""
	assert main(["build", str(SCHEMA), "--target", "c",
	             "--layer", "edit", "--out", str(tmp_path)]) == 0

	said = capsys.readouterr().err
	assert "emits no builder" not in said, said
	assert (tmp_path / "dns_build.h").exists(), sorted(
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
