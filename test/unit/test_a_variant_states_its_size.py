"""A variant states the bytes it occupies (26.1040).

MQTT's fixed header ends in a Remaining Length that is the body's size
(MQTT 3.1.1, 2.2.3), and the body is a switch on the packet type. Before this a PUBLISH
payload, `u8 payload[remaining]`, ran to the end of whatever buffer held
the packet, so a broker could not frame one off a stream: no backend
emitted `packet_required`. `variant body[length] switch (kind)` says the
body is `length` bytes whichever arm is there, and every reader holds the
arm to that window.

Held to bytes worked out by hand from the standard: a PUBLISH of topic
"a/b" and payload "hi" is 30 07 00 03 61 2f 62 68 69 -- nine bytes, and two
bytes after it belong to the next packet.
"""

from __future__ import annotations

from pathlib import Path

import subprocess
import sys

import pytest

from every_schema import ROOT
from situc.cli import analyse
from situc.diagnostics import SituError
from situc.dissector import generate
from situc.parser import parse
from test_dissector import LUA, read_back
from test_walker_c import (COMPILER, VERDICT, _build_driver, c_verdict,
                           image_for, python_verdict, shape_named)
import fourway

MQTT    = ROOT / "example" / "mqtt" / "mqtt.situ"
PUBLISH = bytes.fromhex("30070003612f626869")
NEXT    = bytes.fromhex("eeee")


@pytest.mark.skipif(not fourway.COMPLETE, reason = "needs gcc, g++ and rustc")
def test_the_four_frame_a_packet_by_its_length(tmp_path: Path) -> None:
	commands = fourway.build(tmp_path, MQTT)
	said = {name: fourway.answers(command, PUBLISH + NEXT, tmp_path)
	        for name, command in commands.items()}
	assert len(set(said.values())) == 1, said
	packet = next(iter(said.values())).split("-- packet\n", 1)[1]
	asked = next(line for line in packet.splitlines()
	             if line.startswith("required()"))
	# One answer per prefix, `*` where it is complete: nine once the ninth
	# byte is here, and never the eleven buffered.
	assert asked.split()[-3:] == ["*9", "*9", "*9"], asked


@pytest.mark.skipif(COMPILER is None, reason = "needs a C compiler")
def test_both_walkers_hold_the_length(tmp_path: Path) -> None:
	image = image_for(MQTT)
	shape = shape_named(MQTT, "packet")
	(tmp_path / "img").write_bytes(image)
	drive = _build_driver(tmp_path, image, VERDICT)
	for message, verdict in ((PUBLISH, "0"), (PUBLISH + NEXT, "0"),
	                         (PUBLISH[:8], "1"),
	                         # A SUBSCRIBE whose length leaves its one
	                         # filter's options byte outside: the next
	                         # packet's byte is not this one's, so the
	                         # filter is cut.
	                         (bytes.fromhex("82050001000161ee"), "1"),
	                         (bytes.fromhex("820600010001610100"), "0")):
		assert python_verdict(image, message, shape) == verdict, message.hex()
		assert c_verdict(tmp_path, image, message, shape, drive) == verdict, \
			message.hex()


@pytest.mark.skipif(LUA is None, reason = "no Lua interpreter")
def test_the_dissector_steps_over_the_length(tmp_path: Path) -> None:
	source, resolved, _ = analyse(MQTT)
	lua = tmp_path / "mqtt.lua"
	lua.write_text(generate(parse(source), resolved, "mqtt"), encoding = "ascii")
	# PUBACK's two-byte body in a length of three: the packet is five.
	assert read_back(lua, "packet", bytes.fromhex("4003000901"))[0] == 5
	used, rows = read_back(lua, "packet", bytes.fromhex("82050001000161ee"))
	assert used == 7
	assert ("subscribe_body.filters", 4, 3, "000161") in rows, rows


def test_a_negative_size_is_refused(tmp_path: Path) -> None:
	path = tmp_path / "neg.situ"
	path.write_text("struct one { u8 n; }\n"
	                "struct m { i8 k; variant v[k] switch (k) {"
	                " case 1: one a; default: error; } }\n",
	                encoding = "ascii")
	with pytest.raises(SituError, match = "negative size"):
		analyse(path)


def test_a_builder_computes_the_size_only_from_a_field(tmp_path: Path) -> None:
	"""MQTT's size is a field and its builders compute it from the arm; an
	arithmetic size has no one field to write, and the builder says so."""
	path = tmp_path / "twice.situ"
	path.write_text("struct one { u8 n; }\n"
	                "struct m { u8 k; u8 len; variant v[len * 2] switch (k) {"
	                " case 1: one a; default: error; } }\n",
	                encoding = "ascii")
	done = subprocess.run([sys.executable, "-m", "situc", "build", str(path),
	                       "--target", "c", "--layer", "edit", "--out",
	                       str(tmp_path)], capture_output = True, text = True,
	                      cwd = ROOT)
	assert done.returncode == 0, done.stderr
	assert "`v` states its size, which is the arm's length and computed " \
	       "only" in done.stderr, done.stderr
