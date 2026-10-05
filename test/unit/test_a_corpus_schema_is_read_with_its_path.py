"""Reading a corpus schema keeps the path, and that changes nothing else.

`import` is resolved relative to the importing file's directory (17.0a), so a
schema parsed from a string has none and says so. Thirty sites across fourteen
files read a corpus schema with `parse_text(path.read_text())`, discarding it
-- which is why the feature had no corpus schema at all: a pair written for it
failed 22 tests across 10 files, every one of them with *`import` needs a
schema that came from a file* (26.568).

**This file is the proof the bulk edit needed**, in `evidence.md`'s sense: a
mechanical change over thirty call sites states what must not change and
checks it. What must not change is the output -- for a schema with no
`import`, the path is unused, so the two readings must agree -- and what must
change is nothing else.

The invariant is checked on the generated C rather than on the resolved
schema, because the generated text is the artifact a backend hands over and
two dataclass trees comparing equal is a weaker statement about a schema than
two headers comparing equal.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from every_schema import SCHEMAS, ids, load_schema
from situc.codegen.c import generate as generate_c
from situc.layout import solve
from situc.parser import parse_text
from situc.resolve import resolve


def _generated(schema) -> str:  # type: ignore[no-untyped-def]
	resolved = resolve(schema, solve(schema))
	built    = generate_c(schema, resolved, "unit")
	return "".join(one for _, one in sorted(built.files().items()))


@pytest.mark.parametrize("path", SCHEMAS, ids=ids(SCHEMAS))
def test_the_path_changes_nothing_for_a_schema_that_does_not_import(
		path: Path) -> None:
	"""The thirty sites' old reading and the new one, compared.

	Skipped for a schema that DOES import, because there the old reading
	cannot produce anything to compare -- it raises, which is the whole
	point. That skip is the one place this test could go quiet, so it says
	which schemas it covered rather than passing in silence.
	"""
	text = path.read_text(encoding="utf-8")
	if "\nimport " in text or text.startswith("import "):
		pytest.skip(f"{path.name} imports, so the old reading refuses it")

	assert _generated(load_schema(path)) == _generated(parse_text(text)), (
		f"{path.name}: reading the schema with its path changed the generated "
		"C, which it must not for a schema with no `import`")


def test_the_comparison_covered_the_corpus() -> None:
	"""And the skip above did not swallow it.

	A test parametrized over a list that skips every case reports success as
	loudly as one that checked everything, which is the shape half of section
	26 is about. This counts what was comparable and requires most of the
	corpus to be.
	"""
	importing = [path for path in SCHEMAS
	             if "\nimport " in path.read_text(encoding="utf-8")]
	assert len(importing) < len(SCHEMAS) // 2, (
		f"{len(importing)} of {len(SCHEMAS)} schemas import, so the test "
		"above skipped most of the corpus and proves little")
