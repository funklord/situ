"""A field of a nested struct at a dynamic offset is read where it is (26.916).

`u8 tail[h.len]` with `h` behind a variable member took its length from the
child's own offset, as if the child began the message. The layout says so:
such a member is frame-relative (section 12.2). All four backends read the
number as absolute and agreed, so comparing them found nothing, and the wrong
bytes sized a run.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import fourway
from situc.diagnostics import SituError
from situc.cli import analyse
from situc.codegen.python import generate
from situc.parser import parse

SCHEMA = """target buffer;
endian big;
struct hdr { u8 kind; u16 len; }
struct behind { u8 n; u8 d[n]; hdr h; u8 tail[h.len]; u8 last; }
struct twice { u8 n; u8 d[n]; hdr h; u8 m; u8 e[m]; hdr g;
               u8 tail[h.len + g.len]; }
struct ruled { u8 n; u8 d[n]; hdr h; }
struct picked { u8 n; u8 d[n]; hdr h;
                variant v switch (h.kind) { case 1: u8 a; default: u16 b; } }
when ruled.h.len != ruled.n refuse mismatch "the rule reads where it is";
"""

#: struct -> message -> what `validate` says: 0 whole, 1 short, 2 a rule.
CASES = {
	"behind": {
		"01" "aa" "07" "0002" "bbcc" "dd": "0",
		"00"      "07" "0001" "bb"   "dd": "0",		# no bytes in front
		"03" "aabbcc" "07" "0000"    "dd": "0",
		"01" "aa" "07" "0002" "bbcc":      "1",		# `last` is not there
		"01" "aa" "07" "0009" "bbcc" "dd": "1",		# tail past the frame
		"05" "aabbcc" "07" "00":           "1",		# `h` past the frame
	},
	"twice": {
		"01" "aa" "07" "0001" "00" "07" "0001" "bbcc": "0",
		"01" "aa" "07" "0001" "01" "ee" "07" "0002" "bbccdd": "0",
		"01" "aa" "07" "0001" "00" "07" "0002" "bbcc": "1",
	},
	"ruled": {
		"01" "aa" "07" "0001": "0",
		"01" "aa" "07" "0002": "2",
		"02" "aabb" "07" "0002": "0",
	},
	"picked": {
		"01" "aa" "01" "0000" "55":   "0",
		"01" "aa" "02" "0000" "5566": "0",
		"01" "aa" "02" "0000" "55":   "1",
	},
}


def _section(answers: str, struct: str) -> str:
	return answers[answers.index(f"-- {struct}\n"):].split("\n-- ")[0]


def _line(section: str, name: str) -> str:
	return next(line.split()[1] for line in section.splitlines()
	            if line.startswith(name + " "))


@pytest.mark.skipif(not fourway.COMPLETE, reason = "needs gcc, g++ and rustc")
def test_all_four_read_it_where_it_is(tmp_path: Path) -> None:
	schema = tmp_path / "unit.situ"
	schema.write_text(SCHEMA, encoding = "ascii")
	command = fourway.build(tmp_path, schema)
	for struct, cases in CASES.items():
		for message, verdict in cases.items():
			given = {backend: _section(fourway.answers(
			             argv, bytes.fromhex(message), tmp_path), struct)
			         for backend, argv in command.items()}
			assert len(set(given.values())) == 1, (struct, message, given)
			assert _line(given["c"], "validate") == verdict, \
				(struct, message, given["c"])


BITS = """target buffer;
endian big;
bit_order {order};
struct f {{ u4 hi; u4 lo; }}
struct cross [allow_straddle] {{ u6 a; u4 b; u6 c; }}
struct nib {{ u8 n; u8 d[n]; f h; u8 t[h.lo]; u8 z; }}
struct over {{ u8 n; u8 d[n]; cross h; u8 t[h.b]; u8 z; }}
"""

#: order -> struct -> message -> `validate`. `lo` is a nibble, and `b` crosses
#: from the first byte to the second, the shape bit code gets wrong.
BIT_CASES = {
	"msb_first": {
		"nib": {"01" "aa" "72" "bbcc" "dd": "0",
		        "01" "aa" "72" "bb":       "1"},
		# a = 000000, b = 0011, c = 000000: 0x00 0xC0.
		"over": {"01" "aa" "00c0" "bbccdd" "ee": "0",
		         "01" "aa" "00c0" "bbcc":        "1"},
	},
	"lsb_first": {
		"nib": {"01" "aa" "27" "bbcc" "dd": "0",
		        "01" "aa" "27" "bb":       "1"},
		# Bit 0 is the low bit: a = bits 0-5, b = bits 6-9, so 0xC0 0x00
		# gives b = 3.
		"over": {"01" "aa" "c000" "bbccdd" "ee": "0",
		         "01" "aa" "c000" "bbcc":        "1"},
	},
}


@pytest.mark.skipif(not fourway.COMPLETE, reason = "needs gcc, g++ and rustc")
@pytest.mark.parametrize("order", sorted(BIT_CASES))
def test_a_bit_field_behind_a_length(order: str, tmp_path: Path) -> None:
	"""Read at the struct, in either bit order, across a byte as well."""
	schema = tmp_path / "unit.situ"
	schema.write_text(BITS.format(order = order), encoding = "ascii")
	command = fourway.build(tmp_path, schema)
	for struct, cases in BIT_CASES[order].items():
		for message, verdict in cases.items():
			given = {backend: _section(fourway.answers(
			             argv, bytes.fromhex(message), tmp_path), struct)
			         for backend, argv in command.items()}
			assert len(set(given.values())) == 1, (struct, message, given)
			assert _line(given["c"], "validate") == verdict, \
				(struct, message, given["c"])


@pytest.mark.parametrize("text", [
	# A text number.
	"struct f { decimal u32 v[4]; }\n"
	"struct s { u8 n; u8 d[n]; f h; u8 t[h.v]; }\n",
], ids = ["text-number"])
def test_what_has_no_read_yet_is_refused(text: str, tmp_path: Path) -> None:
	"""Each read the wrong bytes without a word, in every backend."""
	path = tmp_path / "unit.situ"
	path.write_text("target buffer;\nendian big;\nbit_order msb_first;\n"
	                "struct hdr { u8 kind; u16 len; }\n" + text,
	                encoding = "ascii")
	source, resolved, _ = analyse(path)
	with pytest.raises(SituError) as caught:
		generate(parse(source), resolved, "unit")
	assert "is at an offset the message decides" in str(caught.value)
