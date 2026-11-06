"""A header that ends in its body's length (26.920).

`u8 tail[h.n]` with `n` a varint of a nested `h` was out of scope, so a QUIC or
MQTT header could not be a struct of its own. The read goes through `h`'s own
view, so where `n` stands inside `h` does not matter.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import fourway
from test_nesting_costs_no_more_than_its_length import pack_text
from test_walker_c import (VERDICT, _build_driver, c_verdict, python_verdict,
                           shape_named_text)

SCHEMA = """target buffer;
endian big;
varint_type q { encoding = quic; max_bits = 62; }
varint_type l { encoding = leb128; max_bits = 32; }
varint_type z { encoding = leb128; transform = zigzag; max_bits = 32; }
struct head { u8 kind; q n; }
struct long_head { u8 m; u8 d[m]; l first; q n; }
struct signed_head { z n; }
struct plain { head h; u8 tail[h.n]; u8 last; }
struct behind { u8 k; u8 s[k]; head h; u8 tail[h.n]; u8 last; }
struct deep { u8 k; long_head h; u8 tail[h.n + 1]; u8 last; }
struct both { u8 k; u8 s[k]; head a; u8 x[a.n]; head b; u8 y[a.n + b.n]; }
struct ruled { head h; u8 v; }
struct digits { u8 kind; decimal u32 n[2]; }
struct text { u8 k; u8 s[k]; digits h; u8 tail[h.n]; u8 last; }
struct moved { u8 x; head h; u8 tail[h.n]; u8 last; }
struct negative { signed_head h; u8 last; }
when ruled.h.n != ruled.v refuse mismatch "the rule reads it";
when negative.h.n >= 0 refuse positive "the rule reads its sign";
"""

#: struct -> message -> what `validate` says: 0 whole, 1 short, 2 a rule.
CASES = {
	"plain": {
		"07" "02" "aabb" "cc":       "0",
		"07" "4002" "aabb" "cc":     "0",	# the padded spelling of 2
		"07" "00" "cc":              "0",
		"07" "02" "aabb":            "1",	# `last` is not there
		"07" "05" "aabb" "cc":       "1",	# the tail runs off the frame
		"07" "40":                   "1",	# the varint is cut short
	},
	"behind": {
		"01" "ee" "07" "02" "aabb" "cc":  "0",
		"03" "eeeeee" "07" "01" "aa" "cc": "0",
		"01" "ee" "07" "02" "aabb":       "1",
		"05" "ee" "07":                   "1",
	},
	"deep": {
		"00" "02" "eeee" "81" "01" "01" "aabb" "cc": "0",
		"00" "00" "05" "00" "aa" "cc":               "0",
		"00" "00" "05" "01" "aa" "cc":               "1",
	},
	"both": {
		"01" "ee" "07" "01" "aa" "07" "02" "bbccdd": "0",
		"00" "07" "00" "07" "03" "bbccdd":           "0",
		"01" "ee" "07" "01" "aa" "07" "02" "bbcc":   "1",
	},
	"ruled": {
		"07" "05" "05": "0",
		"07" "05" "06": "2",
	},
	# 3032 is "02": two bytes, which 26.916 measured reading as nothing.
	"text": {
		"01" "ee" "07" "3032" "aabb" "cc": "0",
		"00" "07" "3030" "cc":             "0",
		"01" "ee" "07" "3032" "aabb":      "1",
		"01" "ee" "07" "3039" "aabb" "cc": "1",
	},
	"moved": {
		"ee" "07" "02" "aabb" "cc":   "0",
		"ee" "07" "4002" "aabb" "cc": "0",
		"ee" "07" "02" "aabb":        "1",
	},
	# zigzag: 4 is 2 and 3 is -2.
	"negative": {
		"03" "cc": "0",
		"04" "cc": "2",
	},
}


def _section(answers: str, struct: str) -> str:
	return answers[answers.index(f"-- {struct}\n"):].split("\n-- ")[0]


def _line(section: str, name: str) -> str:
	"""A frame below the struct's minimum has no view to ask, which is short."""
	if "no-view" in section:
		return "1"
	return next(line.split()[1] for line in section.splitlines()
	            if line.startswith(name + " "))


@pytest.mark.skipif(not fourway.COMPLETE, reason = "needs gcc, g++ and rustc")
def test_all_four_measure_by_it(tmp_path: Path) -> None:
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


def test_both_walkers_measure_by_it_or_say_they_cannot(tmp_path: Path) -> None:
	"""A walker's program reads a field by its offset, which behind a length
	is `h`'s to know, so it declines there as for any field of `h` (26.916).
	Declining is the only other answer it may give, and both give the same."""
	blob  = pack_text(SCHEMA)
	drive = _build_driver(tmp_path, blob, VERDICT)
	for struct, cases in CASES.items():
		shape = shape_named_text(SCHEMA, struct)
		for message, verdict in cases.items():
			data = bytes.fromhex(message)
			said = c_verdict(tmp_path, blob, data, shape, drive)
			assert said == python_verdict(blob, data, shape), (struct, message)
			wanted = {verdict} if struct in ("plain", "ruled", "moved", "negative") \
				else {verdict, "cannot-say"}
			assert said in wanted, (struct, message, said)
