"""TCP's options are records, read as tshark reads them (26.913).

`[holds]` inside the checksum's `authenticated` block, over a SYN laid out as
Linux sends it. `TSHARK` is that tool's decode of these bytes, through
`text2pcap -i 6`, and not situ's.
"""

from __future__ import annotations

import sys
from typing import Any
from types import ModuleType

import pytest

from every_schema import ROOT
from situc.cli import analyse
from situc.codegen.python import generate
from situc.parser import parse

sys.path.insert(0, str(ROOT / "runtime" / "python"))
from situ_runtime import BoundsError, ConstraintError, Message  # type: ignore[import-not-found]  # noqa: E402

FIXED = bytes.fromhex("c001005000000001" "00000000" "a002" "faf0" "0000" "0000")
#: MSS 1460, SACK permitted, timestamps, No-Operation, window scale 7.
OPTIONS = bytes.fromhex("020405b4" "0402" "080a0001e24000000000" "01" "030307")

#: `tshark -T fields -e tcp.option_kind -e tcp.option_len -e
#: tcp.options.mss_val -e tcp.options.wscale.shift -e
#: tcp.options.timestamp.tsval`.
TSHARK = {"kinds": [2, 4, 8, 1, 3], "lengths": [4, 2, 10, 3], "mss": 1460,
          "shift": 7, "tsval": 123456}


@pytest.fixture(scope = "module")
def tcp() -> ModuleType:
	"""The generated module, as a namespace of its own."""
	source, resolved, _ = analyse(ROOT / "example" / "tcp" / "tcp.situ")
	module = ModuleType("tcp_under_test")
	exec(generate(parse(source), resolved, "tcp").module, module.__dict__)  # noqa: S102
	return module


def segment(tcp: ModuleType, options: bytes) -> Any:
	"""A view of a segment carrying `options`, its offset set to fit them."""
	data = bytearray(FIXED + options)
	data[12] = (5 + len(options) // 4) << 4
	return tcp.tcp_header.at(Message(data), 0, len(data))


def test_a_syn_reads_as_tshark_reads_it(tcp: ModuleType) -> None:
	"""One byte or a length, by kind, and the value is where tshark says."""
	view = segment(tcp, OPTIONS)
	view.validate()
	held = view.options_view
	items = [held.all(at) for at in range(held.all_count)]
	assert [item.kind for item in items] == TSHARK["kinds"]

	sized = {item.kind: item.body_sized for item in items if item.kind != 1}
	assert [item.length for item in sized.values()] == TSHARK["lengths"]
	assert int.from_bytes(sized[2].value, "big") == TSHARK["mss"]
	assert sized[3].value[0] == TSHARK["shift"]
	assert int.from_bytes(sized[8].value[:4], "big") == TSHARK["tsval"]


def test_padding_after_the_end_reads_as_more_of_it(tcp: ModuleType) -> None:
	"""A word's worth of options, closed and padded as 3.1 says."""
	view = segment(tcp, bytes.fromhex("01" "01" "00" "00"))
	view.validate()
	held = view.options_view
	assert [held.all(at).kind for at in range(held.all_count)] == [1, 1, 0, 0]


@pytest.mark.parametrize("claimed", [5, 6, 255])
def test_an_option_past_its_bytes_is_refused(tcp: ModuleType,
		claimed: int) -> None:
	"""The frame is `data_offset`'s, so the payload is not an option's."""
	data = bytearray(FIXED + bytes([2, claimed, 0x05, 0xb4]) + b"payload!")
	data[12] = 6 << 4
	view = tcp.tcp_header.at(Message(data), 0, len(data))
	with pytest.raises(BoundsError):
		view.validate()


def test_a_length_below_its_own_two_bytes_is_refused(tcp: ModuleType) -> None:
	"""RFC 9293 3.1 counts the kind and length, so 1 is no length at all."""
	with pytest.raises(ConstraintError):
		segment(tcp, bytes.fromhex("0201" "0000")).validate()
