"""A run of bytes holds a struct, framed by exactly those bytes (26.898).

    u8 exts[total] [holds = ext_list];

Nothing else said "these records take N bytes": a count counts elements,
and `[remaining]` is the frame's. TLS extensions, BGP path attributes and
records followed by a CRC are all this shape.
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
from situc.diagnostics import SituError
from situc.layout import solve
from situc.parser import parse_text

SCHEMA = """target buffer;
endian big;
struct ext { u16 kind [max = 100]; u16 len; u8 data[len]; }
struct ext_list { ext all[remaining]; }
struct hello { u16 total; u8 exts[total] [holds = ext_list]; u8 after; }
struct guarded { u8 body[remaining] [holds = ext_list]; u16 crc; }
struct covered { authenticated { u16 total; u8 exts[total] [holds = ext_list];
                                 u8 after; }
                 tag u8[2] covers(authenticated); }
"""

#: struct -> message -> what `validate` says: 0 whole, 1 short, 2 a rule.
CASES = {
	"hello": {
		"0009" "00010001aa" "00020000" "07": "0",
		"0000" "07":                         "0",	# an empty list
		"0009" "ff010001aa" "00020000" "07": "2",	# a rule inside it
		"0008" "00010001aa" "000200" "07":   "1",	# element cut by the run
		"0009" "00010001aa" "00020000":      "1",	# `after` past the frame
		"0005" "00010002aa" "07":            "1",	# claims past the run
	},
	"guarded": {
		"00010001aa" "00020000" "beef": "0",
		"beef":                         "0",		# no records, the CRC
		"ff010001aa" "00020000" "beef": "2",
		"00010001aa" "0002" "beef":     "1",		# the CRC is not a record
	},
	# A transparent block keeps its members the struct's own (26.913).
	"covered": {
		"0009" "00010001aa" "00020000" "07" "beef": "0",
		"0009" "ff010001aa" "00020000" "07" "beef": "2",	# a rule inside it
		"0008" "00010001aa" "000200" "07" "beef":   "1",	# cut by the run
		"0005" "00010002aa" "07" "beef":            "1",	# past the run
	},
}


def _section(answers: str, struct: str) -> str:
	return answers[answers.index(f"-- {struct}\n"):].split("\n-- ")[0]


def _line(section: str, name: str) -> str:
	return next(line.split()[1] for line in section.splitlines()
	            if line.startswith(name + " "))


@pytest.mark.skipif(not fourway.COMPLETE, reason="needs gcc, g++ and rustc")
def test_all_four_read_what_the_run_holds(tmp_path: Path) -> None:
	schema = tmp_path / "unit.situ"
	schema.write_text(SCHEMA, encoding="ascii")
	command = fourway.build(tmp_path, schema)
	for struct, cases in CASES.items():
		for message, verdict in cases.items():
			given = {backend: _section(fourway.answers(
			             argv, bytes.fromhex(message), tmp_path), struct)
			         for backend, argv in command.items()}
			assert len(set(given.values())) == 1, (message, given)
			assert _line(next(iter(given.values())), "validate") == verdict, \
				(struct, message, given)


@pytest.mark.skipif(COMPILER is None, reason="needs a C compiler")
@pytest.mark.parametrize("struct", sorted(CASES))
def test_both_walkers_descend_into_the_run(struct: str,
		tmp_path: Path) -> None:
	for message, verdict in CASES[struct].items():
		data  = bytes.fromhex(message)
		image = _inline_image(SCHEMA)
		shape = shape_named_text(SCHEMA, struct)
		assert python_verdict(image, data, shape) == verdict, message
		assert c_verdict(tmp_path, image, data, shape) == verdict, message


@pytest.mark.parametrize("text, wanted", [
	("struct a { u8 x; } struct b { u8 n [holds = a]; }", "means nothing here"),
	("struct b { u8 n; u8 d[n] [holds = nothing]; }", "names a struct"),
	("struct a { parameter u8 k [stream]; u8 d[k]; }\n"
	 "struct b { u8 n; u8 d[n] [holds = a]; }", "takes arguments"),
	("struct a { u8 n; u8 d[n] [holds = a]; }", "contains itself"),
	("struct a { u8 k; u8 d[k] [holds = c]; }\n"
	 "struct c { a inner; }", "is recursive"),
	("struct a { u8 x; }\n"
	 "struct b { u8 k; variant v switch (k) { case 1: u8 d[2] [holds = a];"
	 " default: error; } }", "member of the struct itself"),
])
def test_what_a_run_cannot_hold(text: str, wanted: str) -> None:
	with pytest.raises(SituError) as caught:
		solve(parse_text(f"target buffer;\nendian big;\n{text}\n"))
	assert wanted in str(caught.value)


def test_a_trailer_after_the_run_is_read_from_the_frame_end() -> None:
	"""The tail is 26.894's: nothing here measures the records."""
	layout = solve(parse_text(SCHEMA))
	run = next(held for held in layout.structs["guarded"].placements
	           if held.name == "body")
	assert run.tail_bytes == 2


