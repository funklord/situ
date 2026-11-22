"""A varint cut short ahead of a descent is no level of nesting (26.1135).

A lone CBOR `d9` is a tag whose head wants two more bytes. Its `_len`
answers zero, so `content` is placed at the tag's own start -- the same
byte, read as a tag again -- and the nesting probe descended there to its
cap. All four backends refused one byte as "nests deeper than the format
allows" (CONSTRAINT) where the walker said BOUNDS: a receiver telling a
truncated message from a malformed one would have dropped a message that
had not finished arriving. Found replaying the atheris corpus through the
walker; the four-way replay could not see it, since all four agreed.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "test" / "unit"))

import pytest						# noqa: E402

import fourway						# noqa: E402
from every_schema import load_schema			# noqa: E402
from situc import pack as packer			# noqa: E402
from situc.layout import solve				# noqa: E402
from situc.resolve import resolve			# noqa: E402
from walker import report				# noqa: E402
from walker.image import load				# noqa: E402

SCHEMA = ROOT / "example" / "cbor" / "cbor.situ"

#: A tag cut inside its head, and an array and a map cut inside their
#: counts: each a frame too short, none a nesting.
CUT = ["d9", "d9ff", "99", "9a0000", "b9", "bb00"]

#: A tag of a tag of ... of zero, 70 deep: still too deep.
DEEP = "c1" * 70 + "00"


def _verdict(text: str, struct: str) -> str:
	lines = text.splitlines()
	at = lines.index(f"-- {struct}")
	return next(line for line in lines[at + 1:]
	            if line.startswith(("validate", "-- ")))


def _walked(data: bytes) -> str:
	parsed = load_schema(SCHEMA)
	resolved = resolve(parsed, solve(parsed))
	image = load(packer.pack(parsed, resolved, metadata=True)[0])
	return report.listing(image, data)


@pytest.mark.skipif(not fourway.COMPLETE, reason="needs all four toolchains")
def test_a_cut_head_is_too_short_not_too_deep(tmp_path: Path) -> None:
	command = fourway.build(tmp_path, SCHEMA)
	for message in CUT:
		data = bytes.fromhex(message)
		struct = {0xc0: "tagged", 0x80: "array", 0xa0: "map"}[data[0] & 0xe0]
		said = {name: _verdict(fourway.answers(argv, data, tmp_path), struct)
		        for name, argv in command.items()}
		said["walker"] = _verdict(_walked(data), struct)
		assert set(said.values()) == {"validate 1"}, (message, said)


@pytest.mark.skipif(not fourway.COMPLETE, reason="needs all four toolchains")
def test_a_deep_chain_is_still_too_deep(tmp_path: Path) -> None:
	command = fourway.build(tmp_path, SCHEMA)
	data = bytes.fromhex(DEEP)
	for name, argv in command.items():
		assert _verdict(fourway.answers(argv, data, tmp_path), "tagged") \
			== "validate 2", name
