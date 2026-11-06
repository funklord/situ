"""PUBLISH's packet identifier is read as one, from the QoS its header holds.

And a SUBSCRIBE's options are bits behind a variable filter, reserved ones
checked, which this file once called a real limit.

`example/mqtt` left it out for a reason that did not hold, so at QoS 1 and 2
its two bytes were the start of the payload (26.912). These are paho's bytes,
from `mqtt.vectors`, and the two the standard rules on.
"""

from __future__ import annotations

import sys
from types import ModuleType
from typing import Any

import pytest

from every_schema import ROOT
from situc.cli import analyse
from situc.codegen.python import generate
from situc.parser import parse

sys.path.insert(0, str(ROOT / "runtime" / "python"))
from situ_runtime import BoundsError, ConstraintError, Message, VersionError  # type: ignore[import-not-found]  # noqa: E402

PAHO_QOS0 = "30 07 00 03 61 2F 62 68 69"
PAHO_QOS1 = "32 10 00 08 73 65 6E 73 6F 72 2F 74 00 07 32 31 2E 35"
QOS2      = "34 07 00 01 61 00 2A 68 69"
#: 3.3.1.2: "A PUBLISH Packet MUST NOT have both QoS bits set to 1".
QOS3      = "36 07 00 01 61 00 2A 68 69"
#: The QoS says an identifier follows and the frame holds none.
QOS1_CUT  = "32 03 00 01 61"


@pytest.fixture(scope = "module")
def mqtt() -> ModuleType:
	"""The generated module, as a namespace of its own."""
	source, resolved, _ = analyse(ROOT / "example" / "mqtt" / "mqtt.situ")
	module = ModuleType("mqtt_under_test")
	exec(generate(parse(source), resolved, "mqtt").module, module.__dict__)  # noqa: S102
	return module


def publish(mqtt: ModuleType, text: str) -> Any:
	"""The validated PUBLISH body of a packet given as hex."""
	data = bytearray(bytes.fromhex(text))
	packet = mqtt.packet.at(Message(data), 0, len(data))
	packet.validate()
	return packet.body_publish


def test_qos_0_has_no_identifier(mqtt: ModuleType) -> None:
	"""The payload starts where the topic ends."""
	body = publish(mqtt, PAHO_QOS0)
	assert bytes(body.topic) == b"a/b"
	assert bytes(body.payload) == b"hi"


def test_qos_1_reads_paho_s_identifier(mqtt: ModuleType) -> None:
	"""The identifier paho wrote is an identifier, and not the payload."""
	body = publish(mqtt, PAHO_QOS1)
	assert bytes(body.topic) == b"sensor/t"
	assert body.ident_at_least_once.packet_id == 7
	assert bytes(body.payload) == b"21.5"


def test_qos_2_has_its_own_arm(mqtt: ModuleType) -> None:
	"""Two bits of the flags nibble select it, the shift included."""
	body = publish(mqtt, QOS2)
	assert body.ident_exactly_once.packet_id == 42
	assert bytes(body.payload) == b"hi"


def test_the_forbidden_qos_is_refused(mqtt: ModuleType) -> None:
	"""The standard's MUST NOT is the arm that does not exist."""
	with pytest.raises(VersionError):
		publish(mqtt, QOS3)


def test_an_identifier_past_the_frame_is_refused(mqtt: ModuleType) -> None:
	"""QoS 1 promises two bytes, and a short packet does not have them."""
	with pytest.raises(BoundsError):
		publish(mqtt, QOS1_CUT)


#: paho's SUBSCRIBE, whose last byte is the options after the filter `a/b`.
PAHO_SUBSCRIBE = "82 08 00 01 00 03 61 2F 62 00"


def subscribe(mqtt: ModuleType, last: int) -> Any:
	"""The first filter of paho's SUBSCRIBE, with its options byte replaced."""
	data = bytearray(bytes.fromhex(PAHO_SUBSCRIBE))
	data[-1] = last
	packet = mqtt.packet.at(Message(data), 0, len(data))
	packet.validate()
	return packet.body_subscribe.filters(0)


def test_a_subscription_reads_its_qos(mqtt: ModuleType) -> None:
	"""Two bits, in the byte after a filter whose length the data gives."""
	assert bytes(subscribe(mqtt, 0).filter) == b"a/b"
	assert subscribe(mqtt, 2).options.requested_qos == 2


@pytest.mark.parametrize("last", [0x03, 0x40, 0x04, 0xFE],
                         ids = ["qos-3", "bit-6", "bit-2", "all-reserved"])
def test_a_subscription_s_reserved_bits_are_checked(mqtt: ModuleType,
		last: int) -> None:
	"""3.8.3.1: bits 7-2 are zero, and 3 is no QoS."""
	with pytest.raises(ConstraintError):
		subscribe(mqtt, last)