#: The extension types tshark 4 listed for each vector in
#: `example/tls/tls.vectors`, in order.
TSHARK = {
	"tls13_with_name": [65281, 0, 11, 10, 35, 22, 23, 13, 43, 45, 51, 27],
	"tls12_only":      [65281, 0, 11, 10, 35, 22, 23, 13],
	"tls12_by_address": [65281, 11, 10, 35, 22, 23, 13],
}


def test_a_client_hello_reads_as_tshark_reads_it(tmp_path: Path) -> None:
	"""Three layers of `[holds]`, over bytes OpenSSL wrote."""
	root = Path(__file__).resolve().parents[2]
	subprocess.run([sys.executable, "-m", "situc", "build", "--target",
	                "python", "--out", str(tmp_path),
	                str(root / "example/tls/tls.situ")],
	               check=True, cwd=root, capture_output=True)
	sys.path[:0] = [str(tmp_path), str(root / "runtime/python")]
	try:
		tls = importlib.import_module("tls")
		seen = {}
		for line in (root / "example/tls/tls.vectors").read_text().splitlines():
			if line.startswith("#"):
				continue
			_, name, text = line.split()
			data  = bytearray.fromhex(text)
			view  = tls.record.at(tls.Message(data), 0, len(data))
			view.validate()
			held = view.fragment_view.body_view.extensions_view
			seen[name] = [held.all(at).kind for at in range(held.all_count)]
	finally:
		del sys.path[:2]
		sys.modules.pop("tls", None)
	assert seen == TSHARK



#: A container format: a box's bytes hold more boxes, as ISO-BMFF, RIFF, EBML
#: and BER all do. A turn is two structs, and the innermost box's empty list
#: is an edge too, so four levels of `box` are seven.
TREE = """target buffer;
endian big;
struct box [depth = 8] { u8 kind [max = 9]; u8 len; u8 body[len] [holds = boxes]; }
struct boxes [depth = 8] { box all[remaining]; }
"""


def _nest(levels: int, kind: int = 1) -> bytes:
	message = b""
	for _ in range(levels):
		message = bytes([kind, len(message)]) + message
	return message


TREE_CASES = {
	_nest(1):                                       "0",
	_nest(4):                                       "0",
	_nest(3, 200):                                  "2",
	bytes([1, 4, 1, 0, 1, 0]):                      "0",	# two children
	bytes([1, 3, 1, 9, 0]):                         "1",	# a child past its frame
	_nest(5):                                       "2",	# deeper than `[depth]`
}


@pytest.mark.skipif(not fourway.COMPLETE, reason="needs gcc, g++ and rustc")
def test_a_box_holding_boxes_is_bounded_by_its_depth(tmp_path: Path) -> None:
	schema = tmp_path / "unit.situ"
	schema.write_text(TREE, encoding="ascii")
	command = fourway.build(tmp_path, schema)
	for message, verdict in TREE_CASES.items():
		given = {backend: _section(fourway.answers(argv, message, tmp_path),
		                           "box")
		         for backend, argv in command.items()}
		assert len(set(given.values())) == 1, (message.hex(), given)
		assert _line(next(iter(given.values())), "validate") == verdict, \
			(message.hex(), given)


@pytest.mark.skipif(COMPILER is None, reason="needs a C compiler")
def test_both_walkers_stop_at_the_ceiling(tmp_path: Path) -> None:
	image = _inline_image(TREE)
	shape = shape_named_text(TREE, "box")
	for message, verdict in TREE_CASES.items():
		# Past the ceiling a walker says it stopped, as for any cycle.
		wanted = "cannot-say" if message == _nest(5) else verdict
		assert python_verdict(image, message, shape) == wanted, message.hex()
		assert c_verdict(tmp_path, image, message, shape) == wanted, \
			message.hex()


def test_the_refusal_of_records_before_a_tail_names_the_answer() -> None:
	with pytest.raises(SituError) as refused:
		solve(parse_text("target buffer;\nendian big;\n"
		                 "struct r { u8 a; }\n"
		                 "struct s { r all[remaining]; u16 crc; }\n"))
	assert any("[holds = T]" in note for note in refused.value.diagnostic.notes)

