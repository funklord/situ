"""A terminator-ended run looks for its terminator past an element's lead.

Set by the copyright holder 2026-11-13, answering 26.606's first finding
(26.995). In `(a )` the space before `)` is the lead of the element that
would come next -- `form` opens with `peek u8 kind skip` -- and that element
turns out to be the terminator. Before, the terminator was looked for only
where the lead began, so the space was read as a form of its own: hull's
reader counted `(a )` as two items, and since 1fc9f26f, which validates a
run's elements, every reader refused it outright. hull keeps the cases in
its `test/sexpr_test.c`; these are its three and the shapes beside them.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import fourway
from test_walker_c import (COMPILER, _inline_image, c_verdict, python_verdict,
                           shape_named_text)

SCHEMA = """target buffer;
endian big;
encoding ascii | utf8;
whitespace ' ' | '\\t' | '\\r' | '\\n';
struct form [depth = 64] {
	peek u8  kind  skip;
	variant body switch (kind) {
		case '(':  list    as_list;
		case '"':  text    as_text;
		default:   symbol  as_symbol;
	}
}
struct list [depth = 64] {
	u8    open;
	form  items[]  until ")";
}
struct text {
	u8  open  skip;
	u8  chars[]  until "\\"" [escape = "\\\\"];
}
struct symbol {
	u8  name[]  before ' ' | '\\t' | '\\r' | '\\n' | '(' | ')' | '"';
}
"""

#: The list, its item count, how many bytes it spans, and how many forms
#: it holds at every depth.
CASES = [
	(b"(a )", 1, 4, 1),
	(b"(a)", 1, 3, 1),
	(b"( )", 0, 3, 0),
	(b"()", 0, 2, 0),
	(b"(a b)", 2, 5, 2),
	(b"(a  (b ) )", 2, 10, 3),
	(b"(machine\n  (backend ns)\n)\n", 2, 25, 4),
	(b'("x" \t)', 1, 7, 1),
]


@pytest.mark.skipif(not fourway.COMPLETE, reason = "needs gcc, g++ and rustc")
def test_all_four_count_the_items(tmp_path: Path) -> None:
	source = tmp_path / "s.situ"
	source.write_text(SCHEMA, encoding = "ascii")
	command = fourway.build(tmp_path, source)
	assert len(command) == 4
	for data, items, _, _ in CASES:
		said = {name: fourway.answers(argv, data, tmp_path)
		        for name, argv in command.items()}
		section = {name: one[one.index("-- list\n"):].split("\n-- ")[0]
		           for name, one in said.items()}
		assert len(set(section.values())) == 1, (data, section)
		lines = section["c"].splitlines()
		assert f"items count={items}" in lines, (data, lines)
		assert "validate 0" in lines, (data, lines)


@pytest.mark.skipif(COMPILER is None, reason = "needs a C compiler")
def test_both_walkers_validate_them(tmp_path: Path) -> None:
	image = _inline_image(SCHEMA)
	shape = shape_named_text(SCHEMA, "list")
	for data, _, _, _ in CASES:
		assert python_verdict(image, data, shape) == "0", data
		assert c_verdict(tmp_path, image, data, shape) == "0", data


def test_the_dissector_spans_them(tmp_path: Path) -> None:
	from situc.dissector import generate
	from situc.layout import solve
	from situc.parser import parse_text
	from situc.resolve import resolve
	from test_dissector import LUA, read_back, read_back_experts
	if LUA is None:
		pytest.skip("needs lua")
	schema = parse_text(SCHEMA, path = "s.situ")
	lua = tmp_path / "s.lua"
	lua.write_text(generate(schema, resolve(schema, solve(schema)), "s"),
	               encoding = "ascii")
	for data, _, span, forms in CASES:
		assert read_back_experts(lua, "list", data) == [], data
		consumed, rows = read_back(lua, "list", data)
		assert consumed == span, data
		assert [row[0] for row in rows].count("form.kind") == forms, data
