"""A WebSocket payload length is a marked integer beside a flag (26.1181).

RFC 6455 section 5.2: the second byte of a frame is the mask bit and seven
bits of length. Below 126 they are the length; 126 says a 16-bit length
follows and 127 a 64-bit one, high byte first, whose top bit MUST be 0; and
"the minimal number of bytes MUST be used". A marked form's `max_bits`
bounds its bytes, not its bits, so the top bit is held by the payload it
measures: a length past 2^63 is past any frame. That is CBOR's head with
another table (`ast.MARKED`), so `encoding = websocket` is one row in each
reader's copy of it.

The values below are worked by hand from the RFC, not read from any reader,
since four readers holding one wrong table agree with each other. The flag
is set in half of them: it shares the byte, and a reader that took the
whole byte would read 128 more.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import fourway  # noqa: E402
from test_a_marked_integer_says_its_own_width import _all, _built  # noqa: E402
from test_nesting_costs_no_more_than_its_length import pack_text  # noqa: E402
from test_walker_c import (VERDICT, _build_driver, c_verdict,  # noqa: E402
                           python_verdict, shape_named_text)

SCHEMA = """target buffer;
endian big;
bit_order msb_first;
varint_type ws  { encoding = websocket; max_bits = 64; minimal; }
varint_type wsp { encoding = websocket; max_bits = 64; }
struct a_ws  { bit flag; ws v; u8 last; }
struct a_wsp { bit flag; wsp v; u8 last; }
struct a_frame { bit flag; ws v; u8 payload[v]; }
"""

#: Hex -> (bytes used, value, flag).
SAMPLES = {
	"05":                 (1, 5, 0),
	"fd":                 (1, 125, 1),
	"7e007e":             (3, 126, 0),
	"fe00c8":             (3, 200, 1),
	"7effff":             (3, 65535, 0),
	"ff0000000000010000": (9, 65536, 1),
	"7f7fffffffffffffff": (9, (1 << 63) - 1, 0),
}

#: What `minimal` refuses: a length the shorter form holds, read as zero.
PADDED = ["7e0005", "fe007d", "7f000000000000ffff", "ff0000000000000005"]

#: The RFC's top bit set: read as the number it is, and a frame measured by it
#: refused, its payload being past any frame.
TOP_BIT = "7f8000000000000000"


@pytest.mark.skipif(not fourway.COMPLETE, reason = "needs gcc, g++ and rustc")
def test_all_four_read_each_form_and_keep_the_flag(tmp_path: Path) -> None:
	command = _built(tmp_path, SCHEMA)
	for sample, (size, value, flag) in SAMPLES.items():
		message = bytes.fromhex(sample) + b"\x07"
		assert _all(command, message, "a_ws", "v", tmp_path) \
			== f"len={size} value={value}", sample
		assert _all(command, message, "a_ws", "flag", tmp_path) == str(flag)
		assert _all(command, message, "a_ws", "last", tmp_path) == "7"
	for sample in PADDED:
		message = bytes.fromhex(sample) + b"\x07"
		assert _all(command, message, "a_ws", "v", tmp_path).endswith(
			"value=0"), sample
		assert _all(command, message, "a_wsp", "v", tmp_path) \
			!= _all(command, message, "a_ws", "v", tmp_path), sample
	message = bytes.fromhex(TOP_BIT) + b"\x07"
	assert _all(command, message, "a_ws", "v", tmp_path) \
		== f"len=9 value={1 << 63}"
	assert _all(command, message, "a_frame", "validate", tmp_path) == "1"
	assert _all(command, bytes.fromhex("8568656c6c6f"), "a_frame", "validate",
	            tmp_path) == "0"


def test_both_walkers_measure_what_the_backends_do(tmp_path: Path) -> None:
	blob  = pack_text(SCHEMA)
	drive = _build_driver(tmp_path, blob, VERDICT)
	asked: dict[str, str] = {}
	for sample in SAMPLES:
		asked[sample + "07"] = "0"
		asked[sample] = "1"				# the byte after it is missing
		if len(sample) > 2:
			asked[sample[:-2] + ""] = "1"		# ends inside the length
	shape = shape_named_text(SCHEMA, "a_ws")
	for sample, verdict in asked.items():
		data = bytes.fromhex(sample)
		assert c_verdict(tmp_path, blob, data, shape, drive) == verdict, sample
		assert python_verdict(blob, data, shape) == verdict, sample
	framed = shape_named_text(SCHEMA, "a_frame")
	for sample, verdict in ((TOP_BIT + "07", "1"), ("8568656c6c6f", "0"),
	                        ("8568656c6c", "1")):
		data = bytes.fromhex(sample)
		assert c_verdict(tmp_path, blob, data, framed, drive) == verdict, sample
		assert python_verdict(blob, data, framed) == verdict, sample
