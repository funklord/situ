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
