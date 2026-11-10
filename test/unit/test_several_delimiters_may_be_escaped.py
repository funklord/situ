"""A field ending at any of several delimiters, quoted or escaped (26.955).

A CSV field ends at a comma or at the line's end and may be quoted; an
HTTP parameter ends at `;` or CRLF and may be a quoted-string. 26.290
refused several delimiters beside an escaping form, because the quote's
state would have to be carried past each alternative -- "a fifth scan
rather than a parameter". The scan exists now in the three runtimes, both
walkers already carried the state, and the dissector's scan always did.

The answers are hand-computed rather than taken from any reader: four
backends agreeing is not four backends being right.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import fourway
from test_walker_c import (COMPILER, c_verdict, packed_text, python_verdict,
                           shape_named_text)

SCHEMA = """target buffer;
endian big;
struct row {
	u8  field[]  until "," | "\\r\\n"  [quoted = "\\""];
	u8  rest[]   until "\\r\\n";
}
"""

#: The bytes, and how long `field`'s content is: a quoted comma and a
#: quoted CRLF are content, and the first bare one of either ends it.
CASES = {
	"a bare comma":     (b'ab,cd\r\n', 2),
	"a bare CRLF":      (b'abc\r\nx\r\n', 3),
	"a quoted comma":   (b'"a,b",c\r\n', 5),
	"a quoted CRLF":    (b'"a\r\nb"\r\nz\r\n', 6),
	# `"a,"` then `b`, then `"c,d"`: the second quote closes at 9.
	"two quoted runs":  (b'"a,"b"c,d",e\r\n', 10),
}


def test_all_four_end_the_field_where_the_bytes_say(tmp_path: Path) -> None:
	schema = tmp_path / "unit.situ"
	schema.write_text(SCHEMA, encoding="ascii")
	command = fourway.build(tmp_path, schema)
	for label, (packet, length) in CASES.items():
		given = {name: fourway.answers(argv, packet, tmp_path)
		         for name, argv in command.items()}
		first = next(iter(given.values()))
		assert all(answer == first for answer in given.values()), \
			(label, given)
		lines = first.splitlines()
		said = next(line for line in lines if line.startswith("field "))
		assert said.split()[1] == f"len={length}", f"{label}: {said!r}"
		assert "validate 0" in lines, f"{label}: {lines!r}"


@pytest.mark.skipif(COMPILER is None, reason="no C compiler")
def test_both_walkers_end_it_there_too(tmp_path: Path) -> None:
	blob  = packed_text(SCHEMA)
	shape = shape_named_text(SCHEMA, "row")
	for label, (packet, _length) in CASES.items():
		assert python_verdict(blob, packet, shape) == "0", label
		assert c_verdict(tmp_path, blob, packet, shape) == "0", label
