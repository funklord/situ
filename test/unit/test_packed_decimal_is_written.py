"""A builder writes packed decimal (26.1029).

An RTC's time registers are BCD -- the part was made to drive a display --
and `wall_clock` could not be built: "is packed and its value is not its
bits", "is packed BCD, whose value is not its bytes". Its value is its
digits, a nibble each, which is what the runtime's `situ_bcd_encode` makes
and the reader's `situ_bcd_decode` undoes; the builder now writes through
it, refusing a value past what the field holds. C only.

Held to the DS1307's register map (Maxim DS1307 datasheet, table 2): CH and
the seconds in 00h, then minutes, the 12/24 bit and hours, the day of the
week, the date, the month and the year, each digit in its own nibble.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from test_an_arm_sized_by_its_argument_is_written import (COMPILER, _built,
                                                          _run)


@pytest.mark.skipif(COMPILER is None, reason = "needs a C compiler")
def test_a_ds1307_time(tmp_path: Path) -> None:
	_run(tmp_path, _built(tmp_path, "rtc"), "rtc", """
	/* Saturday 2026-11-14, 02:59:30, 24-hour. */
	static const uint8_t want[7] = { 0x30, 0x59, 0x02, 0x07, 0x14, 0x11, 0x26 };
	uint8_t  buf[16];
	uint32_t wrote = 0;

	if (situ_wall_clock_build(buf, sizeof buf, 0u, 30u, 59u, 0u, 2u, 7u, 14u,
	                          11u, 26u, &wrote) != SITU_OK
	    || wrote != 7u || memcmp(buf, want, 7u) != 0) {
		return 1;
	}
	/* The oscillator halted: CH is bit 7 of 00h, above the seconds. */
	if (situ_wall_clock_build(buf, sizeof buf, 1u, 30u, 59u, 0u, 2u, 7u, 14u,
	                          11u, 26u, &wrote) != SITU_OK || buf[0] != 0xB0) {
		return 2;
	}
	/* Sixty seconds is the schema's `max`; eighty is past what seven bits
	 * of decimal hold at all. Both refused. */
	if (situ_wall_clock_build(buf, sizeof buf, 0u, 60u, 59u, 0u, 2u, 7u, 14u,
	                          11u, 26u, &wrote) != SITU_ERR_CONSTRAINT) {
		return 3;
	}
	if (situ_wall_clock_build(buf, sizeof buf, 0u, 80u, 59u, 0u, 2u, 7u, 14u,
	                          11u, 26u, &wrote) != SITU_ERR_CONSTRAINT) {
		return 4;
	}
	/* 99 is the year's whole range, in a full byte. */
	if (situ_wall_clock_build(buf, sizeof buf, 0u, 0u, 0u, 0u, 0u, 1u, 1u,
	                          1u, 99u, &wrote) != SITU_OK || buf[6] != 0x99) {
		return 5;
	}
""")


@pytest.mark.skipif(COMPILER is None, reason = "needs a C compiler")
def test_fixed_point_is_its_stored_integer(tmp_path: Path) -> None:
	"""26.1030: `calibration` was refused as "fixed point, whose value is
	scaled", though every reader's getter and setter take the stored
	integer. Built as that: a trim of -1.5 ppm in Q8.8 is -384, and a
	32768 Hz reference in UQ16.16 is 0x80000000, both big-endian."""
	_run(tmp_path, _built(tmp_path, "rtc"), "rtc", """
	static const uint8_t want[6] = { 0xFE, 0x80, 0x80, 0x00, 0x00, 0x00 };
	uint8_t  buf[16];
	uint32_t wrote = 0;

	if (situ_calibration_build(buf, sizeof buf, -384, 0x80000000u, &wrote)
	    != SITU_OK || wrote != 6u || memcmp(buf, want, 6u) != 0) {
		return 1;
	}
""")


def test_python_writes_the_same_time(tmp_path: Path) -> None:
	"""26.1032: the other three encode packed decimal too, through their
	runtimes' own `bcd_encode`; Python's DS1307 bytes are C's above."""
	import subprocess
	import sys

	from test_an_arm_sized_by_its_argument_is_written import ROOT

	out = tmp_path / "py"
	done = subprocess.run(
		[sys.executable, "-m", "situc", "build",
		 str(ROOT / "example" / "rtc" / "rtc.situ"), "--target", "python",
		 "--layer", "edit", "--out", str(out)],
		cwd = ROOT, capture_output = True, text = True)
	assert done.returncode == 0, done.stderr
	(out / "situ_runtime.py").write_text(
		(ROOT / "runtime" / "python" / "situ_runtime.py").read_text(
			encoding = "ascii"), encoding = "ascii")
	(out / "run.py").write_text("""import sys
sys.path.insert(0, ".")
import rtc_build
from situ_runtime import ConstraintError
buf = bytearray(16)
n = rtc_build.build_wall_clock(buf, 0, 30, 59, 0, 2, 7, 14, 11, 26)
print(bytes(buf[:n]).hex())
try:
	rtc_build.build_wall_clock(buf, 0, 80, 59, 0, 2, 7, 14, 11, 26)
	print("built")
except ConstraintError:
	print("refused")
""", encoding = "ascii")
	ran = subprocess.run([sys.executable, "-I", "run.py"], cwd = out,
	                     capture_output = True, text = True)
	assert ran.returncode == 0, ran.stderr
	assert ran.stdout.split() == ["30590207141126", "refused"], ran.stdout
