"""MQTT's PUBLISH is built, body and packet (26.1022).

Two refusals stood in the way. `publish_body` takes `parameter u2 qos`, and
a parameter two bits wide was taken for a packed field whose group did not
start on a byte boundary -- though an argument is no bytes at all. And the
packet's PUBLISH arm ends the message but takes `qos = (flags >> 1) & 3`,
which only the message supplies, so the arm could not be validated on its
own; the packet's closing validation checks it with the qos it derives,
as the reader does.

Held to MQTT 3.1.1 section 3.3: QoS 1, topic `a/b`, packet identifier 10,
payload `hi` -- `32 09 00 03 61 2f 62 00 0a 68 69`.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from test_an_arm_sized_by_its_argument_is_written import (COMPILER, ROOT,
                                                          _built, _run)

BODY   = "0003612f62000a6869"
PACKET = "3209" + BODY


@pytest.mark.skipif(COMPILER is None, reason = "needs a C compiler")
def test_c(tmp_path: Path) -> None:
	want = ", ".join(f"0x{PACKET[i:i + 2]}" for i in range(0, len(PACKET), 2))
	_run(tmp_path, _built(tmp_path, "mqtt"), "mqtt", f"""
	static const uint8_t topic[3] = {{ 'a', '/', 'b' }};
	static const uint8_t ident[2] = {{ 0, 10 }};
	static const uint8_t payload[2] = {{ 'h', 'i' }};
	static const uint8_t want[11] = {{ {want} }};
	uint8_t  body[32], packet[32];
	uint32_t body_len = 0, wrote = 0;

	if (situ_publish_body_build_at_least_once(body, sizeof body, 1u, topic,
	                                          3u, ident, 2u, payload, 2u,
	                                          &body_len) != SITU_OK
	    || body_len != 9u) {{
		return 1;
	}}
	/* Flags 0b0010: QoS 1, no DUP, no RETAIN. */
	if (situ_packet_build_publish(packet, sizeof packet, 2u, 9u, body,
	                              body_len, &wrote) != SITU_OK
	    || wrote != 11u || memcmp(packet, want, 11u) != 0) {{
		return 2;
	}}
	/* Flags 0b0110 say QoS 3, which `publish_body` refuses as
	 * `default: error`: the qos reaches the arm. */
	if (situ_packet_build_publish(packet, sizeof packet, 6u, 9u, body,
	                              body_len, &wrote) == SITU_OK) {{
		return 3;
	}}
""")


def test_python(tmp_path: Path) -> None:
	out = tmp_path / "py"
	done = subprocess.run(
		[sys.executable, "-m", "situc", "build",
		 str(ROOT / "example" / "mqtt" / "mqtt.situ"), "--target", "python",
		 "--layer", "edit", "--out", str(out)],
		cwd = ROOT, capture_output = True, text = True)
	assert done.returncode == 0, done.stderr
	(out / "situ_runtime.py").write_text(
		(ROOT / "runtime" / "python" / "situ_runtime.py").read_text(
			encoding = "ascii"), encoding = "ascii")
	(out / "run.py").write_text("""import sys
sys.path.insert(0, ".")
import mqtt_build
from situ_runtime import SituError
body = bytearray(32)
n = mqtt_build.build_publish_body_at_least_once(body, 1, b"a/b", bytes([0, 10]), b"hi")
print(bytes(body[:n]).hex())
packet = bytearray(32)
m = mqtt_build.build_packet_publish(packet, 2, n, bytes(body[:n]))
print(bytes(packet[:m]).hex())
try:
	mqtt_build.build_packet_publish(packet, 6, n, bytes(body[:n]))
	print("built")
except SituError:
	print("refused")
""", encoding = "ascii")
	ran = subprocess.run([sys.executable, "-I", "run.py"], cwd = out,
	                     capture_output = True, text = True)
	assert ran.returncode == 0, ran.stderr
	assert ran.stdout.split() == [BODY, PACKET, "refused"], ran.stdout
