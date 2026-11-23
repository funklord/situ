"""The dissector shows what time a field holds (26.1165, step 6).

A schema saying `u64 expires_at [time = ms, epoch = unix]` had every
generated reader carry the unit as constants and the wire contract name
it, while the one reader a person looks at -- Wireshark -- showed
`1795356529110`. The row keeps the number and gains its meaning: an
instant as the date it names on its epoch's scale, a duration in seconds,
a count on the caller's clock as seconds with no date.

Run in tshark itself; the stub cannot say what Wireshark's `os.date` or
`TvbRange:uint64` do. The dates are worked out independently below with
Python's `datetime`, not read back from the dissector.
"""

from __future__ import annotations

import shutil
import struct
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from situc.cli import analyse
from situc.dissector import generate
from situc.parser import parse

BIG = """target buffer;
endian big;
struct clock {
	u64 expires_at [time = ms, epoch = unix];
	u32 rtt [duration = s / 90000];
	u32 ntp_seconds [time = s, epoch = ntp];
	u32 since_boot [time = ms, epoch = caller];
	u32 from_date [time = s, epoch = "2000-01-01"];
}
"""

LITTLE = """target buffer;
endian little;
struct clock {
	i32 skew [duration = ms];
	u64 stamp [time = us, epoch = unix];
}
"""

INSTANT = 1795356529


def _shown(tmp_path: Path, text: str, packet: bytes) -> str:
	schema = tmp_path / "unit.situ"
	schema.write_text(text, encoding="ascii")
	source, resolved, _ = analyse(schema)
	script = tmp_path / "unit.lua"
	script.write_text(generate(parse(source), resolved, "unit")
	                  + 'DissectorTable.get("wtap_encap")'
	                  ":add(wtap.USER0, clock)\n", encoding="ascii")
	dump = tmp_path / "p.hex"
	dump.write_text("0000 " + " ".join(f"{b:02x}" for b in packet) + "\n",
	                encoding="ascii")
	capture = tmp_path / "p.pcap"
	subprocess.run(["text2pcap", "-q", "-l", "147", str(dump), str(capture)],
	               check=True, capture_output=True)
	shown = subprocess.run(["tshark", "-r", str(capture),
	                        "-X", f"lua_script:{script}", "-V"],
	                       capture_output=True, text=True, timeout=120)
	assert "Lua: Error" not in shown.stderr, shown.stderr
	return shown.stdout


def _utc(when: datetime) -> str:
	return when.strftime("%Y-%m-%d %H:%M:%S")


@pytest.mark.skipif(shutil.which("tshark") is None
                    or shutil.which("text2pcap") is None,
                    reason="needs tshark and text2pcap")
def test_each_epoch_and_a_duration_read_as_they_mean(tmp_path: Path) -> None:
	ntp = INSTANT + 2208988800
	packet = struct.pack(">QIIII", INSTANT * 1000 + 110, 135000, ntp, 1500,
	                     16)
	shown = _shown(tmp_path, BIG, packet)
	unix = datetime.fromtimestamp(INSTANT, timezone.utc)
	since_1900 = datetime(1900, 1, 1, tzinfo=timezone.utc) \
		+ timedelta(seconds=ntp)
	assert f"expires_at: {INSTANT * 1000 + 110} ({_utc(unix)}.110 UTC)" \
		in shown, shown
	assert "rtt: 135000 (1.50000 s)" in shown, shown
	assert f"ntp_seconds: {ntp} ({_utc(since_1900)} UTC)" in shown, shown
	assert "since_boot: 1500 (1.500 s on the caller's clock)" in shown, shown
	assert "from_date: 16 (2000-01-01 00:00:16 UTC)" in shown, shown


@pytest.mark.skipif(shutil.which("tshark") is None
                    or shutil.which("text2pcap") is None,
                    reason="needs tshark and text2pcap")
def test_a_little_endian_signed_duration_keeps_its_sign(
		tmp_path: Path) -> None:
	packet = struct.pack("<iQ", -1500, INSTANT * 1000000 + 250)
	shown = _shown(tmp_path, LITTLE, packet)
	unix = datetime.fromtimestamp(INSTANT, timezone.utc)
	assert "skew: -1500 (-1.500 s)" in shown, shown
	assert f"({_utc(unix)}.000250 UTC)" in shown, shown
