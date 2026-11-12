"""`validate` reaches a member placed by `at` (26.967).

    struct d { u16 a; u16 b [max = 9]; }
    struct t { u32 off; u16 w at off [max = 9]; d first at off; }

A located member's offset is the MESSAGE's, not the view's, and every
backend's `validate` got one wrong a different way: C and C++ did not
compile (`check` called an accessor that takes the message with the view
alone), Rust compared bytes at a fixed offset in its own frame, and
Python compared a `memoryview` with an integer. Both walkers were right:
they read the member where the message places it, refuse a constraint it
breaks, and refuse a member the message does not reach.

C reaches the message through the view's `owner` (26.306), C++ through
its raw view's, Python through `_msg`; a Rust struct holding a located
member takes the message in `validate`, as its located accessors already
do. Verdicts are worked out by hand: 0 OK, 1 BOUNDS, 2 CONSTRAINT.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import fourway
from test_walker_c import (COMPILER, c_verdict, packed_text, python_verdict,
                           shape_named_text)

SCHEMA = """target buffer;
endian big;
struct d {
	u16 a;
	u16 b [max = 9];
}
struct t {
	u32 off;
	u16 w     at off [max = 9];
	d   first at off;
}
"""

#: The message, and its verdict. `w` is `first.a`: both start at `off`.
CASES = {
	"both hold":            ("00000004" "0005" "0003", "0"),
	"w over its max":       ("00000004" "000a" "0003", "2"),
	"first.b over its max": ("00000004" "0005" "000c", "2"),
	"first runs off":       ("00000006" "0005", "1"),
	"off past the end":     ("00000010", "1"),
}


#: A struct the data sizes, placed by `at`: measured where the message
#: puts it. Its length helper was frame-relative and never emitted for a
#: located member, so three backends did not build; the C walker skipped
#: one the message did not hold and called the message valid.
SIZED = """target buffer;
endian big;
struct d {
	u16 count [max = 5];
	u8  e[count];
}
struct t {
	u32 off;
	d   first at off;
}
"""

SIZED_CASES = {
	"two bytes of e":          ("00000004" "0002" "aabb", "0"),
	"e cut short":             ("00000004" "0002" "aa", "1"),
	"count over its max, cut": ("00000004" "0009" "aabb", "1"),
	"count over its max":      ("00000004" "0006" "aabbccddeeff", "2"),
	"first at the very end":   ("00000004", "1"),
}


@pytest.mark.parametrize("schema_text, cases", [(SCHEMA, CASES),
                                                (SIZED, SIZED_CASES)],
                         ids=["fixed", "sized"])
def test_all_four_validate_what_at_places(tmp_path: Path, schema_text: str,
		cases: dict[str, tuple[str, str]]) -> None:
	schema = tmp_path / "unit.situ"
	schema.write_text(schema_text, encoding="ascii")
	command = fourway.build(tmp_path, schema)
	for label, (packet, verdict) in cases.items():
		given = {name: fourway.answers(argv, bytes.fromhex(packet), tmp_path)
		         for name, argv in command.items()}
		first = next(iter(given.values()))
		assert all(answer == first for answer in given.values()), \
			(label, given)
		# The last `validate` line is `t`'s: the driver validates each
		# struct over the whole buffer, `d` first.
		said = [one for one in first.splitlines()
		        if one.startswith("validate ")]
		assert said[-1] == f"validate {verdict}", (label, first)


@pytest.mark.skipif(COMPILER is None, reason="no C compiler")
@pytest.mark.parametrize("schema_text, cases", [(SCHEMA, CASES),
                                                (SIZED, SIZED_CASES)],
                         ids=["fixed", "sized"])
def test_both_walkers_validate_what_at_places(tmp_path: Path,
		schema_text: str, cases: dict[str, tuple[str, str]]) -> None:
	blob  = packed_text(schema_text)
	shape = shape_named_text(schema_text, "t")
	for label, (packet, verdict) in cases.items():
		message = bytes.fromhex(packet)
		assert python_verdict(blob, message, shape) == verdict, label
		assert c_verdict(tmp_path, blob, message, shape) == verdict, label


#: An enum placed by `at`: no backend has a public value getter for one
#: (26.959), so `validate` read nothing and accepted a value the enum does
#: not name, and one the message does not reach, where both walkers
#: refused (26.971). Little-endian and sixteen bits, so the order is read.
ENUMS = """target buffer;
endian little;
enum kind : u16 { a = 1, b = 0x0201 }
struct t {
	u8   off;
	kind k at off;
}
"""

ENUM_CASES = {
	"a, little-endian":        ("01" "0100", "0"),
	"b, little-endian":        ("01" "0102", "0"),
	"b read big-endian is no": ("01" "0201", "2"),
	"past the end":            ("05" "0100", "1"),
}


@pytest.mark.parametrize("schema_text, cases", [(ENUMS, ENUM_CASES)],
                         ids=["enum"])
def test_a_located_enum_is_validated(tmp_path: Path, schema_text: str,
		cases: dict[str, tuple[str, str]]) -> None:
	schema = tmp_path / "unit.situ"
	schema.write_text(schema_text, encoding="ascii")
	command = fourway.build(tmp_path, schema)
	blob  = packed_text(schema_text)
	shape = shape_named_text(schema_text, "t")
	for label, (packet, verdict) in cases.items():
		message = bytes.fromhex(packet)
		given = {name: fourway.answers(argv, message, tmp_path)
		         for name, argv in command.items()}
		first = next(iter(given.values()))
		assert all(answer == first for answer in given.values()), \
			(label, given)
		said = [one for one in first.splitlines()
		        if one.startswith("validate ")]
		assert said[-1] == f"validate {verdict}", (label, first)
		assert python_verdict(blob, message, shape) == verdict, label
		if COMPILER is not None:
			assert c_verdict(tmp_path, blob, message, shape) == verdict, label
