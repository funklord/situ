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

from situc import capmap, wire, requirements
from situc.diagnostics import Source, SituError
from situc.dump import dump
from situc.layout import solve
from situc.resolve import resolve
from situc.parser import parse
from situc.unparse import unparse

from every_schema import SCHEMAS as ALL_SCHEMAS

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
	"""The directory names an example, except where two share one: `std/` and
	`tests/schemas/` hold more than one schema each, so those carry the file
	name too."""
	return [path.parent.name if path.parent.name not in ("std", "schemas")
	        else f"{path.parent.name}/{path.stem}" for path in paths]


CURRENT = [path for path in schemas() if required_phase(path) is None]
FUTURE  = [path for path in schemas() if required_phase(path) is not None]

#: Every schema this repository builds, for the two snapshot checks below.
#: They read `examples/` alone, and `tests/schemas/edges.situ` -- the file that
#: exists to carry the constructs no worked example has -- had a committed map
#: and a committed wire signature that *nothing read*. Both were stale, from a
#: change made in the same session that noticed. A snapshot nobody verifies is
#: worse than none: it looks authoritative and is not (26.35).
SNAPSHOT = [path for path in ALL_SCHEMAS if required_phase(path) is None]


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


@pytest.mark.parametrize("path", SNAPSHOT, ids=ids(SNAPSHOT))
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


# -- the wire signature (section 19.3) --------------------------------------


@pytest.mark.parametrize("path", SNAPSHOT, ids=ids(SNAPSHOT))
def test_committed_wire_signature_is_current(path: Path) -> None:
	"""The byte-level contract, committed for the reason the map is.

	A capability regression is a performance surprise; a wire break is a
	message a deployed peer cannot read, on a machine nobody can recompile.
	The second deserves the reviewable diff at least as much as the first, and
	`situc diff` does not provide it: it compares capability vectors, so a
	byte-order flip reports "No capability change" and a field leaving an
	authenticated region reports an improvement.
	"""
	committed = path.with_suffix(".situ.wire")
	assert committed.exists(), (
		f"{path.parent.name} has no committed wire signature; run:\n"
		f"    python3 -m situc.cli wire {path} > {committed}"
	)

	source   = Source(str(path), path.read_text(encoding="ascii"))
	schema   = parse(source)
	resolved = resolve(schema, solve(schema))

	assert wire.render(schema, resolved, source.path) == committed.read_text(
		encoding="ascii"), (
		f"the wire contract of {path.parent.name} has changed; review the diff "
		f"-- a change here is a change a deployed peer sees -- then run:\n"
		f"    python3 -m situc.cli wire {path} > {committed}"
	)


@pytest.mark.parametrize("path", FUTURE, ids=ids(FUTURE))
def test_future_examples_have_no_stale_signature(path: Path) -> None:
	"""A schema that does not build cannot have a contract to commit."""
	assert not path.with_suffix(".situ.wire").exists()


def test_every_example_the_readmes_name_exists() -> None:
	"""The front page is an artifact like any other here.

	Nothing held either README to the tree, and the top-level one is the first
	thing a prospective adopter reads: it names example directories, and a
	name that has been renamed or removed reads exactly like one that is
	there. This is the same check `test_the_cli_section_lists_every_command`
	makes about the command list, for the same reason -- prose drifts slowly
	because a wrong paragraph usually reads wrong, and a *reference* does not.
	"""
	named: set[str] = set()
	for readme in (EXAMPLES.parent / "README.md", EXAMPLES / "README.md"):
		named |= set(re.findall(r"examples/([a-z0-9_]+)", readme.read_text(
			encoding="ascii")))
		# examples/README.md links relatively: `[mqtt](mqtt/)`.
		if readme.parent.name == "examples":
			named |= set(re.findall(r"\]\(([a-z0-9_]+)/\)", readme.read_text(
				encoding="ascii")))

	missing = sorted(name for name in named if not (EXAMPLES / name).is_dir())
	assert not missing, f"the READMEs name examples that do not exist: {missing}"
