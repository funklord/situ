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
def test_an_http2_payload_is_held_to_its_reader(tmp_path: Path) -> None:
	"""The payloads inside the frame, built on their own: a run sized by
	`max` over the frame's length and a nested part handed arguments
	were refused, having no field to solve for and no `_required` to
	ask. Each is held to the extent its reader states instead."""
	units = _units(HTTP2)
	cases = [
		# One pad-length byte, the field block, two bytes of padding.
		_case(units["padded_headers."], length=5, priority=0,
		      headers=b"hi", padding=bytes(2)),
		# Told six, the reader takes three bytes of field block and is a
		# byte short for the padding.
		_case(units["padded_headers."], length=6, priority=0,
		      headers=b"hi", padding=bytes(2)),
		_case(units["goaway_payload."], length=10, last_stream_id=1,
		      debug=b"xy"),
		_case(units["goaway_payload."], length=11, last_stream_id=1,
		      debug=b"xy"),
		_case(units["padded_data."], length=5, data=b"hi",
		      padding=bytes(2)),
		_case(units["padded_data."], length=4, data=b"hi",
		      padding=bytes(2)),
		# A run of entries `length / 6` counts.
		_case(units["settings_payload."], length=6, entries=SETTING),
		_case(units["settings_payload."], length=12, entries=SETTING),
	]
	assert _said(HTTP2, tmp_path, cases) == [
		"02" + b"hi".hex() + "0000",
		"refused",
		"00000001" + "00000000" + b"xy".hex(),
		"refused",
		"02" + b"hi".hex() + "0000",
		"refused",
		SETTING.hex(),
		"refused",
	]


@pytest.mark.skipif(not fourway.COMPLETE, reason="needs gcc, g++ and rustc")
def test_a_dns_label_is_as_long_as_its_text(tmp_path: Path) -> None:
	label = _units(DNS)["label.text"]
	cases = [_case(label, text=b"example"), _case(label, text=b"a" * 63),
	         _case(label, text=b"a" * 64)]
	assert _said(DNS, tmp_path, cases) == [
		"07" + b"example".hex(), "3f" + "61" * 63, "refused"]


DELIMITED_ARM = """target buffer;
endian big;
struct delimited_arm {
	u8 pick;
	variant held switch (pick) {
		case 1:  u8  line[] until ",";
		case 2:  u8  fixed[3];
		default: error;
	}
	u8  after;
}
"""


@pytest.mark.skipif(not fourway.COMPLETE, reason="needs gcc, g++ and rustc")
def test_a_delimited_arm_is_written_with_its_delimiter(tmp_path: Path) -> None:
	"""`u8 line[] until ","` in an arm was one byte too, with no
	delimiter after it; it is written as a member's run is, and content
	holding the delimiter is refused."""
	schema = tmp_path / "delimited_arm.situ"
	schema.write_text(DELIMITED_ARM, encoding="ascii")
	line = _units(schema)["delimited_arm.line"]
	cases = [_case(line, line=b"ab", after=7), _case(line, line=b"",
	                                                 after=7),
	         _case(line, line=b"a,b", after=7)]
	assert _said(schema, tmp_path, cases) == [
		"016162" + "2c" + "07", "01" + "2c" + "07", "refused"]


@pytest.mark.skipif(not fourway.COMPLETE, reason="needs gcc, g++ and rustc")
def test_a_run_of_boxes_to_the_end_is_built(tmp_path: Path) -> None:
	"""`box boxes[remaining]`, an MP4 file: no `_required` splits a box,
	whose last may run to the end, so the run was refused. It ends the
	message, and the reader walks it over every byte, so the caller's
	bytes are taken where the closing validation takes them."""
	schema = ROOT / "example" / "isobmff" / "isobmff.situ"
	file = _units(schema)["file."]
	ftyp = bytes.fromhex("00000010" "66747970" "69736f6d" "00000200")
	free = bytes.fromhex("00000008" "66726565")
	cases = [_case(file, boxes=ftyp + free), _case(file, boxes=ftyp[:12])]
	assert _said(schema, tmp_path, cases) == [(ftyp + free).hex(),
	                                          "refused"]
