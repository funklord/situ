"""The io_uring driver, held to a real socket (0033, 26.775).

`--driver io_uring` is a shipped event loop for rung 6: it owns the
descriptor, the clock and the timer arithmetic, and pumps the `--layer
drive` state machine that owns everything else. A transcript test proves
the state machine; only a real descriptor proves the driver. The socket
test is `c_driver_harness`, shared by all six C drivers because all six
call the same `situ_drive_hooks_t`: every hook is asked for, `on_done`
exactly once on every path. Io_uring is Linux, so the socket test skips elsewhere; the
refusal tests are pure CLI and run anywhere.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

import c_driver_harness
from every_schema import ROOT

COMPILER = c_driver_harness.COMPILER
SCHEMA   = c_driver_harness.SCHEMA


@pytest.mark.skipif(COMPILER is None or sys.platform != "linux",
                    reason="needs a C compiler and io_uring, which is Linux")
def test_io_uring_driver_calls_every_hook(tmp_path: Path) -> None:
	"""A reply after retransmissions, an expiry, an unmatched reply, a short
	and an oversized datagram, `stop` from a callback and -- where io_uring can
	see one -- an unusable descriptor, each reaching its hook."""
	out = c_driver_harness.run(tmp_path, "io_uring")
	if "SKIP" in out:
		pytest.skip("the kernel offers no io_uring here")
	assert "ALL PASS" in out, out


def test_io_uring_is_refused_on_python(tmp_path: Path) -> None:
	"""A driver crosses the backend axis and io_uring on python is nothing at
	all. The refusal names both the driver and the target."""
	refused = subprocess.run(
		[sys.executable, "-m", "situc.cli", "build", str(SCHEMA),
		 "--target", "python", "--layer", "drive", "--driver", "io_uring",
		 "--out", str(tmp_path / "unused")],
		cwd=ROOT, capture_output=True, text=True)
	assert refused.returncode != 0
	assert "io_uring" in refused.stderr
	assert "python" in refused.stderr


def test_io_uring_requires_the_drive_layer(tmp_path: Path) -> None:
	"""The driver adds a file over the drive layer rather than pulling it in,
	so it must be asked for alongside `--layer drive`. The refusal names both
	the driver and the layer it needs."""
	refused = subprocess.run(
		[sys.executable, "-m", "situc.cli", "build", str(SCHEMA),
		 "--target", "c", "--driver", "io_uring",
		 "--out", str(tmp_path / "unused")],
		cwd=ROOT, capture_output=True, text=True)
	assert refused.returncode != 0
	assert "io_uring" in refused.stderr
	assert "drive" in refused.stderr
