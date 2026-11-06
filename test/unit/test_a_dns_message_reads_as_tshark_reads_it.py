"""A whole DNS message, with every section and the common RDATA (26.914).

`example/dns` stopped at the header and said the body was not described. The
reply in `dnsname.vectors` has a CNAME, an A and an AAAA, an MX in authority, a
TXT in additional and four compression pointers. `TSHARK` is that tool's
decode of those bytes over UDP, and not situ's.
"""

from __future__ import annotations

from pathlib import Path
import sys
from types import ModuleType
from typing import Any

import pytest

import fourway
from every_schema import ROOT
from situc.cli import analyse
from situc.codegen.python import generate
from situc.parser import parse

sys.path.insert(0, str(ROOT / "runtime" / "python"))

#: `tshark -T fields -e dns.resp.type -e dns.a -e dns.aaaa
#: -e dns.mx.preference`, over the vector, through `text2pcap -u 53,5353`.
TSHARK = {"types": [5, 1, 28, 15, 16], "a": "192.0.2.1",
          "aaaa": "20010db8000000000000000000000001", "preference": 10,
          "txt": b"\x05hello"}


@pytest.fixture(scope = "module")
def dns() -> ModuleType:
	"""The generated module, as a namespace of its own."""
	source, resolved, _ = analyse(ROOT / "example" / "dnsname" / "dnsname.situ")
	module = ModuleType("dnsname_under_test")
	exec(generate(parse(source), resolved, "dnsname").module, module.__dict__)  # noqa: S102
	return module


def reply() -> bytearray:
	"""The vector's bytes."""
	path = ROOT / "example" / "dnsname" / "dnsname.vectors"
	line = next(one for one in path.read_text(encoding = "ascii").splitlines()
	            if one.startswith("message"))
	return bytearray.fromhex("".join(line.split()[2:]))


def view(dns: ModuleType, data: bytearray) -> Any:
	"""A view of a message."""
	return dns.message.at(dns.Message(data), 0, len(data))


def test_every_section_reads_as_tshark_reads_it(dns: ModuleType) -> None:
	"""Types in order, and each body where tshark found it."""
	message = view(dns, reply())
	message.validate()
	assert message.questions_count == 1
	found = [message.answers(at) for at in range(message.answers_count)] \
		+ [message.authority(at) for at in range(message.authority_count)] \
		+ [message.additional(at) for at in range(message.additional_count)]
	assert [record.rtype for record in found] == TSHARK["types"]

	address = found[1].rdata_a.data_view
	assert ".".join(str(one) for one in address.octets) == TSHARK["a"]
	assert bytes(found[2].rdata_aaaa.data_view.octets).hex() == TSHARK["aaaa"]
	assert found[3].rdata_mx.data_view.preference == TSHARK["preference"]
	assert bytes(found[4].rdata_other.data) == TSHARK["txt"]


def test_a_name_in_rdata_ends_in_a_pointer(dns: ModuleType) -> None:
	"""`cdn`, then the pointer to `example.com`, inside the CNAME's bytes."""
	message = view(dns, reply())
	held = message.answers(0).rdata_cname.data_view
	labels = [held.labels(at) for at in range(held.labels_count)]
	assert [one.form for one in labels] == [0, 3]
	assert bytes(labels[0].body_text) == b"cdn"
	assert labels[1].body_pointer_low == 0x10


def test_a_section_the_header_overcounts_is_refused(dns: ModuleType) -> None:
	"""ANCOUNT says four where three are there."""
	data = reply()
	data[7] = 4
	with pytest.raises(dns.BoundsError):
		view(dns, data).validate()


def test_an_rdlength_past_the_message_is_refused(dns: ModuleType) -> None:
	"""The TXT is last, so its length has nothing behind it."""
	data = reply()
	data[-7] = 0x40
	with pytest.raises(dns.BoundsError):
		view(dns, data).validate()


def test_an_address_shorter_than_itself_is_refused(dns: ModuleType) -> None:
	"""An A record whose RDLENGTH is 3 cannot hold four octets."""
	data = reply()
	at = data.index(bytes([0, 4, 192, 0, 2, 1]))
	data[at + 1] = 3
	with pytest.raises(dns.BoundsError):
		view(dns, data).validate()


@pytest.mark.skipif(not fourway.COMPLETE, reason = "needs gcc, g++ and rustc")
def test_all_four_read_it_alike(tmp_path: Path) -> None:
	"""Each backend's whole answer about each message, the hostile ones too."""
	command = fourway.build(tmp_path, ROOT / "example" / "dnsname"
	                        / "dnsname.situ")
	assert len(command) == 4
	overcounted = reply()
	overcounted[7] = 4
	for data in (reply(), overcounted):
		given = {backend: fourway.answers(argv, bytes(data), tmp_path)
		         for backend, argv in command.items()}
		assert "-- message\n" in given["python"], given["python"][:400]
		assert len(set(given.values())) == 1, given
