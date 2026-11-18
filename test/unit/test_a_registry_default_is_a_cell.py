"""A registry's `default` is held to be a cell of its type (0070).

A default is written as it would be stored, so it is a message of the
cell's type and within the entry's bounds like any value. Checked when a
schema is analysed, by the registry's own `check` through the accessors
`situc verify` builds in memory, so a default no reader would accept is
refused where it is written.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "test" / "unit"))

import pytest							# noqa: E402

from situc.cli import analyse					# noqa: E402
from situc.diagnostics import SituError				# noqa: E402
from test_a_registry_is_generated_in_c import SCHEMA		# noqa: E402


def test_a_default_that_is_a_cell_is_accepted(tmp_path: Path) -> None:
	path = tmp_path / "ok.situ"
	path.write_text(SCHEMA, encoding="ascii")
	analyse(path)


@pytest.mark.parametrize("written, why", [
	('default = "14x"', "not one of time_unit"),		# the type refuses it
	('default = "3651d"', "outside its bounds"),		# the entry does
	('default = "014d"', "digits_minimal|minimal|not minimal|Constraint"),
])
def test_a_default_that_is_not_a_cell_is_refused(
		tmp_path: Path, written: str, why: str) -> None:
	path = tmp_path / "bad.situ"
	path.write_text(SCHEMA.replace('default = "14d"', written),
	                encoding="ascii")
	with pytest.raises(SituError, match="is not a cell of `archive/gone`") \
			as caught:
		analyse(path)
	import re
	assert re.search(why, str(caught.value.diagnostic.notes)), \
		caught.value.diagnostic.notes
