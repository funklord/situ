"""`--layer edit` has a write half in all four targets (26.854, 26.858).

`project.md` section 1 promises a ladder, and 0032's table calls rung 2
*build or resize a message whose extent is not fixed*. That write half was
C alone until 26.855, and asking `--layer edit` of the other three emitted
`--layer view`'s output byte for byte with no notice. 26.854 named the
silence; 26.855 and 26.858 closed it.

So this holds what replaced the notice: every target emits a builder for a
schema with a struct in its class, and a struct outside the class is named
on stderr. A target's boundary is a struct's refusal now, not a whole
language's absence.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from situc.cli import main				# noqa: E402

#: `example/modbus` holds both cells: plain structs and a variant.
SERVED = ROOT / "example" / "modbus" / "modbus.situ"
#: A delimited run is outside the three's class, and C's builder has it.
OUTSIDE = ROOT / "test" / "schema" / "edges.situ"

BUILT = {"c": "modbus_build.h", "cpp": "modbus_build.hpp",
         "rust": "modbus_build.rs", "python": "modbus_build.py"}


@pytest.mark.parametrize("target", sorted(BUILT))
def test_every_target_gets_a_builder(target: str, tmp_path: Path) -> None:
	assert main(["build", str(SERVED), "--target", target,
	             "--layer", "edit", "--out", str(tmp_path)]) == 0
	assert (tmp_path / BUILT[target]).exists(), sorted(
		one.name for one in tmp_path.iterdir())


@pytest.mark.parametrize("target", ("cpp", "rust", "python"))
def test_a_struct_outside_the_class_is_named(
		target: str, tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
	"""The other cell: a builder that serves nothing and says nothing is
	the silence 26.854 closed, one struct at a time."""
	assert main(["build", str(OUTSIDE), "--target", target,
	             "--layer", "edit", "--out", str(tmp_path)]) == 0
	said = capsys.readouterr().err
	assert "situc: no builder for `" in said, said
	assert "builder does not write yet" in said, said
