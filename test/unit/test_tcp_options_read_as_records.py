"""TCP's and IPv4's options are records, read as tshark reads them (26.913).

`[holds]` inside the checksum's `authenticated` block, over a SYN laid out as
Linux sends it. `TSHARK` is that tool's decode of these bytes, through
`text2pcap -i 6`, and not situ's.
"""

from __future__ import annotations

from typing import Any
import sys
from types import ModuleType

import pytest

from every_schema import ROOT
from situc.cli import analyse
from situc.codegen.python import generate
from situc.parser import parse

sys.path.insert(0, str(ROOT / "runtime" / "python"))

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
	return tcp.tcp_header.at(tcp.Message(data), 0, len(data))


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
	view = tcp.tcp_header.at(tcp.Message(data), 0, len(data))
	with pytest.raises(tcp.BoundsError):
		view.validate()


def test_a_length_below_its_own_two_bytes_is_refused(tcp: ModuleType) -> None:
	"""RFC 9293 3.1 counts the kind and length, so 1 is no length at all."""
	with pytest.raises(tcp.ConstraintError):
		segment(tcp, bytes.fromhex("0201" "0000")).validate()


#: Router Alert, No Operation, Record Route, End of Option List and padding.
IPV4 = ("49000024000100" "004006ee87c0000201c6336407"
        "94040000" "01" "07070400000000" "00000000")

#: `tshark -T fields -e ip.opt.type -e ip.opt.len -e ip.opt.ptr`, through
#: `text2pcap -l 101`. It stops at End of Option List, and the padding after
#: that reads here as more of them.
TSHARK_IPV4 = {"kinds": [148, 1, 7, 0], "lengths": [4, 7], "pointer": 4}


def test_ipv4_options_read_as_tshark_reads_them() -> None:
	"""The same grammar in the other header, kinds with their high bits set."""
	source, resolved, _ = analyse(ROOT / "example" / "ipv4" / "ipv4.situ")
	module = ModuleType("ipv4_under_test")
	exec(generate(parse(source), resolved, "ipv4").module, module.__dict__)  # noqa: S102

	data = bytearray.fromhex(IPV4)
	view = module.ipv4_header.at(module.Message(data), 0, len(data))
	view.validate()
	held = view.options_view
	items = [held.all(at) for at in range(held.all_count)]
	kinds = [item.kind for item in items]
	assert kinds[:kinds.index(0) + 1] == TSHARK_IPV4["kinds"]
	assert not any(kinds[kinds.index(0):])

	sized = [item.body_sized for item in items if item.kind > 1]
	assert [one.length for one in sized] == TSHARK_IPV4["lengths"]
	assert sized[1].value[0] == TSHARK_IPV4["pointer"]
