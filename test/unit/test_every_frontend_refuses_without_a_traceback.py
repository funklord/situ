"""Both of `report.listing`'s non-row verdicts, in every frontend (26.603).

0034 makes `situ-edit` the reference frontend *because* an interactive one
is hard to test -- so the CLI gets the fix and the others are where it goes
missing. That is not hypothetical and it is not even a new lesson in this
file's subject: `situ-edit-tui`'s own `--offset` comment says *wiring one
and not the other would leave the tool that tells you to open a struct at
an offset unable to, which is how this gap existed in the first place.*

26.598 then wired `--arg` into the CLI alone. `Unsupplied` is not a
`Refused` -- pinned by a test, deliberately, so one handler cannot absorb
the other -- and the TUI caught only the second, so a struct that takes a
`parameter` printed a Python stack trace there for as long as it took
somebody to try it.

So the population is DERIVED rather than listed: every script in `bin/`
that imports `open_from` is a frontend that opens a document, and each one
has to refuse both verdicts without a traceback. A third frontend is
covered the day it is written, and `HOW_TO_RUN` is asserted to name every
member so a new one fails loudly instead of being skipped.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import pytest						# noqa: E402

#: Extra flags a frontend needs to run without a terminal. The POPULATION
#: is derived below; this is only how to invoke each member, and every
#: member must appear here.
HOW_TO_RUN: dict[str, list[str]] = {
	"situ-edit": [],
	"situ-edit-tui": ["--no-ui"],
}

#: A schema carrying a `parameter`, and the struct that takes one.
ARGUED_SCHEMA = ROOT / "test" / "schema" / "edges.situ"
ARGUED_STRUCT = "negotiated"

#: Fixed enough for `arp_packet`, whose 28 fixed bytes nothing reaches.
NO_VIEW_SCHEMA = ROOT / "example" / "arp" / "arp.situ"
NO_VIEW_STRUCT = "arp_packet"


def frontends() -> list[str]:
	"""Every script in `bin/` that opens a document.

	`open_from` is the discriminator because it is what builds one: `situc`
	and `situ-walk` do not import it and are not frontends in this sense.
	Derived rather than listed, so the set cannot drift from the tree.
	"""
	return sorted(path.name for path in (ROOT / "bin").iterdir()
	              if path.is_file()
	              and "open_from" in path.read_text(encoding="utf-8"))


def run(name: str, *args: str) -> subprocess.CompletedProcess[str]:
	return subprocess.run(
		[sys.executable, str(ROOT / "bin" / name), *HOW_TO_RUN[name], *args],
		capture_output=True, text=True, cwd=ROOT, timeout=300)


def test_every_frontend_is_named_in_the_invocation_table() -> None:
	"""The partition's second job: a frontend nobody taught this module to
	run would otherwise be skipped, and a skip reads like a pass."""
	found = frontends()
	assert found, "no frontend imports `open_from`; the discriminator moved"
	assert set(found) == set(HOW_TO_RUN), (
		f"derived {found}, table knows {sorted(HOW_TO_RUN)}")


@pytest.mark.parametrize("name", frontends())
def test_a_frame_too_short_is_refused_cleanly(name: str,
		tmp_path: Path) -> None:
	"""`no-view`: a frame too short for the struct."""
	empty = tmp_path / "empty.bin"
	empty.write_bytes(b"")
	done = run(name, str(NO_VIEW_SCHEMA), str(empty),
	           "--struct", NO_VIEW_STRUCT)

	assert done.returncode == 1, done.stdout + done.stderr
	assert "Traceback" not in done.stderr, done.stderr
	assert NO_VIEW_STRUCT in done.stderr


@pytest.mark.parametrize("name", frontends())
def test_a_missing_argument_is_refused_cleanly(name: str,
		tmp_path: Path) -> None:
	"""`needs-arguments`: the verdict that was a traceback in the TUI.

	The message names the parameter and the option, because *takes 1
	argument(s)* is not something a person holding a capture can act on.
	"""
	some = tmp_path / "some.bin"
	some.write_bytes(bytes(range(1, 33)))
	done = run(name, str(ARGUED_SCHEMA), str(some),
	           "--struct", ARGUED_STRUCT)

	assert done.returncode == 1, done.stdout + done.stderr
	assert "Traceback" not in done.stderr, done.stderr
	assert "`block`" in done.stderr
	assert "--arg" in done.stderr


@pytest.mark.parametrize("name", frontends())
def test_every_frontend_can_supply_an_argument(name: str,
		tmp_path: Path) -> None:
	"""And the channel exists, not merely the refusal. A frontend that
	declines helpfully and gives no way to comply has moved the traceback
	into prose."""
	some = tmp_path / "some.bin"
	some.write_bytes(bytes(range(1, 33)))
	done = run(name, str(ARGUED_SCHEMA), str(some),
	           "--struct", ARGUED_STRUCT, "--arg", "4")

	assert done.returncode == 0, done.stdout + done.stderr
	assert "payload" in done.stdout
