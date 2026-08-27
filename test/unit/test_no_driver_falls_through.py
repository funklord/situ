"""Every driver on the axis holds the axis's properties, or this fails.

Nine drivers arrived over two days, and each carries a test of its own that
drives it over a socket. Those are the ones that prove a driver *works*.
This is the other question: whether a driver added tomorrow is a driver at
all, or a name in a dictionary that nobody noticed was never wired up.

`DRIVER_BACKENDS` in `situc/cli.py` was read by exactly one file -- `cli.py`
itself -- so a tenth entry could name a module that does not exist, skip the
`driven()` gate and emit a driver for a schema that states no policy, or
answer neither refusal, and the suite would stay green until somebody ran
the exact combination by hand. Six of the nine per-driver test files were
made by copying a seventh, which is how a property gets *asserted* nine
times and *guaranteed* none: the tenth driver inherits nothing.

So this reads the registry rather than a list of its own. A new driver is
covered the moment it is added to `DRIVER_BACKENDS`, and until it holds
every property here it cannot be added quietly. That is the shape
`test_no_construct_falls_through.py` uses for members, and it is here for
the same reason: the gap that matters is the one nobody wrote a test for.

Everything runs through the CLI rather than by importing a generator,
because the dispatch is part of what is being checked -- a driver whose
module exists and whose `cmd_build` branch does not is exactly the failure
this is for. It needs no toolchain: what is asserted is which files a build
writes and which refusals it prints, not that any of them compile.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from every_schema import ROOT
from situc.cli import DRIVER_BACKENDS

#: The one schema in the tree whose relation states a retransmission policy,
#: so it is the only one any driver emits for at all.
DRIVEN = ROOT / "example" / "dns" / "dns.situ"

#: A schema with no relation, therefore no policy: the `driven()` gate's
#: empty case. A driver that writes a file for this one is emitting a state
#: machine that pumps nothing.
POLICYLESS = ROOT / "example" / "udp" / "udp.situ"

#: Which target a driver is *not* for, to exercise the availability refusal
#: in the direction each driver actually crosses.
OTHER_TARGET = {"c": "python", "cpp": "c", "python": "c", "rust": "c"}


def _build(schema: Path, out: Path, target: str,
		driver: str, layer: str = "drive") -> subprocess.CompletedProcess[str]:
	argv = [sys.executable, "-m", "situc.cli", "build", str(schema),
	        "--target", target, "--driver", driver, "--out", str(out)]
	if layer is not None:
		argv += ["--layer", layer]
	return subprocess.run(argv, cwd=ROOT, capture_output=True, text=True)


def test_the_registry_is_not_empty() -> None:
	"""The parametrised tests below are worth nothing if the list they read
	is empty -- a gate over an empty file list reports success exactly as
	loudly as a real pass."""
	assert len(DRIVER_BACKENDS) >= 9, DRIVER_BACKENDS
	for driver, backends in DRIVER_BACKENDS.items():
		assert backends, f"{driver} names no backend"
		for backend in backends:
			assert backend in OTHER_TARGET, (
				f"{driver} names backend {backend}, which this test has no "
				f"opposite for -- add one to OTHER_TARGET")


@pytest.mark.parametrize("driver", sorted(DRIVER_BACKENDS))
def test_a_driver_emits_a_file_for_a_driven_schema(driver: str,
		tmp_path: Path) -> None:
	"""The dispatch reaches a generator and the generator writes something.

	This is what fails when a name is added to `DRIVER_BACKENDS` and the
	module behind it is missing, misnamed, or has no branch in `cmd_build`
	-- none of which the per-driver tests would catch for a driver that has
	no per-driver test yet.
	"""
	target = DRIVER_BACKENDS[driver][0]
	built = _build(DRIVEN, tmp_path, target, driver)
	assert built.returncode == 0, built.stderr

	made = sorted(p.name for p in tmp_path.glob(f"{DRIVEN.stem}_{driver}.*"))
	assert made, (
		f"--driver {driver} wrote no {DRIVEN.stem}_{driver}.* file; "
		f"it wrote {sorted(p.name for p in tmp_path.iterdir())}")


@pytest.mark.parametrize("driver", sorted(DRIVER_BACKENDS))
def test_a_driver_emits_nothing_where_no_exchange_states_a_policy(
		driver: str, tmp_path: Path) -> None:
	"""The `driven()` gate, which every driver reuses and any new one could
	forget. A driver artifact over a schema with no retransmission policy
	pumps a state machine that was never generated -- the drive layer emits
	nothing for such a schema, so the driver must not either."""
	target = DRIVER_BACKENDS[driver][0]
	built = _build(POLICYLESS, tmp_path, target, driver)
	assert built.returncode == 0, built.stderr

	leaked = sorted(p.name
	                for p in tmp_path.glob(f"{POLICYLESS.stem}_{driver}.*"))
	assert not leaked, (
		f"--driver {driver} emitted {leaked} for a schema that states no "
		f"policy, so there is no state machine for it to pump")


@pytest.mark.parametrize("driver", sorted(DRIVER_BACKENDS))
def test_a_driver_is_refused_on_a_backend_it_is_not_for(driver: str,
		tmp_path: Path) -> None:
	"""A driver crosses the backend axis and not every cell is meaningful.
	The refusal has to name both, so a reader learns which pair is wrong
	rather than that something is."""
	target = OTHER_TARGET[DRIVER_BACKENDS[driver][0]]
	assert target not in DRIVER_BACKENDS[driver]

	refused = _build(DRIVEN, tmp_path / "unused", target, driver)
	assert refused.returncode != 0, refused.stdout
	assert driver in refused.stderr, refused.stderr
	assert target in refused.stderr, refused.stderr


@pytest.mark.parametrize("driver", sorted(DRIVER_BACKENDS))
def test_a_driver_is_refused_without_the_layer_it_pumps(driver: str,
		tmp_path: Path) -> None:
	"""A driver adds a file *over* the drive layer rather than pulling it
	in, so asking for one without the other is a mistake worth naming. The
	message carries the driver's own suffix, which is what caught a Python
	driver advertising a `.c`."""
	target = DRIVER_BACKENDS[driver][0]
	refused = _build(DRIVEN, tmp_path / "unused", target, driver,
	                 layer="view")
	assert refused.returncode != 0, refused.stdout
	assert driver in refused.stderr, refused.stderr
	assert "drive" in refused.stderr, refused.stderr


@pytest.mark.parametrize("driver", sorted(DRIVER_BACKENDS))
def test_a_driver_changes_nothing_else_it_emits(driver: str,
		tmp_path: Path) -> None:
	"""0032's additivity, across the driver axis: `--driver` adds files and
	changes none. Every file the drive layer writes on its own must come
	back byte-identical with a driver asked for as well, or the axis is not
	additive and a caller cannot reason about the two independently."""
	target = DRIVER_BACKENDS[driver][0]

	plain = tmp_path / "plain"
	bare = subprocess.run(
		[sys.executable, "-m", "situc.cli", "build", str(DRIVEN),
		 "--target", target, "--layer", "drive", "--out", str(plain)],
		cwd=ROOT, capture_output=True, text=True)
	assert bare.returncode == 0, bare.stderr

	withdriver = tmp_path / "withdriver"
	built = _build(DRIVEN, withdriver, target, driver)
	assert built.returncode == 0, built.stderr

	for before in sorted(plain.iterdir()):
		after = withdriver / before.name
		assert after.is_file(), f"{before.name} vanished when {driver} was asked for"
		assert after.read_bytes() == before.read_bytes(), (
			f"--driver {driver} changed {before.name}, which it must only "
			f"add beside")
