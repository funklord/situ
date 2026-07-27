"""Every schema in examples/ is exercised by the test suite.

Examples rot silently otherwise: a schema nobody parses stops being true the
first time the language moves. The ones needing a later phase are checked to be
rejected naming that phase, so they pin the phase-gating behaviour instead of
merely sitting there.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from situc import capmap, requirements
from situc.diagnostics import Source, SituError
from situc.dump import dump
from situc.layout import solve
from situc.resolve import resolve
from situc.parser import parse
from situc.unparse import unparse

EXAMPLES = Path(__file__).resolve().parents[2] / "examples"

# `// STATUS: needs phase N.` marks the phase at which a schema becomes fully
# buildable. A schema may be blocked by an earlier phase's construct first --
# packet needs `codec` from phase 7 before it can reach the phase 8 crypto --
# so the reported phase is bounded by the marker rather than equal to it.
STATUS = re.compile(r"^// STATUS: needs phase (\d+)\.", re.MULTILINE)

REPORTED_PHASE = re.compile(r"planned for phase (\d+)")


def schemas() -> list[Path]:
	found = sorted(EXAMPLES.glob("*/*.situ"))
	assert found, "no example schemas found"
	return found


def required_phase(path: Path) -> int | None:
	match = STATUS.search(path.read_text(encoding="ascii"))
	return int(match.group(1)) if match else None


def ids(paths: list[Path]) -> list[str]:
	return [path.parent.name for path in paths]


CURRENT = [path for path in schemas() if required_phase(path) is None]
FUTURE  = [path for path in schemas() if required_phase(path) is not None]


def test_every_example_directory_holds_a_schema() -> None:
	directories = {path.parent for path in schemas()}
	present     = {p for p in EXAMPLES.iterdir() if p.is_dir()}
	assert directories == present


def test_schema_is_named_after_its_directory() -> None:
	"""Codegen will key output filenames off the directory, so keep them equal."""
	for path in schemas():
		assert path.stem == path.parent.name


def test_every_example_builds() -> None:
	"""The future group is empty, and that is the milestone it records.

	Every example that was waiting on a phase has had its phase land, so the
	`// STATUS: needs phase N.` convention currently pins nothing. It stays
	documented and tested here because the next construct to be gated will use
	it again; what pins the phase-gating machinery in the meantime is the
	nested-namespace test in test_namespaces.py.
	"""
	assert len(CURRENT) >= 12
	assert FUTURE == []


@pytest.mark.parametrize("path", CURRENT, ids=ids(CURRENT))
def test_current_examples_parse(path: Path) -> None:
	parse(Source(str(path), path.read_text(encoding="ascii")))


@pytest.mark.parametrize("path", CURRENT, ids=ids(CURRENT))
def test_current_examples_round_trip(path: Path) -> None:
	first = parse(Source(str(path), path.read_text(encoding="ascii")))
	again = parse(Source(str(path), unparse(first)))
	assert dump(again) == dump(first)


@pytest.mark.parametrize("path", CURRENT, ids=ids(CURRENT))
def test_current_examples_state_their_requirements(path: Path) -> None:
	"""An example without a requirement is documentation, not a schema.

	The requirements are what make the capability claims checkable once the
	solver exists, so every example must carry at least one.
	"""
	schema = parse(Source(str(path), path.read_text(encoding="ascii")))
	assert schema.requirements(), f"{path.parent.name} states no requirements"


@pytest.mark.parametrize("path", CURRENT, ids=ids(CURRENT))
def test_current_examples_solve(path: Path) -> None:
	solve(parse(Source(str(path), path.read_text(encoding="ascii"))))


@pytest.mark.parametrize("path", CURRENT, ids=ids(CURRENT))
def test_committed_map_is_current(path: Path) -> None:
	"""The committed map must match what the compiler produces today.

	This is the snapshot test that makes a capability regression appear as a
	reviewable diff at the moment of editing, rather than as a performance
	surprise months later (project.md section 18.1). The `situc map --check`
	CLI that does the same thing for a user's own schemas is phase 9; this
	covers the repository's own examples until then.
	"""
	committed = path.with_suffix(".situ.map")
	assert committed.exists(), (
		f"{path.parent.name} has no committed map; run:\n"
		f"    python3 -m situc.cli map {path} > {committed}"
	)

	source   = Source(str(path), path.read_text(encoding="ascii"))
	schema   = parse(source)
	resolved = resolve(schema, solve(schema))
	requirements.discharge(schema, resolved)

	assert capmap.render(schema, resolved, source.path) == committed.read_text(
		encoding="ascii"), (
		f"the capability map of {path.parent.name} has changed; review the diff, "
		f"then run:\n    python3 -m situc.cli map {path} > {committed}"
	)


@pytest.mark.parametrize("path", FUTURE, ids=ids(FUTURE))
def test_future_examples_have_no_stale_map(path: Path) -> None:
	"""A schema that does not build cannot have a map to commit."""
	assert not path.with_suffix(".situ.map").exists()


@pytest.mark.parametrize("path", FUTURE, ids=ids(FUTURE))
def test_future_examples_are_rejected_naming_their_phase(path: Path) -> None:
	phase = required_phase(path)
	assert phase is not None

	with pytest.raises(SituError) as caught:
		parse(Source(str(path), path.read_text(encoding="ascii")))

	rendered = caught.value.diagnostic.render()
	assert "not yet implemented" in rendered

	match = REPORTED_PHASE.search(rendered)
	assert match is not None, f"no phase named in:\n{rendered}"

	reported = int(match.group(1))
	assert 2 <= reported <= phase, (
		f"{path.parent.name} is marked buildable at phase {phase}, but the parser "
		f"reported phase {reported}:\n{rendered}"
	)
