"""ffmpeg's PES packets come back byte for byte from one builder (26.881).

The arm-value builder had met one real format, mqtt, whose three flags made
eight combinations. PES has six groups written out, which would have been 96
builders. The vectors are not situ's: five are ffmpeg's and the sixth was read
back by tshark, so a builder that agrees with its own reader cannot pass here
by accident. The groups are cut out of each vector by the standard's sizes,
beside the schema and not through it.
"""

from __future__ import annotations

import importlib
import re
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCHEMA = ROOT / "example" / "pes" / "pes.situ"
VECTORS = SCHEMA.with_suffix(".vectors")

#: The fixed nine bytes: start code, stream id, length, two flag bytes, and
#: the header length.
FIXED = 9


def _vectors() -> dict[str, bytes]:
	found = {}
	for line in VECTORS.read_text(encoding="ascii").splitlines():
		words = line.split()
		if words and words[0] == "pes":
			found[words[1]] = bytes.fromhex("".join(words[2:]))
	return found


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory) -> ModuleType:
	out = tmp_path_factory.mktemp("pes")
	subprocess.run([sys.executable, "-m", "situc", "build", str(SCHEMA),
	                "--target", "python", "--layer", "edit", "--out", str(out)],
	               check=True, capture_output=True, cwd=ROOT)
	sys.path[:0] = [str(out), str(ROOT / "runtime" / "python")]
	try:
		return importlib.import_module("pes_build")
	finally:
		del sys.path[:2]
		# The sweeps build `pes` for themselves in this process, and must
		# not be handed this one: its errors are of another `situ_runtime`.
		sys.modules.pop("pes_build", None)
		sys.modules.pop("pes", None)


def _take(packet: bytes, at: int, count: int) -> tuple[bytes, int]:
	return packet[at:at + count], at + count


def _rebuilt(built: ModuleType, packet: bytes) -> bytes:
	"""`packet` again, from its fields and one value per group."""
	flags, options = packet[6], packet[7]
	at = FIXED

	times = built.pes_times_arm.no_time()
	if options >> 6 == 2:
		pts, at = _take(packet, at, 5)
		times = built.pes_times_arm.pts_only(pts)
	elif options >> 6 == 3:
		both, at = _take(packet, at, 10)
		times = built.pes_times_arm.pts_dts(both)

	clock = built.pes_clock_arm.escr_absent()
	if options & 0x20:
		escr, at = _take(packet, at, 6)
		clock = built.pes_clock_arm.escr_present(escr)

	rate = built.pes_rate_arm.rate_absent()
	if options & 0x10:
		es_rate, at = _take(packet, at, 3)
		rate = built.pes_rate_arm.rate_present(es_rate)

	trick = built.pes_trick_arm.trick_absent()
	if options & 0x08:
		trick = built.pes_trick_arm.trick_present(packet[at])
		at += 1

	copy = built.pes_copy_arm.copy_absent()
	if options & 0x04:
		info, at = _take(packet, at, 1)
		copy = built.pes_copy_arm.copy_present(info)

	check = built.pes_check_arm.crc_absent()
	if options & 0x02:
		crc, at = _take(packet, at, 2)
		check = built.pes_check_arm.crc_present(crc)

	out = bytearray(len(packet))
	wrote = built.build_pes(
		out, 0, 1, packet[3], int.from_bytes(packet[4:6], "big"),
		flags >> 6, (flags >> 4) & 3, (flags >> 3) & 1, (flags >> 2) & 1,
		(flags >> 1) & 1, flags & 1, options & 1, packet[8],
		times, clock, rate, trick, copy, check, packet[at:])
	return bytes(out[:wrote])


@pytest.mark.parametrize("name", _vectors())
def test_a_packet_is_rebuilt_byte_for_byte(built: ModuleType, name: str) -> None:
	packet = _vectors()[name]
	assert _rebuilt(built, packet).hex(" ") == packet.hex(" ")


def test_the_flags_are_not_the_callers(built: ModuleType) -> None:
	"""Each arm sets its own flag, so no parameter can contradict it."""
	source = Path(str(built.__file__)).read_text(encoding="ascii")
	signature = re.search(r"def build_pes\((.*?)\) -> int", source, re.S)
	assert signature is not None
	parameters = signature.group(1)
	assert "extension_flag" in parameters
	for flag in ("pts_dts_flags", "escr_flag", "es_rate_flag",
	             "trick_mode_flag", "copy_info_flag", "crc_flag"):
		assert flag not in parameters


def test_the_packet_is_one_builder(built: ModuleType) -> None:
	source = Path(str(built.__file__)).read_text(encoding="ascii")
	assert len(re.findall(r"^def build_pes\w*\(", source, re.M)) == 1
