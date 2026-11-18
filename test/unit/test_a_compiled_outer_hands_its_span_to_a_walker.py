"""A compiled outer struct hands a byte span to a walker (the seam).

The pattern the holder asked about: a situ-generated structure whose
dynamic part's shape is loaded at runtime. The outer is compiled; its body
is a run of bytes it bounds but does not read; a walker loads an image of
whatever inner schema applies and walks the span. Neither half knows the
other, and the seam is one byte span -- so this holds the seam, over random
frames:

- the span the compiled outer hands over is exactly where the walker places
  that member in the outer's own image, no byte lost and none extra;
- the walk of that span with the INNER image -- a separate schema, packed
  separately, as a program loading it at runtime would hold it -- answers
  as the inner schema compiled over the same bytes, in both walkers;
- and the two halves together answer as the whole message compiled in one
  piece, the body declared `[holds = record]`.
"""
from __future__ import annotations

import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "test" / "unit"))

import pytest							# noqa: E402

from situc.diagnostics import Source				# noqa: E402
from situc.layout import solve					# noqa: E402
from situc.parser import parse					# noqa: E402
from situc.resolve import resolve				# noqa: E402
from situc.verify import _module				# noqa: E402
from test_walker_c import (COMPILER, VERDICT, _build_driver,	# noqa: E402
                           c_verdict, packed_text, python_verdict,
                           shape_named_text)
from walker.image import load					# noqa: E402
from walker.walk import Refused, acquire, offset_bits, size_bits	# noqa: E402

HEAD = "target buffer;\nendian big;\n"

#: The dynamic part's schema, which the program loads at runtime.
INNER = HEAD + """struct record {
	u8           n [max = 4];
	u8           items[n];
	decimal u16  code[remaining] [minimal];
}
"""

#: The compiled outer: the body is bytes it bounds and does not read.
OUTER = HEAD + """struct envelope {
	u8   kind [max = 3];
	u8   len;
	u8   body[len];
	u8   tail [must_eq = 0x7e];
}
"""

#: The whole message in one piece, for the third property.
WHOLE = INNER + """struct envelope {
	u8   kind [max = 3];
	u8   len;
	u8   body[len] [holds = record];
	u8   tail [must_eq = 0x7e];
}
"""


def _compiled(text: str, name: str) -> object:
	schema = parse(Source(f"{name}.situ", text))
	return _module(schema, resolve(schema, solve(schema)), name)


def _verdict(module: object, struct: str, data: bytes) -> str:
	"""`validate` as the drivers say it: 0, 1 bounds, 2 constraint."""
	runtime = sys.modules.get("situ_runtime")
	cls = getattr(module, struct)
	from_runtime = module.__situ_refusal__		# type: ignore[attr-defined]
	try:
		msg  = cls.at.__globals__["Message"](bytearray(data))
		view = cls.at(msg, 0, len(data))
		view.validate()
	except from_runtime as why:
		return "1" if type(why).__name__ in ("BoundsError",
		                                     "TruncatedError") else "2"
	del runtime
	return "0"


def _frames(rng: random.Random) -> list[bytes]:
	frames = []
	for _ in range(400):
		items = bytes(rng.randrange(256) for _ in range(rng.randrange(6)))
		digits = rng.choice([b"7", b"42", b"065", b"x", b"", b"60000",
		                     b"99999"])
		body = bytes([rng.choice([len(items), rng.randrange(8)])]) + items \
			+ digits
		length = rng.choice([len(body), len(body), rng.randrange(20)])
		frame = bytes([rng.randrange(5), length & 0xFF]) + body \
			+ bytes([rng.choice([0x7e, 0x7e, 0x00])])
		frames.append(frame[:rng.choice([len(frame), len(frame),
		                                 rng.randrange(len(frame) + 1)])])
	return frames


def test_the_span_and_the_walk_meet_at_the_seam(tmp_path: Path) -> None:
	outer  = _compiled(OUTER, "outer")
	inner  = _compiled(INNER, "inner")
	whole  = _compiled(WHOLE, "whole")
	o_blob = packed_text(OUTER)
	i_blob = packed_text(INNER)
	o_img, i_img = load(o_blob), load(i_blob)
	o_shape = shape_named_text(OUTER, "envelope")
	i_shape = shape_named_text(INNER, "record")
	body    = o_img.structs[o_shape].first_placement + 2
	drive   = (_build_driver(tmp_path, i_blob, VERDICT)
	           if COMPILER is not None else None)

	asked = joined = 0
	for frame in _frames(random.Random(20261118)):
		if _verdict(outer, "envelope", frame) != "0":
			continue
		# The compiled outer's span.
		msg  = outer.envelope.at.__globals__["Message"](bytearray(frame))	# type: ignore[attr-defined]
		view = outer.envelope.at(msg, 0, len(frame))			# type: ignore[attr-defined]
		start, held = view.body_offset, bytes(view.body)
		# ... is where the walker places the member in the outer's image.
		walked = acquire(o_img, frame, o_shape)
		assert (offset_bits(walked, body) // 8,
		        size_bits(walked, body) // 8) == (start, len(held)), frame.hex()

		# The span walked with the inner image, against the inner compiled.
		compiled = _verdict(inner, "record", held)
		assert python_verdict(i_blob, held, i_shape) == compiled, held.hex()
		if drive is not None:
			assert c_verdict(tmp_path, i_blob, held, i_shape, drive) \
				== compiled, held.hex()
		asked += 1

		# Both halves together, against the message compiled in one piece.
		assert _verdict(whole, "envelope", frame) == compiled, frame.hex()
		joined += 1

	assert asked > 100 and joined > 100, (asked, joined)
	try:
		acquire(i_img, b"", i_shape)
	except Refused:
		pass
