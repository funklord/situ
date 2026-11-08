"""situ's radiotap dissector against Wireshark's own (26.932).

Every vector in `radiotap.vectors`, read twice by one tshark: once with
the dissector `gen-dissector` writes, once with Wireshark's built-in
radiotap dissector, which nothing in situ wrote. The cursor stopped at the
first `[align]` arm until 26.932, so no field after the presence words
was shown; once reached, `mactime` and `lock_quality` read byte-reversed,
an arm having been added big-endian, and the channel was never shown.

Rate is left out: Wireshark's `radiotap.datarate` is computed, from the
MCS and VHT fields as well as the rate byte, where situ shows the byte.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from struct import pack

import pytest

ROOT = Path(__file__).resolve().parents[2]

#: situ's field, and Wireshark's, for the same bytes.
FIELDS = [
	("situ.radiotap.tsft_field.mactime",              "radiotap.mactime"),
	("situ.channel.frequency",                        "radiotap.channel.freq"),
	("situ.radiotap.antsignal_field.dbm_antsignal",   "radiotap.dbm_antsignal"),
	("situ.radiotap.lock_field.lock_quality",         "radiotap.quality"),
]


def _capture(path: Path, link: int, packets: list[bytes]) -> None:
	path.write_bytes(pack("<IHHiIII", 0xa1b2c3d4, 2, 4, 0, 0, 65535, link)
	                 + b"".join(pack("<IIII", 0, 0, len(one), len(one)) + one
	                            for one in packets))


def _fields(command: list[str], names: list[str]) -> list[list[str]]:
	ran = subprocess.run([*command, "-T", "fields",
	                      *(arg for name in names for arg in ("-e", name))],
	                     check=True, capture_output=True, text=True, timeout=120)
	return [line.split("\t") for line in ran.stdout.splitlines()]


@pytest.mark.skipif(shutil.which("tshark") is None, reason="no tshark")
def test_every_vector_reads_as_wiresharks_own_dissector(tmp_path: Path) -> None:
	subprocess.run([sys.executable, "-m", "situc", "gen-dissector",
	                str(ROOT / "example/radiotap/radiotap.situ"), "--out",
	                str(tmp_path)], check=True, cwd=ROOT, capture_output=True)
	packets = [bytes.fromhex(line.split()[2]) for line in
	           (ROOT / "example/radiotap/radiotap.vectors")
	           .read_text(encoding="ascii").splitlines()
	           if line.startswith("radiotap ")]
	_capture(tmp_path / "situ.pcap", 147, packets)
	_capture(tmp_path / "own.pcap", 127, packets)

	ours = _fields(["tshark", "-X", f"lua_script:{tmp_path / 'radiotap.lua'}",
	                "-o", 'uat:user_dlts:"User 0 (DLT=147)",'
	                '"situ_radiotap_radiotap","0","","0",""',
	                "-r", str(tmp_path / "situ.pcap")],
	               [one for one, _ in FIELDS])
	theirs = _fields(["tshark", "-r", str(tmp_path / "own.pcap")],
	                 [one for _, one in FIELDS])

	assert len(ours) == len(theirs) == len(packets)
	assert ours == theirs
	# Not vacuous: every field is shown for some vector.
	for column, (name, _) in enumerate(FIELDS):
		assert any(row[column] for row in ours), name
