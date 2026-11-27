"""A builder holds an arm to what its reader reads (26.1189).

Three ways a per-arm builder wrote a message its own reader reads
otherwise, each found in HTTP/2's frame and DNS's label:

- An arm only the whole message measures. SETTINGS is `length / 6`
  entries and GOAWAY `max(length, 8) - 8` bytes of debug data: the frame
  hands its `length` field down, and `_validate` takes bytes past what the
  arm reads as trailing. These builders were declined (26.1182); the
  message is now held to the extent its reader states.
- An argument that chooses the arm. `header_block` switches on the
  `priority` flag its frame hands it, and a builder per arm writes the arm
  it was called for: handed the other arm's value, it wrote bytes the
  reader read as that other arm.
- A byte run in an arm, as long as a field, a parameter or an expression
  says. It was written as ONE byte, so DNS's `u8 text[rest]` built only
  one-letter labels; `rest` is now computed from the label, as any size
  field is, and the rest are held to the reader's extent.

All four builders are handed the same values and must agree on each line,
and the bytes are pinned here, since four builders refusing alike agree.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "test" / "unit"))

import fourway						# noqa: E402
from every_schema import load_schema			# noqa: E402
from test_the_four_builders_agree import (Given, _flat, _given,	# noqa: E402
                                          four_say)

from situc.codegen import plain_build			# noqa: E402
from situc.codegen.plain_build import Unit		# noqa: E402
from situc.layout import solve				# noqa: E402
from situc.resolve import resolve			# noqa: E402

HTTP2 = ROOT / "example" / "http2" / "http2.situ"
DNS   = ROOT / "example" / "dnsname" / "dnsname.situ"

SETTING = bytes.fromhex("000300000064")		# MAX_CONCURRENT_STREAMS 100
GOAWAY  = bytes.fromhex("0000000100000000") + b"xy"


def _units(schema: Path) -> dict[str, Unit]:
	parsed   = load_schema(schema)
	resolved = resolve(parsed, solve(parsed))
	return {f"{unit.struct.name}.{unit.suffix}": unit
	        for unit in plain_build.units(
	            resolved, plain_build.shape(parsed, resolved))}


def _case(unit: Unit, **named: int | bytes) -> tuple[Unit, Given]:
	"""Every part the caller hands a value, by its member's name; a number
	not named is zero, so a flag bit says nothing it was not asked to."""
	given: Given = []
	for part, value in _given(_flat(unit), {}):
		name = part.placement.name or ""
		given.append((part, named.get(name, 0 if isinstance(value, int)
		                              else value)))
	return unit, given


def _said(schema: Path, tmp_path: Path,
		cases: list[tuple[Unit, Given]]) -> list[str]:
	out = tmp_path / schema.stem
	out.mkdir()
	return [line.split()[-1]
	        for line in four_say(schema, out, [], cases).splitlines()]


@pytest.mark.skipif(not fourway.COMPLETE, reason="needs gcc, g++ and rustc")
def test_an_http2_frame_is_held_to_its_reader(tmp_path: Path) -> None:
	units = _units(HTTP2)
	header = "{:06x}{:02x}00{:08x}"
	cases = [
		_case(units["frame.settings"], length=6, settings=SETTING),
		# One byte under `length = 3`: the reader takes no entry, and
		# the byte belongs to no frame.
		_case(units["frame.settings"], length=3, settings=b"\x07"),
		# Seven: a FRAME_SIZE_ERROR, the schema's rule (RFC 9113 6.5).
		_case(units["frame.settings"], length=7, settings=SETTING),
		_case(units["frame.goaway"], length=10, goaway=GOAWAY),
		_case(units["frame.goaway"], length=9, goaway=GOAWAY),
		# The default arm's run: `length` is computed from it.
		_case(units["frame.other"], type=9, stream_id=3, other=b"hi"),
		_case(units["header_block.block"], length=2, priority=0,
		      block=b"hi"),
		# The flag names the prioritized arm, so a reader told it reads
		# five priority bytes that are not there.
		_case(units["header_block.block"], length=2, priority=1,
		      block=b"hi"),
		_case(units["header_block.prioritized"], length=7, priority=1,
		      prioritized=bytes.fromhex("0000000110") + b"hi"),
		_case(units["header_block.prioritized"], length=7, priority=0,
		      prioritized=bytes.fromhex("0000000110") + b"hi"),
		# Two bytes under `length = 3`.
		_case(units["data_payload.bytes"], length=3, padded=0,
		      bytes=b"hi"),
	]
	assert _said(HTTP2, tmp_path, cases) == [
		header.format(6, 4, 0) + SETTING.hex(),
		"refused",
		"refused",
		header.format(10, 7, 0) + GOAWAY.hex(),
		"refused",
		header.format(2, 9, 3) + b"hi".hex(),
		b"hi".hex(),
		"refused",
		"00000001106869",
		"refused",
		"refused",
	]


@pytest.mark.skipif(not fourway.COMPLETE, reason="needs gcc, g++ and rustc")
def test_a_dns_label_is_as_long_as_its_text(tmp_path: Path) -> None:
	label = _units(DNS)["label.text"]
	cases = [_case(label, text=b"example"), _case(label, text=b"a" * 63),
	         _case(label, text=b"a" * 64)]
	assert _said(DNS, tmp_path, cases) == [
		"07" + b"example".hex(), "3f" + "61" * 63, "refused"]
