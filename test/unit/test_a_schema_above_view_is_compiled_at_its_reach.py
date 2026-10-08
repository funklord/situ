"""Why there is no per-rung corpus sweep, and when that stops being safe.

`test_codegen_c.py` sweeps every schema at `view` and again at `frame`, and
nothing sweeps `edit`, `relate`, `converse` or `drive` over the corpus. That
looks like a gap and is not, for a reason worth measuring rather than
assuming: `layers.reach` is the highest rung a schema has content for, and
of the 45 corpus schemas **44 reach only `view`** -- so at any higher rung
they emit what `view` emits and the sweep would be comparing a file with
itself.

One reaches above it, `example/dns/dns.situ` at `drive`, and the nine driver
tests build exactly that schema at exactly that rung. A rung emits
everything below it, so compiling dns at `drive` compiles its `relate`,
`frame` and `converse` output too.

**That is coverage by arithmetic, and the arithmetic has one term.** The
day a second schema declares a relation, it reaches above `view`, nothing
compiles its upper rungs, and no existing test says so -- the frame sweep
would still pass, because `frame` is swept for everything. So the guard
here is not another sweep: it is the claim that every schema reaching above
`view` is one some driver test compiles, with both sides derived (26.604).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "test" / "unit"))

from situc import layers, parser			# noqa: E402
from situc.diagnostics import Source			# noqa: E402
from every_schema import SCHEMAS			# noqa: E402

#: `SCHEMA = ROOT / "example" / "dns" / "dns.situ"`, as the driver tests
#: spell it. `test_tokio_driver.py` assigns a schema STRING instead, which
#: this deliberately does not match -- an inline schema is not a corpus
#: schema and cannot cover one.
_NAMES = re.compile(r'^SCHEMA\s*=\s*ROOT\s*/\s*(.+)$', re.M)


def reach_above_view() -> set[Path]:
	"""Corpus schemas with content above the bottom rung."""
	found = set()
	for path in SCHEMAS:
		schema = parser.parse(Source(str(path),
		                             path.read_text(encoding="utf-8")))
		if layers.reach(schema) != "view":
			found.add(path.resolve())
	return found


def compiled_by_a_driver_test() -> set[Path]:
	"""Corpus schemas some driver test builds at `--layer drive`."""
	found = set()
	for test in sorted((ROOT / "test" / "unit").glob("test_*driver*.py")):
		for tail in _NAMES.findall(test.read_text(encoding="utf-8")):
			parts = re.findall(r'"([^"]+)"', tail)
			if parts:
				found.add(ROOT.joinpath(*parts).resolve())
	return found


def test_the_two_derived_sets_are_not_empty() -> None:
	"""Both halves are derived by matching text, so both can go quietly
	empty -- and an empty left side makes the real assertion vacuous while
	an empty right side makes it fail for the wrong reason."""
	assert reach_above_view(), (
		"no corpus schema reaches above `view`; either the corpus lost its "
		"only relation or `layers.reach` moved")
	assert compiled_by_a_driver_test(), (
		"no driver test names a corpus schema; the `SCHEMA = ROOT / ...` "
		"spelling moved and this module is measuring nothing")


def test_every_schema_above_view_is_compiled_at_its_reach() -> None:
	"""The claim that makes the missing per-rung sweeps safe."""
	above    = reach_above_view()
	compiled = compiled_by_a_driver_test()
	missing  = sorted(p.relative_to(ROOT) for p in above - compiled)
	assert not missing, (
		f"{missing} reach above `view` and no driver test compiles them, so "
		f"their relate, converse and drive output is generated and never "
		f"compiled -- the frame sweep cannot see it, because frame is swept "
		f"for every schema")
