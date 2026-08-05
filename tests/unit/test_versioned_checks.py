"""A versioned member's constraints reach `validate` (26.75).

Each backend records the versioned accessors it emits and `validate` consults
that record rather than re-deriving whether one exists -- which is what makes
the three stop disagreeing (26.74). The record is populated as the accessors
are written, so it is only correct while accessors are emitted *before* the
validator that names them.

Nothing enforced that order, and breaking it fails silently: the set reads
empty, every versioned check is skipped, and the generated code compiles
perfectly while checking nothing. That is strictly worse than the compile
error the record replaced, so this is the test that holds the order.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from every_schema import ROOT
from situc.codegen.c import generate as generate_c
from situc.codegen.cpp import generate as generate_cpp
from situc.codegen.python import generate as generate_py
from situc.diagnostics import Source
from situc.layout import solve
from situc.parser import parse
from situc.resolve import resolve

#: A versioned member with a constraint, at an offset every backend resolves.
#: If the record were empty this would emit no check anywhere.
SCHEMA = """target buffer;
endian big;

struct s [version = ver] {
	u8   ver;
	u16  body;
	u16  added  [since = 2, must_eq = 4660];
}
"""


def built(tmp: Path) -> dict[str, str]:
	path = tmp / "unit.situ"
	path.write_text(SCHEMA, encoding="ascii")
	schema   = parse(Source(str(path), SCHEMA))
	resolved = resolve(schema, solve(schema))

	return {
		"c":      generate_c(schema, resolved, "unit").source,
		"cpp":    generate_cpp(schema, resolved, "unit").header,
		"python": generate_py(schema, resolved, "unit").module,
	}


@pytest.mark.parametrize("backend", ("c", "cpp", "python"))
def test_a_versioned_constraint_is_actually_checked(
		backend: str, tmp_path: Path) -> None:
	"""The constant 4660 must appear in the validator.

	Checked by looking for the value rather than for a call, because what
	fails here is an *absence*: a backend that skips the check emits a
	validator that is merely shorter, and shorter is not something a
	compiler or a type checker objects to.
	"""
	text = built(tmp_path)[backend]

	assert "4660" in text, (
		f"{backend}: a versioned member's `must_eq` reached no validator. "
		f"If accessors are now emitted after the validator that names them, "
		f"the emitter's record reads empty and every versioned check is "
		f"silently skipped -- see 26.75.")


def test_the_record_is_empty_before_anything_is_emitted(tmp_path: Path) -> None:
	"""And the record is per-run, not per-process.

	A set that outlived one schema would answer for members of another, so a
	second schema in the same process would check members it has not got.
	"""
	from situc.codegen.c.emit import Emitter

	path     = tmp_path / "unit.situ"
	path.write_text(SCHEMA, encoding="ascii")
	schema   = parse(Source(str(path), SCHEMA))
	resolved = resolve(schema, solve(schema))

	first  = Emitter(schema, resolved, "unit", "situ")
	second = Emitter(schema, resolved, "unit", "situ")

	assert first._emitted == set()
	first.header()
	assert first._emitted, "the header emitted no versioned accessor to record"
	assert second._emitted == set(), "the record is shared between emitters"
