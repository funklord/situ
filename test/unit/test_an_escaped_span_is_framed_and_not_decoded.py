"""`[escape = "\\"]` frames a span and does not decode it.

The question `suggestion/netcfgd.md` asks of the attribute, in its words:
"the question I would ask of it is whether it frames or decodes". The
answer is framing, and nothing pinned it -- the existing coverage asserts
the scan's LENGTH, its terminated flag and `validate`, which a decoding
reader would satisfy too while handing back different bytes.

It matters to a consumer rather than to a grammar. netcfgd's hottest
member carries whole configuration files, its example config holds 374
double quotes, and `ncfg_json.c` decodes rather than frames: a numeric
escape including surrogate pairs, into UTF-8, in place. So the generated reader
and the hand-written one differ by exactly the part that has the CVEs in
it, which is a fact worth asserting rather than leaving for somebody to
discover from a span that looks wrong.

If situ ever decodes an escaped span, this test is the one that should
fail and be rewritten deliberately.
"""

from __future__ import annotations

import sys
from pathlib import Path

from situc.layout import solve
from situc.parser import parse_text
from situc.resolve import resolve
from situc.codegen.python.emit import generate

ROOT = Path(__file__).resolve().parents[2]

#: A quoted span whose delimiter a backslash makes inert, which is the
#: shape `example/json`'s `text.chars` carries.
QUOTED = (
	"target buffer;\n"
	"endian big;\n"
	"bit_order msb_first;\n"
	"\n"
	"struct text {\n"
	"\tu8 open [must_eq = 0x22];\n"
	"\tu8 content[] until '\"' max 4096 [escape = \"\\\\\"];\n"
	"}\n")


def _reader(tmp_path: Path) -> object:
	"""The generated Python module for `QUOTED`, imported."""
	schema   = parse_text(QUOTED)
	resolved = resolve(schema, solve(schema))
	(tmp_path / "quoted.py").write_text(
		generate(schema, resolved, "quoted").module, encoding="ascii")
	(tmp_path / "situ_runtime.py").write_text(
		(ROOT / "runtime" / "python" / "situ_runtime.py")
		.read_text(encoding="utf-8"), encoding="utf-8")

	sys.path.insert(0, str(tmp_path))
	try:
		import importlib
		import quoted		# type: ignore[import-not-found]
		return importlib.reload(quoted)
	finally:
		sys.path.remove(str(tmp_path))


def test_an_escaped_delimiter_does_not_end_the_span(tmp_path: Path) -> None:
	"""The framing half, which is what the attribute is for (26.299).

	Without it the scan stops at the first `\\"` and the member reads
	short -- 26.299 measured `{"a":"say \\"hi\\""}` at extent 13 of 18
	before the fix. A short extent with no complaint is the answer this
	repository rates worst, so the control is the length.
	"""
	module  = _reader(tmp_path)
	runtime = sys.modules["situ_runtime"]

	# `"a\"b"` -- six bytes, the middle quote escaped.
	buf  = bytearray(b'"a\\"b"')
	view = module.text.at(runtime.Message(buf), 0, len(buf))	# type: ignore[attr-defined]

	assert bytes(view.content) == b'a\\"b', \
		"the span stops at the escaped delimiter"
	assert len(bytes(view.content)) == 4


def test_the_span_comes_back_escaped(tmp_path: Path) -> None:
	"""The answer to netcfgd's question, asserted on the bytes.

	`a\\"b` is four bytes framed and three bytes decoded, so the two
	readings are distinguishable by length alone here -- which is the
	property that makes this checkable rather than a matter of reading
	the emitter.

	`\\\\` is the second case and the one that separates framing from a
	half-decode: a reader that dropped escape bytes would return `a"b`
	for the first and `a\\b` for this, and a framing reader returns both
	spans whole.
	"""
	module  = _reader(tmp_path)
	runtime = sys.modules["situ_runtime"]

	for raw, framed in ((b'"a\\"b"', b'a\\"b'),
			    (b'"a\\\\b"', b'a\\\\b'),
			    (b'"say \\"hi\\""', b'say \\"hi\\"')):
		buf  = bytearray(raw)
		view = module.text.at(runtime.Message(buf), 0, len(buf))	# type: ignore[attr-defined]
		got  = bytes(view.content)

		assert got == framed, f"{raw!r} framed as {got!r}"
		assert b"\\" in got, \
			"an escape byte is missing, so something decoded"
