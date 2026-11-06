"""Radiotap: 24 optional fields, one builder, each aligned from the header.

A builder per combination would be 2^24 of them, which is the fan-out 26.881
removed, and each field aligns to its own size from the start of the header,
which is 26.889. The vectors are laid out from the specification's table by a
script that knows nothing of situ and were read back by tshark. The table here
says the same thing beside the schema and not through it, so a builder that
agrees with its own reader cannot pass by accident.
"""

from __future__ import annotations

import importlib
import inspect
import struct
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

import fourway

ROOT = Path(__file__).resolve().parents[2]
SCHEMA = ROOT / "example" / "radiotap" / "radiotap.situ"
VECTORS = SCHEMA.with_suffix(".vectors")

#: bit: (variant, arm present, arm absent, alignment, size), from the
#: specification. The sizes and alignments are the point of the test.
FIELDS = {
	0:  ("tsft", "mactime", 8, 8),
	1:  ("flags", "frame_flags", 1, 1),
	2:  ("rate", "rate_500kbps", 1, 1),
	3:  ("channel", "channel_data", 2, 4),
	4:  ("fhss", "fhss_data", 2, 2),
	5:  ("antsignal", "dbm_antsignal", 1, 1),
	6:  ("antnoise", "dbm_antnoise", 1, 1),
	7:  ("lock", "lock_quality", 2, 2),
	8:  ("txatt", "tx_attenuation", 2, 2),
	9:  ("dbtxatt", "db_tx_attenuation", 2, 2),
	10: ("txpower", "dbm_tx_power", 1, 1),
	11: ("antenna", "antenna_index", 1, 1),
	12: ("dbsignal", "db_antsignal", 1, 1),
	13: ("dbnoise", "db_antnoise", 1, 1),
	14: ("rxflags", "rx_flags", 2, 2),
	15: ("txflags", "tx_flags", 2, 2),
	16: ("rts", "rts_retries", 1, 1),
	17: ("data", "data_retries", 1, 1),
	18: ("xchannel", "xchannel_data", 4, 8),
	19: ("mcs", "mcs_data", 1, 3),
	20: ("ampdu", "ampdu_data", 4, 8),
	21: ("vht", "vht_data", 2, 12),
	22: ("timestamp", "timestamp_data", 8, 12),
	23: ("he", "he_data", 2, 12),
}

#: Arms that take a number rather than bytes, signed where the schema says.
NUMBERS = {"mactime": "<Q", "frame_flags": "<B", "rate_500kbps": "<B",
           "dbm_antsignal": "<b", "dbm_antnoise": "<b", "lock_quality": "<H",
           "tx_attenuation": "<H", "db_tx_attenuation": "<H",
           "dbm_tx_power": "<b", "antenna_index": "<B", "db_antsignal": "<B",
           "db_antnoise": "<B", "rx_flags": "<H", "tx_flags": "<H",
           "rts_retries": "<B", "data_retries": "<B"}


def _vectors() -> dict[str, bytes]:
	found = {}
	for line in VECTORS.read_text(encoding="ascii").splitlines():
		words = line.split()
		if words and words[0] == "radiotap":
			found[words[1]] = bytes.fromhex(words[2])
	return found


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory) -> ModuleType:
	out = tmp_path_factory.mktemp("radiotap")
	subprocess.run([sys.executable, "-m", "situc", "build", str(SCHEMA),
	                "--target", "python", "--layer", "edit", "--out", str(out)],
	               check=True, capture_output=True, cwd=ROOT)
	sys.path[:0] = [str(out), str(ROOT / "runtime" / "python")]
	try:
		return importlib.import_module("radiotap_build")
	finally:
		del sys.path[:2]
		# As for pes: the sweeps build `radiotap` for themselves here.
		sys.modules.pop("radiotap_build", None)
		sys.modules.pop("radiotap", None)


def _fields(packet: bytes) -> tuple[int, dict[int, bytes], int]:
	"""The extension words' length, each field's bytes, and where they end."""
	present = int.from_bytes(packet[4:8], "little")
	at = 8
	word = present
	while word >> 31:
		word = int.from_bytes(packet[at:at + 4], "little")
		at += 4
	words = at - 8
	found: dict[int, bytes] = {}
	for bit, (_, _, align, size) in FIELDS.items():
		if present >> bit & 1:
			at += -at % align
			found[bit] = packet[at:at + size]
			at += size
	return words, found, at


def _rebuilt(built: ModuleType, packet: bytes) -> bytes:
	"""`packet` again, from one value per field and nothing about padding."""
	words, fields, end = _fields(packet)
	present = int.from_bytes(packet[4:8], "little")
	assert end == int.from_bytes(packet[2:4], "little")

	chosen = {}
	for bit, (name, arm, _, _) in FIELDS.items():
		kind = getattr(built, f"radiotap_{name}_field_arm")
		if bit not in fields:
			chosen[f"{name}_field"] = getattr(kind, f"{name}_absent")()
		elif arm in NUMBERS:
			value = struct.unpack(NUMBERS[arm], fields[bit])[0]
			chosen[f"{name}_field"] = getattr(kind, arm)(value)
		else:
			chosen[f"{name}_field"] = getattr(kind, arm)(fields[bit])

	more = built.radiotap_more_arm
	out = bytearray(len(packet))
	wrote = built.build_radiotap(
		out, version=0, header_pad=packet[1],
		len=int.from_bytes(packet[2:4], "little"),
		reserved0=present >> 24 & 31,
		radiotap_namespace=present >> 29 & 1,
		vendor_namespace=present >> 30 & 1,
		more=(more.more_words(packet[8:8 + words]) if words
		      else more.no_more()),
		unparsed=packet[end:], **chosen)
	return bytes(out[:wrote])


def test_the_table_is_the_header() -> None:
	"""The vectors end where the header's own length says, by this table."""
	for name, packet in _vectors().items():
		assert _fields(packet)[2] == int.from_bytes(packet[2:4], "little"), name


@pytest.mark.parametrize("name", _vectors())
def test_a_header_is_rebuilt_byte_for_byte(built: ModuleType, name: str) -> None:
	packet = _vectors()[name]
	assert _rebuilt(built, packet).hex(" ") == packet.hex(" ")


def test_the_present_bits_are_not_the_callers(built: ModuleType) -> None:
	"""Each arm sets its bit, so no parameter can contradict it."""
	taken = inspect.signature(built.build_radiotap).parameters
	assert not [one for one in taken if one.startswith("has_")]


def test_the_header_is_one_builder(built: ModuleType) -> None:
	assert [name for name in dir(built)
	        if name.startswith("build_radiotap")] == ["build_radiotap"]


def test_a_pad_takes_no_value(built: ModuleType) -> None:
	"""The caller says `channel` once, and its pad comes with the arm."""
	assert not [one for one in dir(built) if one.endswith("_align_arm")]


@pytest.mark.skipif(not fourway.COMPLETE, reason="needs all four toolchains")
def test_the_four_read_every_vector_alike(tmp_path: Path) -> None:
	"""And every prefix of two of them, which is where a pad meets the end."""
	command = fourway.build(tmp_path, SCHEMA)
	vectors = _vectors()
	cut = [packet[:length] for name in ("mac80211_rx", "modern_rx")
	       for packet in (vectors[name],) for length in range(len(packet))]
	for packet in (*vectors.values(), *cut):
		given = {name: fourway.answers(argv, packet, tmp_path)
		         for name, argv in command.items()}
		assert len(set(given.values())) == 1, (packet.hex(), given)
