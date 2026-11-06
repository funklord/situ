"""A struct that ends in its frame, as a parent's last member (26.907).

It carries no length of its own, so it had no view: MQTT's PUBLISH body could
be neither read nor validated, and nothing said so but a comment. Its extent
is the view it is handed, which is a known number, and a parent's last member
is handed the rest.
"""

from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path

import pytest

import fourway
from test_walker_c import (COMPILER, _inline_image, c_verdict,
                           python_verdict, shape_named_text)

SCHEMA = """target buffer;
endian big;

struct plain {
	u8  kind [max = 9];
	u8  rest[remaining];
}

struct nested { u8 head; plain body; }

struct armed {
	u8 head;
	variant body switch (head) {
		case 0:  plain     one;
		case 1:  u8        two;
		default: error;
	}
}

struct deeper { u8 head; nested inner; }
"""

#: The `[max = 9]` is the point: nothing validated it, in any reader.
CASES = {
	"nested": {
		"0005":        "0",
		"0005aabb":    "0",
		"000a":        "2",
		"000aaabb":    "2",
	},
	"armed": {
		"0005":        "0",
		"0005aa":      "0",
		"000a":        "2",
		"0107":        "0",
		"02aa":        "3",
	},
	"deeper": {
		"000005aa":    "0",
		"00000a":      "2",
	},
}


def _section(answers: str, name: str) -> str:
	return answers[answers.index(f"-- {name}\n"):].split("\n-- ")[0]


def _line(section: str, name: str) -> str:
	return next(line.split()[1] for line in section.splitlines()
	            if line.startswith(name + " "))


@pytest.mark.skipif(not fourway.COMPLETE, reason="needs gcc, g++ and rustc")
def test_all_four_reach_it_alike(tmp_path: Path) -> None:
	schema = tmp_path / "unit.situ"
	schema.write_text(SCHEMA, encoding="ascii")
	command = fourway.build(tmp_path, schema)
	for name, cases in CASES.items():
		for message, verdict in cases.items():
			given = {backend: _section(fourway.answers(
			             argv, bytes.fromhex(message), tmp_path), name)
			         for backend, argv in command.items()}
			assert len(set(given.values())) == 1, (message, given)
			assert _line(next(iter(given.values())), "validate") == verdict, \
				(message, given)


@pytest.mark.skipif(COMPILER is None, reason="needs a C compiler")
@pytest.mark.parametrize("name", sorted(CASES))
def test_both_walkers_reach_it_alike(name: str, tmp_path: Path) -> None:
	image = _inline_image(SCHEMA)
	shape = shape_named_text(SCHEMA, name)
	for message, verdict in CASES[name].items():
		data = bytes.fromhex(message)
		assert python_verdict(image, data, shape) == verdict, message
		assert c_verdict(tmp_path, image, data, shape) == verdict, message


def test_a_publish_body_can_be_read(tmp_path: Path) -> None:
	"""MQTT's, over the bytes paho wrote, which no view reached."""
	root = Path(__file__).resolve().parents[2]
	subprocess.run([sys.executable, "-m", "situc", "build", "--target",
	                "python", "--out", str(tmp_path),
	                str(root / "example/mqtt/mqtt.situ")],
	               check=True, cwd=root, capture_output=True)
	sys.path[:0] = [str(tmp_path), str(root / "runtime/python")]
	try:
		mqtt = importlib.import_module("mqtt")
		data = bytearray.fromhex("3007" "0003612f62" "6869")
		view = mqtt.packet.at(mqtt.Message(data), 0, len(data))
		view.validate()
		body = view.body_publish
		assert bytes(body.topic) == b"a/b"
		assert bytes(body.payload) == b"hi"
	finally:
		del sys.path[:2]
		sys.modules.pop("mqtt", None)
