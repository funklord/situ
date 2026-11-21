"""The frame's length selects an arm: `switch (remaining)` (26.1125).

fuzznet (inbox 20261120T2230Z) keeps a record that gained a member, and
journals still hold the shorter one; only the record's length says which.
NTP's datagram is the same shape: RFC 5905 7.3's authenticator is absent
at 48 bytes, an MD5 digest at 68 and a SHA-1 at 72, and nothing in the
packet says so. Both are a variant switched on `remaining`.

The four backends and both walkers are held to one answer for each
length, through the four-way drivers and the walker's listing; the layout
is held to refusing the three schemas that cannot mean what they say.
"""

from __future__ import annotations

import pytest

from pathlib import Path

from situc.diagnostics import SituError
from situc.layout import solve
from situc.pack import pack
from situc.parser import parse_text
from situc.resolve import resolve

from every_schema import ROOT, load_schema
from fourway import COMPLETE, answers, build
from test_walker import _by_member

import sys
sys.path.insert(0, str(ROOT))
from walker import report				# noqa: E402
from walker.image import load			# noqa: E402

NTP = ROOT / "example" / "ntp" / "ntp.situ"

SETTING = """target buffer;
endian big;

struct ranked { u8 rank; u8 signature[64]; }
struct unranked { u8 signature[64]; }

struct setting [fills_frame] {
	u16 key;
	u32 value;
	variant tail switch (remaining) {
		case 65: ranked   with_rank;
		default: unranked without_rank;
	}
}
"""


def _refused(source: str) -> str:
	parsed = parse_text(source)
	with pytest.raises(SituError) as caught:
		solve(parsed)
	return str(caught.value)


def test_a_setting_of_either_length_lays_out() -> None:
	parsed = parse_text(SETTING)
	resolve(parsed, solve(parsed))


def test_a_case_arm_is_the_size_its_case_says() -> None:
	assert "is read when 64 bytes remain, and is 65 bytes" in _refused(
		SETTING.replace("case 65:", "case 64:"))


def test_a_default_arm_is_no_cases_size() -> None:
	assert "which a `case` claims" in _refused(SETTING.replace(
		"case 65: ranked   with_rank;", "case 64: unranked with_rank;"))


def test_the_variant_ends_the_frame() -> None:
	assert "is not the frame's last member" in _refused(
		SETTING.replace("\t}\n}\n", "\t}\n\tu8 trailer;\n}\n"))


def _datagram(authenticator: int) -> bytes:
	"""An NTPv4 server reply, then `authenticator` bytes of key and digest."""
	packet = bytes([0x24, 2, 6, 0xEC]) + bytes(44)
	return packet + bytes(range(1, authenticator + 1))


LENGTHS = (0, 20, 24, 2, 21, 30)


@pytest.mark.skipif(not COMPLETE, reason="a backend is missing")
def test_four_backends_and_the_walker_read_one_datagram_alike(
		tmp_path: Path) -> None:
	command = build(tmp_path, NTP)
	parsed = load_schema(NTP)
	blob, _ = pack(parsed, resolve(parsed, solve(parsed)), metadata=True)
	image = load(blob)
	verdicts = []
	for extra in LENGTHS:
		packet = _datagram(extra)
		said = {backend: {key: line for key, line in
		                  _by_member(answers(argv, packet, tmp_path)).items()
		                  if key[0] == "ntp_datagram"}
		        for backend, argv in command.items()}
		first = said["c"]
		assert first, extra
		for backend, lines in said.items():
			assert lines == first, (extra, backend)
		walked = _by_member(report.listing(image, packet))
		assert ("ntp_datagram", "validate") in walked, extra
		for key, line in walked.items():
			if key[0] == "ntp_datagram":
				assert first.get(key) == line, (extra, key)
		verdicts.append(first[("ntp_datagram", "validate")])
	# Three lengths the variant names, and three it refuses as an
	# unknown discriminant -- `default: error`.
	assert verdicts[:3] == ["validate 0"] * 3, verdicts
	assert len(set(verdicts[3:])) == 1 and verdicts[3] != "validate 0", \
		verdicts
